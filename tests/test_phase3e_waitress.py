"""Repeat the real TCP contract against Waitress on numeric loopback only."""
import importlib.util
from threading import Event
import unittest
from unittest.mock import patch

import test_phase3d_http as contract


class WaitressServer:
    def __init__(self, host, port, app, **unused):
        from waitress import create_server, wasyncore
        if host != '127.0.0.1' or port != 0:
            raise ValueError('Test server must bind ephemeral numeric loopback')
        self.channels = {}
        self.server = create_server(app, host=host, port=port, map=self.channels,
                                    threads=2, asyncore_loop_timeout=.02)
        self.server_port = int(self.server.effective_port)
        self.stopping = Event()
        self.asyncore = wasyncore
        assert self.server.socket.getsockname()[0] == '127.0.0.1'

    def serve_forever(self, poll_interval=.02):
        try:
            while not self.stopping.is_set():
                self.asyncore.loop(timeout=poll_interval, count=1, map=self.channels)
        finally:
            self.server.task_dispatcher.shutdown()
            self.asyncore.close_all(self.channels)

    def shutdown(self):
        self.stopping.set()

    def server_close(self):
        # Cleanup runs on the server thread; the shared fixture joins it.
        pass


@unittest.skipUnless(importlib.util.find_spec('waitress'), 'UNEXECUTED: Waitress unavailable')
class WaitressHTTPIntegrationTests(contract.LocalHTTPIntegrationTests):
    def setUp(self):
        factory = patch('phase3d_support.make_server', WaitressServer)
        factory.start()
        self.addCleanup(factory.stop)
        super().setUp()


if __name__ == '__main__':
    unittest.main()
