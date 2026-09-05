"""Offline boundary integration: synthetic files and explicit API/engine fakes."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch
import ast
import hashlib
import importlib
import json
import shutil
import tempfile
import unittest

from fxtick import artifacts as a, gdrive, trusted_config
from fxtick.policy import ExportPurpose, SourcePolicy
from fxtick.provenance import LicenseClass, Provenance
from fxtick.export_service import build_zip, streamlit_download, guarded_conversion
from fxtick.query import Query
from phase2_fakes import fake_engines, FakeCon, FakeDrive

LOCAL, DIST = ExportPurpose.LOCAL_TEST, ExportPurpose.DISTRIBUTION
APPROVAL = SourcePolicy("synthetic", "test", LicenseClass.DISTRIBUTABLE, True, True, "fictional-test-only")


class Phase2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.counter = 0
        self.config = patch.multiple(trusted_config, SOURCE_POLICIES=(APPROVAL,), DISTRIBUTION_ATTESTATIONS={})
        self.config.start()
        self.addCleanup(self.config.stop)
        self.roots = gdrive.DriveRoots("private", "distribution")

    def path(self, suffix=".csv"):
        self.counter += 1
        return self.root / f"data{self.counter}{suffix}"

    def artifact(self, node=None, *, attest=False):
        path = self.path()
        path.write_text(f"synthetic test data {self.counter}\n")
        result = a.seal(path, a.Lineage(node or a.new_dukascopy()))
        if attest:
            trusted_config.DISTRIBUTION_ATTESTATIONS[result.sha256] = hashlib.sha256(a.canonical(result.lineage.payload()).encode()).hexdigest()
        return result

    def approved(self, *, attest=True):
        return self.artifact(Provenance(f"approved-{self.counter}", source="synthetic", provider="test",
            license_class=LicenseClass.DISTRIBUTABLE, redistributable=True,
            acquired_at=datetime.now(timezone.utc)), attest=attest)

    def legacy(self):
        path, ledger = self.path(), self.path(".json")
        path.write_bytes(b"old synthetic owner history")
        return a.register_legacy(path, ledger, owner_confirmed=True)

    def test_duka_local_allowed_distribution_denied(self):
        artifact = self.artifact()
        self.assertTrue(artifact.check(LOCAL).allowed)
        with self.assertRaises(PermissionError): artifact.check(DIST)

    def test_unknown_denied_both_purposes(self):
        artifact = self.artifact(Provenance("unknown"))
        for purpose in (LOCAL, DIST):
            with self.assertRaises(PermissionError): artifact.check(purpose)

    def test_missing_manifest_denied_both_purposes(self):
        artifact = self.artifact()
        a.sidecar(artifact.path).unlink()
        for purpose in (LOCAL, DIST):
            with self.assertRaises(PermissionError): artifact.check(purpose)

    def test_registration_needs_explicit_owner_confirmation(self):
        path = self.path(); path.write_bytes(b"old")
        with self.assertRaises(PermissionError): a.register_legacy(path, self.path(".json"))
        self.assertEqual(path.read_bytes(), b"old")

    def test_registration_preserves_bytes_and_records_required_fields(self):
        artifact = self.legacy()
        self.assertEqual(artifact.path.read_bytes(), b"old synthetic owner history")
        record = a.parse(artifact.ledger.read_text())["files"][str(artifact.path)]
        self.assertEqual(record["source"], "DUKASCOPY")
        self.assertEqual(record["allowed_use"], "LOCAL_TEST")
        self.assertEqual(record["license_class"], "PRIVATE_REFERENCE")
        self.assertIs(record["redistributable"], False)
        self.assertEqual(record["sha256"], artifact.sha256)
        self.assertEqual(record["size"], artifact.size)
        self.assertTrue(record["registered_at"])
        self.assertEqual(record["schema_version"], 1)
        self.assertTrue(artifact.check(LOCAL).allowed)
        with self.assertRaises(PermissionError): artifact.check(DIST)

    def test_registration_idempotent(self):
        artifact = self.legacy()
        before = artifact.ledger.read_bytes()
        self.assertEqual(a.register_legacy(artifact.path, artifact.ledger, owner_confirmed=True), artifact)
        self.assertEqual(artifact.ledger.read_bytes(), before)

    def test_ledger_does_not_register_neighbors_or_names(self):
        artifact = self.legacy()
        other = self.root / "DUKASCOPY_PRIVATE_REFERENCE.csv"
        other.write_bytes(artifact.path.read_bytes())
        with self.assertRaises(PermissionError): a.inspect(other, ledger=artifact.ledger)

    def test_legacy_hash_change_denied_both_purposes(self):
        artifact = self.legacy()
        artifact.path.write_bytes(b"changed")
        for purpose in (LOCAL, DIST):
            with self.assertRaises(PermissionError): artifact.check(purpose)

    def test_ledger_flag_or_identity_tampering_never_grants_distribution(self):
        artifact = self.legacy()
        original = artifact.ledger.read_text()
        for field, value in (("source", "synthetic"), ("redistributable", True),
                             ("license_class", "DISTRIBUTABLE"), ("allowed_use", "DISTRIBUTION")):
            with self.subTest(field=field):
                data = a.parse(original)
                data["files"][str(artifact.path)][field] = value
                artifact.ledger.write_text(a.canonical(data))
                with self.assertRaises(PermissionError): a.inspect(artifact.path, ledger=artifact.ledger)

    def test_ledger_inner_provenance_tampering_rejected(self):
        artifact = self.legacy()
        data = a.parse(artifact.ledger.read_text())
        data["files"][str(artifact.path)]["provenance"]["source"] = "synthetic"
        artifact.ledger.write_text(a.canonical(data))
        with self.assertRaises(PermissionError): artifact.check(LOCAL)

    def test_sidecar_cannot_override_legacy_ledger(self):
        artifact = self.legacy()
        a.seal(artifact.path, self.approved().lineage)
        with self.assertRaises(PermissionError): a.inspect(artifact.path, ledger=artifact.ledger)
        with self.assertRaises(PermissionError): a.inspect(artifact.path).check(DIST)

    def test_legacy_and_new_inputs_can_be_combined_for_local_validation(self):
        legacy, managed = self.legacy(), self.artifact()
        with fake_engines():
            duck = importlib.import_module("fxtick.duck")
            query = duck.union_sources([legacy.path, managed.path], ledger=legacy.ledger)
            self.assertTrue(query.check(LOCAL).check(LOCAL).allowed)
            with self.assertRaises(PermissionError): query.check(DIST)

    def test_tampered_bytes_rejected(self):
        artifact = self.artifact()
        artifact.path.write_text("changed")
        with self.assertRaises(PermissionError): artifact.check(LOCAL)

    def test_same_size_tampering_rejected(self):
        artifact = self.artifact()
        artifact.path.write_bytes(b"x" * artifact.size)
        with self.assertRaises(PermissionError): artifact.check(LOCAL)

    def test_renamed_with_manifest_still_duka(self):
        artifact = self.artifact()
        renamed = self.root / "COMMERCIAL_APPROVED.csv"
        shutil.copyfile(artifact.path, renamed)
        shutil.copyfile(a.sidecar(artifact.path), a.sidecar(renamed))
        self.assertTrue(a.inspect(renamed).check(LOCAL).allowed)
        with self.assertRaises(PermissionError): a.inspect(renamed).check(DIST)

    def test_renamed_without_manifest_denied(self):
        artifact = self.artifact()
        renamed = self.path(); shutil.copyfile(artifact.path, renamed)
        with self.assertRaises(PermissionError): a.inspect(renamed)

    def test_self_claimed_approval_and_hash_not_sufficient(self):
        artifact = self.approved(attest=False)
        with self.assertRaises(PermissionError): artifact.check(DIST)

    def test_attested_approved_synthetic_distribution_allowed(self):
        self.assertTrue(self.approved().check(DIST).allowed)

    def test_attestation_does_not_override_source_policy(self):
        artifact = self.approved()
        with patch.object(trusted_config, "SOURCE_POLICIES", ()):
            with self.assertRaises(PermissionError): artifact.check(DIST)

    def test_metadata_replacement_invalidates_content_attestation(self):
        artifact = self.approved()
        data = a.parse(a.sidecar(artifact.path).read_text())
        data["lineage"]["root"]["dataset_id"] = "replaced"
        a.sidecar(artifact.path).write_text(a.canonical(data))
        with self.assertRaises(PermissionError): a.inspect(artifact.path).check(DIST)

    def test_duplicate_json_keys_denied(self):
        with self.assertRaises(PermissionError): a.parse('{"source":1,"source":2}')

    def test_mixed_one_duka_parent_denies_distribution(self):
        lineage = a.derive([self.approved().lineage, self.artifact().lineage])
        self.assertFalse(lineage.root.redistributable)
        self.assertEqual(lineage.root.license_class, LicenseClass.PRIVATE_REFERENCE)
        self.assertTrue(lineage.check(LOCAL).allowed)
        with self.assertRaises(PermissionError): lineage.check(DIST)

    def test_missing_ancestor_rejected(self):
        lineage = a.derive([self.artifact().lineage])
        with self.assertRaises(PermissionError): a.Lineage(lineage.root).check(LOCAL)

    def test_plain_sql_cannot_reach_mt_writer(self):
        with fake_engines():
            mt = importlib.import_module("fxtick.mt_export")
            con = FakeCon()
            with self.assertRaises(PermissionError): mt.export_mt5_ticks(con, "SELECT 1", self.path())
            self.assertEqual(con.sql, [])

    def frame(self):
        class Frame:
            empty = False
            def __init__(self):
                self.text = "timestamp,bidPrice,askPrice,bidVolume,askVolume\n2026-09-01T00:00:00Z,1,2,1,1\n"
                self.attrs = {"fxtick_lineage": a.Lineage(a.new_dukascopy()),
                              "fxtick_sha256": hashlib.sha256(self.text.encode()).hexdigest()}
            def to_csv(self, dest=None, *, header=True):
                if dest is None: return self.text
                dest.write(self.text if header else self.text.split("\n", 1)[1])
            def __len__(self): return 1
        return Frame()

    def test_acquisition_csv_chunks_preserve_both_parents(self):
        with fake_engines():
            fetcher = importlib.import_module("fxtick.fetcher")
            first, second, target = self.frame(), self.frame(), self.path()
            fetcher.append_csv(first, target)
            initial = a.inspect(target)
            fetcher.append_csv(second, target)
            artifact = a.inspect(target)
            self.assertTrue(artifact.check(LOCAL).allowed)
            self.assertEqual(set(artifact.lineage.root.derived_from),
                {initial.lineage.root.dataset_id, second.attrs["fxtick_lineage"].root.dataset_id})
            with self.assertRaises(PermissionError): artifact.check(DIST)

    def test_acquisition_unmarked_or_mutated_frame_does_not_create_csv(self):
        with fake_engines():
            fetcher = importlib.import_module("fxtick.fetcher")
            for missing in (True, False):
                frame, target = self.frame(), self.path()
                if missing: frame.attrs.clear()
                else: frame.text += "changed"
                with self.assertRaises(PermissionError): fetcher.append_csv(frame, target)
                self.assertFalse(target.exists())

    def test_acquisition_never_appends_to_unregistered_history(self):
        with fake_engines():
            fetcher = importlib.import_module("fxtick.fetcher")
            target = self.path(); target.write_bytes(b"existing history")
            with self.assertRaises(PermissionError): fetcher.append_csv(self.frame(), target)
            self.assertEqual(target.read_bytes(), b"existing history")

    def test_acquisition_range_rejects_existing_path_before_fetch(self):
        with fake_engines():
            fetcher = importlib.import_module("fxtick.fetcher")
            target = self.path(); target.write_bytes(b"existing history")
            now = datetime.now(timezone.utc)
            with patch.object(fetcher, "fetch_ticks") as fetch:
                with self.assertRaises(FileExistsError): fetcher.download_range_to_csv("TEST", now, now, target)
                fetch.assert_not_called()
            self.assertEqual(target.read_bytes(), b"existing history")

    def test_mt4_mt5_local_conversion_and_inherited_distribution_denial(self):
        with fake_engines():
            duck = importlib.import_module("fxtick.duck")
            mt = importlib.import_module("fxtick.mt_export")
            artifact = self.legacy()
            query = duck.normalized_select(duck.source_sql(artifact.path, ledger=artifact.ledger))
            for writer in (mt.export_mt4_ticks, mt.export_mt5_ticks):
                output = self.path()
                writer(FakeCon(), query, output, "utc", 5, purpose=LOCAL)
                converted = a.inspect(output)
                self.assertTrue(converted.check(LOCAL).allowed)
                self.assertIn(artifact.lineage.root.dataset_id, converted.lineage.root.derived_from)
                with self.assertRaises(PermissionError): converted.check(DIST)
                forbidden = self.path()
                con = FakeCon()
                with self.assertRaises(PermissionError): writer(con, query, forbidden, "utc", 5, purpose=DIST)
                self.assertFalse(forbidden.exists())
                self.assertEqual(con.sql, [])

    def test_hst_distribution_rejected_before_formatter(self):
        with fake_engines():
            mt = importlib.import_module("fxtick.mt_export")
            output = self.path(".hst")
            with self.assertRaises(PermissionError):
                mt.export_hst(FakeCon(), Query("SELECT 1", (self.artifact(),)), output, "TEST", 1, 5, purpose=DIST)
            self.assertFalse(output.exists())

    def test_mt_writer_rechecks_after_conversion(self):
        artifact = self.artifact()
        @guarded_conversion
        def changing_writer(con, query, out):
            out.write_text("output")
            artifact.path.write_text("changed during conversion")
        target = self.path()
        with self.assertRaises(PermissionError): changing_writer(None, Query("SELECT 1", (artifact,)), target)
        self.assertFalse(target.exists())

    def test_mt_output_never_overwrites_history(self):
        @guarded_conversion
        def writer(*args): raise AssertionError("must not run")
        artifact = self.artifact()
        with self.assertRaises(FileExistsError): writer(None, Query("SELECT 1", (artifact,)), artifact.path)
        artifact.check(LOCAL)

    def test_parquet_fake_propagation_and_distribution_denial(self):
        with fake_engines():
            duck = importlib.import_module("fxtick.duck")
            artifact = self.artifact()
            parquet = self.path(".parquet")
            self.assertEqual(duck.csv_to_parquet(FakeCon(), artifact.path, parquet), 2)
            result = a.inspect(parquet)
            self.assertTrue(result.check(LOCAL).allowed)
            self.assertFalse(result.lineage.root.redistributable)
            with self.assertRaises(PermissionError): result.check(DIST)

    def test_parquet_sidecar_disagreement_rejected(self):
        with fake_engines():
            duck = importlib.import_module("fxtick.duck")
            path = self.path(".parquet")
            duck.csv_to_parquet(FakeCon(), self.artifact().path, path)
            data = a.parse(a.sidecar(path).read_text())
            data["lineage"]["root"]["dataset_id"] = "fake"
            a.sidecar(path).write_text(a.canonical(data))
            with self.assertRaises(PermissionError): a.inspect(path)

    def test_parquet_missing_embedded_metadata_rejected(self):
        with fake_engines():
            path = self.path(".parquet"); path.write_text('{"metadata":{}}')
            with self.assertRaises(PermissionError): a.seal(path, self.artifact().lineage)

    def test_month_merge_retains_all_inputs(self):
        with fake_engines():
            duck = importlib.import_module("fxtick.duck")
            left, right = self.approved(), self.artifact()
            out = self.path(".parquet")
            duck.merge_month(FakeCon(), left.path, 2026, 9, [right.path], out)
            lineage = a.inspect(out).lineage
            self.assertEqual(set(lineage.root.derived_from), {left.lineage.root.dataset_id, right.lineage.root.dataset_id})
            with self.assertRaises(PermissionError): lineage.check(DIST)

    def test_union_missing_input_never_silently_drops_it(self):
        with fake_engines():
            duck = importlib.import_module("fxtick.duck")
            with self.assertRaises(FileNotFoundError): duck.union_sources([self.artifact().path, self.path()])

    def test_zip_duka_rejected_before_file_creation(self):
        artifact, target = self.artifact(), self.path(".zip")
        with self.assertRaises(PermissionError): build_zip([(artifact.path, "tick.csv")], target)
        self.assertFalse(target.exists())

    def test_local_zip_preserves_private_policy(self):
        artifact = self.artifact()
        archive = build_zip([(artifact.path, "tick.csv")], self.path(".zip"), purpose=LOCAL)
        self.assertTrue(archive.check(LOCAL).allowed)
        with self.assertRaises(PermissionError): archive.check(DIST)

    def test_zip_rechecks_member_at_write_boundary(self):
        artifact = self.approved()
        target = self.path(".zip")
        import zipfile
        original = zipfile.ZipFile
        def changing_archive(*args, **kwargs):
            artifact.path.write_text("changed immediately before ZIP write")
            return original(*args, **kwargs)
        with patch("fxtick.export_service.zipfile.ZipFile", side_effect=changing_archive):
            with self.assertRaises(PermissionError): build_zip([(artifact.path, "tick.csv")], target)
        self.assertFalse(target.exists())
        self.assertFalse(a.sidecar(target).exists())

    def test_trusted_content_registry_cannot_upgrade_duka_or_unknown(self):
        for artifact in (self.artifact(), self.artifact(Provenance("unknown"))):
            trusted_config.DISTRIBUTION_ATTESTATIONS[artifact.sha256] = hashlib.sha256(
                a.canonical(artifact.lineage.payload()).encode()).hexdigest()
            with self.assertRaises(PermissionError): artifact.check(DIST)

    def test_unregistered_legacy_denied_for_local_and_distribution(self):
        registered = self.legacy()
        other = self.path()
        other.write_bytes(b"not owner-approved")
        for purpose in (LOCAL, DIST):
            with self.assertRaises(PermissionError): a.inspect(other, ledger=registered.ledger).check(purpose)

    def test_approved_zip_and_streamlit_delivery(self):
        artifact = self.approved()
        archive = build_zip([(artifact.path, "tick.csv")], self.path(".zip"))
        st = Mock()
        streamlit_download(st, archive, label="download")
        self.assertEqual(st.download_button.call_args.kwargs["data"], archive.path.read_bytes())

    def test_streamlit_duka_download_denied(self):
        st = Mock()
        with self.assertRaises(PermissionError): streamlit_download(st, self.artifact(), label="download")
        st.download_button.assert_not_called()

    def test_streamlit_rechecks_current_content_and_policy(self):
        artifact = self.approved()
        st = Mock()
        with patch.object(trusted_config, "SOURCE_POLICIES", ()):
            with self.assertRaises(PermissionError): streamlit_download(st, artifact)
        artifact.path.write_text("changed")
        with self.assertRaises(PermissionError): streamlit_download(st, artifact)
        st.download_button.assert_not_called()

    def test_drive_upload_duka_and_unknown_rejected_before_api(self):
        for artifact in (self.artifact(), self.artifact(Provenance("unknown"))):
            service = Mock()
            with self.assertRaises(PermissionError): gdrive.upload_file(service, "distribution", "data.csv", artifact.path, roots=self.roots)
            self.assertEqual(service.mock_calls, [])

    def test_drive_upload_content_rechecked(self):
        artifact = self.approved()
        artifact.path.write_bytes(b"changed")
        service = Mock()
        with self.assertRaises(PermissionError): gdrive.upload_file(service, "distribution", "data.csv", artifact.path, roots=self.roots)
        self.assertEqual(service.mock_calls, [])

    def test_drive_roots_cannot_equal_or_be_missing(self):
        for values in (("a", "a"), ("", "b")):
            with self.assertRaises(PermissionError): gdrive.DriveRoots(*values)

    def test_drive_private_cannot_upload_to_distribution_folder(self):
        service = FakeDrive()
        with self.assertRaises(PermissionError):
            gdrive.upload_file(service, "distribution", "data.csv", self.artifact().path,
                zone=gdrive.StorageZone.PRIVATE_REFERENCE, roots=self.roots)
        self.assertEqual(service.writes, [])

    def test_drive_nested_roots_rejected(self):
        service = FakeDrive()
        service.nodes["distribution"]["parents"] = ["private"]
        with self.assertRaises(PermissionError): gdrive.assert_zone(service, "distribution", gdrive.StorageZone.DISTRIBUTION, self.roots)

    def test_private_folder_inherited_public_permission_rejected(self):
        service = FakeDrive()
        service.acls["private"] = [{"type": "anyone", "role": "reader"}]
        with self.assertRaises(PermissionError): gdrive.assert_zone(service, "private", gdrive.StorageZone.PRIVATE_REFERENCE, self.roots)

    def test_private_upload_fake_api_allows_owner_only_new_artifact(self):
        service = FakeDrive()
        module = ModuleType("googleapiclient.http")
        module.MediaFileUpload = lambda *args, **kwargs: object()
        with patch.dict("sys.modules", {"googleapiclient": ModuleType("googleapiclient"), "googleapiclient.http": module}):
            gdrive.upload_file(service, "private", "data.csv", self.artifact().path,
                zone=gdrive.StorageZone.PRIVATE_REFERENCE, roots=self.roots)
        self.assertEqual(len(service.writes), 2)
        props = service.writes[-1][1]["body"]["appProperties"]
        self.assertEqual(props["fxtick_zone"], "PRIVATE_REFERENCE")

    def test_public_share_private_remote_denied(self):
        service = FakeDrive()
        service.nodes["file"] = {"id": "file", "parents": ["private"], "version": "1",
                                 "appProperties": {"fxtick_zone": "PRIVATE_REFERENCE", "fxtick_manifest": "meta"}}
        with self.assertRaises(PermissionError): gdrive.share_anyone_reader(service, "file", roots=self.roots)
        self.assertEqual(service.writes, [])

    def remote(self, artifact):
        service = FakeDrive()
        service.nodes["file"] = {"id": "file", "name": "data.csv", "parents": ["distribution"], "version": "1",
            "appProperties": {"fxtick_zone": "DISTRIBUTION", "fxtick_manifest": "meta", "fxtick_sha256": artifact.sha256}}
        def download(service_arg, file_id, dest):
            shutil.copyfile(artifact.path if file_id == "file" else a.sidecar(artifact.path), dest)
        return service, download

    def test_public_share_rechecks_actual_remote_duka_despite_zone_claim(self):
        service, download = self.remote(self.artifact())
        with patch.object(gdrive, "_download_raw", side_effect=download):
            with self.assertRaises(PermissionError): gdrive.share_anyone_reader(service, "file", roots=self.roots)
        self.assertEqual(service.writes, [])

    def test_rejected_remote_download_leaves_no_destination(self):
        service, download = self.remote(self.artifact())
        destination = self.path()
        with patch.object(gdrive, "_download_raw", side_effect=download):
            with self.assertRaises(PermissionError):
                gdrive.download_file(service, "file", destination, zone=gdrive.StorageZone.DISTRIBUTION, roots=self.roots)
        self.assertFalse(destination.exists())
        self.assertFalse(a.sidecar(destination).exists())
        self.assertEqual(service.writes, [])

    def test_public_share_rechecks_changed_remote_version(self):
        artifact = self.approved()
        service, download = self.remote(artifact)
        def changing(*args):
            download(*args)
            service.nodes["file"]["version"] = str(int(service.nodes["file"]["version"]) + 1)
        with patch.object(gdrive, "_download_raw", side_effect=changing):
            with self.assertRaises(PermissionError): gdrive.share_anyone_reader(service, "file", roots=self.roots)
        self.assertEqual(service.writes, [])

    def test_public_share_allows_only_verified_approved_remote(self):
        service, download = self.remote(self.approved())
        service.nodes["file"]["webContentLink"] = "https://example.invalid/synthetic-download"
        with patch.object(gdrive, "_download_raw", side_effect=download):
            self.assertEqual(gdrive.share_anyone_reader(service, "file", roots=self.roots),
                             "https://example.invalid/synthetic-download")
        self.assertEqual(len(service.writes), 1)
        self.assertEqual(service.writes[0][0], "permissions")

    def test_listing_filters_blocked_content(self):
        service, download = self.remote(self.artifact())
        with patch.object(gdrive, "_download_raw", side_effect=download):
            self.assertEqual(gdrive.eligible_distribution_files(service, [{"id": "file", "name": "EURUSD_2026_ticks.parquet"}], roots=self.roots), {})

    def test_mail_url_rechecks_remote_before_smtp(self):
        with fake_engines():
            sync = importlib.import_module("sync_and_upload")
            service, download = self.remote(self.artifact())
            with patch.object(gdrive, "_download_raw", side_effect=download), patch.object(sync.smtplib, "SMTP_SSL") as smtp:
                with self.assertRaises(PermissionError): sync.send_email("test@example.invalid", service, "file", "pwd", "date", roots=self.roots)
                smtp.assert_not_called()

    def test_build_distribution_blocks_duka_before_conversion(self):
        with fake_engines():
            sync = importlib.import_module("sync_and_upload")
            con, service = FakeCon(), Mock()
            with self.assertRaises(PermissionError): sync.build_distribution(service, con, "distribution", {"TEST": self.artifact().path}, self.root, datetime.now(timezone.utc))
            self.assertEqual(con.sql, [])
            self.assertEqual(service.mock_calls, [])

    def test_sync_distribution_request_rejected_before_service(self):
        with fake_engines():
            sync = importlib.import_module("sync_and_upload")
            with patch.multiple(sync, ROOT_FOLDER_ID="private", DISTRIBUTION_FOLDER_ID="distribution", TOKEN_JSON="fake", TARGET_FORMAT="MT5"), patch.object(gdrive, "service_from_token_json") as service:
                self.assertEqual(sync.main(), 2)
                service.assert_not_called()

    def test_static_entrypoints_have_no_unchecked_zip_or_download(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("app_cloud.py", "sync_and_upload.py"):
            tree = ast.parse((root / name).read_text(encoding="utf-8"))
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
            self.assertFalse(any(isinstance(n.func, ast.Attribute) and n.func.attr in {"ZipFile", "AESZipFile", "download_button"} for n in calls))
            for call in calls:
                if isinstance(call.func, ast.Attribute) and call.func.attr in {"export_mt4_ticks", "export_mt5_ticks"}:
                    self.assertTrue(any(k.arg == "purpose" and isinstance(k.value, ast.Attribute) and k.value.attr == "DISTRIBUTION" for k in call.keywords))

    def test_workflow_private_only_and_no_old_root_fallback(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/tick_sync.yml").read_text(encoding="utf-8")
        self.assertIn("TARGET_FORMAT: 'NONE'", workflow)
        self.assertNotIn("secrets.GDRIVE_FOLDER_ID", workflow)
        self.assertNotIn("MAIL_PASSWORD", workflow)
        self.assertNotIn("target_format:", workflow)


if __name__ == "__main__": unittest.main()
