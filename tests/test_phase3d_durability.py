"""Hard-kill only child processes created here; never touch an external process."""
from pathlib import Path
from queue import Queue
import json
import sqlite3
import subprocess
import sys
import tempfile
from threading import Thread
import unittest

from phase3d_support import Clock, snapshot
from phase3d_crash_worker import DurableSink, build_monitor
from fxtick.config import ConfigError
from fxtick.collectors.monitoring import Severity
from fxtick.watchdog.auth import sign
from fxtick.watchdog.heartbeat import Heartbeat
from fxtick.watchdog.store import SQLiteState


class DurabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); (self.root/'phase3d-fixture.marker').write_text('synthetic-only')

    def crash(self,mode):
        command=[sys.executable,'-B',str(Path(__file__).with_name('phase3d_crash_worker.py')),str(self.root),mode]
        child=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            lines=Queue()
            reader=Thread(target=lambda:lines.put(child.stdout.readline()),daemon=True); reader.start()
            self.assertEqual(lines.get(timeout=5).strip(),'FIXTURE_READY')
            child.kill()  # Terminate only this synthetic fixture, without Python cleanup.
            child.wait(timeout=5)
            self.assertNotEqual(child.returncode,0)
        finally:
            if child.poll() is None: child.kill(); child.wait(timeout=5)
            child.stdout.close(); child.stderr.close()

    def reopen(self,idempotent=True):
        clock=Clock(); clock.advance(241)
        state,monitor,keys=build_monitor(self.root,clock,DurableSink(self.root/'downstream.sqlite',idempotent=idempotent))
        self.addCleanup(state.close)
        return clock,state,monitor,keys

    def test_force_kill_restores_last_heartbeat_and_critical_state(self):
        self.crash('committed')
        clock,state,monitor,keys=self.reopen()
        self.assertEqual(state.latest('london-01').sequence,1)
        rows=state.connection.execute('SELECT state FROM monitor_incidents').fetchall()
        self.assertEqual(json.loads(rows[0][0])['current_state'],'CRITICAL')
        self.assertEqual(monitor.evaluate(),[])

    def test_force_kill_rolls_back_uncommitted_sqlite_transaction(self):
        self.crash('transaction')
        _,state,monitor,_=self.reopen()
        self.assertTrue(state.check())
        self.assertEqual(state.latest('london-01').sequence,1)
        self.assertEqual(monitor.evaluate(),[])

    def downstream(self):
        db=sqlite3.connect(self.root/'downstream.sqlite')
        try: return db.execute('SELECT event,attempts,effects FROM deliveries').fetchall()
        finally: db.close()

    def test_crash_after_accept_retries_same_id_and_dedup_sink_applies_once(self):
        self.crash('after-send')
        before=self.downstream(); self.assertEqual(before[0][1:],(1,1))
        _,state,monitor,_=self.reopen()
        self.assertEqual(len(state.pending()),1)
        self.assertEqual(monitor.dispatch(),1)
        after=self.downstream()
        self.assertEqual(after[0][0],before[0][0])
        self.assertEqual(after[0][1:],(2,1))
        self.assertEqual(state.pending(),[])

    def test_non_idempotent_sink_demonstrates_duplicate_delivery_risk(self):
        self.crash('after-send-no-dedup')
        _,_,monitor,_=self.reopen(idempotent=False)
        monitor.dispatch()
        self.assertEqual(self.downstream()[0][1:],(2,2))

    def test_recovery_not_repeated_after_restart_backup_restore(self):
        self.crash('committed')
        clock,state,monitor,keys=self.reopen()
        beat=Heartbeat(snapshot(clock),'boot-test',2)
        monitor.receive(beat.encode(),sign(beat,'key-test','fixture-key',keys,clock()))
        self.assertEqual([e.severity for e in monitor.evaluate()],[Severity.RECOVERY])
        monitor.dispatch()
        backup=self.root/'backup.sqlite'; state.backup_to(backup)
        restored=SQLiteState(backup); self.addCleanup(restored.close)
        from fxtick.watchdog.monitor import ExternalMonitor
        restart=ExternalMonitor(monitor.config,restored,monitor.authenticator,clock,monitor.routes.items())
        self.assertEqual(restart.evaluate(),[])
        self.assertEqual(restored.latest('london-01').sequence,2)
        self.assertEqual(restored.pending(),[])
        self.assertTrue(restored.check())

    def test_corrupt_backup_is_rejected_without_replacement(self):
        path=self.root/'corrupt.sqlite'; data=b'corrupted-synthetic-state'; path.write_bytes(data)
        with self.assertRaises(ConfigError): SQLiteState(path)
        self.assertEqual(path.read_bytes(),data)


if __name__=='__main__': unittest.main()
