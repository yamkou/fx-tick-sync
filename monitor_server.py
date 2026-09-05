"""Explicit private WSGI backend; do not use Python's development HTTP server."""
import argparse
import sys

from fxtick.config import ConfigError
from fxtick.watchdog.production_config import load_production_config
from fxtick.watchdog.http import HeartbeatApplication
from fxtick.watchdog.runtime import MonitorRuntime


def main(argv=None):
    parser=argparse.ArgumentParser(description='Private monitoring backend behind an operator-managed TLS proxy')
    parser.add_argument('--config',required=True)
    parser.add_argument('--check',action='store_true',help='Validate config only; no secrets, DB or listener access')
    args=parser.parse_args(argv)
    try:
        config=load_production_config(args.config)
        if args.check:
            print('Monitoring configuration valid; no runtime started')
            return 0
        try:
            from waitress import serve
        except ImportError:
            print('Production WSGI server unavailable; prepare an approved environment',file=sys.stderr)
            return 2
        runtime=MonitorRuntime(config.monitor,config.state_path,config.senders,config.secrets,
            [(r.route,r.build(config.secrets)) for r in config.routes],
            evaluation_timeout_seconds=config.evaluation_timeout_seconds,auth_window_seconds=config.auth_window_seconds)
        app=HeartbeatApplication(runtime.inbox,runtime.health,(n.collector_id for n in config.monitor.nodes),config.receiver)
        runtime.start()
        try:
            serve(app,host=config.listen_host,port=config.listen_port,threads=8,
                max_request_body_size=config.receiver.max_payload_bytes,max_request_header_size=8192,
                channel_timeout=15,connection_limit=64,clear_untrusted_proxy_headers=True,
                expose_tracebacks=False)
        finally:
            runtime.stop()
        return 0
    except Exception:
        print('Monitor startup failed; details withheld',file=sys.stderr)
        return 2


if __name__=='__main__': raise SystemExit(main())
