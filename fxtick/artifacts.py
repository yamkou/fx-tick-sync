"""Content-bound provenance. No licences or source approvals are inferred here.

Legacy registration is explicitly owner initiated and ONLY grants Dukascopy local
reference identity. Distribution additionally needs a trusted content attestation
outside the data/sidecar/legacy ledger and the existing Phase 1 source policy.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .policy import ExportPurpose, PolicyDeniedError, evaluate_policy
from .provenance import LicenseClass, Provenance

PARQUET_KEY = b"fxtick.lineage.v1"


class IntegrityError(PermissionError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def parse(text):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise IntegrityError("Duplicate metadata field")
            out[key] = value
        return out
    try:
        return json.loads(text, object_pairs_hook=unique)
    except (ValueError, TypeError) as exc:
        raise IntegrityError("Invalid metadata JSON") from exc


def fingerprint(path):
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as src:
        for block in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def sidecar(path):
    return Path(str(path) + ".provenance.json")


def new_dukascopy():
    return Provenance(str(uuid4()), source="dukascopy", provider="dukascopy",
                      acquired_at=datetime.now(timezone.utc), account_type="reference",
                      acquisition_mechanism="dukascopy_python")


@dataclass(frozen=True)
class Lineage:
    root: Provenance
    parents: tuple[Provenance, ...] = ()

    def payload(self):
        return {"root": self.root.to_dict(), "parents": [p.to_dict() for p in self.parents]}

    @classmethod
    def decode(cls, payload):
        try:
            if set(payload) != {"root", "parents"} or not isinstance(payload["parents"], list):
                raise ValueError("Invalid graph")
            root = Provenance.from_dict(payload["root"])
            parents = tuple(Provenance.from_dict(p) for p in payload["parents"])
            if len({p.dataset_id for p in parents}) != len(parents):
                raise ValueError("Duplicate parent")
            # Reject flags that the model would otherwise safely normalize: this
            # persistence boundary must also diagnose inconsistent metadata.
            result = cls(root, parents)
            if result.payload() != payload:
                raise ValueError("Noncanonical provenance")
            return result
        except (ValueError, TypeError, KeyError) as exc:
            raise IntegrityError("Invalid lineage") from exc

    def check(self, purpose):
        from . import trusted_config
        decision = evaluate_policy(self.root, purpose,
            parents={p.dataset_id: p for p in self.parents},
            source_policies=trusted_config.SOURCE_POLICIES)
        if not decision.allowed:
            raise PolicyDeniedError(decision)
        return decision


def derive(lineages):
    records = {}
    roots = []
    for lineage in lineages:
        roots.append(lineage.root.dataset_id)
        for node in (lineage.root, *lineage.parents):
            if node.dataset_id in records and records[node.dataset_id] != node:
                raise IntegrityError("Conflicting ancestry")
            records[node.dataset_id] = node
    if not roots:
        raise IntegrityError("No inputs")
    root = Provenance(str(uuid4()), source="derived", provider="fxtick",
        license_class=LicenseClass.DISTRIBUTABLE, redistributable=True,
        acquired_at=datetime.now(timezone.utc), derived_from=tuple(dict.fromkeys(roots)),
        acquisition_mechanism="transformation")
    # Reuse Phase 1's ordering and graph evaluation, including source restrictions.
    # The provisional positive metadata claim never itself grants permission.
    from . import trusted_config
    decision = evaluate_policy(root, ExportPurpose.DISTRIBUTION,
                               parents=records, source_policies=trusted_config.SOURCE_POLICIES)
    root = replace(root, license_class=decision.effective_license_class,
                   redistributable=decision.allowed)
    return Lineage(root, tuple(records.values()))


@dataclass(frozen=True)
class Artifact:
    path: Path
    lineage: Lineage
    sha256: str
    size: int
    ledger: Path | None = None

    def check(self, purpose):
        current = inspect(self.path, ledger=self.ledger)
        if current != self:
            raise IntegrityError("Artifact changed since selection")
        decision = self.lineage.check(purpose)
        if purpose == ExportPurpose.DISTRIBUTION:
            from . import trusted_config
            graph_hash = hashlib.sha256(canonical(self.lineage.payload()).encode()).hexdigest()
            if trusted_config.DISTRIBUTION_ATTESTATIONS.get(self.sha256) != graph_hash:
                raise IntegrityError("No trusted content/lineage distribution attestation")
        return decision


def _embedded(path):
    if not is_parquet(path):
        return None
    import pyarrow.parquet as pq
    metadata = pq.read_metadata(path).metadata or {}
    return metadata.get(PARQUET_KEY)


def is_parquet(path):
    with open(path, "rb") as src:
        magic = src.read(4)
    return magic == b"PAR1" or Path(path).suffix.lower() == ".parquet"


def inspect(path, *, ledger=None):
    path = Path(path).resolve()
    digest, size = fingerprint(path)
    if sidecar(path).exists():
        data = parse(sidecar(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != {"schema_version", "sha256", "size", "lineage"} or type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise IntegrityError("Invalid artifact manifest")
        if data["sha256"] != digest or type(data["size"]) is not int or data["size"] != size:
            raise IntegrityError("Content hash/size mismatch")
        lineage = Lineage.decode(data["lineage"])
        embedded = _embedded(path)
        if is_parquet(path) and (
            embedded is None or parse(embedded) != lineage.payload()
        ):
            raise IntegrityError("Parquet/sidecar lineage mismatch")
        # An explicit legacy record can never be superseded by a sidecar.
        if ledger is not None:
            registry = parse(Path(ledger).read_text(encoding="utf-8"))
            _validate_ledger(registry)
            if str(path) in registry["files"]:
                legacy = _legacy(path, digest, size, ledger)
                if legacy.lineage != lineage:
                    raise IntegrityError("Legacy ledger/sidecar disagreement")
            else:
                # A ledger may accompany a selection of legacy AND new managed
                # inputs. New managed inputs use their own verified manifests.
                ledger = None
        return Artifact(path, lineage, digest, size, Path(ledger).resolve() if ledger else None)
    if ledger is not None:
        return _legacy(path, digest, size, ledger)
    raise IntegrityError("UNKNOWN: missing provenance; owner registration required for legacy local use")


def seal(path, lineage):
    """Attach a manifest to a NEW artifact; never replace an existing manifest."""
    path = Path(path)
    digest, size = fingerprint(path)
    payload = {"schema_version": 1, "sha256": digest, "size": size, "lineage": lineage.payload()}
    with sidecar(path).open("x", encoding="utf-8") as out:
        out.write(canonical(payload))
    return inspect(path)


def register_legacy(path, ledger, *, owner_confirmed=False):
    """Register exactly one explicitly selected file. Never edit the source bytes."""
    if owner_confirmed is not True:
        raise IntegrityError("Explicit owner confirmation is required")
    path, ledger = Path(path).resolve(), Path(ledger).resolve()
    if path == ledger or sidecar(path).exists():
        raise IntegrityError("Registration requires a separate ledger and an unmanaged legacy file")
    digest, size = fingerprint(path)
    data = parse(ledger.read_text(encoding="utf-8")) if ledger.exists() else {"schema_version": 1, "files": {}}
    _validate_ledger(data)
    if str(path) in data["files"]:
        return inspect(path, ledger=ledger)
    provenance = new_dukascopy().to_dict()
    provenance["acquisition_mechanism"] = "owner_attestation"
    data["files"][str(path)] = {
        "source": "DUKASCOPY", "license_class": "PRIVATE_REFERENCE", "redistributable": False,
        "allowed_use": "LOCAL_TEST", "sha256": digest, "size": size,
        "registered_at": datetime.now(timezone.utc).isoformat(), "schema_version": 1,
        "provenance": provenance,
    }
    # A failed write cannot yield a permissive result: malformed ledgers deny.
    ledger.write_text(canonical(data), encoding="utf-8")
    return inspect(path, ledger=ledger)


def _validate_ledger(data):
    if not isinstance(data, dict) or set(data) != {"schema_version", "files"} or type(data["schema_version"]) is not int or data["schema_version"] != 1 or not isinstance(data["files"], dict):
        raise IntegrityError("Invalid legacy ledger")


def _legacy(path, digest, size, ledger):
    data = parse(Path(ledger).read_text(encoding="utf-8"))
    _validate_ledger(data)
    record = data["files"].get(str(path))
    required = {"source", "license_class", "redistributable", "allowed_use", "sha256", "size", "registered_at", "schema_version", "provenance"}
    if not isinstance(record, dict) or set(record) != required:
        raise IntegrityError("UNKNOWN: file not explicitly registered")
    if (record["source"] != "DUKASCOPY" or record["license_class"] != "PRIVATE_REFERENCE"
        or record["redistributable"] is not False or record["allowed_use"] != "LOCAL_TEST"
        or type(record["schema_version"]) is not int or record["schema_version"] != 1
        or record["sha256"] != digest or type(record["size"]) is not int or record["size"] != size):
        raise IntegrityError("Invalid legacy identity or changed content")
    try:
        registered = datetime.fromisoformat(record["registered_at"])
        if registered.utcoffset() is None:
            raise ValueError("naive")
        node = Provenance.from_dict(record["provenance"])
    except (TypeError, ValueError) as exc:
        raise IntegrityError("Invalid registration") from exc
    if (node.to_dict() != record["provenance"] or node.source != "dukascopy"
        or node.provider != "dukascopy" or node.acquisition_mechanism != "owner_attestation"
        or node.derived_from):
        raise IntegrityError("Ledger cannot grant other source identities")
    return Artifact(path, Lineage(node), digest, size, Path(ledger).resolve())
