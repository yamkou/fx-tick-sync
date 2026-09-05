"""Transport adapters. Construction/import never sends; credentials stay injected."""
import json
import logging
import math
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from ..config import ConfigError, logical_id
from .monitor import event_dict
from .messages import format_notification


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _endpoint(value):
    try:
        parts = urlsplit(value)
        if (parts.scheme != 'https' or not parts.hostname or parts.username is not None or
            parts.password is not None or parts.fragment or any(ord(c) < 33 for c in value)):
            raise ValueError()
        parts.port
    except (ValueError, TypeError, AttributeError):
        raise ConfigError('Invalid HTTPS destination; value omitted') from None
    return value


class HTTPSPoster:
    """Explicit HTTPS POST, no redirect/proxy inheritance or response-body logging."""
    def __init__(self, timeout_seconds=10):
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 60):
            raise ConfigError('HTTP timeout must be finite and within 60 seconds')
        self.timeout = timeout_seconds

    def post(self, endpoint, body, headers):
        endpoint = _endpoint(endpoint)
        request = Request(endpoint, data=body, headers=headers, method='POST')
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=self.timeout) as response:
            return response.status


class _Destination:
    def __init__(self, endpoint_reference, token_reference, secrets, poster=None):
        logical_id(endpoint_reference)
        if token_reference is not None:
            logical_id(token_reference)
        self.endpoint_reference, self.token_reference = endpoint_reference, token_reference
        self.secrets, self.poster = secrets, poster or HTTPSPoster()

    def post(self, body, event_id):
        try:
            endpoint = _endpoint(self.secrets.get(self.endpoint_reference))
            headers = {'Content-Type': 'application/json', 'Idempotency-Key': event_id}
            if self.token_reference:
                token = self.secrets.get(self.token_reference)
                if not isinstance(token, str) or not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
                    return False
                headers['Authorization'] = 'Bearer ' + token
            status = self.poster.post(endpoint, body, headers)
            return type(status) is int and 200 <= status < 300
        except Exception:
            # URL/query/header/response/exception may contain credentials.
            return False


class GenericWebhookProvider:
    """NotificationProvider for an operator-selected gateway, not a LINE client."""
    def __init__(self, endpoint_reference, secrets, token_reference=None, poster=None):
        self.destination = _Destination(endpoint_reference, token_reference, secrets, poster)

    def send(self, event, route):
        payload = {**event_dict(event), 'route_id': route.route_id, 'channel': route.channel.value,
                   'message': format_notification(event)}
        return self.destination.post(json.dumps(payload, separators=(',', ':')).encode(), event.event_id)


class LoggingNotificationProvider:
    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger('fxtick.watchdog.notification')

    def send(self, event, route):
        # Only typed logical IDs/state, never arbitrary exception or health payload.
        self.logger.info('Monitoring event=%s collector=%s check=%s severity=%s downtime=%s route=%s',
            event.event_id, event.incident.collector_id, event.incident.check.value,
            event.severity.value, event.outage_seconds, route.route_id)
        return True


class FakeNotificationProvider:
    def __init__(self):
        self.deliveries = []
        self.succeed = True

    def send(self, event, route):
        if not self.succeed:
            return False
        self.deliveries.append((event, route))
        return True


class HTTPSHeartbeatTransport:
    """Out-of-band bearer proof; receiver authentication is a separate adapter."""
    def __init__(self, endpoint_reference, token_reference, secrets, poster=None):
        if token_reference is None:
            raise ConfigError('Heartbeat transport requires an authentication reference')
        self.destination = _Destination(endpoint_reference, token_reference, secrets, poster)

    def send(self, heartbeat):
        identity = f'{heartbeat.snapshot.collector_id}-{heartbeat.boot_id}-{heartbeat.sequence}'
        return self.destination.post(heartbeat.encode(), identity)
