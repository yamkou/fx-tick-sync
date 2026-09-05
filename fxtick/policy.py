"""Pure, fail-closed application policy; intentionally unused by live entry points.

SourcePolicy records are trusted project configuration, NEVER dataset metadata.
No real broker/feed is approved by default. LOCAL_TEST grants no cloud-storage,
sharing or legal permission. All selected parents must be evaluated together.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from .provenance import LicenseClass, Provenance, identity, is_dukascopy


class ExportPurpose(str, Enum):
    LOCAL_TEST = "LOCAL_TEST"
    DISTRIBUTION = "DISTRIBUTION"


@dataclass(frozen=True)
class SourcePolicy:
    source: str
    provider: str
    license_class: LicenseClass = LicenseClass.UNKNOWN
    redistributable: bool = False
    local_test_allowed: bool = False
    approval_reference: str | None = None
    account_type: str = "unspecified"
    acquisition_mechanism: str = "unspecified"

    def __post_init__(self) -> None:
        for field in ("source", "provider", "account_type", "acquisition_mechanism"):
            object.__setattr__(self, field, identity(getattr(self, field)))
        if not isinstance(self.license_class, LicenseClass):
            raise ValueError("license_class must be a LicenseClass")
        if type(self.redistributable) is not bool or type(self.local_test_allowed) is not bool:
            raise ValueError("policy permissions must be booleans")
        if self.approval_reference is not None and (
            not isinstance(self.approval_reference, str) or not self.approval_reference.strip()
        ):
            raise ValueError("approval_reference must be non-empty or None")
        if self.redistributable and (
            self.license_class != LicenseClass.DISTRIBUTABLE or not self.approval_reference
        ):
            raise ValueError("redistribution requires a DISTRIBUTABLE class and approval reference")
        if is_dukascopy(self.source, self.provider):
            raise ValueError("Dukascopy policy is built in and cannot be overridden")

    @property
    def policy_key(self) -> tuple[str, str, str, str]:
        return self.source, self.provider, self.account_type, self.acquisition_mechanism


@dataclass(frozen=True)
class PolicyIssue:
    code: str
    dataset_id: str


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    purpose: ExportPurpose
    effective_license_class: LicenseClass
    issues: tuple[PolicyIssue, ...]


class PolicyDeniedError(PermissionError):
    def __init__(self, decision: PolicyDecision):
        self.decision = decision
        super().__init__("; ".join(f"{i.dataset_id}: {i.code}" for i in decision.issues))


# UNKNOWN is also denied for LOCAL_TEST in this foundation. This does not alter
# legacy local exporters: they do not call this new policy layer in Phase 1.
_STRICTNESS = {
    LicenseClass.DISTRIBUTABLE: 0,
    LicenseClass.INTERNAL_ONLY: 1,
    LicenseClass.PRIVATE_REFERENCE: 2,
    LicenseClass.UNKNOWN: 3,
}
_DERIVED_SOURCES = {"derived", "mixed"}


def evaluate_policy(
    provenance: Provenance | None,
    purpose: ExportPurpose,
    *,
    parents: Mapping[str, Provenance] | None = None,
    source_policies: tuple[SourcePolicy, ...] = (),
) -> PolicyDecision:
    """Inspect root AND every reachable ancestor, without I/O or mutations.

