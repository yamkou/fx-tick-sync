"""Actual TCP over numeric loopback; no production server/TLS claim."""
from dataclasses import replace
from datetime import timedelta
import json
import sqlite3
import tempfile
import unittest

from phase3d_support import LoopbackMonitor, snapshot, wait_for
from fxtick.collectors.monitoring import Check, Severity
from fxtick.watchdog.auth import sign
from fxtick.watchdog.heartbeat import Heartbeat
from fxtick.watchdog.manager import ManagedCollector, CollectorManager


class LocalHTTPIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root=tempfile.TemporaryDirectory(); self.addCleanup(self.root.cleanup)
        self.local=LoopbackMonitor(self.root.name)
        self.addCleanup(self.local.stop); self.local.start()
        self.beat=Heartbeat(snapshot(self.local.clock),'boot-test',1)

    def test_collector_to_http_auth_sqlite_and_fake_notification(self):
        local=self.local
        class Probe:
            def sample(self,now): return snapshot(local.clock)
        class LocalTransport:
            def send(self,heartbeat): return local.send(heartbeat)[0]==202
        collector=ManagedCollector(local.nodes[0],Probe(),LocalTransport(),'boot-test')
        manager=CollectorManager((collector,),local.clock,lambda:0)
        self.assertTrue(manager.step()['london-01']['heartbeat_sent'])
        self.assertEqual(local.rows('SELECT sequence FROM monitor_nodes')[0][0],1)
        local.clock.advance(120)
        wait_for(lambda:any(e.severity==Severity.WARNING for e in local.sink.events))
        local.clock.advance(60)
        wait_for(lambda:any(e.severity==Severity.CRITICAL for e in local.sink.events))
        for _ in range(3): self.assertEqual(local.request(method='GET',path='/healthz')[0],200)
        self.assertEqual(len([e for e in local.sink.events if e.severity==Severity.CRITICAL]),1)
        local.send(Heartbeat(snapshot(local.clock),'boot-test',2))
        wait_for(lambda:any(e.severity==Severity.RECOVERY for e in local.sink.events))
        self.assertEqual(len([e for e in local.sink.events if e.severity==Severity.RECOVERY]),1)

    def test_invalid_hmac_no_receipt(self):
        proof=replace(sign(self.beat,'key-test','fixture-key',self.local.secrets,self.local.clock()),signature='0'*64)
        status,_=self.local.request(self.beat.encode(),{'Content-Type':'application/json',**proof.headers()})
        self.assertEqual(status,403)
        self.assertIsNone(self.local.rows('SELECT receipt FROM monitor_nodes')[0][0])

    def test_expired_signature(self):
        proof=sign(self.beat,'key-test','fixture-key',self.local.secrets,self.local.clock())
        self.local.clock.advance(91)
        self.assertEqual(self.local.request(self.beat.encode(),{'Content-Type':'application/json',**proof.headers()})[0],403)

    def test_replay_real_http(self):
        self.assertEqual(self.local.send(self.beat)[0],202)
        self.assertEqual(self.local.send(self.beat)[0],403)
        self.assertEqual(self.local.rows('SELECT sequence FROM monitor_nodes')[0][0],1)

    def test_unknown_collector(self):
        beat=replace(self.beat,snapshot=snapshot(self.local.clock,'unknown-node'))
        self.assertEqual(self.local.send(beat)[0],403)

    def test_malformed_and_oversized_payload(self):
        self.assertEqual(self.local.request(b'{')[0],400)
        self.assertEqual(self.local.request(b'x'*65537)[0],413)

    def test_collector_stop_tick_stop_write_stop_recovery(self):
        cases=((dict(collector_alive=False),Check.COLLECTOR,Severity.CRITICAL),
               (dict(last_tick_time=self.local.clock()-timedelta(seconds=181)),Check.LAST_TICK,Severity.WARNING),
               (dict(last_successful_write=self.local.clock()-timedelta(seconds=301)),Check.LAST_WRITE,Severity.WARNING))
        seq=0
        for changes,check,severity in cases:
            seq+=1
            self.assertEqual(self.local.send(Heartbeat(snapshot(self.local.clock,**changes),'boot-test',seq))[0],202)
            wait_for(lambda:any(e.incident.check==check and e.severity==severity for e in self.local.sink.events))
            seq+=1; self.local.send(Heartbeat(snapshot(self.local.clock),'boot-test',seq))
            wait_for(lambda:any(e.incident.check==check and e.severity==Severity.RECOVERY for e in self.local.sink.events))

    def test_provider_outage_health_degraded_then_retries(self):
        self.local.send(self.beat); self.local.sink.fail=True; self.local.clock.advance(180)
        wait_for(lambda:self.local.runtime.health()['notification_state']=='degraded')
        self.assertEqual(self.local.request(method='GET',path='/healthz')[0],503)
        self.local.sink.fail=False; self.local.clock.advance(60)
        wait_for(lambda:self.local.runtime.health()['notification_state']=='healthy')
        self.assertEqual(len(self.local.sink.events),1)

    def test_temporary_sqlite_lock_fails_safe_then_explicit_restart(self):
        self.local.send(self.beat)
        db=sqlite3.connect(self.local.state_path)
        try:
            db.execute('BEGIN IMMEDIATE')
            self.local.runtime.worker.join(7)
            self.assertFalse(self.local.runtime.health()['ready'])
            self.assertEqual(self.local.request(method='GET',path='/healthz')[0],503)
        finally: db.rollback(); db.close()
        # Runtime deliberately needs supervision/restart; it never replaces the DB.
        self.local.stop(); self.local.start()
        self.assertEqual(self.local.rows('SELECT sequence FROM monitor_nodes')[0][0],1)
        self.assertEqual(self.local.send(replace(self.beat,sequence=2))[0],202)


if __name__=='__main__': unittest.main()
