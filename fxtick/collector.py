"""Runnable staging collector with an explicit fake source and real signed sender."""
import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import signal
import threading
import time

from .config import ConfigError, logical_id
from .staging import load_staging, REFERENCES
from .staging_logging import StagingLogs, Event
from .collectors.fake_source import FakeSourceAdapter
from .collectors.sender_state import SenderState
from .collectors.health import HealthSnapshot, TerminalHealth
from .watchdog.heartbeat import Heartbeat
from .watchdog.manager import CollectorManager, ManagedCollector
from .watchdog.config import NodePolicy
from .watchdog.auth import SignedHeartbeatTransport, key_bytes
from .watchdog.secrets import EnvironmentSecrets


def load_runtime(path):
    def unique(pairs):
        result = {}
        for k,v in pairs:
            if k in result: raise ConfigError('Duplicate runtime key')
            result[k] = v
        return result
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=unique)
        if set(value) != {'schema_version','adapter','boot_id','key_id','heartbeat_interval_seconds'}:
            raise ValueError()
        if type(value['schema_version']) is not int or value['schema_version'] != 1 or value['adapter'] != 'fake':
            raise ValueError()
        logical_id(value['boot_id']); logical_id(value['key_id'])
        interval = value['heartbeat_interval_seconds']
        if type(interval) is not int or not 1 <= interval <= 60: raise ValueError()
        return value
    except Exception:
        raise ConfigError('Invalid runtime config; values omitted') from None


def prepare(config_path, runtime_path):
    config, roots, root = load_staging(config_path)
    settings = load_runtime(runtime_path)
    node = config.collectors[0].collector_id
    for directory in (root/'state'/node, roots['log_root']/node, roots['data_root']/node):
        if directory.resolve() != directory:
            raise ConfigError('Collector path must not redirect')
    return config, roots, root, settings


def dry_run(config_path, runtime_path):
    config, roots, root, settings = prepare(config_path, runtime_path)
    health = HealthSnapshot(config.collectors[0].collector_id, datetime.now(timezone.utc),
                            collector_alive=False, terminals=(TerminalHealth(config.terminals[0].terminal_id),))
    heartbeat = Heartbeat(health, settings['boot_id'], 0)
    Heartbeat.decode(heartbeat.encode())
    return {'mode':'dry-run', 'adapter':'fake', 'collector_id':health.collector_id,
            'environment':'staging', 'terminal_id':config.terminals[0].terminal_id,
            'health_initialized':True, 'heartbeat_constructed':True, 'paths_validated':True,
            'distribution_enabled':False, 'network_sent':False, 'real_source_ready':False}


