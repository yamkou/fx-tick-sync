import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fxtick.config import ConfigError
from fxtick.staging import initialize, load_staging, dry_run
from fxtick import preflight
from fxtick.staging_logging import StagingLogs, Event

TEMPLATE = Path('deployment/windows-staging/collector.template.json')


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()/'deployment'

    def init(self, **kwargs):
        return initialize(self.root, TEMPLATE, **kwargs)

    def test_one_terminal_quarantine_and_configurable_broker(self):
        path = self.init(broker='another-broker', terminal_id='terminal-01')
        config, roots, root = load_staging(path)
        self.assertEqual(config.terminals[0].broker, 'another-broker')
        self.assertEqual(config.terminals[0].terminal_id, 'terminal-01')
        self.assertEqual(config.storage[0].zone, 'QUARANTINE')
        self.assertEqual(len(config.terminals), 1)
        self.assertEqual(roots['data_root'], root/'data')
        self.assertFalse((root/'terminals/terminal-01/terminal64.exe').exists())

    def test_existing_configuration_never_overwritten(self):
        path = self.init(); before = path.read_bytes()
        with self.assertRaises(ConfigError): self.init()
        self.assertEqual(path.read_bytes(), before)

    def test_existing_history_preserved(self):
        folder = self.root/'data'; folder.mkdir(parents=True)
        history = folder/'existing.parquet'; history.write_bytes(b'existing synthetic history')
        self.init()
        self.assertEqual(history.read_bytes(), b'existing synthetic history')

    def test_invalid_id_no_side_effect(self):
        with self.assertRaises(ConfigError): self.init(collector_id='../escape')
        self.assertFalse(self.root.exists())

    def test_distribution_template_rejected_before_creation(self):
        data = json.loads(TEMPLATE.read_text())
        data['storage'][0]['zone'] = 'DISTRIBUTION'
        template = Path(self.temp.name)/'bad.json'; template.write_text(json.dumps(data))
        with self.assertRaises(ConfigError): initialize(self.root, template)
        self.assertFalse(self.root.exists())

    def test_production_and_extra_terminal_rejected(self):
        path = self.init(); original = json.loads(path.read_text())
        for key, value in [('environment','production'), ('terminals',original['terminals']*2)]:
            data = dict(original); data[key] = value; path.write_text(json.dumps(data))
            with self.assertRaises(ConfigError): load_staging(path)

    def test_external_write_root_rejected(self):
        path = self.init(); data = json.loads(path.read_text())
        data['paths']['log_root'] = str(Path(self.temp.name)/'other-logs')
        path.write_text(json.dumps(data))
        with self.assertRaises(ConfigError): load_staging(path)
        self.assertFalse((Path(self.temp.name)/'other-logs').exists())

    def test_dry_run_no_files_or_network_and_not_runtime_ready(self):
        path = self.init(); before = set(self.root.rglob('*'))
        with patch('socket.socket.connect', side_effect=AssertionError('network forbidden')):
            for component in ('collector','heartbeat'):
                result = dry_run(path, component)
                self.assertFalse(result['runtime_ready'])
                self.assertFalse(result['network_sent'])
                self.assertFalse(result['distribution_enabled'])
        self.assertEqual(set(self.root.rglob('*')), before)

    def test_explicit_dry_run_logs(self):
        path = self.init()
        dry_run(path, 'collector', write_logs=True)
        dry_run(path, 'heartbeat', write_logs=True)
        for name in ('collector','heartbeat'):
            self.assertIn('dry-run', (self.root/'logs/london-01'/f'{name}.log').read_text())

    def test_missing_secrets_and_mt5_fail_with_no_network(self):
        path = self.init()
        with patch.dict(os.environ, {}, clear=True), patch.object(preflight, 'outbound_check') as network:
            report = preflight.check(path)
        states = {r['check']:r['status'] for r in report['checks']}
        self.assertEqual(states['heartbeat-secret-present'], 'FAIL')
        self.assertEqual(states['monitor-endpoint'], 'FAIL')
        self.assertEqual(states['mt5-path'], 'FAIL')
        self.assertEqual(states['outbound-monitor-health'], 'NOT_RUN')
        self.assertEqual(states['state-writable'], 'PASS')
        self.assertFalse(report['ready']); network.assert_not_called()

    def test_secret_values_never_in_report_and_no_false_readiness(self):
        path = self.init()
        with patch.dict(os.environ, {'FX_STAGING_HMAC':'synthetic-sensitive-value',
             'FX_STAGING_MONITOR_ENDPOINT':'https://monitor.invalid/v1/heartbeat'}):
            report = preflight.check(path)
        self.assertNotIn('synthetic-sensitive-value', json.dumps(report))
        self.assertNotIn('monitor.invalid', json.dumps(report))
        self.assertFalse(report['ready'])
        self.assertIn({'check':'heartbeat-secret-present','status':'PASS'}, report['checks'])

    def test_network_opt_in_only_calls_injected_checker(self):
        path = self.init()
        with patch.dict(os.environ, {'FX_STAGING_MONITOR_ENDPOINT':'https://monitor.invalid/v1/heartbeat'}), \
             patch.object(preflight, 'outbound_check', return_value=True) as network:
            report = preflight.check(path, check_network=True)
        network.assert_called_once()
        self.assertIn({'check':'outbound-monitor-health','status':'PASS'}, report['checks'])

    def test_network_error_is_redacted(self):
        path = self.init()
        with patch.dict(os.environ, {'FX_STAGING_MONITOR_ENDPOINT':'https://monitor.invalid/v1/heartbeat'}), \
             patch.object(preflight, 'outbound_check', side_effect=RuntimeError('sensitive-exception')):
            report = preflight.check(path, check_network=True)
        self.assertNotIn('sensitive-exception', json.dumps(report))
        self.assertIn({'check':'outbound-monitor-health','status':'FAIL'}, report['checks'])

    def test_endpoint_rejects_credentials_queries_and_plain_http(self):
        for endpoint in ('http://monitor.invalid/v1/heartbeat', 'https://user:pass@monitor.invalid/v1/heartbeat',
                         'https://monitor.invalid/v1/heartbeat?token=hidden', 'https://monitor.invalid/other'):
            self.assertFalse(preflight.endpoint_valid(endpoint))

    def test_low_disk_and_write_failure_are_reported(self):
        path = self.init()
        with patch.object(preflight, 'writable', return_value=False):
            report = preflight.check(path, minimum_free_bytes=10**30)
        for name in ('data','log','state'):
            self.assertIn({'check':name+'-writable','status':'FAIL'}, report['checks'])
            self.assertIn({'check':name+'-disk-free','status':'FAIL'}, report['checks'])

    def test_invalid_config_does_not_probe_paths(self):
        path = self.init(); path.write_text('{"secret":"synthetic-hidden"}')
        with patch.object(preflight, 'writable') as probe:
            report = preflight.check(path)
        probe.assert_not_called()
        self.assertFalse(report['ready']); self.assertNotIn('synthetic-hidden', json.dumps(report))

    def test_logs_rotate_and_reject_free_text(self):
        directory = Path(self.temp.name)
        with StagingLogs(directory, max_bytes=100, backups=2) as logs:
            with self.assertRaises(ValueError): logs.emit('collector', 'password=synthetic')
            for _ in range(12): logs.emit('collector', Event.DRY_RUN)
            logs.emit('watchdog', Event.PREFLIGHT_BLOCKED)
            logs.emit('heartbeat', Event.DRY_RUN)
        self.assertTrue((directory/'collector.log.2').exists())
        self.assertFalse((directory/'collector.log.3').exists())
        self.assertIn('preflight-blocked', (directory/'error.log').read_text())
        self.assertNotIn('password', ''.join(p.read_text() for p in directory.glob('*.log*')))

    def test_examples_contain_variable_names_only(self):
        import re
        for path in Path('deployment/windows-staging').glob('*env.example'):
            for line in path.read_text().splitlines():
                self.assertRegex(line, r'^[A-Z][A-Z0-9_]*$')


if __name__ == '__main__': unittest.main()
