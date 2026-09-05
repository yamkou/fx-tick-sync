"""Offline monitoring tests: fake clocks/auth/providers and temporary SQLite."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from fxtick.config import ConfigError
from fxtick.collectors.health import HealthSnapshot, TerminalHealth
from fxtick.collectors.monitoring import Check, Channel, MonitoringPolicy, NotificationRoute, Severity
from fxtick.watchdog.config import MonitorConfig, NodePolicy
from fxtick.watchdog.heartbeat import Heartbeat
from fxtick.watchdog.health import evaluate_health
from fxtick.watchdog.monitor import ExternalMonitor, key_text
from fxtick.watchdog.store import SQLiteState

NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


class Clock:
    def __init__(self): self.now = NOW
    def __call__(self): return self.now
    def advance(self, seconds): self.now += timedelta(seconds=seconds)


class FakeAuth:
    def verify(self, collector_id, boot_id, payload, proof): return proof == 'test-proof'


class FakeProvider:
    def __init__(self): self.events = []; self.fail = False
    def send(self, event, route):
        if self.fail: raise RuntimeError('sensitive-test-value')
        self.events.append(event)
        return True


def healthy(now=NOW, name='london-01', **changes):
    return replace(HealthSnapshot(name, now, True, now, now, True, 10**12, True), **changes)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'monitor.sqlite')
        self.store = SQLiteState(self.path)
        self.addCleanup(lambda: self.store.close())
        self.clock = Clock()
        self.provider = FakeProvider()
        self.route = NotificationRoute('test-route', Channel.PUSH)
        self.config = MonitorConfig('tokyo-monitor-01', (NodePolicy('london-01'), NodePolicy('london-02')))
        self.monitor = self.make_monitor()
        self.sequence = 0

    def make_monitor(self):
        return ExternalMonitor(self.config, self.store, FakeAuth(), self.clock, [(self.route, self.provider)])

    def receive(self, name='london-01', snapshot=None, boot='boot-one'):
        self.sequence += 1
        return self.monitor.receive(Heartbeat(snapshot or healthy(self.clock(), name), boot, self.sequence).encode(), 'test-proof')

    def events(self, check=Check.HEARTBEAT):
        return [e for e in self.monitor.run_once() if e.incident.collector_id == 'london-01' and e.incident.check == check]

    def test_healthy_heartbeat(self):
        self.receive()
        self.assertEqual(self.events(), [])
        self.assertEqual(self.provider.events, [])

    def test_warning_timeout(self):
        self.receive(); self.clock.advance(120)
        self.assertEqual(self.events()[0].severity, Severity.WARNING)

    def test_critical_timeout_without_sender(self):
        self.receive(); self.clock.advance(180)
        self.assertEqual(self.events()[0].severity, Severity.CRITICAL)

    def test_never_seen_node_detected(self):
        self.clock.advance(180)
        self.assertEqual({e.incident.collector_id for e in self.monitor.evaluate()}, {'london-01', 'london-02'})

    def test_duplicate_suppression_and_cooldown(self):
        self.receive(); self.clock.advance(180)
        self.assertEqual(len(self.events()), 1)
        for _ in range(4):
            self.clock.advance(60)
            self.assertEqual(self.events(), [])
        self.clock.advance(60)
        self.assertEqual(len(self.events()), 1)

    def test_recovery_once_with_duration(self):
        self.receive(); self.clock.advance(180); self.events()
        self.clock.advance(502); self.receive()
        events = self.events()
        self.assertEqual(events[0].severity, Severity.RECOVERY)
        self.assertEqual(events[0].outage_seconds, 502)
        self.assertEqual(self.events(), [])

    def test_warning_escalates_during_cooldown(self):
        self.receive(); self.clock.advance(120); self.events()
        self.clock.advance(60)
        self.assertEqual(self.events()[0].severity, Severity.CRITICAL)

    def test_multiple_collectors_independent(self):
        self.receive(); self.receive('london-02'); self.clock.advance(180); self.receive('london-02')
        events = self.monitor.run_once()
        self.assertEqual([e.incident.collector_id for e in events], ['london-01'])

    def test_state_survives_restart(self):
        self.receive(); self.clock.advance(180); self.events()
        self.store.close(); self.store = SQLiteState(self.path); self.monitor = self.make_monitor()
        self.assertEqual(self.events(), [])
        self.receive()
        self.assertEqual(self.events()[0].severity, Severity.RECOVERY)

    def test_startup_grace_not_reset_by_restart(self):
        self.clock.advance(179); self.monitor = self.make_monitor(); self.clock.advance(1)
        self.assertEqual(self.events()[0].severity, Severity.CRITICAL)

    def test_stale_tick_while_collector_alive(self):
        self.receive(snapshot=healthy(last_tick_time=NOW-timedelta(seconds=181)))
        self.assertEqual(self.events(Check.LAST_TICK)[0].severity, Severity.WARNING)

    def test_stale_write_independent_of_fresh_tick(self):
        self.receive(snapshot=healthy(last_successful_write=NOW-timedelta(seconds=301)))
        events = self.monitor.evaluate()
        self.assertEqual([e.incident.check for e in events], [Check.LAST_WRITE])

    def test_closed_schedule_suspends_age_checks(self):
        policy = NodePolicy('london-01', active_windows=())
        values = evaluate_health(healthy(last_tick_time=None, last_successful_write=None), policy, NOW)
        self.assertNotIn(Check.LAST_TICK, [k.check for k in values])

    def test_schedule_adapter_can_override_for_market(self):
        class Closed:
            def active(self, collector_id, check, now): return False
        self.monitor.schedule = Closed()
        self.receive(snapshot=healthy(last_tick_time=None))
        self.assertEqual(self.events(Check.LAST_TICK), [])

    def test_closed_market_does_not_falsely_recover(self):
        self.receive(snapshot=healthy(last_tick_time=None)); self.events(Check.LAST_TICK)
        class Closed:
            def active(self, *args): return False
        self.monitor.schedule = Closed()
        self.receive(); self.assertEqual(self.events(Check.LAST_TICK), [])
        self.monitor.schedule = None
        self.assertEqual(self.events(Check.LAST_TICK)[0].severity, Severity.RECOVERY)

    def test_component_checks_suspended_when_heartbeat_missing(self):
        self.receive(snapshot=healthy(source_connected=False)); self.events(Check.SOURCE)
        self.clock.advance(180)
        self.assertEqual(self.events(Check.SOURCE), [])

    def test_unknown_health_warns(self):
        self.receive(snapshot=healthy(collector_alive=None))
        self.assertEqual(self.events(Check.COLLECTOR)[0].severity, Severity.WARNING)

    def test_disk_low_or_inaccessible_and_error(self):
        self.receive(snapshot=healthy(disk_free_bytes=1, error_state='write-failed'))
        self.assertEqual({e.incident.check for e in self.monitor.evaluate()}, {Check.DISK, Check.COLLECTOR})

    def test_terminal_missing_warns_and_dead_is_critical(self):
        node = NodePolicy('london-01', terminal_ids=('terminal-one',))
        values = evaluate_health(healthy(), node, NOW)
        self.assertEqual(next(v for k,v in values.items() if k.check == Check.TERMINAL), Severity.WARNING)
        values = evaluate_health(healthy(terminals=(TerminalHealth('terminal-one', False),)), node, NOW)
        self.assertEqual(next(v for k,v in values.items() if k.check == Check.TERMINAL), Severity.CRITICAL)

    def test_malformed_payload_no_state_write(self):
        for payload in (b'{}', b'[]', b'bad', b'x'*65537, b'{"a":1,"a":2}'):
            with self.subTest(payload=payload[:10]), self.assertRaises(ConfigError):
                self.monitor.receive(payload, 'test-proof')
        self.assertIsNone(self.store.node('london-01')['receipt'])

    def test_unknown_collector_rejected(self):
        with self.assertRaises(ConfigError): self.receive('frankfurt-01')
        self.assertIsNone(self.store.node('frankfurt-01'))

    def test_auth_failure_does_not_update_receipt(self):
        with self.assertRaises(ConfigError): self.monitor.receive(Heartbeat(healthy(), 'boot-one', 1).encode())
        self.assertIsNone(self.store.node('london-01')['receipt'])

    def test_auth_exception_is_sanitized(self):
        class Broken:
            def verify(self, *args): raise RuntimeError('sensitive-test-value')
        self.monitor.authenticator = Broken()
        with self.assertRaises(ConfigError) as error: self.receive()
        self.assertNotIn('sensitive-test-value', str(error.exception))

    def test_future_and_old_timestamp_rejected(self):
        for seconds in (31, -91):
            with self.assertRaises(ConfigError): self.receive(snapshot=healthy(NOW+timedelta(seconds=seconds)))
        self.assertIsNone(self.store.node('london-01')['receipt'])

    def test_receiver_clock_used_with_tolerated_skew(self):
        self.receive(snapshot=healthy(NOW+timedelta(seconds=20)))
        self.clock.advance(120)
        self.assertEqual(self.events()[0].severity, Severity.WARNING)

    def test_replay_rejected_after_restart(self):
        self.receive(); self.monitor = self.make_monitor()
        with self.assertRaises(ConfigError): self.monitor.receive(Heartbeat(healthy(), 'boot-one', 1).encode(), 'test-proof')

    def test_retired_boot_rejected(self):
        self.receive(); self.receive(boot='boot-two')
        with self.assertRaises(ConfigError): self.receive(boot='boot-one')

    def test_sequence_and_schema_validation(self):
        for seq in (True, -1, 2**63):
            with self.assertRaises(ConfigError): Heartbeat(healthy(), 'boot-one', seq)
        raw = json.loads(Heartbeat(healthy(), 'boot-one', 1).encode()); raw['schema_version'] = True
        with self.assertRaises(ConfigError): Heartbeat.decode(json.dumps(raw).encode())

    def test_payload_extra_secret_field_rejected_without_echo(self):
        raw = json.loads(Heartbeat(healthy(), 'boot-one', 1).encode()); raw['token'] = 'sensitive-test-value'
        with self.assertRaises(ConfigError) as error: Heartbeat.decode(json.dumps(raw).encode())
        self.assertNotIn('sensitive-test-value', str(error.exception))

    def test_monitor_clock_regression_rejected(self):
        self.clock.advance(-1)
        with self.assertRaises(ConfigError): self.monitor.evaluate()

    def test_notification_failure_does_not_crash_or_log_secret(self):
        self.provider.fail = True; self.clock.advance(180)
        with self.assertLogs('fxtick.watchdog.monitor') as logs:
            self.assertEqual(len(self.monitor.run_once()), 2)
        self.assertNotIn('sensitive-test-value', ''.join(logs.output))
        self.assertEqual(len(self.store.pending()), 2)

    def test_failed_send_retries_after_interval(self):
        self.provider.fail = True; self.clock.advance(180); self.monitor.run_once()
        self.provider.fail = False
        self.assertEqual(self.monitor.dispatch(), 0)
        self.clock.advance(60)
        self.assertEqual(self.monitor.dispatch(), 2)
        self.assertEqual(self.monitor.dispatch(), 0)

    def test_delivery_state_survives_restart(self):
        self.provider.fail = True; self.clock.advance(180); self.monitor.run_once()
        self.monitor = self.make_monitor(); self.provider.fail = False; self.clock.advance(60)
        self.assertEqual(self.monitor.dispatch(), 2)

    def test_failed_route_does_not_suppress_other_route(self):
        good = FakeProvider(); self.provider.fail = True
        self.monitor.routes[NotificationRoute('email-route', Channel.EMAIL)] = good
        self.clock.advance(180); self.monitor.run_once()
        self.assertEqual(len(good.events), 2)
        self.assertEqual(len(self.provider.events), 0)

    def test_recovery_queued_behind_failure(self):
        self.provider.fail = True; self.clock.advance(180); self.monitor.run_once()
        self.receive(); self.monitor.run_once()
        self.provider.fail = False; self.clock.advance(60); self.monitor.dispatch()
        own = [e for e in self.provider.events if e.incident.collector_id == 'london-01']
        self.assertEqual([e.severity for e in own], [Severity.CRITICAL, Severity.RECOVERY])

    def test_notification_sent_only_after_confirmation(self):
        self.clock.advance(180); event = self.monitor.evaluate()[0]
        self.assertFalse(self.store.incident(key_text(event.incident))['notification_sent'])
        self.monitor.dispatch()
        self.assertTrue(self.store.incident(key_text(event.incident))['notification_sent'])

    def test_failed_delivery_does_not_accumulate_cooldown_duplicates(self):
        self.clock.advance(180); self.monitor.evaluate()
        self.clock.advance(600); self.monitor.evaluate()
        self.assertEqual(len(self.store.pending()), 2)

    def test_duplicate_routes_rejected(self):
        with self.assertRaises(ConfigError):
            ExternalMonitor(self.config, self.store, FakeAuth(), self.clock,
                [(self.route, self.provider), (self.route, self.provider)])


class ConfigurationTests(unittest.TestCase):
    def test_invalid_thresholds(self):
        with self.assertRaises(ConfigError): NodePolicy('london-01', warning_seconds=180)

    def test_invalid_schedule(self):
        for value in (((10, 2),), ((0, 10081),), ((0, 30), (20, 40))):
            with self.assertRaises(ConfigError): NodePolicy('london-01', active_windows=value)

    def test_duplicate_collector(self):
        with self.assertRaises(ConfigError): MonitorConfig('tokyo-01', (NodePolicy('london-01'),)*2)

    def test_utc_window_edges(self):
        node = NodePolicy('london-01', active_windows=((0, 60),))
        self.assertTrue(node.active(NOW))
        self.assertFalse(node.active(NOW+timedelta(hours=1)))


if __name__ == '__main__': unittest.main()
