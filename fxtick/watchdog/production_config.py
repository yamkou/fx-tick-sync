"""Production wiring config; no credential values or implicit network startup."""
from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path

from ..config import ConfigError, native_path
from .config import load_monitor_config
from .auth import SenderKeys
from .http import ReceiverConfig
from .delivery_config import DeliveryConfig
from .secrets import EnvironmentSecrets


@dataclass(frozen=True)
class ProductionConfig:
    monitor: object
    state_path: Path
    senders: tuple
    secrets: EnvironmentSecrets
    routes: tuple
    receiver: ReceiverConfig
    listen_host: str
    listen_port: int
    evaluation_timeout_seconds: int
    auth_window_seconds: int


def load_production_config(path):
    def unique(pairs):
        result = {}
        for k,v in pairs:
            if k in result: raise ValueError()
            result[k]=v
        return result
    try:
        path=Path(path).resolve()
        data=json.loads(path.read_text(encoding='utf-8'),object_pairs_hook=unique)
        fields={'schema_version','monitor_config','state_path','senders','secret_environment',
                'routes','receiver','listen_host','listen_port','evaluation_timeout_seconds','auth_window_seconds'}
        if set(data)!=fields or type(data['schema_version']) is not int or data['schema_version']!=1: raise ValueError()
        # The bundled runner is a private backend; TLS belongs at an explicitly
        # configured reverse proxy. No wildcard/public bind in example runner.
        if not ipaddress.ip_address(data['listen_host']).is_loopback: raise ValueError()
        if type(data['listen_port']) is not int or not 1024<=data['listen_port']<=65535: raise ValueError()
        if type(data['evaluation_timeout_seconds']) is not int or not 2<=data['evaluation_timeout_seconds']<=300: raise ValueError()
        if type(data['auth_window_seconds']) is not int or not 1<=data['auth_window_seconds']<=300: raise ValueError()
        if not isinstance(data['receiver'],dict) or set(data['receiver'])!=set(ReceiverConfig.__dataclass_fields__): raise ValueError()
        receiver=ReceiverConfig(**data['receiver'])
        if not isinstance(data['senders'],list) or not isinstance(data['routes'],list): raise ValueError()
        senders=[]
        for sender in data['senders']:
            if set(sender)!={'collector_id','keys','boot_ids'} or not isinstance(sender['keys'],list) or not isinstance(sender['boot_ids'],list): raise ValueError()
            if any(not isinstance(pair,list) for pair in sender['keys']): raise ValueError()
            senders.append(SenderKeys(sender['collector_id'],tuple(tuple(pair) for pair in sender['keys']),tuple(sender['boot_ids'])))
        if len({s.collector_id for s in senders})!=len(senders): raise ValueError()
        monitor=load_monitor_config(native_path(data['monitor_config'],path.parent))
        if {s.collector_id for s in senders}!={n.collector_id for n in monitor.nodes}: raise ValueError()
        if not isinstance(data['secret_environment'],dict): raise ValueError()
        secrets=EnvironmentSecrets(data['secret_environment'])
        routes=tuple(DeliveryConfig.from_dict(v) for v in data['routes'])
        if len({r.route.route_id for r in routes})!=len(routes): raise ValueError()
        references={ref for s in senders for _,ref in s.keys}
        references.update(ref for r in routes for ref in (r.endpoint_reference,r.token_reference) if ref)
        if not references.issubset(secrets.references): raise ValueError()
        return ProductionConfig(monitor,native_path(data['state_path'],path.parent),tuple(senders),secrets,routes,
            receiver,data['listen_host'],data['listen_port'],data['evaluation_timeout_seconds'],data['auth_window_seconds'])
    except (ValueError,TypeError,KeyError,UnicodeError):
        raise ConfigError('Invalid production monitoring configuration; values omitted') from None
