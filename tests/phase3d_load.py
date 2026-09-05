"""Bounded synthetic load measurement, never a production capacity guarantee."""
import argparse
import gc
import json
from pathlib import Path
import platform
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from phase3d_support import Clock, TestSecrets, snapshot
from fxtick.collectors.monitoring import NotificationRoute, Channel, Severity
from fxtick.watchdog.auth import HMACAuthenticator, SenderKeys, sign
from fxtick.watchdog.config import MonitorConfig, NodePolicy
from fxtick.watchdog.heartbeat import Heartbeat
from fxtick.watchdog.monitor import ExternalMonitor
from fxtick.watchdog.providers import FakeNotificationProvider
from fxtick.watchdog.store import SQLiteState


def measure(count):
    if count not in (10,20,50): raise ValueError('Only bounded fixture sizes are supported')
    tracemalloc.start()
    cpu_start=time.process_time(); wall_start=time.perf_counter()
    samples=[]
    with tempfile.TemporaryDirectory(prefix='phase3d-load-') as root:
        path=Path(root)/'monitor.sqlite'
        state=SQLiteState(path)
        try:
            nodes=tuple(NodePolicy(f'collector-{i:02d}') for i in range(1,count+1))
            config=MonitorConfig('tokyo-load-test',nodes)
            clock=Clock(); secrets=TestSecrets(); sink=FakeNotificationProvider()
            senders=tuple(SenderKeys(n.collector_id,(('fixture-key','fixture-key'),),('fixture-boot',)) for n in nodes)
            auth=HMACAuthenticator(senders,secrets,clock)
            routes=[(NotificationRoute('load-fake-push',Channel.PUSH),sink)]
            monitor=ExternalMonitor(config,state,auth,clock,routes)
            heartbeat_start=time.perf_counter()
            for cycle in range(3):
                for node in nodes:
                    beat=Heartbeat(snapshot(clock,node.collector_id),'fixture-boot',cycle+1)
                    monitor.receive(beat.encode(),sign(beat,'fixture-key','fixture-key',secrets,clock()))
                assert monitor.evaluate()==[]
                gc.collect(); samples.append(tracemalloc.get_traced_memory()[0])
                clock.advance(60)
            heartbeat_seconds=time.perf_counter()-heartbeat_start
            clock.advance(120)
            critical=monitor.evaluate()
            assert len(critical)==count and all(e.severity==Severity.CRITICAL for e in critical)
            peak_queue=len(state.pending()); assert peak_queue==count
            assert monitor.dispatch()==count
            clock.advance(1)
            for node in nodes:
                beat=Heartbeat(snapshot(clock,node.collector_id),'fixture-boot',4)
                monitor.receive(beat.encode(),sign(beat,'fixture-key','fixture-key',secrets,clock()))
            recovery=monitor.evaluate()
            assert len(recovery)==count and all(e.severity==Severity.RECOVERY for e in recovery)
            assert monitor.dispatch()==count and state.pending()==[]
            assert len(sink.deliveries)==count*2
            state.close(); state=SQLiteState(path)
            monitor=ExternalMonitor(config,state,auth,clock,routes)
            assert monitor.evaluate()==[]
            assert all(state.latest(n.collector_id).sequence==4 for n in nodes)
            current,peak=tracemalloc.get_traced_memory()
            return {'collectors':count,'authenticated_heartbeats':count*4,
                'critical_events':len(critical),'recovery_events':len(recovery),
                'notification_deliveries':len(sink.deliveries),'peak_pending_queue':peak_queue,
                'pending_after_dispatch':0,'restart_restored_nodes':count,
                'wall_seconds':round(time.perf_counter()-wall_start,6),
                'cpu_seconds':round(time.process_time()-cpu_start,6),
                'healthy_heartbeat_batch_seconds':round(heartbeat_seconds,6),
                'python_heap_after_healthy_cycles_bytes':samples,
                'python_heap_current_bytes':current,'python_heap_peak_bytes':peak,
                'sqlite_bytes':path.stat().st_size}
        finally:
            state.close(); tracemalloc.stop()


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--output',required=True)
    args=parser.parse_args()
    result={'kind':'offline-synthetic-short-load','python':platform.python_version(),
        'os':platform.system(),'memory_metric':'tracemalloc Python allocations, not total process RSS',
        'transport':'direct authenticated receiver (actual loopback HTTP is tested separately)',
        'results':[measure(n) for n in (10,20,50)]}
    with Path(args.output).open('x',encoding='utf-8') as out: json.dump(result,out,indent=2); out.write('\n')
    print(json.dumps(result,indent=2))


if __name__=='__main__': main()
