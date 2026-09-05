"""Phase 3B monitoring/notification contracts only: no polling, sending or timers."""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from ..config import ConfigError, logical_id
from .health import utc_time


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    RECOVERY = "RECOVERY"


class Channel(str, Enum):
    LINE = "LINE"
    PUSH = "Push"
    EMAIL = "Email"


class Check(str, Enum):
    HEARTBEAT = "heartbeat"
    TERMINAL = "terminal"
    SOURCE = "source"
    LAST_TICK = "last-tick"
    LAST_WRITE = "last-write"
    DISK = "disk"
    COLLECTOR = "collector"


@dataclass(frozen=True)
class MonitoringPolicy:
    heartbeat_timeout_seconds: int = 180
    last_tick_timeout_seconds: int = 180
    last_write_timeout_seconds: int = 300
    min_disk_free_bytes: int = 1073741824
    cooldown_seconds: int = 300
    recovery_notification: bool = True

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if name == "recovery_notification":
                if type(value) is not bool:
                    raise ConfigError("Recovery flag must be boolean")
            elif type(value) is not int or value <= 0:
                raise ConfigError("Monitoring thresholds must be positive integers")

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__) | {"schema_version"}:
            raise ConfigError("Invalid monitoring policy fields")
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ConfigError("Invalid monitoring policy schema")
        return cls(**{key: value for key, value in data.items() if key != "schema_version"})


@dataclass(frozen=True)
class NotificationRoute:
    route_id: str
    channel: Channel

    def __post_init__(self):
        logical_id(self.route_id)
        if not isinstance(self.channel, Channel):
            raise ConfigError("Unknown notification channel")
        # Only a logical route. Recipients/tokens are resolved outside Core/config.


@dataclass(frozen=True)
class IncidentKey:
    collector_id: str
    check: Check
    terminal_id: str | None = None

    def __post_init__(self):
        logical_id(self.collector_id)
        if not isinstance(self.check, Check):
            raise ConfigError("Invalid monitoring check")
        if self.terminal_id is not None:
            logical_id(self.terminal_id)
        if (self.check == Check.TERMINAL) != (self.terminal_id is not None):
            raise ConfigError("Terminal checks require an explicit logical terminal ID")


@dataclass(frozen=True)
class IncidentState:
    """External durable dedup/cooldown state. 3B owns transitions and persistence."""
    key: IncidentKey
    first_seen_at: datetime
    last_notified_at: datetime | None = None
    recovered_at: datetime | None = None

    def __post_init__(self):
        if not isinstance(self.key, IncidentKey):
            raise ConfigError("Invalid incident identity")
        object.__setattr__(self, "first_seen_at", utc_time(self.first_seen_at))
        for name in ("last_notified_at", "recovered_at"):
            value = utc_time(getattr(self, name), optional=True)
            if value is not None and value < self.first_seen_at:
                raise ConfigError("Invalid incident chronology")
            object.__setattr__(self, name, value)


@dataclass(frozen=True)
class NotificationEvent:
    event_id: str
    incident: IncidentKey
    severity: Severity
    occurred_at: datetime
    outage_seconds: int | None = None

    def __post_init__(self):
        logical_id(self.event_id)
        if not isinstance(self.incident, IncidentKey) or not isinstance(self.severity, Severity):
            raise ConfigError("Invalid notification event")
        object.__setattr__(self, "occurred_at", utc_time(self.occurred_at))
        if self.outage_seconds is not None and (type(self.outage_seconds) is not int or self.outage_seconds < 0):
            raise ConfigError("Invalid outage duration")
        if self.severity == Severity.RECOVERY and self.outage_seconds is None:
            raise ConfigError("Recovery event requires the outage duration")


class NotificationProvider(Protocol):
    def send(self, event: NotificationEvent, route: NotificationRoute) -> bool:
        """3B adapter returns confirmed success; never record a failed send as sent."""
        ...


class HeartbeatStore(Protocol):
    def latest(self, collector_id: str):
        """Return latest authenticated receipt or None; independent of collector host."""
        ...


class IncidentStore(Protocol):
    def load(self, key: IncidentKey) -> IncidentState | None: ...
    def save(self, state: IncidentState) -> None: ...