class CollectorRuntime:
    """Single bounded worker; adapters must return promptly. No OS/service control.

    Signal/service adapters call request_stop; run's finally closes adapter, state
    and logs after the in-flight bounded send. Unexpected source exceptions end the
    worker; only the independent monitor can announce its subsequent timeout.
    """
    def __init__(self, config_path, runtime_path, source, transport,
                 clock=lambda: datetime.now(timezone.utc), monotonic=time.monotonic,
                 execution_profile=None, resource_probe=None):
        self.config, self.roots, self.root, self.settings = prepare(config_path, runtime_path)
        self.source, self.transport, self.clock, self.monotonic = source, transport, clock, monotonic
        self.stop_event = threading.Event()
        self.state = self.logs = None
        self.manager = None
        self.last_tick = self.last_write = None
        self.health = None
        self.closed = False
        self.sender_failure = False
        self.error_state = None
        self.execution_profile = execution_profile
        self.resource_probe = resource_probe
        self.resource_observer = None
        self.resource_logging_failed = False

    def start(self):
        if self.manager is not None or self.closed: raise ConfigError('Runtime already started or closed')
        node = self.config.collectors[0].collector_id
        try:
            self.state = SenderState(self.root/'state'/node, node, self.settings['boot_id'])
            log_options = ({'max_bytes':self.execution_profile.log_max_bytes,
                            'backups':self.execution_profile.log_backups}
                           if self.execution_profile else {})
            self.logs = StagingLogs(self.roots['log_root']/node, **log_options)
            if self.execution_profile:
                from .resource_monitor import ResourceObserver
                if self.resource_probe is None:
                    from .platform.windows_resources import WindowsResourceProbe
                    self.resource_probe = WindowsResourceProbe(self.roots['data_root'])
                self.resource_observer = ResourceObserver(self.execution_profile, self.resource_probe,
                    self.roots['log_root']/node, self.monotonic)
            self.health = HealthSnapshot(node, self.clock(), collector_alive=True,
                terminals=(TerminalHealth(self.config.terminals[0].terminal_id),))
            outer = self
            class Probe:
                def sample(self, now): return replace(outer.health, observed_at=now)
            class DurableTransport:
                def send(self, heartbeat):
                    try:
                        heartbeat = replace(heartbeat, sequence=outer.state.reserve())
                    except Exception:
                        outer.sender_failure = True
                        raise
                    sent = outer.transport.send(heartbeat) is True
                    outer.logs.emit('heartbeat', Event.STARTED if sent else Event.FAILED)
                    return sent
            policy = NodePolicy(node, heartbeat_interval_seconds=self.settings['heartbeat_interval_seconds'])
            self.manager = CollectorManager((ManagedCollector(policy, Probe(), DurableTransport(),
                                             self.settings['boot_id']),), self.clock, self.monotonic)
            self.logs.emit('collector', Event.STARTED)
        except Exception:
            self.close()
            raise

    def step(self):
        if self.manager is None or self.closed: raise ConfigError('Runtime not active')
        if self.stop_event.is_set(): return {}
        now = self.clock()
        observed = self.source.poll(now)
        if observed.tick:
            self.last_tick = now
            if observed.write_failure:
                self.error_state = 'write-failed'
            else:
                self.state.synthetic_write(now)
                self.last_write = now
                self.error_state = None
        if self.resource_observer:
            try:
                self.resource_observer.sample_if_due()
                self.resource_logging_failed = self.resource_observer.logging_failed
            except OSError:
                # Metrics I/O must not lower source polling frequency or discard ticks.
                # Existing heartbeat/write evidence remains independent of metrics.
                self.resource_logging_failed = True
        disk = shutil.disk_usage(self.roots['data_root'])
        self.health = HealthSnapshot(self.config.collectors[0].collector_id, now,
            collector_alive=True, source_connected=observed.connected,
            last_tick_time=self.last_tick, last_successful_write=self.last_write,
            error_state=self.error_state or ('resource-monitor-failed' if self.resource_logging_failed else None),
            disk_path_accessible=True, disk_free_bytes=disk.free,
            # No claim that a real terminal is running: this adapter is synthetic.
            terminals=(TerminalHealth(self.config.terminals[0].terminal_id, None),))
        if self.stop_event.is_set(): return {}
        result = self.manager.step()
        if self.sender_failure:
            raise ConfigError('Durable sender reservation failed')
        return result

    def request_stop(self): self.stop_event.set()

    def run(self):
        try:
            self.start()
            while not self.stop_event.is_set():
                self.step()
                self.stop_event.wait(1)
        except Exception:
            if self.logs and not self.closed: self.logs.emit('collector', Event.FAILED)
            raise
        finally:
            self.close()

    def close(self):
        if self.closed: return
        self.closed = True
        self.stop_event.set()
        try:
            self.source.close()
        finally:
            try:
                if self.state: self.state.close()
            finally:
                try:
                    if self.logs:
                        try: self.logs.emit('collector', Event.STOPPED)
                        finally: self.logs.close()
                finally:
                    if self.resource_observer: self.resource_observer.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--runtime', required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--execution-profile')
    args = parser.parse_args()
    try:
        profile = None
        if args.execution_profile:
            from .resources import load_profile
            profile = load_profile(args.execution_profile)
        if args.dry_run:
            result = dry_run(args.config, args.runtime)
            if profile:
                result.update(profile=profile.profile, low_resource_mode=profile.low_resource_mode)
            print(json.dumps(result))
            return 0
        _, _, _, settings = prepare(args.config, args.runtime)
        secrets = EnvironmentSecrets(REFERENCES)
        key_bytes(secrets, 'heartbeat-hmac')
        from .preflight import endpoint_valid
        if not endpoint_valid(secrets.get('monitor-endpoint')): raise ConfigError('Invalid monitor destination')
        transport = SignedHeartbeatTransport('monitor-endpoint', settings['key_id'],
                                            'heartbeat-hmac', secrets, lambda: datetime.now(timezone.utc))
        runtime = CollectorRuntime(args.config, args.runtime, FakeSourceAdapter(), transport,
                                   execution_profile=profile)
        signals = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, 'SIGBREAK'): signals.append(signal.SIGBREAK)
        for sig in signals:
            signal.signal(sig, lambda *_: runtime.request_stop())
        runtime.run()
        return 0
    except Exception:
        print(json.dumps({'error':'collector-failed-details-omitted'}))
        return 2


if __name__ == '__main__': raise SystemExit(main())
