from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fxtick import artifacts as a
from fxtick.config import ConfigError, Collector, Environment, SourceType, load_config
from fxtick.collectors.identity import AcquisitionRecord
from fxtick.collectors.health import HealthSnapshot, HeartbeatReceipt, TerminalHealth
from fxtick.collectors.monitoring import (Channel, Check, IncidentKey, IncidentState,
    MonitoringPolicy, NotificationEvent, NotificationRoute, Severity, NotificationDeliveryState)
from fxtick.policy import ExportPurpose

NOW = datetime(2026, 9, 6, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        # TEMP may use an 8.3 alias; runtime paths intentionally resolve it.
        self.root = Path(self.temp.name).resolve()
        path = self.root / "synthetic.csv"; path.write_bytes(b"synthetic")
        self.artifact = a.seal(path, a.Lineage(a.new_dukascopy()))
        self.collector = Collector("london-01", "london", SourceType.DUKASCOPY, "dukascopy", ("XAUUSD",), "private-buffer")

    def record(self):
        return AcquisitionRecord.for_artifact(self.artifact, self.collector, Environment.TESTING, "XAUUSD")

    def test_collector_record_keeps_provenance_v1_unchanged(self):
        before = self.artifact.lineage.root.to_json()
        record = self.record()
        self.assertEqual(before, self.artifact.lineage.root.to_json())
        self.assertEqual(record.dataset_id, self.artifact.lineage.root.dataset_id)
        self.assertEqual(record.source, "dukascopy")
        self.assertEqual(record.collector_id, "london-01")
        self.assertEqual(record.acquired_at, self.artifact.lineage.root.acquired_at)

    def test_record_roundtrip_and_hash_binding(self):
        record = self.record()
        path = self.root / "acquisition.json"
        record.write(path, self.artifact)
        restored = AcquisitionRecord.read(path)
        self.assertEqual(restored, record)
        restored.verify(self.artifact)

    def test_record_does_not_change_duka_policy(self):
        self.record()
        self.assertTrue(self.artifact.check(ExportPurpose.LOCAL_TEST).allowed)
        with self.assertRaises(PermissionError): self.artifact.check(ExportPurpose.DISTRIBUTION)

    def test_collector_identity_does_not_approve_unknown_broker(self):
        from fxtick.provenance import Provenance
        path = self.root / "unknown.csv"; path.write_bytes(b"synthetic candidate")
        artifact = a.seal(path, a.Lineage(Provenance("candidate", source="mt5", provider="test_broker", acquired_at=NOW)))
        collector = Collector("london-02", "london", SourceType.MT5, "test-broker", ("EURUSD",), "candidate-buffer")
        AcquisitionRecord.for_artifact(artifact, collector, Environment.TESTING, "EURUSD")
        for purpose in ExportPurpose:
            with self.assertRaises(PermissionError): artifact.check(purpose)

    def test_collector_record_cannot_relabel_a_derived_dataset(self):
        path = self.root / "derived.csv"; path.write_bytes(b"synthetic derivative")
        artifact = a.seal(path, a.derive((self.artifact.lineage,)))
        with self.assertRaises(PermissionError): AcquisitionRecord.for_artifact(artifact, self.collector, Environment.TESTING, "XAUUSD")

    def test_ten_terminals_two_collectors_use_independent_namespaces(self):
        from collector_plan import plan
        example = ROOT / "configs/windows-vps.example.json"
        config = load_config(example)
        self.assertEqual(len(config.terminals), 10)
        first, second = plan(example, "london-01"), plan(example, "london-02")
        self.assertEqual(len(first["terminal_ids"]), 5)
        self.assertEqual(len(second["terminal_ids"]), 5)
        self.assertNotEqual(first["paths"]["data_root"], second["paths"]["data_root"])

    def test_mismatched_collector_source_rejected(self):
        collector = replace(self.collector, source_type=SourceType.CTRADER)
        with self.assertRaises(PermissionError): AcquisitionRecord.for_artifact(self.artifact, collector, Environment.TESTING, "XAUUSD")

    def test_mismatched_broker_rejected(self):
        collector = replace(self.collector, broker="other-broker")
        with self.assertRaises(PermissionError): AcquisitionRecord.for_artifact(self.artifact, collector, Environment.TESTING, "XAUUSD")

    def test_unselected_symbol_rejected(self):
        with self.assertRaises(PermissionError): AcquisitionRecord.for_artifact(self.artifact, self.collector, Environment.TESTING, "EURUSD")

    def test_changed_content_invalidates_observation(self):
        record = self.record()
        self.artifact.path.write_bytes(b"changed")
        with self.assertRaises(PermissionError): record.verify(self.artifact)

    def test_record_does_not_overwrite_existing_file(self):
        target = self.root / "record.json"; target.write_bytes(b"old")
        with self.assertRaises(FileExistsError): self.record().write(target, self.artifact)
        self.assertEqual(target.read_bytes(), b"old")

    def test_record_contains_no_machine_paths(self):
        payload = self.record().to_dict()
        self.assertFalse(any("path" in key or "machine" in key for key in payload))
        self.assertNotIn(str(self.root), json.dumps(payload))

    def test_record_cannot_smuggle_license_or_secrets(self):
        for key in ("redistributable", "license_class", "password"):
            payload = self.record().to_dict(); payload[key] = True
            with self.assertRaises(ConfigError): AcquisitionRecord.from_dict(payload)

    def test_portable_cli_plan_is_read_only(self):
        from collector_plan import plan
        path = self.root / "config.json"
        path.write_bytes((ROOT / "configs/collector.example.json").read_bytes())
        before = sorted(p.name for p in self.root.iterdir())
        result = plan(path, "london-01")
        self.assertEqual(result["mode"], "plan-only")
        self.assertIs(result["runtime_implemented"], False)
        self.assertEqual(before, sorted(p.name for p in self.root.iterdir()))

    def test_configured_local_export_roots(self):
        from local_export import resolve_io
        path = self.root / "config.json"
        path.write_bytes((ROOT / "configs/collector.example.json").read_bytes())
        args = SimpleNamespace(config=str(path), inputs=["ticks.csv"], output="mt5.txt", ledger=None)
        inputs, output, ledger, temp_root = resolve_io(args)
        self.assertEqual(inputs, [self.root / "runtime/data/ticks.csv"])
        self.assertEqual(output, self.root / "runtime/exports/mt5.txt")
        self.assertIsNone(ledger)
        self.assertEqual(temp_root, self.root / "runtime/temp")

    def test_configured_export_cannot_escape_root(self):
        from local_export import resolve_io
        path = self.root / "config.json"; path.write_bytes((ROOT / "configs/collector.example.json").read_bytes())
        args = SimpleNamespace(config=str(path), inputs=["ticks.csv"], output=str(self.root / "outside.csv"), ledger=None)
        with self.assertRaises(ConfigError): resolve_io(args)

    def test_legacy_cli_without_config_unchanged(self):
        from local_export import resolve_io
        args = SimpleNamespace(config=None, inputs=["ticks.csv"], output="mt5.txt", ledger="ledger.json")
        self.assertEqual(resolve_io(args), (["ticks.csv"], "mt5.txt", "ledger.json", None))


class HealthTests(unittest.TestCase):
    def test_minimal_health_has_unknown_states(self):
        health = HealthSnapshot("london-01", NOW)
        self.assertIsNone(health.source_connected)
        self.assertIsNone(health.disk_free_bytes)
        self.assertIsNone(health.collector_alive)

    def test_health_serialization(self):
        health = HealthSnapshot("london-01", NOW, True, NOW, NOW, True, 12345, True,
                                None, (TerminalHealth("mt5-01", True),))
        self.assertEqual(HealthSnapshot.from_dict(health.to_dict()), health)

    def test_serialized_health_cannot_mutate_original(self):
        health = HealthSnapshot("london-01", NOW, terminals=(TerminalHealth("mt5-01", True),))
        payload = health.to_dict()
        payload["terminals"][0]["terminal_id"] = "changed"
        self.assertEqual(health.terminals[0].terminal_id, "mt5-01")

    def test_health_utc_normalization(self):
        time = NOW.astimezone(timezone(timedelta(hours=9)))
        self.assertEqual(HealthSnapshot("london-01", time).observed_at, NOW)

    def test_naive_health_time_rejected(self):
        with self.assertRaises(ConfigError): HealthSnapshot("london-01", datetime(2026, 9, 6))

    def test_health_future_activity_rejected(self):
        with self.assertRaises(ConfigError): HealthSnapshot("london-01", NOW, last_tick_time=NOW + timedelta(seconds=1))

    def test_health_negative_disk_space_and_truthy_flags_rejected(self):
        for kwargs in ({"disk_free_bytes": -1}, {"disk_free_bytes": True}, {"source_connected": "yes"}, {"collector_alive": 1}):
            with self.assertRaises(ConfigError): HealthSnapshot("london-01", NOW, **kwargs)

    def test_error_state_is_code_not_raw_exception(self):
        with self.assertRaises(ConfigError): HealthSnapshot("london-01", NOW, error_state="Connection password=secret")
        self.assertEqual(HealthSnapshot("london-01", NOW, error_state="source-disconnected").error_state, "source-disconnected")

    def test_duplicate_terminal_health_rejected(self):
        with self.assertRaises(ConfigError): HealthSnapshot("london-01", NOW, terminals=(TerminalHealth("mt5-01"), TerminalHealth("mt5-01")))

    def test_external_receipt_keeps_receiver_time_separate(self):
        health = HealthSnapshot("london-01", NOW)
        receipt = HeartbeatReceipt("tokyo-monitor-01", NOW + timedelta(seconds=3), health, "boot-one", 1)
        self.assertNotEqual(receipt.received_at, receipt.snapshot.observed_at)
        self.assertEqual(receipt.monitor_id, "tokyo-monitor-01")

    def test_receipt_supports_clock_skew_and_restart_sequence(self):
        receipt = HeartbeatReceipt("tokyo-monitor-01", NOW, HealthSnapshot("london-01", NOW + timedelta(seconds=30)), "boot-two", 0)
        self.assertEqual(receipt.sequence, 0)
        with self.assertRaises(ConfigError): replace(receipt, sequence=-1)

    def test_health_rejects_extra_secret_fields(self):
        payload = HealthSnapshot("london-01", NOW).to_dict(); payload["token"] = "dummy"
        with self.assertRaises(ConfigError): HealthSnapshot.from_dict(payload)


class MonitoringTests(unittest.TestCase):
    def test_policy_defaults_and_example(self):
        data = json.loads((ROOT / "configs/monitoring.example.json").read_text())
        policy = MonitoringPolicy.from_dict(data)
        self.assertEqual(policy.heartbeat_timeout_seconds, 180)
        self.assertEqual(policy.cooldown_seconds, 300)
        self.assertTrue(policy.recovery_notification)

    def test_policy_invalid_thresholds_rejected(self):
        for value in (0, -1, True, 3.5):
            with self.assertRaises(ConfigError): MonitoringPolicy(heartbeat_timeout_seconds=value)

    def test_policy_secrets_not_supported(self):
        data = MonitoringPolicy().__dict__.copy(); data["line_token"] = "dummy"
        with self.assertRaises(ConfigError): MonitoringPolicy.from_dict(data)

    def test_four_severities(self):
        self.assertEqual({s.value for s in Severity}, {"INFO", "WARNING", "CRITICAL", "RECOVERY"})

    def test_notification_routes_have_no_recipients_or_secrets(self):
        for channel in Channel:
            route = NotificationRoute("operations", channel)
            self.assertEqual(set(route.__dict__), {"route_id", "channel"})

    def test_stable_incident_key_for_deduplication(self):
        left = IncidentKey("london-01", Check.HEARTBEAT)
        self.assertEqual(left, IncidentKey("london-01", Check.HEARTBEAT))
        self.assertEqual(len({left, IncidentKey("london-01", Check.HEARTBEAT)}), 1)

    def test_separate_terminal_incidents(self):
        self.assertNotEqual(IncidentKey("london-01", Check.TERMINAL, "mt5-01"), IncidentKey("london-01", Check.TERMINAL, "mt5-02"))
        with self.assertRaises(ConfigError): IncidentKey("london-01", Check.TERMINAL)

    def test_cooldown_and_recovery_state_fields(self):
        key = IncidentKey("london-01", Check.HEARTBEAT)
        state = IncidentState(key, NOW, NOW + timedelta(seconds=180), NOW + timedelta(seconds=502))
        event = NotificationEvent("recovery-london-01-one", key, Severity.RECOVERY, state.recovered_at, 502)
        self.assertEqual(event.outage_seconds, 8 * 60 + 22)

    def test_recovery_needs_duration(self):
        with self.assertRaises(ConfigError): NotificationEvent("event-one", IncidentKey("london-01", Check.HEARTBEAT), Severity.RECOVERY, NOW)

    def test_fake_notification_provider_only(self):
        class FakeProvider:
            def __init__(self): self.events = []
            def send(self, event, route): self.events.append((event, route)); return True
        provider = FakeProvider()
        event = NotificationEvent("event-one", IncidentKey("london-01", Check.HEARTBEAT), Severity.CRITICAL, NOW)
        self.assertTrue(provider.send(event, NotificationRoute("operations", Channel.LINE)))
        self.assertEqual(len(provider.events), 1)

    def test_delivery_cooldown_state_is_separate_per_route(self):
        key = IncidentKey("london-01", Check.HEARTBEAT)
        line = NotificationDeliveryState(key, "operations-line", "event-one", NOW)
        email = NotificationDeliveryState(key, "operations-email")
        self.assertNotEqual(line.route_id, email.route_id)
        self.assertIsNone(email.last_successful_send_at)

    def test_delivery_success_needs_event_and_timestamp(self):
        key = IncidentKey("london-01", Check.HEARTBEAT)
        with self.assertRaises(ConfigError): NotificationDeliveryState(key, "operations", "event-one")


class PlatformTests(unittest.TestCase):
    def test_core_imports_without_windows_or_data_dependencies(self):
        script = '''
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'MetaTrader5','win32api','win32service','duckdb','pyarrow','numpy','streamlit','googleapiclient'}:
            raise ImportError('platform dependency blocked')
sys.meta_path.insert(0, Block())
import fxtick.config, fxtick.provenance, fxtick.policy, fxtick.artifacts
import fxtick.collectors.identity, fxtick.collectors.health, fxtick.collectors.monitoring
import fxtick.secret_provider
'''
        for platform in ("darwin", "linux"):
            result = subprocess.run([sys.executable, "-S", "-B", "-c", f"import sys; sys.platform={platform!r}\n" + script], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_windows_adapter_import_does_not_initialize_mt5(self):
        module = importlib.import_module("fxtick.platform.windows")
        self.assertNotIn("MetaTrader5", sys.modules)
        from fxtick.config import Terminal
        with patch.object(module, "os", SimpleNamespace(name="posix")):
            with self.assertRaises(module.WindowsOnlyError):
                module.terminal_path(Terminal("mt5-01", "london-01", "test-broker", "terminals/mt5-01/terminal64.exe"), Path.cwd())


if __name__ == "__main__": unittest.main()
