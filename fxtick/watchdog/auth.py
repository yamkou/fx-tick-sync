"""HMAC-SHA256 over exact request bytes; no keys/nonces in diagnostic output."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import re
from uuid import uuid4

from ..config import ConfigError, logical_id
from ..collectors.health import utc_time
from .providers import HTTPSPoster, _endpoint


@dataclass(frozen=True, repr=False)
class HMACProof:
    key_id: str
    timestamp: int
    nonce: str = field(repr=False)
    signature: str = field(repr=False)

    def __post_init__(self):
        logical_id(self.key_id)
        if type(self.timestamp) is not int or not 0 <= self.timestamp < 2**40:
            raise ConfigError('Invalid authentication timestamp')
        if not isinstance(self.nonce, str) or not re.fullmatch('[a-f0-9]{32}', self.nonce):
            raise ConfigError('Invalid authentication nonce')
        if not isinstance(self.signature, str) or not re.fullmatch('[a-f0-9]{64}', self.signature):
            raise ConfigError('Invalid authentication signature')

    def headers(self):
        return {'X-FX-Key-Id': self.key_id, 'X-FX-Timestamp': str(self.timestamp),
                'X-FX-Nonce': self.nonce, 'X-FX-Signature': self.signature}

    @classmethod
    def from_headers(cls, headers):
        try:
            stamp = headers['X-FX-Timestamp']
            if not isinstance(stamp, str) or not re.fullmatch('[0-9]{1,13}', stamp):
                raise ValueError()
            return cls(headers['X-FX-Key-Id'], int(stamp), headers['X-FX-Nonce'], headers['X-FX-Signature'])
        except (ValueError, TypeError, KeyError):
            raise ConfigError('Invalid authentication headers; values omitted') from None


@dataclass(frozen=True)
class SenderKeys:
    collector_id: str
    keys: tuple  # (public key ID, logical SecretProvider reference), rotation overlap supported
    boot_ids: tuple

    def __post_init__(self):
        logical_id(self.collector_id)
        if not isinstance(self.keys, tuple) or not self.keys or not isinstance(self.boot_ids, tuple) or not self.boot_ids:
            raise ConfigError('Sender needs keys and explicitly authorized boots')
        for pair in self.keys:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ConfigError('Invalid signing key reference')
            for value in pair: logical_id(value)
        for boot in self.boot_ids: logical_id(boot)
        if len({k for k, _ in self.keys}) != len(self.keys) or len(set(self.boot_ids)) != len(self.boot_ids):
            raise ConfigError('Duplicate sender key or boot')


def signing_bytes(collector, boot, payload, key_id, timestamp, nonce):
    return '\n'.join(('fx-heartbeat-hmac-v1', 'POST', '/v1/heartbeat', collector, boot,
        key_id, str(timestamp), nonce, hashlib.sha256(payload).hexdigest())).encode('ascii')


def key_bytes(secrets, reference):
    value = secrets.get(reference)
    if not isinstance(value, str) or len(value.encode('utf-8')) < 32:
        raise ConfigError('Signing key unavailable or too short')
    return value.encode('utf-8')


def sign(heartbeat, key_id, key_reference, secrets, now, nonce=None):
    logical_id(key_id); logical_id(key_reference)
    timestamp = int(utc_time(now).timestamp())
    nonce = nonce or uuid4().hex
    # Validate all header fields before forming a signature.
    HMACProof(key_id, timestamp, nonce, '0'*64)
    message = signing_bytes(heartbeat.snapshot.collector_id, heartbeat.boot_id,
                            heartbeat.encode(), key_id, timestamp, nonce)
    signature = hmac.new(key_bytes(secrets, key_reference), message, hashlib.sha256).hexdigest()
    return HMACProof(key_id, timestamp, nonce, signature)


class HMACAuthenticator:
    def __init__(self, senders, secrets, clock, window_seconds=90):
        senders = tuple(senders)
        if not senders or any(not isinstance(s, SenderKeys) for s in senders) or len({s.collector_id for s in senders}) != len(senders):
            raise ConfigError('Invalid signing sender registry')
        if type(window_seconds) is not int or not 1 <= window_seconds <= 300:
            raise ConfigError('Invalid signature acceptance window')
        self.senders = {s.collector_id: s for s in senders}
        self.secrets, self.clock, self.window = secrets, clock, window_seconds

    def verify(self, collector_id, boot_id, payload, proof):
        try:
            sender = self.senders[collector_id]
            if not isinstance(proof, HMACProof) or boot_id not in sender.boot_ids:
                return False
            age = utc_time(self.clock()).timestamp() - proof.timestamp
            if abs(age) > self.window:
                return False
            reference = dict(sender.keys)[proof.key_id]
            expected = hmac.new(key_bytes(self.secrets, reference),
                signing_bytes(collector_id, boot_id, payload, proof.key_id, proof.timestamp, proof.nonce), hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, proof.signature)
        except Exception:
            return False

    def commit_proof(self, db, collector_id, boot_id, payload, proof, now):
        """Consume nonce in the SAME transaction as the durable heartbeat receipt."""
        if not self.verify(collector_id, boot_id, payload, proof):
            raise ConfigError('Authentication expired before receipt commit')
        prefix = 'hmac-nonce:'
        # Hash nonce; key rotation cannot reopen an already consumed nonce.
        identity = prefix + collector_id + ':' + hashlib.sha256(proof.nonce.encode()).hexdigest()
        db.execute('DELETE FROM monitor_meta WHERE key LIKE ? AND CAST(value AS REAL) < ?',
                   (prefix+'%', now.timestamp()))
        if db.execute('SELECT 1 FROM monitor_meta WHERE key=?', (identity,)).fetchone():
            raise ConfigError('Authentication replay rejected')
        db.execute('INSERT INTO monitor_meta VALUES (?,?)', (identity, str(proof.timestamp+self.window)))


class SignedHeartbeatTransport:
    def __init__(self, endpoint_reference, key_id, key_reference, secrets, clock, poster=None):
        for value in (endpoint_reference, key_id, key_reference): logical_id(value)
        self.endpoint_reference, self.key_id, self.key_reference = endpoint_reference, key_id, key_reference
        self.secrets, self.clock, self.poster = secrets, clock, poster or HTTPSPoster()

    def send(self, heartbeat):
        try:
            endpoint = _endpoint(self.secrets.get(self.endpoint_reference))
            proof = sign(heartbeat, self.key_id, self.key_reference, self.secrets, self.clock())
            status = self.poster.post(endpoint, heartbeat.encode(), {'Content-Type': 'application/json', **proof.headers()})
            return type(status) is int and 200 <= status < 300
        except Exception:
            return False
