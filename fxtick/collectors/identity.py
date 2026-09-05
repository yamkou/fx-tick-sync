"""Collector observations alongside, not inside, the unchanged provenance v1.

Observations identify where acquisition happened. They NEVER grant policy rights.
Derived artifacts retain their existing parent IDs, which can join these records.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path

from ..artifacts import Artifact, IntegrityError, canonical, inspect, parse
from ..config import Collector, ConfigError, Environment, SourceType, logical_id
from ..provenance import identity


@dataclass(frozen=True)
class AcquisitionRecord:
    dataset_id: str
    content_sha256: str
    lineage_sha256: str
    collector_id: str
    location: str
    environment: Environment
    source: str
    broker: str
    symbol: str
    acquired_at: datetime

    def __post_init__(self):
        import re
        for value in (self.collector_id, self.location, self.broker):
            logical_id(value)
        if not isinstance(self.environment, Environment):
            raise ConfigError("Invalid record environment")
        if not isinstance(self.dataset_id, str) or not self.dataset_id.strip():
            raise ConfigError("Missing dataset identity")
        for value in (self.content_sha256, self.lineage_sha256):
            if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
                raise ConfigError("Invalid record hash")
        if self.source not in {s.value for s in SourceType}:
            raise ConfigError("Invalid acquisition source")
        if not isinstance(self.symbol, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}", self.symbol):
            raise ConfigError("Invalid record symbol")
        if not isinstance(self.acquired_at, datetime) or self.acquired_at.utcoffset() is None:
            raise ConfigError("Acquisition time must be timezone aware")
        object.__setattr__(self, "acquired_at", self.acquired_at.astimezone(timezone.utc))

    @classmethod
    def for_artifact(cls, artifact: Artifact, collector: Collector, environment: Environment, symbol: str):
        current = inspect(artifact.path, ledger=artifact.ledger)
        if current != artifact:
            raise IntegrityError("Acquisition artifact changed")
        root = artifact.lineage.root
        # Collector names/configuration cannot relabel a dataset or its provider.
        if root.source != identity(collector.source_type.value) or root.provider != identity(collector.broker):
            raise IntegrityError("Collector/source/provider mismatch")
        if symbol not in collector.symbols or root.derived_from:
            raise IntegrityError("Acquisition record needs a selected symbol and raw source root")
        return cls(root.dataset_id, artifact.sha256,
            hashlib.sha256(canonical(artifact.lineage.payload()).encode()).hexdigest(),
            collector.collector_id, collector.location, environment, collector.source_type.value,
            collector.broker, symbol, root.acquired_at)

    def verify(self, artifact: Artifact):
        current = inspect(artifact.path, ledger=artifact.ledger)
        if (current != artifact or self.dataset_id != current.lineage.root.dataset_id
            or self.content_sha256 != current.sha256
            or self.lineage_sha256 != hashlib.sha256(canonical(current.lineage.payload()).encode()).hexdigest()
            or self.acquired_at != current.lineage.root.acquired_at
            or identity(self.source) != current.lineage.root.source
            or identity(self.broker) != current.lineage.root.provider):
            raise IntegrityError("Acquisition record no longer matches the artifact")

    def to_dict(self):
        return {**self.__dict__, "schema_version": 1, "environment": self.environment.value,
                "acquired_at": self.acquired_at.isoformat()}

    def write(self, path, artifact):
        self.verify(artifact)
        with Path(path).open("x", encoding="utf-8") as dest:
            dest.write(canonical(self.to_dict()))

    @classmethod
    def from_dict(cls, data):
        required = {"dataset_id", "content_sha256", "lineage_sha256", "collector_id", "location", "environment",
                    "source", "broker", "symbol", "acquired_at", "schema_version"}
        if not isinstance(data, dict) or set(data) != required or type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ConfigError("Invalid acquisition record schema")
        values = dict(data); del values["schema_version"]
        try:
            values["environment"] = Environment(values["environment"])
            values["acquired_at"] = datetime.fromisoformat(values["acquired_at"])
            return cls(**values)
        except (TypeError, ValueError):
            raise ConfigError("Invalid acquisition record; values omitted") from None

    @classmethod
    def read(cls, path):
        return cls.from_dict(parse(Path(path).read_text(encoding="utf-8")))
