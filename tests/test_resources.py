from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from fxtick.config import ConfigError
from fxtick.resources import BoundedBatch, BufferPressure, ResourceMetrics, load_profile, memory_severity
from fxtick.resource_monitor import ResourceObserver


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile('configs/execution.collector.json')

    def metrics(self, memory=4000000000, private=100):
        return ResourceMetrics(datetime.now(timezone.utc).isoformat(),
                               available_memory=memory,process_private_memory=private)

    def test_thresholds_and_unknown_are_not_healthy(self):
        self.assertEqual(memory_severity(self.metrics(100),self.profile),'CRITICAL')
        self.assertEqual(memory_severity(self.metrics(2000000000),self.profile),'WARNING')
        self.assertIsNone(memory_severity(self.metrics(),self.profile))
        self.assertEqual(memory_severity(self.metrics(None),self.profile),'WARNING')

    def test_profile_validation_and_analysis_separation(self):
        self.assertFalse(load_profile('configs/execution.analysis.json').low_resource_mode)
        for changes in ({'sampling_interval_seconds':True},{'sampling_interval_seconds':1},
                        {'memory_critical_bytes':self.profile.memory_warning_bytes},
                        {'growth_windows_seconds':[3600,3600]}):
            with self.assertRaises(ConfigError): replace(self.profile,**changes)

    def test_failed_flush_preserves_all_records_and_rejects_incoming(self):
        profile=replace(self.profile,max_buffer_bytes=4,max_batch_records=2)
        calls=[]
        def sink(records): calls.append(records); return False
        buffer=BoundedBatch(profile,sink)
        buffer.put(b'ab'); buffer.put(b'cd')
        with self.assertRaises(BufferPressure): buffer.put(b'ef')
        self.assertEqual(buffer.records,[b'ab',b'cd']); self.assertEqual(buffer.bytes,4)
        durable=[]
        buffer.sink=lambda records: durable.extend(records) is None
        buffer.put(b'ef'); buffer.flush()
        self.assertEqual(durable,[b'ab',b'cd',b'ef'])
        self.assertEqual(buffer.bytes,0)

    def test_oversized_record_not_accepted(self):
        buffer=BoundedBatch(replace(self.profile,max_buffer_bytes=2),lambda _:True)
        with self.assertRaises(BufferPressure): buffer.put(b'abc')
        self.assertEqual(buffer.records,[])

    def test_sink_exception_keeps_batch_for_idempotent_retry(self):
        def fail(records): raise OSError('synthetic')
        buffer=BoundedBatch(self.profile,fail); buffer.put(b'a')
        with self.assertRaises(BufferPressure): buffer.flush()
        self.assertEqual(buffer.records,[b'a'])

    def test_sampling_dedup_recovery_and_growth(self):
        clock=[0]; samples=[self.metrics(),self.metrics(100,200),self.metrics(100,300),self.metrics(private=400)]
        class Probe:
            def sample(self): return samples.pop(0)
        with tempfile.TemporaryDirectory() as directory:
            observer=ResourceObserver(replace(self.profile,growth_windows_seconds=[30]),Probe(),directory,lambda:clock[0])
            observer.sample_if_due(); observer.sample_if_due()
            self.assertEqual(len(samples),3)
            for value in (30,60,90): clock[0]=value; observer.sample_if_due()
            observer.close()
            records=[json.loads(line) for line in (Path(directory)/'resources.jsonl').read_text().splitlines()]
            self.assertEqual([r['transition'] for r in records],[None,'CRITICAL',None,'RECOVERY'])
            self.assertEqual(records[-1]['growth']['30']['private_bytes_delta'],100)
            self.assertTrue(all(r['automatic_kill'] is False for r in records))

    def test_probe_failure_reports_unknown_without_raising(self):
        class Probe:
            def sample(self): raise PermissionError('private exception')
        with tempfile.TemporaryDirectory() as directory:
            observer=ResourceObserver(self.profile,Probe(),directory)
            self.assertIsNone(observer.sample_if_due().available_memory)
            observer.close()
            content=(Path(directory)/'resources.jsonl').read_text()
            self.assertNotIn('private exception',content)
            self.assertEqual(json.loads(content)['memory_severity'],'WARNING')

    def test_invalid_metrics(self):
        with self.assertRaises(ConfigError): replace(self.metrics(),cpu_percent=float('nan'))

    def test_resource_log_failure_stays_failed_until_successful_sample(self):
        from unittest.mock import patch
        clock=[0]; outer=self
        class Probe:
            def sample(self): return outer.metrics()
        with tempfile.TemporaryDirectory() as directory:
            observer=ResourceObserver(self.profile,Probe(),directory,lambda:clock[0])
            try:
                with patch.object(observer.handler,'_open',side_effect=OSError('synthetic')):
                    with self.assertRaises(OSError): observer.sample_if_due()
                self.assertTrue(observer.logging_failed)
                clock[0]=1; observer.sample_if_due()
                self.assertTrue(observer.logging_failed)
                clock[0]=30; observer.sample_if_due()
                self.assertFalse(observer.logging_failed)
            finally: observer.close()

    def test_terminal_plan_rejects_shared_data_directory(self):
        from fxtick.platform.terminal_start import selected_terminals
        source=json.loads(Path('configs/windows-vps.example.json').read_text())
        source['terminals'][1]['path']=source['terminals'][0]['path']
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'registry.json'; target.write_text(json.dumps(source))
            with self.assertRaises(ValueError): selected_terminals(target,10)

    def test_collector_memory_pressure_does_not_stop_or_throttle_source(self):
        from fxtick.collector import CollectorRuntime
        from fxtick.collectors.fake_source import FakeSourceAdapter
        from fxtick.staging import initialize
        from phase3d_support import Clock
        clock=Clock()
        outer=self
        class Probe:
            calls=0
            def sample(self): self.calls+=1; return outer.metrics(100)
        class Sink:
            beats=[]
            def send(self,beat): self.beats.append(beat); return True
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); config=initialize(root,'deployment/windows-staging/collector.template.json')
            runtime_path=root/'config/runtime.json'
            runtime_path.write_text(json.dumps(dict(schema_version=1,adapter='fake',boot_id='boot-test',
                key_id='key-test',heartbeat_interval_seconds=1)))
            probe=Probe(); sink=Sink()
            runtime=CollectorRuntime(config,runtime_path,FakeSourceAdapter(),sink,clock,lambda:clock().timestamp(),
                                     execution_profile=self.profile,resource_probe=probe)
            try:
                runtime.start()
                for _ in range(5): runtime.step(); clock.advance(1)
                self.assertEqual(len(sink.beats),5)
                self.assertEqual(probe.calls,1)
                self.assertFalse(runtime.stop_event.is_set())
                self.assertEqual(runtime.resource_observer.severity,'CRITICAL')
            finally: runtime.close()


if __name__=='__main__': unittest.main()
