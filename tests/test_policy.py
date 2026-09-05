"""Synthetic data only. No feeds, files, Drive, exporters or credentials."""
from dataclasses import replace
from datetime import datetime, timezone
import unittest

from fxtick.policy import (
    ExportPurpose, PolicyDeniedError, SourcePolicy,
    assert_distribution_allowed, evaluate_policy,
)
from fxtick.provenance import LicenseClass, Provenance


UTC_TIME = datetime(2026, 9, 5, tzinfo=timezone.utc)
LOCAL = ExportPurpose.LOCAL_TEST
DIST = ExportPurpose.DISTRIBUTION
APPROVED = SourcePolicy(
    source="synthetic_feed", provider="test_provider",
    license_class=LicenseClass.DISTRIBUTABLE, redistributable=True,
    local_test_allowed=True, approval_reference="test-only-approval-001",
)


def approved_data(dataset_id="approved", **changes):
    return replace(Provenance(
        dataset_id=dataset_id, source="synthetic_feed", provider="test_provider",
        license_class=LicenseClass.DISTRIBUTABLE, redistributable=True,
        acquired_at=UTC_TIME,
    ), **changes)


def dukascopy(dataset_id="duka", **changes):
    return replace(Provenance(
        dataset_id=dataset_id, source="dukascopy", provider="dukascopy",
        acquired_at=UTC_TIME,
    ), **changes)


def derived(dataset_id="mixed", parent_ids=("approved", "duka"), **changes):
    return replace(Provenance(
        dataset_id=dataset_id, source="derived", provider="internal",
        license_class=LicenseClass.DISTRIBUTABLE, redistributable=True,
        acquired_at=UTC_TIME, derived_from=parent_ids,
    ), **changes)


