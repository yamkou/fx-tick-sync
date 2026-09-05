from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_phase3b import Clock, NOW, healthy, FakeAuth
from fxtick.config import ConfigError
from fxtick.collectors.monitoring import NotificationEvent, NotificationRoute, IncidentKey, Check, Severity, Channel
from fxtick.watchdog.config import NodePolicy, MonitorConfig
from fxtick.watchdog.config import load_monitor_config
from fxtick.watchdog.delivery_config import DeliveryConfig
from fxtick.watchdog.health import probe_disk
from fxtick.watchdog.manager import ManagedCollector, CollectorManager
from fxtick.watchdog.providers import (GenericWebhookProvider, LoggingNotificationProvider,
    FakeNotificationProvider, HTTPSHeartbeatTransport, HTTPSPoster, _NoRedirect)
from fxtick.watchdog.heartbeat import Heartbeat, HeartbeatInbox
from fxtick.watchdog.monitor import ExternalMonitor
from fxtick.watchdog.store import SQLiteState


class FakeSecrets:
    def __init__(self):
        self.values = {'endpoint-ref': 'https://example.invalid/monitor', 'token-ref': 'test-only-value'}
    def get(self, reference): return self.values[reference]


class FakePoster:
    def __init__(self): self.calls = []; self.status = 204
    def post(self, endpoint, body, headers):
        self.calls.append((endpoint, body, headers))
        return self.status


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.secrets, self.poster = FakeSecrets(), FakePoster()
        self.route = NotificationRoute('operations', Channel.LINE)
        self.event = NotificationEvent('event-one', IncidentKey('london-01', Check.HEARTBEAT), Severity.RECOVERY, NOW, 502)
        self.provider = GenericWebhookProvider('endpoint-ref', self.secrets, 'token-ref', self.poster)

    def test_webhook_serializes_recovery_context(self):
        self.assertTrue(self.provider.send(self.event, self.route))
        endpoint, body, headers = self.poster.calls[0]
        value = json.loads(body)
        self.assertEqual(value['first_seen_at'], (NOW-timedelta(seconds=502)).isoformat())
        self.assertEqual(value['recovered_at'], NOW.isoformat())
        self.assertEqual(value['outage_seconds'], 502)
        self.assertEqual(value['channel'], 'LINE')
        self.assertNotIn('test-only-value', body.decode())
        self.assertEqual(headers['Idempotency-Key'], 'event-one')

    def test_non_success_status_returns_failure(self):
        for status in (301, 401, 429, 500, True):
            self.poster.status = status
            self.assertFalse(self.provider.send(self.event, self.route))

    def test_invalid_endpoints_never_send(self):
        for endpoint in ('http://example.invalid', 'https://user:pass@example.invalid', 'https://example.invalid/#fragment', 'https://example.invalid/\n'):
            self.secrets.values['endpoint-ref'] = endpoint
            self.assertFalse(self.provider.send(self.event, self.route))
        self.assertEqual(self.poster.calls, [])

    def test_header_injection_never_sends(self):
        self.secrets.values['token-ref'] = 'test\r\nInjected: value'
        self.assertFalse(self.provider.send(self.event, self.route))
        self.assertEqual(self.poster.calls, [])

    def test_secret_provider_failure_does_not_escape(self):
        self.secrets.values.clear()
        self.assertFalse(self.provider.send(self.event, self.route))

    def test_transport_exception_is_not_logged(self):
        class Broken:
            def post(self, *args): raise RuntimeError('sensitive-test-value')
        self.provider.destination.poster = Broken()
        with self.assertNoLogs():
            self.assertFalse(self.provider.send(self.event, self.route))

    def test_logging_provider_reports_only_event_fields(self):
        with self.assertLogs('fxtick.watchdog.notification', level='INFO') as logs:
            self.assertTrue(LoggingNotificationProvider().send(self.event, self.route))
        self.assertIn('RECOVERY', ''.join(logs.output))
        self.assertNotIn('test-only-value', ''.join(logs.output))

    def test_fake_provider_records_confirmed_deliveries(self):
        fake = FakeNotificationProvider()
        fake.succeed = False
        self.assertFalse(fake.send(self.event, self.route)); self.assertEqual(fake.deliveries, [])
        fake.succeed = True
        self.assertTrue(fake.send(self.event, self.route)); self.assertEqual(len(fake.deliveries), 1)

    def test_heartbeat_transport_proof_outside_payload(self):
        sender = HTTPSHeartbeatTransport('endpoint-ref', 'token-ref', self.secrets, self.poster)
        self.assertTrue(sender.send(Heartbeat(healthy(), 'boot-one', 1)))
        _, payload, headers = self.poster.calls[0]
        self.assertNotIn('test-only-value', payload.decode())
        self.assertIn('Authorization', headers)
        self.assertEqual(Heartbeat.decode(payload).snapshot.collector_id, 'london-01')

    def test_heartbeat_transport_requires_auth_reference(self):
        with self.assertRaises(ConfigError): HTTPSHeartbeatTransport('endpoint-ref', None, self.secrets)

    def test_http_timeout_bounds(self):
        for value in (0, 61, float('nan'), float('inf'), True):
            with self.assertRaises(ConfigError): HTTPSPoster(value)

    def test_http_request_uses_timeout_and_does_not_follow_redirect(self):
        class Response:
            status = 204
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with patch('fxtick.watchdog.providers.build_opener') as build:
            build.return_value.open.return_value = Response()
            self.assertEqual(HTTPSPoster(5).post('https://example.invalid', b'{}', {}), 204)
            self.assertEqual(build.return_value.open.call_args.kwargs['timeout'], 5)
            self.assertTrue(any(isinstance(item, _NoRedirect) for item in build.call_args.args))
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.invalid'))

    def test_delivery_config_builds_without_resolving_secrets(self):
        config = DeliveryConfig.from_dict(json.loads(Path('configs/notification.example.json').read_text()))
        self.assertIsInstance(config.build(self.secrets, self.poster), GenericWebhookProvider)
        self.assertEqual(self.poster.calls, [])

    def test_delivery_config_requires_separate_secrets(self):
        config = DeliveryConfig(self.route, 'webhook', 'endpoint-ref')
        with self.assertRaises(ConfigError): config.build()

    def test_delivery_config_rejects_inline_secret(self):
        raw = json.loads(Path('configs/notification.example.json').read_text())
        raw['token'] = 'sensitive-test-value'
        with self.assertRaises(ConfigError) as error: DeliveryConfig.from_dict(raw)
        self.assertNotIn('sensitive-test-value', str(error.exception))

    def test_notification_channels_are_replaceable(self):
        for channel in Channel:
            config = DeliveryConfig(NotificationRoute('operations', channel), 'fake')
            self.assertTrue(config.build().send(self.event, config.route))

    def test_monitor_example_loads_three_nodes(self):
        config = load_monitor_config('configs/external-monitor.example.json')
        self.assertEqual(len(config.nodes), 3)
        self.assertEqual(config.monitor_id, 'tokyo-monitor-01')
        deployment = json.loads(Path('configs/windows-vps.example.json').read_text())
        expected = {(t['collector_id'], t['terminal_id']) for t in deployment['terminals']}
        actual = {(n.collector_id, t) for n in config.nodes for t in n.terminal_ids}
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 10)

    def test_monitor_config_rejects_inline_secret_and_wrong_arrays(self):
        raw = json.loads(Path('configs/external-monitor.example.json').read_text())
        for field, value in (('terminal_ids', 'terminal-one'), ('active_windows', 'closed')):
            changed = json.loads(json.dumps(raw)); changed['nodes'][0][field] = value
            with self.assertRaises(ConfigError): MonitorConfig.from_dict(changed)
        raw['secret'] = 'sensitive-test-value'
        with self.assertRaises(ConfigError): MonitorConfig.from_dict(raw)

    def test_configuration_missing_fields_rejected(self):
        raw = json.loads(Path('configs/external-monitor.example.json').read_text())
        del raw['monitor_id']
        with self.assertRaises(ConfigError): MonitorConfig.from_dict(raw)

    def test_configuration_duplicate_json_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'config.json'; path.write_text('{"schema_version":1,"schema_version":1}')
            with self.assertRaises(ConfigError): load_monitor_config(path)

    def test_demo_recovers_once_after_502_seconds(self):
        from monitor_demo import demonstrate
        result = demonstrate()
        recovery = [v for v in result['events'] if v['severity'] == 'RECOVERY']
        self.assertEqual(len(recovery), 1)
        self.assertEqual(recovery[0]['outage_seconds'], 502)
        self.assertFalse(result['network_access'])


