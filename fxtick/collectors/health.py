"""Serializable observations, not probes or a watchdog. All times are UTC.

An external monitor owns HeartbeatReceipt.received_at. A stopped VPS cannot
produce a final snapshot; timeout evaluation must live outside that VPS in 3B.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from ..config import ConfigError, logical_id


def utc_time(value, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ConfigError("Observation time must be timezone aware")
    return value.astimezone(timezone.utc)


def state(value):
    if value is not None and type(value) is not bool:
        raise ConfigError("Observation state must be boolean or unknown")


@dataclass(frozen=True)
class TerminalHealth:
    terminal_id: str
    process_alive: bool | None = None

    def __post_init__(self):
        logical_id(self.terminal_id)
        state(self.process_alive)


@dataclass(frozen=True)
class HealthSnapshot:
    collector_id: str
    observed_at: datetime
    collector_alive: bool | None = None
    last_tick_time: datetime | None = None
    last_successful_write: datetime | None = None
    disk_path_accessible: bool | None = None
    disk_free_bytes: int | None = None
    source_connected: bool | None = None
    error_state: str | None = None
    terminals: tuple[TerminalHealth, ...] = ()

    def __post_init__(self):
        logical_id(self.collector_id)
        object.__setattr__(self, "observed_at", utc_time(self.observed_at))
        for name in ("last_tick_time", "last_successful_write"):
            value = utc_time(getattr(self, name), optional=True)
            if value is not None and value > self.observed_at:
                raise ConfigError("Activity timestamp cannot follow snapshot observation")
            object.__setattr__(self, name, value)
        for value in (self.collector_alive, self.disk_path_accessible, self.source_connected):
            state(value)
        if self.disk_free_bytes is not None and (type(self.disk_free_bytes) is not int or self.disk_free_bytes < 0):
            raise ConfigError("Disk capacity must be nonnegative bytes or unknown")
        if self.error_state is not None:
            logical_id(self.error_state)  # Stable error code, never raw credential-bearing exceptions.
        if not isinstance(self.terminals, tuple) or any(not isinstance(t, TerminalHealth) for t in self.terminals):
            raise ConfigError("Invalid terminal health registry")
        if len({t.terminal_id for t in self.terminals}) != len(self.terminals):
            raise ConfigError("Duplicate terminal health ID")

    def to_dict(self):
        return {**self.__dict__, "schema_version": 1, "observed_at": self.observed_at.isoformat(),
                "last_tick_time": self.last_tick_time.isoformat() if self.last_tick_time else None,
                "last_successful_write": self.last_successful_write.isoformat() if self.last_successful_write else None,
                "terminals": [dict(t.__dict__) for t in self.terminals]}

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__) | {"schema_version"}:
            raise ConfigError("Invalid health schema fields")
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ConfigError("Invalid health schema version")
        values = dict(data); del values["schema_version"]
        try:
            for name in ("observed_at", "last_tick_time", "last_successful_write"):
                if values[name] is not None:
                    values[name] = datetime.fromisoformat(values[name])
            if not isinstance(values["terminals"], list):
                raise ConfigError("Terminal states must be an array")
            values["terminals"] = tuple(TerminalHealth(**v) for v in values["terminals"])
            return cls(**values)
        except (ValueError, TypeError):
            raise ConfigError("Invalid health observation; values omitted") from None


@dataclass(frozen=True)
class HeartbeatReceipt:
    monitor_id: str
    received_at: datetime
    snapshot: HealthSnapshot
    boot_id: str
    sequence: int

    def __post_init__(self):
        logical_id(self.monitor_id)
        logical_id(self.boot_id)
        object.__setattr__(self, "received_at", utc_time(self.received_at))
        if not isinstance(self.snapshot, HealthSnapshot) or type(self.sequence) is not int or self.sequence < 0:
            raise ConfigError("Invalid heartbeat receipt")
        # Do not order sender/receiver timestamps: their clocks can differ.
        # 3B must authenticate sender and reject replay per boot_id/sequence.
