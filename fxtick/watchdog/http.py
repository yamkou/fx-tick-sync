"""WSGI ingress for a maintained production HTTP server/TLS reverse proxy.

No socket is bound here. All untrusted bodies, proofs and headers are omitted
from error responses/logs. The HTTP server must reject ambiguous framing.
"""
from dataclasses import dataclass
from concurrent.futures import TimeoutError
import ipaddress
import json
import re
from threading import Lock
import time
from typing import Protocol

from ..config import ConfigError
from .auth import HMACProof
from .heartbeat import Heartbeat, ReceiverUnavailable


@dataclass(frozen=True)
class ReceiverConfig:
    max_payload_bytes: int = 65536
    acceptance_timeout_seconds: int = 5
    allow_loopback_http: bool = True
    burst: int = 30
    requests_per_minute: int = 120
    max_clients: int = 1024

    def __post_init__(self):
        for name in ('max_payload_bytes','acceptance_timeout_seconds','burst','requests_per_minute','max_clients'):
            if type(getattr(self,name)) is not int or getattr(self,name) <= 0:
                raise ConfigError('Receiver limits must be positive integers')
        if self.max_payload_bytes > 65536 or self.acceptance_timeout_seconds > 30 or self.max_clients > 100000:
            raise ConfigError('Receiver limits exceed supported bounds')
        if type(self.allow_loopback_http) is not bool:
            raise ConfigError('Invalid receiver transport setting')


class RateLimiter(Protocol):
    def allow(self, client: str) -> bool: ...


class TokenBucketLimiter:
    """Bounded per-peer buckets; replace with shared ingress limits for a fleet."""
    def __init__(self, config, clock=time.monotonic):
        self.config, self.clock, self.lock, self.buckets = config, clock, Lock(), {}

    def allow(self, client):
        now = self.clock()
        with self.lock:
            rate = self.config.requests_per_minute / 60
            if client not in self.buckets:
                # Evict only fully refilled idle buckets; prevent unbounded IP state.
                idle = max(60, self.config.burst / rate)
                self.buckets = {k:v for k,v in self.buckets.items() if now-v[1] < idle}
                if len(self.buckets) >= self.config.max_clients:
                    return False
            tokens, last = self.buckets.get(client, (self.config.burst, now))
            tokens = min(self.config.burst, tokens+max(0, now-last)*rate)
            allowed = tokens >= 1
            self.buckets[client] = (tokens-1 if allowed else tokens, now)
            return allowed


class HeartbeatApplication:
    def __init__(self, inbox, health, collector_ids, config=ReceiverConfig(), limiter=None):
        self.inbox, self.health, self.collectors, self.config = inbox, health, frozenset(collector_ids), config
        self.limiter = limiter or TokenBucketLimiter(config)

    def __call__(self, environ, start_response):
        status, value = self.handle(environ)
        labels = {200:'OK',202:'Accepted',400:'Bad Request',403:'Forbidden',404:'Not Found',
            405:'Method Not Allowed',411:'Length Required',413:'Payload Too Large',415:'Unsupported Media Type',
            429:'Too Many Requests',503:'Service Unavailable'}
        body = json.dumps(value, separators=(',',':')).encode('utf-8')
        headers = [('Content-Type','application/json'),('Content-Length',str(len(body))),
                   ('Cache-Control','no-store'),('X-Content-Type-Options','nosniff')]
        if status in (429,503): headers.append(('Retry-After','5'))
        start_response(f'{status} {labels[status]}', headers)
        return [body]

    def handle(self, env):
        try:
            client = ipaddress.ip_address(env.get('REMOTE_ADDR',''))
            if env.get('wsgi.url_scheme') != 'https' and not (self.config.allow_loopback_http and client.is_loopback):
                return 403, {'error':'secure-transport-required'}
            # Never trust client-controlled X-Forwarded-For/Proto here.
            if not self.limiter.allow(str(client)):
                return 429, {'error':'rate-limit'}
            path, method = env.get('PATH_INFO'), env.get('REQUEST_METHOD')
            if env.get('QUERY_STRING'):
                return 400, {'error':'query-not-supported'}
            if path == '/healthz':
                if method != 'GET': return 405, {'error':'method-not-allowed'}
                status = self.health()
                return (200 if status.get('healthy', status['ready']) else 503), status
            if path != '/v1/heartbeat': return 404, {'error':'not-found'}
            if method != 'POST': return 405, {'error':'method-not-allowed'}
            if env.get('HTTP_TRANSFER_ENCODING') or env.get('HTTP_CONTENT_ENCODING'):
                return 400, {'error':'unsupported-framing'}
            content_type = env.get('CONTENT_TYPE','').lower().replace(' ','')
            if content_type not in ('application/json','application/json;charset=utf-8'):
                return 415, {'error':'json-required'}
            length = env.get('CONTENT_LENGTH','')
            if not length: return 411, {'error':'length-required'}
            if not re.fullmatch('[0-9]{1,10}',length): return 400, {'error':'invalid-length'}
            length = int(length)
            if length > self.config.max_payload_bytes: return 413, {'error':'payload-too-large'}
            if not length: return 400, {'error':'empty-payload'}
            payload = env['wsgi.input'].read(length)
            if len(payload) != length: return 400, {'error':'incomplete-payload'}
            heartbeat = Heartbeat.decode(payload)
            if heartbeat.snapshot.collector_id not in self.collectors:
                return 403, {'error':'sender-rejected'}
            proof = HMACProof.from_headers({name: env.get('HTTP_'+name.upper().replace('-','_'))
                for name in ('X-FX-Key-Id','X-FX-Timestamp','X-FX-Nonce','X-FX-Signature')})
            if not self.health()['ready']:
                return 503, {'error':'monitor-unavailable'}
            try:
                future = self.inbox.submit(payload, proof)
            except ConfigError:
                return 503, {'error':'ingress-capacity'}
            try:
                if future.result(timeout=self.config.acceptance_timeout_seconds) is True:
                    return 202, {'accepted':True}
                return 503, {'error':'receipt-unconfirmed'}
            except TimeoutError:
                future.cancel()
                return 503, {'error':'receipt-unconfirmed'}
            except ReceiverUnavailable:
                return 503, {'error':'monitor-unavailable'}
            except ConfigError:
                return 403, {'error':'sender-rejected'}
        except (ValueError, TypeError, KeyError):
            return 400, {'error':'invalid-request'}
        except Exception:
            return 503, {'error':'monitor-unavailable'}
