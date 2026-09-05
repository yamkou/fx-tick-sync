"""Portable execution budgets and loss-explicit buffering; no OS tuning or trading."""
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path

from .config import ConfigError


@dataclass(frozen=True)
class ExecutionProfile:
    schema_version: int
    profile: str
    low_resource_mode: bool
    sampling_interval_seconds: int
    memory_warning_bytes: int
    memory_critical_bytes: int
    startup_delay_seconds: int
    startup_settle_seconds: int
    max_buffer_bytes: int
    max_batch_records: int
    log_max_bytes: int
    log_backups: int
    growth_windows_seconds: list

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ConfigError('Invalid execution profile version')
        if self.profile not in ('collector', 'analysis') or type(self.low_resource_mode) is not bool:
            raise ConfigError('Invalid execution profile')
        for name in ('sampling_interval_seconds','memory_warning_bytes','memory_critical_bytes',
                     'startup_delay_seconds','startup_settle_seconds','max_buffer_bytes',
                     'max_batch_records','log_max_bytes','log_backups'):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ConfigError('Profile budgets must be positive integers')
        if not 5 <= self.sampling_interval_seconds <= 3600:
            raise ConfigError('Resource sampling interval outside safe bounds')
        if (self.memory_critical_bytes >= self.memory_warning_bytes or self.log_backups > 20
                or self.log_max_bytes < 64):
            raise ConfigError('Invalid threshold ordering or log retention')
        if (not isinstance(self.growth_windows_seconds, list) or not self.growth_windows_seconds
                or any(type(v) is not int or v < self.sampling_interval_seconds
                       for v in self.growth_windows_seconds)
                or self.growth_windows_seconds != sorted(set(self.growth_windows_seconds))):
            raise ConfigError('Invalid growth windows')


def load_profile(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ConfigError('Duplicate execution profile key')
            result[key] = value
        return result
    try:
        return ExecutionProfile(**json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=unique))
    except Exception:
        raise ConfigError('Invalid execution profile; values omitted') from None


@dataclass(frozen=True)
class ResourceMetrics:
    """Separate versioned future-heartbeat payload; absent metrics mean UNKNOWN.

    Does not change schema-1 heartbeat compatibility. No paths, accounts or prices.
    IO counters are cumulative; consumers calculate deltas between observations.
    """
    observed_at: str
    total_memory: int | None = None
    available_memory: int | None = None
    commit_memory: int | None = None
    commit_limit: int | None = None
    cpu_percent: float | None = None
    process_memory: int | None = None
    process_private_memory: int | None = None
    process_read_bytes: int | None = None
    process_write_bytes: int | None = None
    disk_free: int | None = None
    process_count: int | None = None
    queue_depth: int | None = None
    tick_rate: float | None = None
    write_latency: float | None = None

    def __post_init__(self):
        from datetime import datetime
        from .collectors.health import utc_time
        utc_time(datetime.fromisoformat(self.observed_at))
        for key, value in asdict(self).items():
            if key == 'observed_at' or value is None: continue
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ConfigError('Invalid resource observation')
        if self.cpu_percent is not None and self.cpu_percent > 100:
            raise ConfigError('Invalid CPU percentage')

    def to_dict(self):
        return {'schema_version': 1, **asdict(self)}


def memory_severity(metrics, profile):
    if metrics.available_memory is None: return 'WARNING'
    if metrics.available_memory < profile.memory_critical_bytes: return 'CRITICAL'
    if metrics.available_memory < profile.memory_warning_bytes: return 'WARNING'
    return None


class BufferPressure(RuntimeError):
    """Incoming record was NOT accepted; caller retains it and must retry/replay."""


class BoundedBatch:
    """Single-owner immutable byte buffer; no lossy overflow or unbounded copies.

    Sink must durably commit the batch before returning exactly True. On failure,
    the batch is retained and BufferPressure propagates before accepting new data.
    An adapter MUST retain/replay the incoming record on pressure. Ambiguous sink
    failure requires idempotent writes; this class is not a durable source cursor.
    Use the existing provenance/policy gates in any market-data sink.
    """
    def __init__(self, profile, sink):
        self.profile, self.sink = profile, sink
        self.records, self.bytes = [], 0

    def flush(self):
        if not self.records: return
        try:
            accepted = self.sink(tuple(self.records)) is True
        except Exception:
            accepted = False
        if not accepted:
            raise BufferPressure('CRITICAL: batch uncommitted; retain input and apply backpressure')
        self.records.clear(); self.bytes = 0

    def put(self, record):
        if not isinstance(record, bytes): raise TypeError('Immutable bytes required')
        if len(record) > self.profile.max_buffer_bytes:
            raise BufferPressure('CRITICAL: oversized record not accepted; durable replay required')
        if (self.bytes + len(record) > self.profile.max_buffer_bytes
                or len(self.records) >= self.profile.max_batch_records):
            self.flush()
        self.records.append(record); self.bytes += len(record)
