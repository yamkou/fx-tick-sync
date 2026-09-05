"""Child process deliberately killed by tests; synthetic marked temp directories only."""
from pathlib import Path
import sqlite3
import sys
from threading import Event

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from phase3d_support import Clock, TestSecrets, snapshot
from fxtick.collectors.monitoring import Channel, NotificationRoute
from fxtick.watchdog.auth import HMACAuthenticator, SenderKeys, sign
from fxtick.watchdog.config import MonitorConfig, NodePolicy
from fxtick.watchdog.heartbeat import Heartbeat
from fxtick.watchdog.monitor import ExternalMonitor
from fxtick.watchdog.store import SQLiteState


def pause():
    print('FIXTURE_READY',flush=True)
    Event().wait()


class DurableSink:
    """Fake downstream: dedup is scoped to (event ID, route ID), not process memory."""
    def __init__(self,path,pause_after_accept=False,idempotent=True):
        self.path=path; self.pause_after_accept=pause_after_accept; self.idempotent=idempotent

    def send(self,event,route):
        db=sqlite3.connect(self.path)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS deliveries(event TEXT,route TEXT,attempts INTEGER,effects INTEGER,PRIMARY KEY(event,route))')
            row=db.execute('SELECT attempts,effects FROM deliveries WHERE event=? AND route=?',(event.event_id,route.route_id)).fetchone()
            attempts,effects=row if row else (0,0)
            effects += 0 if self.idempotent and row else 1
            db.execute('INSERT OR REPLACE INTO deliveries VALUES (?,?,?,?)',(event.event_id,route.route_id,attempts+1,effects))
            db.commit()
        finally: db.close()
        if self.pause_after_accept: pause()
        return True


def build_monitor(root,clock,sink):
    keys=TestSecrets()
    config=MonitorConfig('tokyo-test',(NodePolicy('london-01'),))
    auth=HMACAuthenticator((SenderKeys('london-01',(('key-test','fixture-key'),),('boot-test',)),),keys,clock)
    state=SQLiteState(root/'monitor.sqlite')
    monitor=ExternalMonitor(config,state,auth,clock,[(NotificationRoute('fixture-push',Channel.PUSH),sink)])
    return state,monitor,keys


def main():
    root=Path(sys.argv[1]).resolve(); mode=sys.argv[2]
    if (root/'phase3d-fixture.marker').read_text()!='synthetic-only': raise RuntimeError('Not a marked fixture')
    clock=Clock(); sink=DurableSink(root/'downstream.sqlite',pause_after_accept=mode in ('after-send','after-send-no-dedup'))
    state,monitor,keys=build_monitor(root,clock,sink)
    heartbeat=Heartbeat(snapshot(clock),'boot-test',1)
    monitor.receive(heartbeat.encode(),sign(heartbeat,'key-test','fixture-key',keys,clock()))
    clock.advance(180); monitor.evaluate()
    if mode=='transaction':
        with state.transaction() as db:
            db.execute('UPDATE monitor_nodes SET sequence=999 WHERE id=?',('london-01',))
            pause()
    elif mode in ('after-send','after-send-no-dedup'):
        monitor.dispatch()
        raise AssertionError('Parent should terminate before local delivery confirmation')
    elif mode=='committed': pause()
    else: raise ValueError('Unknown fixture mode')


if __name__=='__main__': main()
