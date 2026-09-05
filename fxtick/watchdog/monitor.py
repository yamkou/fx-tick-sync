"""Independent receiver and state machine; deterministic clock injection."""
from datetime import datetime, timedelta
from dataclasses import dataclass
import json
import logging
from uuid import uuid4

from ..config import ConfigError
from ..collectors.health import HealthSnapshot, utc_time
from ..collectors.monitoring import Check, IncidentKey, NotificationEvent, Severity
from .heartbeat import Heartbeat
from .health import evaluate_health

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatchdogEvent(NotificationEvent):
    first_seen_at: datetime | None = None

    def __post_init__(self):
        super().__post_init__()
        if self.first_seen_at is not None:
            first = utc_time(self.first_seen_at)
            if first > self.occurred_at or self.outage_seconds != int((self.occurred_at - first).total_seconds()):
                raise ConfigError('Invalid event chronology')
            object.__setattr__(self, 'first_seen_at', first)


def key_text(key):
    return json.dumps([key.collector_id, key.check.value, key.terminal_id])


def event_dict(event):
    return {'schema_version': 1, 'event_id': event.event_id,
        'collector_id': event.incident.collector_id, 'check': event.incident.check.value,
        'terminal_id': event.incident.terminal_id, 'severity': event.severity.value,
        'occurred_at': event.occurred_at.isoformat(), 'outage_seconds': event.outage_seconds,
        'first_seen_at': (getattr(event, 'first_seen_at', None) or
                          event.occurred_at - timedelta(seconds=event.outage_seconds or 0)).isoformat(),
        'recovered_at': event.occurred_at.isoformat() if event.severity == Severity.RECOVERY else None}


def read_event(raw):
    value = json.loads(raw)
    return WatchdogEvent(value['event_id'], IncidentKey(value['collector_id'], Check(value['check']), value['terminal_id']),
        Severity(value['severity']), datetime.fromisoformat(value['occurred_at']), value['outage_seconds'],
        datetime.fromisoformat(value['first_seen_at']))


