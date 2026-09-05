"""Synthetic local-only fixtures. Never use this development server in production."""
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import sqlite3
from socketserver import ThreadingMixIn
from threading import Thread
import time
from wsgiref.simple_server import make_server, WSGIRequestHandler, WSGIServer

from fxtick.collectors.health import HealthSnapshot
from fxtick.collectors.monitoring import Channel, NotificationRoute
from fxtick.watchdog.auth import SenderKeys, sign
from fxtick.watchdog.config import MonitorConfig, NodePolicy
from fxtick.watchdog.http import HeartbeatApplication, ReceiverConfig
from fxtick.watchdog.runtime import MonitorRuntime

START = datetime(2026, 9, 7, tzinfo=timezone.utc)


class Clock:
    def __init__(self): self.now = START
    def __call__(self): return self.now
    def advance(self, seconds): self.now += timedelta(seconds=seconds)


class TestSecrets:
    def get(self, reference):
        if reference != 'fixture-key': raise KeyError('Unknown synthetic reference')
        return 'phase3d-synthetic-only-not-a-production-key'


def snapshot(clock, name='london-01', **changes):
    values = dict(collector_id=name, observed_at=clock(), collector_alive=True,
        last_tick_time=clock(), last_successful_write=clock(), disk_path_accessible=True,
        disk_free_bytes=10**12, source_connected=True)
    values.update(changes)
    return HealthSnapshot(**values)


def wait_for(predicate, timeout=4):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if predicate(): return
        time.sleep(.01)
    raise AssertionError('Synthetic condition did not complete within bounded timeout')


class NotificationSink:
    def __init__(self): self.events=[]; self.fail=False
    def send(self, event, route):
        if self.fail: return False
        self.events.append(event)
        return True


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args): pass


class ThreadedWSGI(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class LoopbackMonitor:
    def __init__(self, root, clock=None, nodes=None):
        self.root=Path(root); self.clock=clock or Clock(); self.secrets=TestSecrets()
        self.nodes=nodes or (NodePolicy('london-01'),)
        self.sink=NotificationSink()
        self.config=MonitorConfig('tokyo-test',self.nodes)
        self.senders=tuple(SenderKeys(n.collector_id,(('key-test','fixture-key'),),('boot-test',)) for n in self.nodes)
        self.state_path=self.root/'monitor.sqlite'
        self.runtime=None; self.server=None; self.thread=None

    def start(self):
        self.runtime=MonitorRuntime(self.config,self.state_path,self.senders,self.secrets,
            [(NotificationRoute('fixture-push',Channel.PUSH),self.sink)],clock=self.clock)
        self.runtime.start()
        wait_for(lambda:self.runtime.health()['ready'])
        app=HeartbeatApplication(self.runtime.inbox,self.runtime.health,(n.collector_id for n in self.nodes),
            ReceiverConfig(burst=1000,requests_per_minute=10000))
        self.server=make_server('127.0.0.1',0,app,server_class=ThreadedWSGI,handler_class=QuietHandler)
        self.port=self.server.server_port
        self.thread=Thread(target=self.server.serve_forever,kwargs={'poll_interval':.02},daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown(); self.server.server_close(); self.thread.join(3)
            self.server=None
        if self.runtime: self.runtime.stop()

    def request(self, body=b'', headers=None, method='POST', path='/v1/heartbeat'):
        # Numeric loopback only: no DNS, proxy inheritance or remote endpoint.
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=6)
        try:
            connection.request(method,path,body=body,headers=headers or {'Content-Type':'application/json'})
            response=connection.getresponse()
            return response.status,json.loads(response.read())
        finally: connection.close()

    def send(self, heartbeat):
        proof=sign(heartbeat,'key-test','fixture-key',self.secrets,self.clock())
        return self.request(heartbeat.encode(),{'Content-Type':'application/json',**proof.headers()})

    def rows(self, sql, args=()):
        db=sqlite3.connect(self.state_path.as_uri()+'?mode=ro',uri=True)
        try: return db.execute(sql,args).fetchall()
        finally: db.close()
