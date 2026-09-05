"""Readiness checks with fixed output codes; no credential values or auto repair."""
import argparse
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler

from .config import native_path
from .staging import load_staging, REFERENCES
from .watchdog.providers import _NoRedirect
from .watchdog.secrets import EnvironmentSecrets


def endpoint_valid(value):
    try:
        p = urlsplit(value)
        return (p.scheme == 'https' and bool(p.hostname) and p.username is None
                and p.password is None and not p.query and not p.fragment
                and p.path == '/v1/heartbeat' and (p.port is None or 1 <= p.port <= 65535)
                and not any(ord(c) <= 32 for c in value))
    except (ValueError, TypeError):
        return False


def outbound_check(endpoint):
    # Explicit opt-in GET only, no auth header, proxy or redirect; no heartbeat POST.
    p = urlsplit(endpoint)
    request = Request(f'https://{p.netloc}/healthz', method='GET')
    with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=5) as response:
        return response.status == 200


def writable(directory):
    if not directory.is_dir():
        return False
    # Touch only a uniquely named probe created by this invocation, then remove it.
    with tempfile.TemporaryFile(prefix='fxtick-preflight-', dir=directory) as probe:
        probe.write(b'preflight'); probe.flush(); os.fsync(probe.fileno())
    return True


def check(config_path, check_network=False, minimum_free_bytes=5 * 1024**3):
    results = []
    def record(name, operation):
        try:
            ok = operation() is True
        except Exception:
            ok = False
        results.append({'check': name, 'status': 'PASS' if ok else 'FAIL'})
        return ok
    record('windows-server-2025', lambda: platform.system() == 'Windows'
           and platform.win32_ver()[0] == '2025Server')
    record('x64', lambda: struct.calcsize('P') == 8 and platform.machine().lower() in ('amd64','x86_64'))
    record('python-3.12', lambda: sys.version_info[:2] == (3,12))
    record('git', lambda: shutil.which('git') is not None)
    repo = Path(__file__).resolve().parents[1]
    record('dedicated-venv', lambda: Path(sys.prefix).resolve() == repo / '.venv'
           and sys.prefix != sys.base_prefix
           and 'include-system-site-packages = false' in (repo / '.venv/pyvenv.cfg').read_text())
    # Validate against tested constraints, not merely successful imports.
    try:
        from packaging.requirements import Requirement
        for line in (repo / 'constraints-windows-py312.txt').read_text(encoding='utf-8').splitlines():
            if not line or line.startswith('#'): continue
            requirement = Requirement(line)
            record('package-' + requirement.name, lambda r=requirement:
                   metadata.version(r.name) in r.specifier)
    except Exception:
        record('package-manifest', lambda: False)
    try:
        config, roots, root = load_staging(config_path)
    except Exception:
        record('staging-config', lambda: False)
        return {'ready': False, 'checks': results}
    record('staging-config', lambda: True)
    record('collector-id-and-terminal-registry', lambda: True)
    node = config.collectors[0].collector_id
    for name, directory in [('data',roots['data_root']/node), ('log',roots['log_root']/node),
                            ('state',root/'state'/node)]:
        contained = directory.resolve() == directory and directory.parent.parent == root
        record(name+'-writable', lambda d=directory,c=contained: c and writable(d))
        record(name+'-disk-free', lambda d=directory,c=contained: c and
               shutil.disk_usage(d).free >= minimum_free_bytes)
    terminal = native_path(config.terminals[0].path, Path(config_path).resolve().parent)
    record('mt5-path', lambda: terminal.name.lower() == 'terminal64.exe' and terminal.is_file())
    secrets = EnvironmentSecrets(REFERENCES)
    record('heartbeat-secret-present', lambda: bool(secrets.get('heartbeat-hmac').strip()))
    endpoint = os.environ.get(REFERENCES['monitor-endpoint'], '')
    endpoint_ok = record('monitor-endpoint', lambda: endpoint_valid(endpoint))
    if check_network:
        record('outbound-monitor-health', lambda: endpoint_ok and outbound_check(endpoint))
    else:
        results.append({'check':'outbound-monitor-health','status':'NOT_RUN'})
    # Do not turn passing prerequisites into a misleading real-collector readiness claim.
    record('real-collector-probe-and-durable-sender-entrypoint', lambda: False)
    return {'ready': False, 'checks': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--check-network', action='store_true')
    args = parser.parse_args()
    try:
        result = check(args.config, args.check_network)
    except Exception:
        result = {'ready':False, 'checks':[{'check':'preflight','status':'FAIL'}]}
    print(json.dumps(result, indent=2))
    return 0 if result['ready'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