class PolicyTests(unittest.TestCase):
    def decide(self, node, purpose=DIST, parents=None, policies=(APPROVED,)):
        return evaluate_policy(node, purpose, parents=parents, source_policies=policies)

    def assertBlocked(self, decision, reason=None):
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.issues)
        if reason:
            self.assertIn(reason, {issue.code for issue in decision.issues})

    def test_dukascopy_local_allowed(self):
        decision = self.decide(dukascopy(), LOCAL)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.effective_license_class, LicenseClass.PRIVATE_REFERENCE)
        self.assertEqual(decision.issues, ())

    def test_dukascopy_distribution_blocked(self):
        self.assertBlocked(self.decide(dukascopy()), "DUKASCOPY_DISTRIBUTION_FORBIDDEN")

    def test_unknown_distribution_blocked(self):
        self.assertBlocked(self.decide(Provenance("unknown", acquired_at=UTC_TIME)), "UNKNOWN_LICENSE")

    def test_unknown_local_test_is_also_blocked(self):
        self.assertBlocked(self.decide(Provenance("unknown", acquired_at=UTC_TIME), LOCAL))

    def test_explicitly_approved_source_distribution_allowed(self):
        decision = self.decide(approved_data())
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.effective_license_class, LicenseClass.DISTRIBUTABLE)

    def test_metadata_true_is_not_project_approval(self):
        self.assertBlocked(self.decide(approved_data(), policies=()), "SOURCE_NOT_EXPLICITLY_APPROVED")

    def test_mixed_dukascopy_blocked(self):
        parents = {"approved": approved_data(), "duka": dukascopy()}
        decision = self.decide(derived(), parents=parents)
        self.assertBlocked(decision, "DUKASCOPY_DISTRIBUTION_FORBIDDEN")
        self.assertEqual(decision.effective_license_class, LicenseClass.PRIVATE_REFERENCE)
        self.assertEqual(parents["approved"].license_class, LicenseClass.DISTRIBUTABLE)

    def test_mixed_unknown_blocked(self):
        parents = {"approved": approved_data(), "unknown": Provenance("unknown", acquired_at=UTC_TIME)}
        decision = self.decide(derived(parent_ids=("approved", "unknown")), parents=parents)
        self.assertBlocked(decision)
        self.assertEqual(decision.effective_license_class, LicenseClass.UNKNOWN)

    def test_missing_provenance_blocked(self):
        self.assertBlocked(self.decide(None), "MISSING_OR_INVALID_PROVENANCE")

    def test_missing_provenance_local_blocked(self):
        self.assertBlocked(self.decide(None, LOCAL))

    def test_invalid_provenance_not_accepted_as_an_object(self):
        self.assertBlocked(self.decide({"redistributable": True}), "MISSING_OR_INVALID_PROVENANCE")

    def test_derived_from_dukascopy_blocks_even_approved_root(self):
        root = approved_data(derived_from=("duka",))
        self.assertBlocked(self.decide(root, parents={"duka": dukascopy()}))

    def test_transitive_dukascopy_cannot_be_laundered(self):
        parents = {"m1": derived("m1", ("duka",)), "duka": dukascopy()}
        self.assertBlocked(self.decide(derived("mt5", ("m1",)), parents=parents))

    def test_all_approved_mixed_allowed(self):
        parents = {"approved": approved_data(), "second": approved_data("second")}
        self.assertTrue(self.decide(derived(parent_ids=tuple(parents)), parents=parents).allowed)

    def test_mixed_private_reference_local_capability_preserved(self):
        parents = {"approved": approved_data(), "duka": dukascopy()}
        self.assertTrue(self.decide(derived(), LOCAL, parents=parents).allowed)

    def test_mixed_unknown_local_is_blocked(self):
        node = derived(parent_ids=("unknown",))
        self.assertBlocked(self.decide(node, LOCAL, parents={"unknown": Provenance("unknown")}))

    def test_missing_parent_cannot_be_ignored(self):
        for purpose in (LOCAL, DIST):
            with self.subTest(purpose=purpose):
                self.assertBlocked(self.decide(derived(), purpose), "MISSING_OR_INVALID_PARENT")

    def test_parent_id_mismatch_blocked(self):
        node = derived(parent_ids=("approved",))
        self.assertBlocked(self.decide(node, parents={"approved": approved_data("other")}))

    def test_conflicting_root_blocked(self):
        self.assertBlocked(self.decide(approved_data(), parents={"approved": dukascopy("approved")}), "CONFLICTING_ROOT")

    def test_self_cycle_blocked(self):
        self.assertBlocked(self.decide(derived("a", ("a",))), "CYCLIC_LINEAGE")

    def test_indirect_cycle_blocked(self):
        self.assertBlocked(self.decide(derived("a", ("b",)), parents={"b": derived("b", ("a",))}), "CYCLIC_LINEAGE")

    def test_shared_parent_dag_is_not_a_cycle(self):
        parents = {"left": derived("left", ("approved",)), "right": derived("right", ("approved",)), "approved": approved_data()}
        self.assertTrue(self.decide(derived("root", ("left", "right")), parents=parents).allowed)

    def test_long_lineage_is_checked_without_recursion(self):
        parents = {"duka": dukascopy()}
        last = "duka"
        for n in range(1500):
            node = derived(f"node-{n}", (last,))
            parents[node.dataset_id] = node
            last = node.dataset_id
        self.assertBlocked(self.decide(derived("root", (last,)), parents=parents), "DUKASCOPY_DISTRIBUTION_FORBIDDEN")

    def test_derived_without_parents_is_blocked(self):
        for source in ("derived", "mixed"):
            with self.subTest(source=source):
                self.assertBlocked(self.decide(derived(parent_ids=(), source=source)), "DERIVED_DATASET_WITHOUT_PARENTS")

    def test_root_can_further_restrict_approved_parents(self):
        root = derived(parent_ids=("approved",), license_class=LicenseClass.PRIVATE_REFERENCE, redistributable=False)
        decision = self.decide(root, parents={"approved": approved_data()})
        self.assertBlocked(decision)
        self.assertEqual(decision.effective_license_class, LicenseClass.PRIVATE_REFERENCE)

    def test_parent_false_flag_is_not_overridden_by_child(self):
        parent = approved_data(redistributable=False)
        self.assertBlocked(self.decide(derived(parent_ids=("approved",)), parents={"approved": parent}))

    def test_restrictive_source_configuration_is_inherited(self):
        policy = replace(APPROVED, license_class=LicenseClass.INTERNAL_ONLY, redistributable=False)
        decision = self.decide(derived(parent_ids=("approved",)), parents={"approved": approved_data()}, policies=(policy,))
        self.assertBlocked(decision)
        self.assertEqual(decision.effective_license_class, LicenseClass.INTERNAL_ONLY)

    def test_approval_never_upgrades_unknown_metadata(self):
        self.assertBlocked(self.decide(approved_data(license_class=LicenseClass.UNKNOWN)))

    def test_known_candidate_names_have_no_default_approval(self):
        for source in ("IC Markets", "IC_MARKETS", "AXIORY", "FxPro", "cTrader", "Binance"):
            with self.subTest(source=source):
                node = approved_data(source=source, provider=source)
                for purpose in (LOCAL, DIST):
                    self.assertBlocked(self.decide(node, purpose), "SOURCE_NOT_EXPLICITLY_APPROVED")

    def test_approval_scope_must_match_provider_account_and_mechanism(self):
        for changes in ({"provider": "other"}, {"account_type": "live"}, {"acquisition_mechanism": "other_api"}):
            with self.subTest(changes=changes):
                self.assertBlocked(self.decide(approved_data(**changes)), "SOURCE_NOT_EXPLICITLY_APPROVED")

    def test_local_and_distribution_permissions_are_independent(self):
        policy = replace(APPROVED, local_test_allowed=False)
        self.assertTrue(self.decide(approved_data(), policies=(policy,)).allowed)
        self.assertBlocked(self.decide(approved_data(), LOCAL, policies=(policy,)), "LOCAL_TEST_NOT_APPROVED")

    def test_missing_acquisition_time_is_unknown(self):
        self.assertBlocked(self.decide(approved_data(acquired_at=None)), "MISSING_ACQUISITION_TIME")

    def test_duplicate_source_config_fails_closed(self):
        self.assertBlocked(self.decide(approved_data(), policies=(APPROVED, APPROVED)), "DUPLICATE_SOURCE_POLICY")

    def test_invalid_source_config_fails_closed(self):
        self.assertBlocked(self.decide(approved_data(), policies=({"redistributable": True},)), "INVALID_SOURCE_POLICY")

    def test_approval_requires_evidence_reference_and_class(self):
        for changes in ({"approval_reference": None}, {"approval_reference": " "}, {"license_class": LicenseClass.UNKNOWN}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(APPROVED, **changes)

    def test_dukascopy_source_policy_cannot_be_overridden(self):
        for changes in ({"source": "dukascopy"}, {"provider": "Dukascopy"}, {"source": "DUKASCOPY-PYTHON"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(APPROVED, **changes)

    def test_forged_dukascopy_flags_do_not_grant_distribution(self):
        node = dukascopy(license_class=LicenseClass.DISTRIBUTABLE, redistributable=True)
        self.assertBlocked(self.decide(node), "DUKASCOPY_DISTRIBUTION_FORBIDDEN")

    def test_dataset_name_never_grants_rights(self):
        node = dukascopy("COMMERCIAL/approved-binance.parquet")
        self.assertBlocked(self.decide(node))
        self.assertTrue(self.decide(approved_data("PRIVATE_REFERENCE/dukascopy.csv")).allowed)

    def test_unrelated_records_do_not_taint_selected_lineage(self):
        self.assertTrue(self.decide(approved_data(), parents={"duka": dukascopy()}).allowed)

    def test_denial_exception_preserves_reasons(self):
        with self.assertRaises(PolicyDeniedError) as raised:
            assert_distribution_allowed(dukascopy())
        self.assertFalse(raised.exception.decision.allowed)
        self.assertIn("DUKASCOPY_DISTRIBUTION_FORBIDDEN", str(raised.exception))

    def test_assertion_returns_positive_decision(self):
        self.assertTrue(assert_distribution_allowed(approved_data(), source_policies=(APPROVED,)).allowed)

    def test_invalid_purpose_never_defaults_to_local(self):
        with self.assertRaises(ValueError):
            self.decide(dukascopy(), "distribution_typo")

    def test_decisions_are_repeatable_and_inputs_unchanged(self):
        root = derived()
        parents = {"approved": approved_data(), "duka": dukascopy()}
        before = {key: value.to_json() for key, value in parents.items()}
        self.assertEqual(self.decide(root, parents=parents), self.decide(root, parents=parents))
        self.assertEqual(before, {key: value.to_json() for key, value in parents.items()})


if __name__ == "__main__":
    unittest.main()
