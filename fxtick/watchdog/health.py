"""Internal watchdog evaluation shared with the independent external monitor."""
from typing import Protocol
from ..config import ConfigError
from ..collectors.health import HealthSnapshot, utc_time
from ..collectors.monitoring import Check, IncidentKey, Severity


class MonitoringSchedule(Protocol):
    def active(self, collector_id: str, check: Check, now) -> bool:
        """Adapter may use symbol/session/holiday calendars; no feed access here."""
        ...


class HealthProbe(Protocol):
    def sample(self, now) -> HealthSnapshot: ...


class RecoveryAction(Protocol):
    def execute(self, incident: IncidentKey) -> bool:
        """Future separately authorized restart adapter; monitor never invokes it."""
        ...


def evaluate_health(snapshot, policy, now, schedule=None):
    """Return check -> severity; None means observed healthy, absent means suspended.

    Unknown observations warn; a closed session suspends age checks without
    inventing a recovery. Tick and write ages are independent.
    """
    now = utc_time(now)
    if not isinstance(snapshot, HealthSnapshot) or snapshot.collector_id != policy.collector_id:
        raise ConfigError('Health observation must match monitored collector')
    result = {}
    def put(check, severity, terminal=None):
        result[IncidentKey(policy.collector_id, check, terminal)] = severity
    def boolean(value):
        return None if value is True else Severity.CRITICAL if value is False else Severity.WARNING
    put(Check.COLLECTOR, Severity.CRITICAL if snapshot.error_state else boolean(snapshot.collector_alive))
    put(Check.SOURCE, boolean(snapshot.source_connected))
    disk = boolean(snapshot.disk_path_accessible)
    if disk is None:
        disk = (Severity.WARNING if snapshot.disk_free_bytes is None else
                Severity.CRITICAL if snapshot.disk_free_bytes < policy.health.min_disk_free_bytes else None)
    put(Check.DISK, disk)
    for check, value, threshold in (
        (Check.LAST_TICK, snapshot.last_tick_time, policy.health.last_tick_timeout_seconds),
        (Check.LAST_WRITE, snapshot.last_successful_write, policy.health.last_write_timeout_seconds)):
        active = schedule.active(policy.collector_id, check, now) if schedule else policy.active(now)
        if active:
            age = (now - value).total_seconds() if value else None
            put(check, Severity.WARNING if age is None or age >= threshold else None)
    terminals = {t.terminal_id: t.process_alive for t in snapshot.terminals}
    for terminal in policy.terminal_ids:
        put(Check.TERMINAL, boolean(terminals.get(terminal)), terminal)
    return result
