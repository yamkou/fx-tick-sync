"""Own SQLite on one worker thread; HTTP health is independent and fail-safe."""
from datetime import datetime, timezone
from threading import Event, Lock, Thread
import time

from ..config import ConfigError
from .store import SQLiteState
from .monitor import ExternalMonitor
from .heartbeat import HeartbeatInbox
from .auth import HMACAuthenticator, key_bytes


class MonitorRuntime:
    def __init__(self, config, state_path, senders, secrets, routes=(),
                 clock=None, monotonic=time.monotonic, evaluation_timeout_seconds=30, auth_window_seconds=90):
        if type(evaluation_timeout_seconds) is not int or not 2 <= evaluation_timeout_seconds <= 300:
            raise ConfigError('Invalid monitor health freshness limit')
        if {s.collector_id for s in senders} != {n.collector_id for n in config.nodes}:
            raise ConfigError('Every monitored collector needs signing configuration')
        self.config, self.state_path, self.senders, self.secrets, self.routes = config, state_path, senders, secrets, tuple(routes)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic, self.freshness, self.auth_window = monotonic, evaluation_timeout_seconds, auth_window_seconds
        self.inbox, self.stop_event, self.lock = HeartbeatInbox(), Event(), Lock()
        self.worker = None
        self.status = {'db_accessible':False, 'last_evaluation_time':None, 'notification_state':'unavailable'}
        self.evaluated_monotonic = None

    def start(self):
        if self.worker is not None: raise ConfigError('Runtime already started')
        self.worker = Thread(target=self._run, name='fx-monitor-owner', daemon=True)
        self.worker.start()

    def health(self):
        with self.lock:
            result = dict(self.status)
            fresh = self.evaluated_monotonic is not None and 0 <= self.monotonic()-self.evaluated_monotonic <= self.freshness
        alive = self.worker is not None and self.worker.is_alive() and not self.stop_event.is_set()
        result.update(schema_version=1, process_alive=True, monitor_worker_alive=alive,
            ready=bool(alive and fresh and result['db_accessible']))
        # Notification degradation is visible to uptime tooling but does not
        # reject incoming heartbeats (which would turn a delivery outage into VPS alarms).
        result['healthy'] = result['ready'] and result['notification_state'] in ('healthy','disabled')
        return result

    def _run(self):
        state = None
        try:
            for sender in self.senders:
                for _, reference in sender.keys: key_bytes(self.secrets,reference)
            state = SQLiteState(self.state_path)
            auth = HMACAuthenticator(self.senders, self.secrets, self.clock, self.auth_window)
            monitor = ExternalMonitor(self.config,state,auth,self.clock,self.routes,inbox=self.inbox)
            while not self.stop_event.is_set():
                # Expose evaluation freshness before a potentially slow delivery.
                self.inbox.drain(monitor.receive)
                monitor.evaluate()
                state.check()
                with self.lock:
                    self.status.update(db_accessible=True, last_evaluation_time=self.clock().isoformat())
                    self.evaluated_monotonic = self.monotonic()
                monitor.dispatch()
                pending = state.pending()
                notification = ('disabled' if not self.routes else
                    'degraded' if any(r['last_attempt'] is not None for r in pending) else 'healthy')
                with self.lock: self.status['notification_state'] = notification
                self.stop_event.wait(0.1)
        except Exception:
            with self.lock:
                self.status.update(db_accessible=False, notification_state='unavailable')
            # No automatic DB replacement, recovery events or raw exception logging.
        finally:
            if state is not None: state.close()

    def stop(self, timeout=5):
        self.stop_event.set()
        if self.worker: self.worker.join(timeout)
