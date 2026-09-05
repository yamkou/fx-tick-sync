import json
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from fxtick.collector import CollectorRuntime, dry_run, load_runtime
from fxtick.collectors.fake_source import FakeSourceAdapter
from fxtick.collectors.sender_state import SenderState
from fxtick.config import ConfigError
from fxtick.staging import initialize
from fxtick.collectors.monitoring import Severity
from fxtick.watchdog.config import NodePolicy
from phase3d_support import Clock, LoopbackMonitor, wait_for
from phase4b_support import signed_transport


class Sink:
    def __init__(self): self.beats=[]; self.succeed=True
    def send(self, beat): self.beats.append(beat); return self.succeed


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()/'collector'
        self.config=initialize(self.root, 'deployment/windows-staging/collector.template.json')
        self.settings=self.root/'config/runtime.staging.json'
        self.settings.write_text(json.dumps(dict(schema_version=1,adapter='fake',boot_id='boot-test',
                                                key_id='key-test',heartbeat_interval_seconds=1)))
        self.clock=Clock(); self.source=FakeSourceAdapter(); self.sink=Sink()

    def runtime(self, source=None, transport=None):
        runtime=CollectorRuntime(self.config,self.settings,source or self.source,transport or self.sink,
                                 clock=self.clock,monotonic=lambda:self.clock().timestamp())
        self.addCleanup(runtime.close)
        return runtime

    def test_startup_and_staging_identity(self):
        runtime=self.runtime(); runtime.start()
        self.assertTrue(runtime.health.collector_alive)
        self.assertIsNone(runtime.health.source_connected)
        runtime.step()
        self.assertEqual(runtime.config.environment.value,'staging')
        self.assertEqual(self.sink.beats[0].snapshot.collector_id,'london-01')
        self.assertTrue((self.root/'state/london-01/sender.sqlite').exists())

    def test_config_failure_before_state(self):
        self.settings.write_text('{"secret":"synthetic"}')
        with self.assertRaises(ConfigError): self.runtime()
        self.assertFalse((self.root/'state/london-01/sender.sqlite').exists())

    def test_fake_ticks_health_and_write_evidence(self):
        runtime=self.runtime(); runtime.start(); runtime.step()
        health=self.sink.beats[-1].snapshot
        self.assertTrue(health.collector_alive); self.assertTrue(health.source_connected)
        self.assertEqual(health.last_tick_time,self.clock())
        self.assertEqual(health.last_successful_write,self.clock())
        self.assertIsNone(health.terminals[0].process_alive)
        self.assertEqual(runtime.state.db.execute('SELECT COUNT(*) FROM synthetic_write').fetchone()[0],1)

    def test_disconnect_reconnect(self):
        runtime=self.runtime(); runtime.start(); runtime.step()
        self.source.mode='disconnect'; self.clock.advance(1); runtime.step()
        self.assertFalse(self.sink.beats[-1].snapshot.source_connected)
        self.source.mode='reconnect'; self.clock.advance(1); runtime.step()
        self.assertTrue(self.sink.beats[-1].snapshot.source_connected)

    def test_stale_tick_does_not_refresh_activity(self):
        runtime=self.runtime(); runtime.start(); runtime.step(); stamp=runtime.last_tick
        self.source.mode='stale-tick'; self.clock.advance(190); runtime.step()
        self.assertEqual(self.sink.beats[-1].snapshot.last_tick_time,stamp)
        self.assertEqual(self.sink.beats[-1].snapshot.last_successful_write,stamp)

    def test_write_failure_keeps_last_success(self):
        runtime=self.runtime(); runtime.start(); runtime.step(); stamp=runtime.last_write
        self.source.mode='write-failure'; self.clock.advance(1); runtime.step()
        health=self.sink.beats[-1].snapshot
        self.assertEqual(health.error_state,'write-failed')
        self.assertEqual(health.last_successful_write,stamp)
        self.assertEqual(health.last_tick_time,self.clock())

    def test_periodic_send_and_durable_sequence(self):
        runtime=self.runtime(); runtime.start(); runtime.step(); runtime.step()
        self.assertEqual(len(self.sink.beats),1)
        self.clock.advance(1); runtime.step()
        self.assertEqual([b.sequence for b in self.sink.beats],[1,2])
        self.assertEqual(runtime.state.db.execute('SELECT sequence FROM sender').fetchone()[0],2)

    def test_failed_send_reservation_is_not_reused(self):
        runtime=self.runtime(); runtime.start(); self.sink.succeed=False; runtime.step(); runtime.close()
        self.clock.advance(1); self.sink.succeed=True
        other=self.runtime(source=FakeSourceAdapter()); other.start(); other.step()
        self.assertEqual([b.sequence for b in self.sink.beats],[1,2])

    def test_second_process_owner_rejected(self):
        runtime=self.runtime(); runtime.start()
        with self.assertRaises(ConfigError): SenderState(self.root/'state/london-01','london-01','boot-test')

    def test_boot_mismatch_and_corruption_never_reset(self):
        runtime=self.runtime(); runtime.start(); runtime.close()
        with self.assertRaises(ConfigError): SenderState(self.root/'state/london-01','london-01','other-boot')
        path=self.root/'state/london-01/sender.sqlite'; path.write_bytes(b'synthetic-corrupt')
        with self.assertRaises(ConfigError): SenderState(path.parent,'london-01','boot-test')
        self.assertEqual(path.read_bytes(),b'synthetic-corrupt')

    def test_reservation_failure_stops_worker(self):
        runtime=self.runtime(); runtime.start()
        with patch.object(runtime.state,'reserve',side_effect=RuntimeError('synthetic hidden')):
            with self.assertRaises(ConfigError): runtime.step()
        self.assertEqual(self.sink.beats,[])

    def test_graceful_worker_shutdown_and_flush(self):
        runtime=self.runtime()
        worker=threading.Thread(target=runtime.run)
        worker.start()
        try: wait_for(lambda:len(self.sink.beats)==1)
        finally: runtime.request_stop(); worker.join(5)
        self.assertFalse(worker.is_alive()); self.assertTrue(self.source.closed)
        self.assertIsNone(runtime.state.db)
        self.assertIn('stopped',(self.root/'logs/london-01/collector.log').read_text())

    def test_collector_exception_closes_all_resources(self):
        self.source.mode='exception'; runtime=self.runtime()
        with self.assertRaises(RuntimeError): runtime.run()
        self.assertTrue(runtime.closed); self.assertTrue(self.source.closed)
        self.assertIsNone(runtime.state.db); self.assertEqual(self.sink.beats,[])

    def test_dry_run_no_files_network_or_secret_requirement(self):
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch('socket.socket.connect',side_effect=AssertionError('network forbidden')):
            result=dry_run(self.config,self.settings)
        self.assertTrue(result['heartbeat_constructed']); self.assertFalse(result['network_sent'])
        self.assertEqual(before,{p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_formal_cli_dry_run(self):
        result=subprocess.run([sys.executable,'-B','-m','fxtick.collector','--config',str(self.config),
                               '--runtime',str(self.settings),'--dry-run'],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0)
        self.assertTrue(json.loads(result.stdout)['health_initialized'])

    def test_real_adapter_config_rejected(self):
        data=json.loads(self.settings.read_text()); data['adapter']='mt5'; self.settings.write_text(json.dumps(data))
        with self.assertRaises(ConfigError): load_runtime(self.settings)

    def test_write_error_persists_until_successful_write(self):
        runtime=self.runtime(); runtime.start(); self.source.mode='write-failure'; runtime.step()
        self.source.mode='stale-tick'; self.clock.advance(1); runtime.step()
        self.assertEqual(runtime.health.error_state,'write-failed')
        self.source.mode='reconnect'; self.clock.advance(1); runtime.step()
        self.assertIsNone(runtime.health.error_state)

    def test_preflight_runtime_config_removes_obsolete_blocker(self):
        from fxtick import preflight
        with patch.object(preflight.platform,'system',return_value='Windows'), \
             patch.object(preflight.platform,'win32_ver',return_value=('2025Server','','','')), \
             patch.dict('os.environ',{'FX_STAGING_HMAC':'synthetic-only-'*4,
                        'FX_STAGING_MONITOR_ENDPOINT':'https://monitor.invalid/v1/heartbeat'}):
            result=preflight.check(self.config,runtime_path=self.settings)
        self.assertTrue(result['ready']); self.assertFalse(result['real_source_ready'])
        self.assertEqual(result['scope'],'fake-staging-only')

    def test_invalid_runtime_keeps_preflight_closed(self):
        from fxtick import preflight
        self.settings.write_text('{}')
        result=preflight.check(self.config,runtime_path=self.settings)
        self.assertFalse(result['ready'])
        self.assertIn({'check':'collector-runtime-config','status':'FAIL'},result['checks'])

    def test_signed_heartbeat_monitor_sqlite(self):
        monitor=LoopbackMonitor(Path(self.temp.name),clock=self.clock,
                                nodes=(NodePolicy('london-01',terminal_ids=('icmarkets-01',)),))
        self.addCleanup(monitor.stop); monitor.start()
        runtime=self.runtime(transport=signed_transport(monitor.port,self.clock)); runtime.start(); runtime.step()
        self.assertEqual(monitor.rows('SELECT sequence FROM monitor_nodes')[0][0],1)

    def test_hard_crash_timeout_restart_single_recovery(self):
        monitor=LoopbackMonitor(Path(self.temp.name),clock=self.clock,
                                nodes=(NodePolicy('london-01',terminal_ids=('icmarkets-01',)),))
        self.addCleanup(monitor.stop); monitor.start()
        (self.root/'.phase4b-fixture').write_text('synthetic-only')
        children=[]
        def launch():
            # Windows venv python.exe is a redirector: killing only that process
            # can leave its interpreter child running. This stdlib-only fixture
            # uses the actual base interpreter so Popen owns the worker itself.
            child=subprocess.Popen([sys._base_executable,'-B','tests/phase4b_worker.py',str(self.root),
                                    str(monitor.port),self.clock().isoformat()],stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,text=True)
            children.append(child)
            ready=queue.Queue()
            reader=threading.Thread(target=lambda:ready.put(child.stdout.readline()),daemon=True); reader.start()
            self.assertEqual(ready.get(timeout=10).strip(),'FIXTURE_READY')
            return child
        try:
            child=launch(); child.kill(); child.wait(timeout=5)
            self.clock.advance(181)
            wait_for(lambda:any(e.severity==Severity.CRITICAL for e in monitor.sink.events))
            launch()
            wait_for(lambda:any(e.severity==Severity.RECOVERY for e in monitor.sink.events))
            self.assertEqual(monitor.rows('SELECT sequence FROM monitor_nodes')[0][0],2)
            for _ in range(3): monitor.request(method='GET',path='/healthz')
            self.assertEqual(sum(e.severity==Severity.RECOVERY for e in monitor.sink.events),1)
        finally:
            for child in children:
                if child.poll() is None: child.kill()
                child.wait(timeout=5); child.stdout.close(); child.stderr.close()


if __name__=='__main__': unittest.main()