class ExternalMonitor:
    def __init__(self, config, store, authenticator, clock, routes=(), schedule=None, inbox=None):
        self.config, self.store, self.authenticator, self.clock = config, store, authenticator, clock
        self.nodes = {n.collector_id: n for n in config.nodes}
        route_pairs = tuple(routes)
        if len({r.route_id for r, _ in route_pairs}) != len(route_pairs):
            raise ConfigError('Duplicate notification route ID')
        self.routes = dict(route_pairs)  # (NotificationRoute, provider) iterable
        self.schedule = schedule
        self.inbox = inbox
        now = utc_time(clock())
        with store.transaction() as db:
            row = db.execute("SELECT value FROM monitor_meta WHERE key='monitor-id'").fetchone()
            if row and row[0] != config.monitor_id:
                raise ConfigError('State belongs to another monitor')
            db.execute("INSERT OR IGNORE INTO monitor_meta VALUES ('monitor-id', ?)", (config.monitor_id,))
            self._clock(db, now)
            for node in self.nodes:
                db.execute('INSERT OR IGNORE INTO monitor_nodes(id,enrolled) VALUES (?,?)', (node, now.isoformat()))

    def _clock(self, db, now):
        row = db.execute("SELECT value FROM monitor_meta WHERE key='clock'").fetchone()
        if row and now < datetime.fromisoformat(row[0]):
            raise ConfigError('Monitor clock moved backwards')
        db.execute("INSERT OR REPLACE INTO monitor_meta VALUES ('clock', ?)", (now.isoformat(),))

    def receive(self, payload, proof=None):
        heartbeat = Heartbeat.decode(payload)
        node = self.nodes.get(heartbeat.snapshot.collector_id)
        if node is None:
            raise ConfigError('Unregistered heartbeat collector')
        try:
            authorized = self.authenticator.verify(node.collector_id, heartbeat.boot_id, payload, proof) is True
        except Exception:
            authorized = False
        if not authorized:
            raise ConfigError('Heartbeat authentication rejected')
        now = utc_time(self.clock())
        age = (now - heartbeat.snapshot.observed_at).total_seconds()
        if age < -node.max_clock_skew_seconds or age > node.max_payload_age_seconds:
            raise ConfigError('Heartbeat timestamp outside acceptance window')
        if set(t.terminal_id for t in heartbeat.snapshot.terminals) - set(node.terminal_ids):
            raise ConfigError('Unregistered heartbeat terminal')
        with self.store.transaction() as db:
            self._clock(db, now)
            previous = self.store.node(node.collector_id)
            if previous['boot'] == heartbeat.boot_id:
                if heartbeat.sequence <= previous['sequence']:
                    raise ConfigError('Heartbeat replay rejected')
            elif db.execute('SELECT 1 FROM monitor_boots WHERE node=? AND boot=?', (node.collector_id, heartbeat.boot_id)).fetchone():
                raise ConfigError('Retired heartbeat boot rejected')
            if previous['payload']:
                old = Heartbeat.decode(previous['payload'].encode())
                if heartbeat.snapshot.observed_at < old.snapshot.observed_at:
                    raise ConfigError('Heartbeat observation moved backwards')
            db.execute('INSERT OR IGNORE INTO monitor_boots VALUES (?,?)', (node.collector_id, heartbeat.boot_id))
            db.execute('UPDATE monitor_nodes SET receipt=?,payload=?,boot=?,sequence=? WHERE id=?',
                (now.isoformat(), payload.decode('utf-8'), heartbeat.boot_id, heartbeat.sequence, node.collector_id))
        return True

    def _transition(self, db, key, severity, now, policy):
        encoded_key = key_text(key)
        state = self.store.incident(encoded_key)
        events = []
        if state is None and severity is None:
            return events
        active = state is not None and state['current_state'] != 'RECOVERY'
        emit = False
        if severity is None:
            if not active:
                return events
            state['current_state'] = 'RECOVERY'
            state['recovered_at'] = now.isoformat()
            emit = True  # Recovery events are always generated, independently of routing.
        else:
            if not active:
                state = {'first_seen': now.isoformat(), 'last_event_at': None,
                         'current_state': severity.value, 'notification_sent': False, 'recovered_at': None,
                         'last_notified_at': None}
                emit = True
            elif state['current_state'] != severity.value:
                emit = True
            elif state['last_event_at'] is not None:
                emit = (now - datetime.fromisoformat(state['last_event_at'])).total_seconds() >= policy.cooldown_seconds
                if state.get('last_notified_at'):
                    emit = emit and (now - datetime.fromisoformat(state['last_notified_at'])).total_seconds() >= policy.cooldown_seconds
                # An undelivered reminder is retried with its existing event ID;
                # do not accumulate another copy every cooldown during outage.
                if emit and any(read_event(row['event']).event_id == state['event_id'] for row in self.store.pending()):
                    emit = False
            state['current_state'] = severity.value
        state['last_seen'] = now.isoformat()
        if emit:
            duration = int((now - datetime.fromisoformat(state['first_seen'])).total_seconds())
            event = WatchdogEvent('event-' + uuid4().hex, key, severity or Severity.RECOVERY, now, duration,
                datetime.fromisoformat(state['first_seen']))
            state['last_event_at'] = now.isoformat()
            state['event_id'] = event.event_id
            state['notification_sent'] = False
            serialized = json.dumps(event_dict(event))
            for route in self.routes:
                if event.severity != Severity.RECOVERY or policy.recovery_notification:
                    db.execute('INSERT INTO monitor_outbox(event,route) VALUES (?,?)', (serialized, route.route_id))
            events.append(event)
        db.execute('INSERT OR REPLACE INTO monitor_incidents VALUES (?,?)', (encoded_key, json.dumps(state)))
        return events

    def evaluate(self):
        now = utc_time(self.clock())
        events = []
        with self.store.transaction() as db:
            self._clock(db, now)
            for name, policy in self.nodes.items():
                row = self.store.node(name)
                if row['receipt'] is None:
                    age = (now - datetime.fromisoformat(row['enrolled'])).total_seconds()
                    if age < policy.startup_grace_seconds:
                        continue
                    severity = Severity.CRITICAL
                else:
                    age = (now - datetime.fromisoformat(row['receipt'])).total_seconds()
                    severity = (Severity.CRITICAL if age >= policy.critical_seconds else
                                Severity.WARNING if age >= policy.warning_seconds else None)
                events.extend(self._transition(db, IncidentKey(name, Check.HEARTBEAT), severity, now, policy.health))
                # Missing/stale receipts are not evidence of component recovery.
                if severity is not None or row['payload'] is None:
                    continue
                snapshot = Heartbeat.decode(row['payload'].encode()).snapshot
                for key, state in evaluate_health(snapshot, policy, now, self.schedule).items():
                    events.extend(self._transition(db, key, state, now, policy.health))
        return events

    def dispatch(self):
        """Ordered per-route outbox, bounded retries; exceptions never leak payloads."""
        now = utc_time(self.clock())
        providers = {r.route_id: (r, p) for r, p in self.routes.items()}
        blocked = set()
        sent = 0
        for row in self.store.pending():
            route_id = row['route']
            event = read_event(row['event'])
            delivery_key = (route_id, key_text(event.incident))
            if delivery_key in blocked or route_id not in providers:
                continue
            if row['last_attempt'] and (now - datetime.fromisoformat(row['last_attempt'])).total_seconds() < self.config.retry_seconds:
                blocked.add(delivery_key)
                continue
            route, provider = providers[route_id]
            with self.store.transaction() as db:
                self._clock(db, now)
                db.execute('UPDATE monitor_outbox SET last_attempt=? WHERE number=?', (now.isoformat(), row['number']))
            try:
                success = provider.send(event, route) is True
            except Exception:
                success = False
            if success:
                with self.store.transaction() as db:
                    db.execute('UPDATE monitor_outbox SET delivered=1 WHERE number=?', (row['number'],))
                    key = key_text(event.incident)
                    state = self.store.incident(key)
                    remaining = db.execute('SELECT 1 FROM monitor_outbox WHERE event=? AND delivered=0', (row['event'],)).fetchone()
                    if state and state['event_id'] == event.event_id and not remaining:
                        state['notification_sent'] = True
                        state['last_notified_at'] = now.isoformat()
                        db.execute('UPDATE monitor_incidents SET state=? WHERE key=?', (json.dumps(state), key))
                sent += 1
            else:
                blocked.add(delivery_key)
                LOG.warning('Notification delivery failed; details omitted')
        return sent

    def run_once(self):
        if self.inbox is not None:
            self.inbox.drain(self.receive)
        events = self.evaluate()
        self.dispatch()
        return events

    def run(self, stop, poll_seconds=1):
        """External-host loop. Caller supplies authenticated ingress separately."""
        if type(poll_seconds) not in (int, float) or not 0 < poll_seconds <= 60:
            raise ConfigError('Invalid monitor poll interval')
        while not stop.is_set():
            self.run_once()
            stop.wait(poll_seconds)
