"""In-memory provenance contract. No data/file access or rights inference from paths.

Metadata is an assertion, not a licence approval or proof of data identity. Phase 2
must bind it to the actual bytes and supply complete, verified parent records.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class LicenseClass(str, Enum):
    UNKNOWN = "UNKNOWN"
    PRIVATE_REFERENCE = "PRIVATE_REFERENCE"
    INTERNAL_ONLY = "INTERNAL_ONLY"
    DISTRIBUTABLE = "DISTRIBUTABLE"


def identity(value: str) -> str:
    """Canonical explicit source identifiers; never inspect a filename or folder."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("identity must be a non-empty string")
    return value.strip().casefold().replace("-", "_")


def is_dukascopy(source: str, provider: str) -> bool:
    return bool({identity(source), identity(provider)} & {"dukascopy", "dukascopy_python"})


@dataclass(frozen=True)
class Provenance:
    dataset_id: str
    source: str = "unknown"
    provider: str = "unknown"
    license_class: LicenseClass = LicenseClass.UNKNOWN
    redistributable: bool = False
    acquired_at: datetime | None = None
    derived_from: tuple[str, ...] = ()
    account_type: str = "unspecified"
    acquisition_mechanism: str = "unspecified"
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.dataset_id, str) or not self.dataset_id.strip():
            raise ValueError("dataset_id must be a non-empty string")
        for field in ("source", "provider", "account_type", "acquisition_mechanism"):
            object.__setattr__(self, field, identity(getattr(self, field)))
        if not isinstance(self.license_class, LicenseClass):
            raise ValueError("license_class must be a LicenseClass")
        if type(self.redistributable) is not bool:
            raise ValueError("redistributable must be a boolean")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported provenance schema_version")
        if not isinstance(self.derived_from, tuple) or any(
            not isinstance(parent, str) or not parent.strip() for parent in self.derived_from
        ):
            raise ValueError("derived_from must be a tuple of non-empty dataset IDs")
        if len(set(self.derived_from)) != len(self.derived_from):
            raise ValueError("duplicate parent dataset IDs")
        if self.acquired_at is not None:
            if not isinstance(self.acquired_at, datetime) or self.acquired_at.utcoffset() is None:
                raise ValueError("acquired_at must be timezone-aware or None")
            object.__setattr__(self, "acquired_at", self.acquired_at.astimezone(timezone.utc))
        # No supplied flag or licence claim can upgrade Dukascopy's classification.
        if is_dukascopy(self.source, self.provider):
            object.__setattr__(self, "license_class", LicenseClass.PRIVATE_REFERENCE)
            object.__setattr__(self, "redistributable", False)

    @property
    def policy_key(self) -> tuple[str, str, str, str]:
        return self.source, self.provider, self.account_type, self.acquisition_mechanism

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "dataset_id": self.dataset_id,
            "source": self.source,
            "provider": self.provider,
            "license_class": self.license_class.value,
            "redistributable": self.redistributable,
            "acquired_at": self.acquired_at.isoformat() if self.acquired_at else None,
            "derived_from": list(self.derived_from),
            "account_type": self.account_type,
            "acquisition_mechanism": self.acquisition_mechanism,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Provenance:
        if not isinstance(value, dict):
            raise ValueError("provenance must be an object")
        required = {
            "schema_version", "dataset_id", "source", "provider", "license_class",
            "redistributable", "acquired_at", "derived_from",
        }
        optional = {"account_type", "acquisition_mechanism"}
        if not required <= value.keys() or value.keys() - required - optional:
            raise ValueError("missing or unsupported provenance fields")
        data = dict(value)
        if not isinstance(data["derived_from"], list):
            raise ValueError("derived_from must be an array of dataset IDs")
        data["derived_from"] = tuple(data["derived_from"])
        try:
            data["license_class"] = LicenseClass(data["license_class"])
            if data["acquired_at"] is not None:
                if not isinstance(data["acquired_at"], str):
                    raise ValueError("acquired_at must be an ISO timestamp or null")
                data["acquired_at"] = datetime.fromisoformat(data["acquired_at"])
            return cls(**data)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid provenance: {exc}") from exc

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> Provenance:
        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate provenance field: {key}")
                result[key] = value
            return result

        try:
            return cls.from_dict(json.loads(text, object_pairs_hook=unique_object))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid provenance JSON: {exc}") from exc