Missing/conflicting parents and cycles block both purposes. Derived/mixed source
IDs denote transformations, require parents, and cannot create fresh rights.
Only a trusted SourcePolicy for an exact source/provider/account/mechanism scope
can approve a non-Dukascopy source. A metadata redistributable flag is insufficient.
"""
    if not isinstance(purpose, ExportPurpose):
        raise ValueError("purpose must be an ExportPurpose")
    issues: list[PolicyIssue] = []
    effective = LicenseClass.DISTRIBUTABLE

    def restrict(license_class: LicenseClass) -> None:
        nonlocal effective
        if _STRICTNESS[license_class] > _STRICTNESS[effective]:
            effective = license_class

    def block(code: str, dataset_id: str, *, unknown: bool = False) -> None:
        issues.append(PolicyIssue(code, dataset_id))
        if unknown:
            restrict(LicenseClass.UNKNOWN)

    registry: dict[tuple[str, str, str, str], SourcePolicy] = {}
    for policy in source_policies:
        if not isinstance(policy, SourcePolicy):
            block("INVALID_SOURCE_POLICY", "<configuration>", unknown=True)
            continue
        if policy.policy_key in registry:
            block("DUPLICATE_SOURCE_POLICY", "<configuration>", unknown=True)
        registry[policy.policy_key] = policy

    if not isinstance(provenance, Provenance):
        block("MISSING_OR_INVALID_PROVENANCE", "<root>", unknown=True)
        return PolicyDecision(False, purpose, effective, tuple(issues))

    records = dict(parents) if parents is not None else {}
    if provenance.dataset_id in records and records[provenance.dataset_id] != provenance:
        block("CONFLICTING_ROOT", provenance.dataset_id, unknown=True)
    records[provenance.dataset_id] = provenance
    active: set[str] = set()
    done: set[str] = set()
    # Iterative DFS avoids Python recursion limits for long derived chains.
    stack = [(provenance.dataset_id, False)]
    while stack:
        dataset_id, leaving = stack.pop()
        if leaving:
            active.remove(dataset_id)
            done.add(dataset_id)
            continue
        if dataset_id in active:
            block("CYCLIC_LINEAGE", dataset_id, unknown=True)
            continue
        if dataset_id in done:
            continue
        node = records.get(dataset_id)
        if not isinstance(node, Provenance) or node.dataset_id != dataset_id:
            block("MISSING_OR_INVALID_PARENT", dataset_id, unknown=True)
            done.add(dataset_id)
            continue
        active.add(dataset_id)
        stack.append((dataset_id, True))
        stack.extend((parent_id, False) for parent_id in reversed(node.derived_from))
        restrict(node.license_class)
        if node.acquired_at is None:
            block("MISSING_ACQUISITION_TIME", dataset_id, unknown=True)
        if node.source == "unknown" or node.provider == "unknown":
            block("UNKNOWN_IDENTITY", dataset_id, unknown=True)
        if node.license_class == LicenseClass.UNKNOWN:
            block("UNKNOWN_LICENSE", dataset_id, unknown=True)

        if is_dukascopy(node.source, node.provider):
            restrict(LicenseClass.PRIVATE_REFERENCE)
            if purpose == ExportPurpose.DISTRIBUTION:
                block("DUKASCOPY_DISTRIBUTION_FORBIDDEN", dataset_id)
            continue

        if node.source in _DERIVED_SOURCES:
            if not node.derived_from:
                block("DERIVED_DATASET_WITHOUT_PARENTS", dataset_id, unknown=True)
            # Transformations impose their own restrictions; parent evaluation
            # below supplies rights. No independent transformation approval.
            if purpose == ExportPurpose.DISTRIBUTION and (
                node.license_class != LicenseClass.DISTRIBUTABLE or not node.redistributable
            ):
                block("DATASET_NOT_REDISTRIBUTABLE", dataset_id)
            continue

        policy = registry.get(node.policy_key)
        if policy is None:
            block("SOURCE_NOT_EXPLICITLY_APPROVED", dataset_id, unknown=True)
            continue
        restrict(policy.license_class)
        if policy.license_class == LicenseClass.UNKNOWN:
            block("UNKNOWN_SOURCE_POLICY", dataset_id, unknown=True)
        if purpose == ExportPurpose.LOCAL_TEST:
            if not policy.local_test_allowed:
                block("LOCAL_TEST_NOT_APPROVED", dataset_id)
        elif (
            node.license_class != LicenseClass.DISTRIBUTABLE
            or not node.redistributable
            or policy.license_class != LicenseClass.DISTRIBUTABLE
            or not policy.redistributable
            or not policy.approval_reference
        ):
            block("DATASET_NOT_REDISTRIBUTABLE", dataset_id)

    return PolicyDecision(not issues, purpose, effective, tuple(issues))


def assert_distribution_allowed(
    provenance: Provenance | None,
    *,
    parents: Mapping[str, Provenance] | None = None,
    source_policies: tuple[SourcePolicy, ...] = (),
) -> PolicyDecision:
    decision = evaluate_policy(
        provenance, ExportPurpose.DISTRIBUTION, parents=parents, source_policies=source_policies
    )
    if not decision.allowed:
        raise PolicyDeniedError(decision)
    return decision
