from fxtick.watchdog.auth import SignedHeartbeatTransport
from phase3d_support import TestSecrets
import http.client


class Secrets(TestSecrets):
    def get(self, reference):
        if reference == 'endpoint': return 'https://monitor.invalid/v1/heartbeat'
        return super().get(reference)


class LoopbackPoster:
    def __init__(self, port): self.port = port
    def post(self, endpoint, body, headers):
        assert endpoint == 'https://monitor.invalid/v1/heartbeat'
        con = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            con.request('POST', '/v1/heartbeat', body, headers)
            response = con.getresponse(); response.read()
            return response.status
        finally: con.close()


def signed_transport(port, clock):
    return SignedHeartbeatTransport('endpoint', 'key-test', 'fixture-key', Secrets(), clock,
                                    poster=LoopbackPoster(port))
