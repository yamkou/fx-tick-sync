"""Cross-platform configuration tests: no third-party imports or external I/O."""
from copy import deepcopy
from pathlib import Path, PureWindowsPath, PurePosixPath
import json
import os
import tempfile
import unittest

from fxtick.config import ConfigError, DeploymentConfig, load_config, logical_id, native_path, resolve_path

EXAMPLE = Path(__file__).resolve().parents[1] / "configs/collector.example.json"


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(EXAMPLE.read_text(encoding="utf-8"))

    def test_example_loads_without_creating_paths(self):
        config = load_config(EXAMPLE)
        with tempfile.TemporaryDirectory() as td:
            config.paths.resolve(Path(td))
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_windows_absolute_lexical_resolution(self):
        self.assertEqual(resolve_path(r"C:\ticks\data", PureWindowsPath("D:/config")), PureWindowsPath("C:/ticks/data"))

    def test_windows_relative_resolution(self):
        self.assertEqual(resolve_path("runtime/data", PureWindowsPath("D:/config")), PureWindowsPath("D:/config/runtime/data"))

    def test_posix_linux_and_macos_resolution(self):
        for base in ("/srv/fxtick", "/Users/operator/fxtick"):
            self.assertEqual(resolve_path("runtime/data", PurePosixPath(base)), PurePosixPath(base) / "runtime/data")
            self.assertEqual(resolve_path("/data/ticks", PurePosixPath(base)), PurePosixPath("/data/ticks"))

    def test_native_resolution_returns_path(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsInstance(native_path("runtime/data", Path(td)), Path)
            self.assertEqual(native_path("runtime/data", Path(td)), Path(td).resolve() / "runtime/data")

    def test_foreign_absolute_path_is_not_silently_reinterpreted(self):
        with self.assertRaises(ConfigError): resolve_path("C:/ticks", PurePosixPath("/config"))
        with self.assertRaises(ConfigError): resolve_path("/srv/ticks", PureWindowsPath("C:/config"))

    def test_ambiguous_expanded_or_traversing_paths_rejected(self):
        for path in ("C:data", "../data", "a/../data", "$HOME/data", "%USERPROFILE%/data", "~/data", "a\x00b", "*.parquet", "\\\\?\\C:\\data"):
            with self.subTest(path=path), self.assertRaises(ConfigError): resolve_path(path, PureWindowsPath("C:/config"))

    def test_reserved_names_and_credentials_in_path_rejected(self):
        for path in ("CON", "data/NUL.csv", "data/file:stream", "data./ticks", "https://user:password@example.invalid/data"):
            with self.assertRaises(ConfigError): resolve_path(path, PurePosixPath("/config"))

    def test_required_top_level_fields(self):
        for key in self.data:
            data = deepcopy(self.data); del data[key]
            with self.assertRaises(ConfigError): DeploymentConfig.from_dict(data)

    def test_required_collector_fields(self):
        for key in self.data["collectors"][0]:
            data = deepcopy(self.data); del data["collectors"][0][key]
            with self.assertRaises(ConfigError): DeploymentConfig.from_dict(data)

    def test_unknown_environment_rejected(self):
        self.data["environment"] = "anything"
        with self.assertRaises(ConfigError): DeploymentConfig.from_dict(self.data)

    def test_collector_id_validation(self):
        for value in ("London-01", "../node", "a--b", "node_1", "", "x" * 64, 3):
            with self.assertRaises(ConfigError): logical_id(value)
        self.assertEqual(logical_id("tokyo-monitor-01"), "tokyo-monitor-01")

    def test_duplicate_collector_rejected(self):
        self.data["collectors"].append(deepcopy(self.data["collectors"][0]))
        with self.assertRaises(ConfigError): DeploymentConfig.from_dict(self.data)

    def mt5(self):
        self.data["collectors"][0].update(source_type="mt5", broker="test-broker")
        self.data["terminals"] = [{"terminal_id": "test-01", "collector_id": "london-01", "broker": "test-broker", "path": "terminals/test-01/terminal64.exe"}]

    def test_terminal_registry_valid(self):
        self.mt5()
        self.assertEqual(DeploymentConfig.from_dict(self.data).terminals[0].terminal_id, "test-01")

    def test_duplicate_terminal_id_rejected(self):
        self.mt5(); self.data["terminals"].append(deepcopy(self.data["terminals"][0]))
        with self.assertRaises(ConfigError): DeploymentConfig.from_dict(self.data)

    def test_duplicate_terminal_path_case_insensitive(self):
        self.mt5()
        self.data["terminals"].append({**self.data["terminals"][0], "terminal_id": "test-02", "path": "TERMINALS/TEST-01/terminal64.exe"})
        with self.assertRaises(ConfigError): DeploymentConfig.from_dict(self.data)

    def test_terminal_broker_and_collector_must_match(self):
        for key in ("broker", "collector_id"):
            self.mt5(); self.data["terminals"][0][key] = "different"
            with self.assertRaises(ConfigError): DeploymentConfig.from_dict(self.data)

    def test_mt5_needs_terminal(self):
        self.mt5(); self.data["terminals"] = []
        with self.assertRaises(ConfigError): DeploymentConfig.from_dict(self.data)

    def test_unknown_source_rejected(self):
        self.data["collectors"][0]["source_type"] = "automatic"
        with self.assertRaises(ConfigError): DeploymentConfig.from_dict(self.data)

    def test_unknown_storage_rejected(self):
        self.data["collectors"][0]["storage_destination"] = "missing"
        with self.assertRaises(ConfigError): DeploymentConfig.from_dict(self.data)

    def test_duplicate_storage_rejected(self):
        self.data["storage"].append(deepcopy(self.data["storage"][0]))
        with self.assertRaises(ConfigError): DeploymentConfig.from_dict(self.data)

    def test_secret_fields_rejected_at_every_level(self):
        for section in (None, "paths", "collectors", "storage"):
            data = deepcopy(self.data)
            target = data if section is None else data[section] if section == "paths" else data[section][0]
            target["password"] = "DO-NOT-ECHO-THIS"
            with self.assertRaises(ConfigError) as caught: DeploymentConfig.from_dict(data)
            self.assertNotIn("DO-NOT-ECHO-THIS", str(caught.exception))

    def test_malformed_json_error_does_not_echo_values(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"; path.write_text('{"password":"DO-NOT-ECHO-THIS",broken}', encoding="utf-8")
            with self.assertRaises(ConfigError) as caught: load_config(path)
            self.assertNotIn("DO-NOT-ECHO-THIS", str(caught.exception))

    def test_duplicate_json_field_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"; path.write_text('{"schema_version":1,"schema_version":1}')
            with self.assertRaises(ConfigError): load_config(path)

    def test_overlapping_roots_rejected(self):
        self.data["paths"]["temp_root"] = "runtime/data/tmp"
        config = DeploymentConfig.from_dict(self.data)
        with tempfile.TemporaryDirectory() as td, self.assertRaises(ConfigError): config.paths.resolve(Path(td))

    def test_registry_cannot_be_inside_temp_or_export(self):
        for root in ("temp", "exports"):
            self.data["paths"]["provenance_registry"] = f"runtime/{root}/ledger.json"
            config = DeploymentConfig.from_dict(self.data)
            with tempfile.TemporaryDirectory() as td, self.assertRaises(ConfigError): config.paths.resolve(Path(td))

    def test_schema_and_array_types_rejected(self):
        for key, value in (("schema_version", True), ("schema_version", 2), ("collectors", {}), ("terminals", None)):
            data = deepcopy(self.data); data[key] = value
            with self.assertRaises(ConfigError): DeploymentConfig.from_dict(data)


if __name__ == "__main__": unittest.main()
