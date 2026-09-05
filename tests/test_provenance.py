from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import json
import unittest

from fxtick.provenance import LicenseClass, Provenance


class ProvenanceTests(unittest.TestCase):
    def sample(self):
        return Provenance(
            dataset_id="ticks-001", source="dukascopy", provider="dukascopy",
            acquired_at=datetime(2026, 9, 5, 18, tzinfo=timezone(timedelta(hours=9))),
            derived_from=("parent-001",), account_type="reference",
            acquisition_mechanism="dukascopy_python",
        )

    def test_required_fields_roundtrip(self):
        node = self.sample()
        self.assertEqual(node, Provenance.from_dict(node.to_dict()))
        self.assertEqual(node, Provenance.from_json(node.to_json()))
        self.assertTrue({"source", "provider", "license_class", "redistributable", "acquired_at", "derived_from"} <= node.to_dict().keys())

    def test_utc_normalization(self):
        self.assertEqual(self.sample().acquired_at, datetime(2026, 9, 5, 9, tzinfo=timezone.utc))
        self.assertIs(self.sample().acquired_at.tzinfo, timezone.utc)

    def test_unknown_defaults(self):
        node = Provenance("unclassified")
        self.assertEqual(node.license_class, LicenseClass.UNKNOWN)
        self.assertFalse(node.redistributable)
        self.assertIsNone(node.acquired_at)

    def test_dukascopy_classification_is_not_overridable(self):
        for source, provider in ((" Dukascopy ", "dukascopy"), ("dukascopy-python", "reference"), ("custom", "DUKASCOPY")):
            with self.subTest(source=source, provider=provider):
                node = replace(self.sample(), source=source, provider=provider, license_class=LicenseClass.DISTRIBUTABLE, redistributable=True)
                self.assertEqual(node.license_class, LicenseClass.PRIVATE_REFERENCE)
                self.assertFalse(node.redistributable)

    def test_naive_time_rejected(self):
        with self.assertRaises(ValueError):
            replace(self.sample(), acquired_at=datetime(2026, 9, 5))

    def test_models_are_immutable(self):
        node = self.sample()
        with self.assertRaises(FrozenInstanceError):
            node.redistributable = True

    def test_input_mapping_and_output_do_not_mutate_record(self):
        payload = self.sample().to_dict()
        node = Provenance.from_dict(payload)
        payload["derived_from"].clear()
        output = node.to_dict()
        output["derived_from"].append("untrusted")
        self.assertEqual(node.derived_from, ("parent-001",))

    def test_missing_required_fields_rejected(self):
        for field in ("schema_version", "dataset_id", "source", "provider", "license_class", "redistributable", "acquired_at", "derived_from"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                payload = self.sample().to_dict()
                del payload[field]
                Provenance.from_dict(payload)

    def test_unrecognized_metadata_fields_are_rejected(self):
        payload = self.sample().to_dict()
        payload["approval_reference"] = "self-approved"
        with self.assertRaises(ValueError):
            Provenance.from_dict(payload)

    def test_strings_and_integers_are_not_booleans(self):
        for value in ("true", "false", 1, 0, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                payload = self.sample().to_dict()
                payload["redistributable"] = value
                Provenance.from_dict(payload)

    def test_invalid_or_future_schema_rejected(self):
        for value in (True, 2, "1", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(self.sample(), schema_version=value)

    def test_invalid_classes_rejected(self):
        for value in ("probably_allowed", "", None, {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                payload = self.sample().to_dict()
                payload["license_class"] = value
                Provenance.from_dict(payload)

    def test_invalid_parent_representations_rejected(self):
        for value in ("dukascopy", None, {}, [""], [7], ["a", "a"], [{"source": "dukascopy"}]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                payload = self.sample().to_dict()
                payload["derived_from"] = value
                Provenance.from_dict(payload)

    def test_duplicate_json_fields_rejected(self):
        text = self.sample().to_json()
        text = text[:-1] + ', "redistributable": true}'
        with self.assertRaises(ValueError):
            Provenance.from_json(text)

    def test_non_object_or_invalid_json_rejected(self):
        for value in ("null", "[]", '"filename.parquet"', "{broken", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Provenance.from_json(value)

    def test_invalid_timestamp_rejected(self):
        for value in ("yesterday", "2026-09-05T09:00:00", 123, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                payload = self.sample().to_dict()
                payload["acquired_at"] = value
                Provenance.from_dict(payload)

    def test_empty_identities_rejected(self):
        for field in ("dataset_id", "source", "provider", "account_type", "acquisition_mechanism"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                replace(self.sample(), **{field: " "})

    def test_json_input_does_not_add_approval_to_an_unknown_source(self):
        payload = self.sample().to_dict()
        payload.update(source="binance", provider="binance", license_class="UNKNOWN", redistributable=False)
        node = Provenance.from_json(json.dumps(payload))
        self.assertEqual(node.license_class, LicenseClass.UNKNOWN)
        self.assertFalse(node.redistributable)


if __name__ == "__main__":
    unittest.main()
