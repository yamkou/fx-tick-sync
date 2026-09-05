"""Offline REAL library integration. Explicit skips when dependencies are absent.

No real feed/Drive calls: acquisition and Google transport are always patched.
Run without -S in an existing environment that has the declared dependencies.
"""
from datetime import datetime, timezone
import hashlib
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fxtick import artifacts as a, trusted_config, gdrive
from fxtick.policy import ExportPurpose, SourcePolicy
from fxtick.provenance import LicenseClass, Provenance


def available(*names):
    return all(importlib.util.find_spec(name) is not None for name in names)


@unittest.skipUnless(available("duckdb", "pyarrow", "numpy"), "UNEXECUTED: real DuckDB/PyArrow/NumPy unavailable")
class RealConversionTests(unittest.TestCase):
    def setUp(self):
        from fxtick import duck
        self.duck = duck
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.con = duck.connect(temp_dir=self.root / "spill")
        self.addCleanup(self.con.close)
        self.csv = self.root / "input.csv"
        self.csv.write_text("timestamp,bidPrice,askPrice,bidVolume,askVolume\n"
            "2026-09-01T00:00:00.123Z,1.12345,1.12355,1,1\n"
            "2026-09-01T00:00:01.456Z,1.12346,1.12356,1,1\n", encoding="utf-8")
        self.input = a.seal(self.csv, a.Lineage(a.new_dukascopy()))

    def test_csv_parquet_mt4_mt5_hst_local_roundtrip(self):
        from fxtick import mt_export
        out = self.root / "ticks.parquet"
        self.assertEqual(self.duck.csv_to_parquet(self.con, self.csv, out), 2)
        artifact = a.inspect(out)
        self.assertTrue(artifact.check(ExportPurpose.LOCAL_TEST).allowed)
        with self.assertRaises(PermissionError): artifact.check(ExportPurpose.DISTRIBUTION)
        query = self.duck.normalized_select(self.duck.source_sql(out))
        mt4, mt5, hst = self.root / "mt4.csv", self.root / "mt5.txt", self.root / "TEST1.hst"
        mt_export.export_mt4_ticks(self.con, query, mt4, "utc", 5)
        mt_export.export_mt5_ticks(self.con, query, mt5, "utc", 5)
        self.assertEqual(mt_export.export_hst(self.con, query, hst, "TEST", 1, 5, "utc"), 1)
        self.assertIn("00:00:00.123", mt4.read_text())
        self.assertIn("00:00:01.456", mt5.read_text())
        self.assertEqual(hst.stat().st_size, 148 + 60)
        for path in (mt4, mt5, hst):
            self.assertTrue(a.inspect(path).check(ExportPurpose.LOCAL_TEST).allowed)
            with self.assertRaises(PermissionError): a.inspect(path).check(ExportPurpose.DISTRIBUTION)
        self.assertEqual(self.csv.read_bytes(), self.input.path.read_bytes())

    def test_legacy_parquet_registration_does_not_edit_footer(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        legacy = self.root / "legacy.parquet"
        table = pa.table({"timestamp": [datetime(2026, 9, 1, tzinfo=timezone.utc)],
                          "bidPrice": [1.1], "askPrice": [1.2], "bidVolume": [1.0], "askVolume": [1.0]})
        pq.write_table(table, legacy)
        before = legacy.read_bytes()
        ledger = self.root / "ledger.json"
        a.register_legacy(legacy, ledger, owner_confirmed=True)
        query = self.duck.union_sources([legacy], ledger=ledger)
        converted = self.root / "derived.parquet"
        self.duck.write_parquet(self.con, query, converted)
        self.assertEqual(legacy.read_bytes(), before)
        self.assertFalse(a.inspect(converted).lineage.root.redistributable)

    def test_real_parquet_footer_sidecar_disagreement(self):
        out = self.root / "ticks.parquet"
        self.duck.csv_to_parquet(self.con, self.csv, out)
        data = a.parse(a.sidecar(out).read_text())
        data["lineage"]["root"]["dataset_id"] = "tampered"
        a.sidecar(out).write_text(a.canonical(data))
        with self.assertRaises(PermissionError): a.inspect(out)

    def test_real_month_merge_deduplicates_and_retains_lineage(self):
        first = self.root / "first.parquet"
        self.duck.csv_to_parquet(self.con, self.csv, first)
        second = self.root / "second.parquet"
        self.assertEqual(self.duck.merge_month(self.con, self.csv, 2026, 9, [first], second), 2)
        self.assertEqual(len(a.inspect(second).lineage.root.derived_from), 2)
        with self.assertRaises(PermissionError): a.inspect(second).check(ExportPurpose.DISTRIBUTION)


@unittest.skipUnless(available("dukascopy_python", "pandas", "dateutil"), "UNEXECUTED: real acquisition dependencies unavailable")
class RealAcquisitionTests(unittest.TestCase):
    def test_mocked_feed_marks_frame_and_csv(self):
        import pandas as pd
        from fxtick import fetcher
        frame = pd.DataFrame({"bidPrice": [1.1], "askPrice": [1.2], "bidVolume": [1.0], "askVolume": [1.0]},
                             index=pd.to_datetime(["2026-09-01T00:00:00.123Z"]))
        with tempfile.TemporaryDirectory() as td, patch.object(fetcher.dukascopy_python, "fetch", return_value=frame):
            target = Path(td) / "new.csv"
            self.assertEqual(fetcher.download_range_to_csv("TEST", datetime(2026, 9, 1, tzinfo=timezone.utc),
                datetime(2026, 9, 2, tzinfo=timezone.utc), target), 1)
            self.assertTrue(a.inspect(target).check(ExportPurpose.LOCAL_TEST).allowed)
            with self.assertRaises(PermissionError): a.inspect(target).check(ExportPurpose.DISTRIBUTION)


@unittest.skipUnless(available("streamlit", "duckdb", "pyarrow", "numpy"), "UNEXECUTED: real Streamlit/DuckDB/PyArrow/NumPy unavailable")
class RealStreamlitTests(unittest.TestCase):
    def test_real_streamlit_download_boundary_denies_duka(self):
        from streamlit.testing.v1 import AppTest
        script = '''
import streamlit as st
from pathlib import Path
import tempfile
from fxtick.artifacts import seal, Lineage, new_dukascopy
from fxtick.export_service import streamlit_download
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "private.zip"
    p.write_bytes(b"synthetic only")
    artifact = seal(p, Lineage(new_dukascopy()))
    try:
        streamlit_download(st, artifact, label="download")
    except PermissionError:
        st.success("PRIVATE_REFERENCE blocked")
'''
        app = AppTest.from_string(script).run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.success[0].value, "PRIVATE_REFERENCE blocked")
        self.assertEqual(len(app.get("download_button")), 0)


@unittest.skipUnless(available("pyzipper"), "UNEXECUTED: real pyzipper unavailable")
class RealEncryptedArchiveTests(unittest.TestCase):
    def test_local_encrypted_zip_retains_denial(self):
        from fxtick.export_service import build_zip
        import pyzipper
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ticks.csv"
            path.write_bytes(b"synthetic")
            a.seal(path, a.Lineage(a.new_dukascopy()))
            archive = build_zip([(path, "ticks.csv")], Path(td) / "local.zip",
                                purpose=ExportPurpose.LOCAL_TEST, password="test-only")
            with pyzipper.AESZipFile(archive.path) as src:
                src.setpassword(b"test-only")
                self.assertEqual(src.read("ticks.csv"), b"synthetic")
            with self.assertRaises(PermissionError): archive.check(ExportPurpose.DISTRIBUTION)


if __name__ == "__main__": unittest.main()
