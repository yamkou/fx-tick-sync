"""Single-terminal staging preparation only; never starts a market collector."""
import argparse
import json
from pathlib import Path

from .config import ConfigError, Environment, SourceType, load_config, logical_id

REFERENCES = {'heartbeat-hmac': 'FX_STAGING_HMAC', 'monitor-endpoint': 'FX_STAGING_MONITOR_ENDPOINT'}


def validate_model(config):
    if (config.environment != Environment.STAGING or len(config.collectors) != 1
            or len(config.terminals) != 1 or len(config.storage) != 1):
        raise ConfigError('Staging requires exactly one collector, terminal and storage')
    collector = config.collectors[0]
    if (collector.source_type != SourceType.MT5 or config.storage[0].kind != 'local'
            or config.storage[0].zone != 'QUARANTINE'):
        raise ConfigError('Staging requires local quarantine MT5 configuration')


def load_staging(path):
    path = Path(path).resolve()
    config = load_config(path)
    validate_model(config)
    roots = config.paths.resolve(path.parent)
    from .config import native_path
    if native_path(config.storage[0].location, path.parent) != roots['data_root']:
        raise ConfigError('Staging storage must match data root')
    # Standard package layout prevents config from pointing preflight writes into
    # an unrelated installation. Symlink/junction resolution occurs in native_path.
    root = path.parent.parent
    for key, directory in (('data_root','data'), ('log_root','logs'),
                           ('temp_root','temp'), ('export_root','exports')):
        if roots[key] != (root / directory).resolve() or roots[key].parent != root:
            raise ConfigError('Staging roots must belong to this deployment')
    if roots['provenance_registry'] != root / 'config' / 'reference-ledger.json':
        raise ConfigError('Staging ledger must remain separate')
    return config, roots, root


def initialize(root, template, collector_id='london-01', terminal_id='icmarkets-01', broker='example-broker'):
    for value in (collector_id, terminal_id, broker):
        logical_id(value)
    root = Path(root).resolve()
    target = root / 'config' / 'collector.staging.json'
    if target.exists():
        raise ConfigError('Existing staging config is never overwritten')
    data = json.loads(Path(template).read_text(encoding='utf-8'))
    data['paths'] = {key: str(root / folder) for key, folder in
                     [('data_root','data'), ('log_root','logs'), ('temp_root','temp'),
                      ('export_root','exports'), ('provenance_registry','config/reference-ledger.json')]}
    data['collectors'][0].update(collector_id=collector_id, broker=broker)
    data['terminals'][0].update(collector_id=collector_id, terminal_id=terminal_id, broker=broker,
                              path=str(root / 'terminals' / terminal_id / 'terminal64.exe'))
    data['storage'][0]['location'] = str(root / 'data')
    from .config import DeploymentConfig
    model = DeploymentConfig.from_dict(data)
    validate_model(model)
    for folder in ('config','data','logs','state','temp','terminals','backup','exports'):
        directory = root / folder
        if directory.is_symlink() or directory.resolve().parent != root:
            raise ConfigError('Deployment directory must not redirect elsewhere')
        child = directory / collector_id
        if child.is_symlink() or child.resolve() != child:
            raise ConfigError('Collector directory must not redirect elsewhere')
    for folder in ('config','data','logs','state','temp','terminals','backup','exports'):
        (root / folder).mkdir(parents=True, exist_ok=True)
    for folder in ('data','logs','state','temp','exports'):
        (root / folder / collector_id).mkdir(exist_ok=True)
    with target.open('x', encoding='utf-8') as dst:
        json.dump(data, dst, indent=2)
        dst.write('\n')
    load_staging(target)
    return target


def dry_run(path, component, write_logs=False):
    if component not in ('collector', 'heartbeat'):
        raise ConfigError('Unsupported dry-run component')
    config, roots, _ = load_staging(path)
    if write_logs:
        from .staging_logging import StagingLogs, Event
        directory = roots['log_root'] / config.collectors[0].collector_id
        if directory.resolve() != directory:
            raise ConfigError('Log directory must not redirect elsewhere')
        with StagingLogs(directory) as logs:
            logs.emit(component, Event.DRY_RUN)
    return {'mode': 'dry-run', 'component': component, 'environment': 'staging',
            'configuration_valid': True, 'collector_id': config.collectors[0].collector_id,
            'terminal_id': config.terminals[0].terminal_id, 'distribution_enabled': False,
            'network_sent': False, 'runtime_ready': False,
            'blocker': 'real-probe-and-durable-sender-entrypoint-required'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--root', required=True)
    init.add_argument('--template', required=True)
    init.add_argument('--collector-id', default='london-01')
    init.add_argument('--terminal-id', default='icmarkets-01')
    init.add_argument('--broker', default='example-broker')
    dry = sub.add_parser('dry-run')
    dry.add_argument('--config', required=True)
    dry.add_argument('--component', choices=('collector','heartbeat'), required=True)
    dry.add_argument('--write-logs', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'init':
            initialize(args.root, args.template, args.collector_id, args.terminal_id, args.broker)
            result = {'configuration_created': True, 'runtime_started': False}
        else:
            result = dry_run(args.config, args.component, args.write_logs)
    except Exception:
        print(json.dumps({'error': 'staging-preparation-failed-values-omitted'}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
