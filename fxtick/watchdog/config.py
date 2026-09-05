"""Non-secret monitor configuration. UTC weekly windows have explicit semantics."""
from dataclasses import dataclass
import json
from pathlib import Path

from ..config import ConfigError, logical_id
from ..collectors.monitoring import MonitoringPolicy


@dataclass(frozen=True)
class NodePolicy:
    collector_id: str
    heartbeat_interval_seconds: int = 60
    warning_seconds: int = 120
    critical_seconds: int = 180
    startup_grace_seconds: int = 180
    max_clock_skew_seconds: int = 30
    max_payload_age_seconds: int = 90
    # Minute offsets since Monday 00:00 UTC; half-open intervals. Empty = closed.
    active_windows: tuple = ((0, 10080),)
    terminal_ids: tuple = ()
    health: MonitoringPolicy = MonitoringPolicy()

    def __post_init__(self):
        logical_id(self.collector_id)
        for name in ('heartbeat_interval_seconds', 'warning_seconds', 'critical_seconds',
                     'startup_grace_seconds', 'max_clock_skew_seconds', 'max_payload_age_seconds'):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ConfigError('Monitor intervals must be positive integers')
        if not self.heartbeat_interval_seconds < self.warning_seconds < self.critical_seconds:
            raise ConfigError('Heartbeat intervals must increase: interval, warning, critical')
        if not isinstance(self.health, MonitoringPolicy):
            raise ConfigError('Invalid health policy')
        if not isinstance(self.terminal_ids, tuple) or len(set(self.terminal_ids)) != len(self.terminal_ids):
            raise ConfigError('Invalid terminal registry')
        for terminal in self.terminal_ids:
            logical_id(terminal)
        if not isinstance(self.active_windows, tuple):
            raise ConfigError('Invalid monitoring schedule')
        previous = -1
        for window in self.active_windows:
            if (not isinstance(window, tuple) or len(window) != 2 or
                any(type(v) is not int for v in window) or
                not 0 <= window[0] < window[1] <= 10080 or window[0] < previous):
                raise ConfigError('Schedule windows must be sorted, disjoint UTC minute intervals')
            previous = window[1]

    def active(self, now):
        minute = now.weekday() * 1440 + now.hour * 60 + now.minute
        return any(start <= minute < end for start, end in self.active_windows)


@dataclass(frozen=True)
class MonitorConfig:
    monitor_id: str
    nodes: tuple[NodePolicy, ...]
    retry_seconds: int = 60

    def __post_init__(self):
        logical_id(self.monitor_id)
        if (not isinstance(self.nodes, tuple) or not self.nodes or
            any(not isinstance(n, NodePolicy) for n in self.nodes) or
            len({n.collector_id for n in self.nodes}) != len(self.nodes)):
            raise ConfigError('Invalid or duplicate monitored collector')
        if type(self.retry_seconds) is not int or self.retry_seconds <= 0:
            raise ConfigError('Retry interval must be positive')

    @classmethod
    def from_dict(cls, value):
        try:
            if set(value) != {'schema_version', 'monitor_id', 'nodes', 'retry_seconds'} or type(value['schema_version']) is not int or value['schema_version'] != 1:
                raise ValueError()
            if not isinstance(value['nodes'], list):
                raise ValueError()
            nodes = []
            for raw in value['nodes']:
                if set(raw) != set(NodePolicy.__dataclass_fields__):
                    raise ValueError()
                if not isinstance(raw['terminal_ids'], list) or not isinstance(raw['active_windows'], list):
                    raise ValueError()
                if any(not isinstance(w, list) for w in raw['active_windows']):
                    raise ValueError()
                nodes.append(NodePolicy(**{**raw, 'health': MonitoringPolicy.from_dict(raw['health']),
                    'active_windows': tuple(tuple(w) for w in raw['active_windows']),
                    'terminal_ids': tuple(raw['terminal_ids'])}))
            return cls(value['monitor_id'], tuple(nodes), value['retry_seconds'])
        except (ValueError, TypeError, KeyError):
            raise ConfigError('Invalid monitor configuration; values omitted') from None


def load_monitor_config(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ConfigError('Duplicate monitor configuration key')
            result[key] = value
        return result
    try:
        return MonitorConfig.from_dict(json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=unique))
    except (ValueError, TypeError):
        raise ConfigError('Invalid monitor configuration; values omitted') from None
