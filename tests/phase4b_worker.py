"""Only used in a marked temporary fixture by the hard-kill integration test."""
from pathlib import Path
import sys
from datetime import datetime

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fxtick.collector import CollectorRuntime
from fxtick.collectors.fake_source import FakeSourceAdapter
from phase4b_support import signed_transport


if __name__ == '__main__':
    root = Path(sys.argv[1])
    if (root/'.phase4b-fixture').read_text() != 'synthetic-only': raise SystemExit(2)
    port = int(sys.argv[2]); now = datetime.fromisoformat(sys.argv[3])
    transport = signed_transport(port, lambda: now)
    class ReadyTransport:
        def send(self, heartbeat):
            result = transport.send(heartbeat)
            if result: print('FIXTURE_READY', flush=True)
            return result
    CollectorRuntime(root/'config/collector.staging.json',root/'config/runtime.staging.json',
                     FakeSourceAdapter(),ReadyTransport(),clock=lambda: now).run()
