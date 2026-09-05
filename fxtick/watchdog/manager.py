"""Collector-side observation/sending loop; no market acquisition or restart."""
from dataclasses import dataclass
import logging
import time
from typing import Protocol

from ..config import ConfigError, logical_id
from ..collectors.health import HealthSnapshot, utc_time
from .heartbeat import Heartbeat
from .health import evaluate_health
from .config import NodePolicy

LOG = logging.getLogger(__name__)


class HeartbeatTransport(Protocol):
    def send(self, heartbeat: Heartbeat) -> bool: ...


@dataclass
class ManagedCollector:
    policy: NodePolicy
    probe: object
    transport: HeartbeatTransport
    boot_id: str
    sequence: int = 0
    next_due: float = 0

    def __post_init__(self):
        if not isinstance(self.policy, NodePolicy):
            raise ConfigError('Invalid managed collector policy')
        logical_id(self.boot_id)
        if type(self.sequence) is not int or not 0 <= self.sequence < 2**63:
            raise ConfigError('Invalid starting sequence')


class CollectorManager:
    """Adapters must return within bounded time; monotonic sending cadence.

    A new process needs a new externally authorized boot ID (or durable sequence
    recovery). Reusing a boot with sequence zero is intentionally rejected by the
    receiver. No credential or boot authorization is generated here.
    """
    def __init__(self, collectors, clock, monotonic=time.monotonic, schedule=None):
        self.collectors = tuple(collectors)
        if not self.collectors or len({c.policy.collector_id for c in self.collectors}) != len(self.collectors):
            raise ConfigError('Invalid manager collector registry')
        self.clock, self.monotonic, self.schedule = clock, monotonic, schedule

    def step(self):
        results = {}
        for collector in self.collectors:
            due = self.monotonic()
            if due < collector.next_due:
                continue
            now = utc_time(self.clock())
            try:
                snapshot = collector.probe.sample(now)
                if (not isinstance(snapshot, HealthSnapshot) or snapshot.collector_id != collector.policy.collector_id
                    or snapshot.observed_at != now):
                    raise ConfigError('Probe must return a current observation for its collector')
            except Exception:
                snapshot = HealthSnapshot(collector.policy.collector_id, now, error_state='probe-failed')
                LOG.warning('Collector probe failed; details omitted')
            health = evaluate_health(snapshot, collector.policy, now, self.schedule)
            collector.sequence += 1
            heartbeat = Heartbeat(snapshot, collector.boot_id, collector.sequence)
            try:
                sent = collector.transport.send(heartbeat) is True
            except Exception:
                sent = False
            collector.next_due = self.monotonic() + collector.policy.heartbeat_interval_seconds
            if not sent:
                LOG.warning('Heartbeat delivery failed; details omitted')
            results[collector.policy.collector_id] = {'health': health, 'heartbeat_sent': sent}
        return results

    def run(self, stop, poll_seconds=1):
        if type(poll_seconds) not in (int, float) or not 0 < poll_seconds <= 60:
            raise ConfigError('Invalid manager poll interval')
        while not stop.is_set():
            self.step()
            stop.wait(poll_seconds)