class Probe:
    def __init__(self, name): self.name = name
    def sample(self, now): return healthy(now, self.name)


class Transport:
    def __init__(self): self.sent = []
    def send(self, heartbeat): self.sent.append(heartbeat); return True


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(); self.elapsed = 0
        self.transport = Transport()
        self.node = ManagedCollector(NodePolicy('london-01'), Probe('london-01'), self.transport, 'boot-one')
        self.manager = CollectorManager([self.node], self.clock, lambda: self.elapsed)

    def test_cadence_and_sequence(self):
        self.assertTrue(self.manager.step()['london-01']['heartbeat_sent'])
        self.assertEqual(self.manager.step(), {})
        self.elapsed = 60; self.clock.advance(60); self.manager.step()
        self.assertEqual([b.sequence for b in self.transport.sent], [1, 2])

    def test_monotonic_cadence_ignores_wall_clock_jump(self):
        self.manager.step(); self.clock.advance(500)
        self.assertEqual(self.manager.step(), {})

    def test_probe_failure_still_sends_unhealthy_heartbeat(self):
        class Broken:
            def sample(self, now): raise RuntimeError('sensitive-test-value')
        self.node.probe = Broken()
        with self.assertLogs('fxtick.watchdog.manager') as logs: self.manager.step()
        self.assertEqual(self.transport.sent[0].snapshot.error_state, 'probe-failed')
        self.assertNotIn('sensitive-test-value', ''.join(logs.output))

    def test_wrong_identity_is_not_forwarded(self):
        self.node.probe = Probe('london-02'); self.manager.step()
        self.assertEqual(self.transport.sent[0].snapshot.collector_id, 'london-01')
        self.assertEqual(self.transport.sent[0].snapshot.error_state, 'probe-failed')

    def test_transport_failure_does_not_stop_other_collectors(self):
        class Broken:
            def send(self, heartbeat): raise RuntimeError('sensitive-test-value')
        self.node.transport = Broken()
        second = ManagedCollector(NodePolicy('london-02'), Probe('london-02'), Transport(), 'boot-two')
        manager = CollectorManager([self.node, second], self.clock, lambda: 0)
        with self.assertLogs('fxtick.watchdog.manager') as logs: result = manager.step()
        self.assertFalse(result['london-01']['heartbeat_sent'])
        self.assertTrue(result['london-02']['heartbeat_sent'])
        self.assertNotIn('sensitive-test-value', ''.join(logs.output))

    def test_manager_duplicate_ids_rejected(self):
        with self.assertRaises(ConfigError): CollectorManager([self.node, self.node], self.clock)

    def test_stopped_manager_never_probes(self):
        class Stop:
            def is_set(self): return True
        self.manager.run(Stop())
        self.assertEqual(self.transport.sent, [])

    def test_manager_to_external_monitor_end_to_end_fake_transport(self):
        with tempfile.TemporaryDirectory() as root:
            store = SQLiteState(str(Path(root)/'state.sqlite'))
            try:
                monitor = ExternalMonitor(MonitorConfig('tokyo-01', (self.node.policy,)), store, FakeAuth(), self.clock)
                class LocalTransport:
                    def send(self, heartbeat): return monitor.receive(heartbeat.encode(), 'test-proof')
                self.node.transport = LocalTransport()
                self.manager.step(); self.assertEqual(monitor.evaluate(), [])
                self.clock.advance(180)  # No further manager steps: simulate full VPS loss.
                self.assertEqual(monitor.evaluate()[0].severity, Severity.CRITICAL)
                self.elapsed = 180; self.manager.step()
                self.assertEqual(monitor.evaluate()[0].severity, Severity.RECOVERY)
            finally: store.close()

    def test_disk_probe_does_not_create_files(self):
        with tempfile.TemporaryDirectory() as root:
            before = list(Path(root).iterdir())
            accessible, free = probe_disk(root)
            self.assertTrue(accessible); self.assertGreaterEqual(free, 0)
            self.assertEqual(list(Path(root).iterdir()), before)
            self.assertEqual(probe_disk(Path(root)/'absent'), (False, None))

    def test_core_import_without_windows_or_third_party_modules(self):
        script = '''
import sys
from importlib.abc import MetaPathFinder
# Load the host stdlib before simulating another OS: Windows Python does not
# ship macOS urllib's native _scproxy helper. This is not a native OS test.
import urllib.request
class Block(MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'MetaTrader5','win32api','duckdb','pyarrow','streamlit','requests'}:
            raise ImportError('Unavailable adapter')
sys.meta_path.insert(0, Block())
sys.platform = 'darwin'
import fxtick.watchdog.manager, fxtick.watchdog.monitor, fxtick.watchdog.providers
sys.platform = 'linux'
import fxtick.watchdog.config, fxtick.watchdog.health
'''
        subprocess.run([sys.executable, '-S', '-B', '-c', script], check=True, capture_output=True)

    def test_ingress_ack_after_durable_acceptance_on_owner_thread(self):
        from threading import Thread
        with tempfile.TemporaryDirectory() as root:
            state = SQLiteState(str(Path(root)/'state.sqlite'))
            try:
                inbox = HeartbeatInbox()
                monitor = ExternalMonitor(MonitorConfig('tokyo-01', (self.node.policy,)), state, FakeAuth(), self.clock, inbox=inbox)
                results = []
                thread = Thread(target=lambda: results.append(inbox.submit(Heartbeat(healthy(), 'boot-one', 1).encode(), 'test-proof')))
                thread.start(); thread.join()
                self.assertFalse(results[0].done()); self.assertIsNone(state.latest('london-01'))
                monitor.run_once()
                self.assertTrue(results[0].result()); self.assertIsNotNone(state.latest('london-01'))
            finally: state.close()

    def test_ingress_capacity_and_cancellation(self):
        inbox = HeartbeatInbox(1)
        future = inbox.submit(b'{}')
        with self.assertRaises(ConfigError): inbox.submit(b'{}')
        future.cancel()
        calls = []
        inbox.drain(lambda *args: calls.append(args))
        self.assertEqual(calls, [])

    def test_invalid_ingress_does_not_crash_drain(self):
        inbox = HeartbeatInbox()
        future = inbox.submit(b'{}')
        def reject(*args): raise ConfigError('sensitive-test-value')
        inbox.drain(reject)
        with self.assertRaises(ConfigError) as error: future.result()
        self.assertNotIn('sensitive-test-value', str(error.exception))


if __name__ == '__main__': unittest.main()
