"""Validated heartbeat envelope. Authentication proof travels out of band."""
from dataclasses import dataclass
import json
from typing import Protocol

from ..config import ConfigError, logical_id
from ..collectors.health import HealthSnapshot


class HeartbeatAuthenticator(Protocol):
    def verify(self, collector_id: str, boot_id: str, payload: bytes, proof) -> bool:
        """Authenticate exact bytes and bind identity/boot to an authorized sender.

        Production adapter must authorize boot changes, not just accept any
        self-selected boot ID. No permissive default implementation exists.
        """
        ...


def summary(snapshot):
    values = (snapshot.collector_alive, snapshot.source_connected, snapshot.disk_path_accessible)
    if snapshot.error_state or False in values or any(t.process_alive is False for t in snapshot.terminals):
        return 'degraded'
    if None in values or snapshot.last_tick_time is None or snapshot.last_successful_write is None or snapshot.disk_free_bytes is None:
        return 'unknown'
    return 'observed'  # Age/capacity policy is evaluated by the monitor, not the sender.


@dataclass(frozen=True)
class Heartbeat:
    snapshot: HealthSnapshot
    boot_id: str
    sequence: int

    def __post_init__(self):
        if not isinstance(self.snapshot, HealthSnapshot):
            raise ConfigError('Invalid heartbeat snapshot')
        logical_id(self.boot_id)
        if type(self.sequence) is not int or not 0 <= self.sequence < 2**63:
            raise ConfigError('Invalid heartbeat sequence')

    def encode(self):
        return json.dumps({'schema_version': 1, 'boot_id': self.boot_id, 'sequence': self.sequence,
            'status': summary(self.snapshot), 'health': self.snapshot.to_dict()}, separators=(',', ':')).encode('utf-8')

    @classmethod
    def decode(cls, payload):
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError()
                value[key] = item
            return value
        try:
            if not isinstance(payload, bytes) or not 0 < len(payload) <= 65536:
                raise ValueError()
            raw = json.loads(payload, object_pairs_hook=unique)
            if not isinstance(raw, dict) or set(raw) != {'schema_version', 'boot_id', 'sequence', 'status', 'health'}:
                raise ValueError()
            if type(raw['schema_version']) is not int or raw['schema_version'] != 1:
                raise ValueError()
            value = cls(HealthSnapshot.from_dict(raw['health']), raw['boot_id'], raw['sequence'])
            if raw['status'] != summary(value.snapshot):
                raise ValueError()
            return value
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise ConfigError('Invalid heartbeat; payload omitted') from None
