"""Synthetic offline demo: temporary state, fake notification, no HTTP listener."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile

from fxtick.collectors.health import HealthSnapshot
from fxtick.collectors.monitoring import NotificationRoute, Channel
from fxtick.watchdog.config import MonitorConfig, NodePolicy
from fxtick.watchdog.heartbeat import Heartbeat
from fxtick.watchdog.monitor import ExternalMonitor, event_dict
from fxtick.watchdog.providers import FakeNotificationProvider
from fxtick.watchdog.store import SQLiteState


def demonstrate():
    class DemoAuth:
        def verify(self, collector_id, boot_id, payload, proof):
            return proof == 'offline-demo-only'
    now = datetime(2026, 9, 7, tzinfo=timezone.utc)
    provider = FakeNotificationProvider()
    config = MonitorConfig('tokyo-monitor-01', (NodePolicy('london-01'), NodePolicy('london-02')))
    route = NotificationRoute('offline-demo', Channel.PUSH)
    with tempfile.TemporaryDirectory(prefix='fx-monitor-demo-') as root:
        state = SQLiteState(str(Path(root) / 'synthetic.sqlite'))
        try:
            monitor = ExternalMonitor(config, state, DemoAuth(), lambda: now, [(route, provider)])
            for second in (0, 60, 120, 180, 240, 300, 360, 420, 480, 540, 600, 622):
                now = datetime(2026, 9, 7, tzinfo=timezone.utc) + timedelta(seconds=second)
                for collector in ('london-01', 'london-02'):
                    if collector == 'london-01' and second not in (0, 622):
                        continue
                    snapshot = HealthSnapshot(collector, now, True, now, now, True, 10**12, True)
                    monitor.receive(Heartbeat(snapshot, 'demo-boot', second).encode(), 'offline-demo-only')
                monitor.run_once()
        finally:
            state.close()
    return {'mode': 'offline-synthetic', 'network_access': False,
        'events': [event_dict(event) for event, _ in provider.deliveries]}


if __name__ == '__main__':
    print(json.dumps(demonstrate(), indent=2))
