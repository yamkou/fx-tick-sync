"""Auditable recovery plans; no executor, subprocess, service or OS restart."""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol
from ..config import ConfigError, logical_id
from ..collectors.health import utc_time
from ..collectors.monitoring import IncidentKey


class RecoveryKind(str,Enum):
    COLLECTOR_RESTART='collector-restart'
    MT5_RESTART='mt5-restart'
    CTRADER_RECONNECT='ctrader-reconnect'
    SERVICE_RESTART='service-restart'


@dataclass(frozen=True)
class RecoveryTarget:
    collector_id: str
    target_id: str
    allowed_actions: tuple[RecoveryKind,...]

    def __post_init__(self):
        logical_id(self.collector_id); logical_id(self.target_id)
        if not isinstance(self.allowed_actions,tuple) or any(not isinstance(v,RecoveryKind) for v in self.allowed_actions):
            raise ConfigError('Recovery actions require an explicit allowlist')


@dataclass(frozen=True)
class RecoveryPlan:
    incident: IncidentKey
    action: RecoveryKind
    target_id: str
    created_at: datetime
    requires_approval: bool=field(default=True,init=False)
    executable: bool=field(default=False,init=False)

    def __post_init__(self):
        if not isinstance(self.incident,IncidentKey) or not isinstance(self.action,RecoveryKind):
            raise ConfigError('Invalid recovery plan')
        logical_id(self.target_id)
        object.__setattr__(self,'created_at',utc_time(self.created_at))


class RecoveryPlanner:
    def __init__(self,targets):
        self.targets=tuple(targets)
        if any(not isinstance(t,RecoveryTarget) for t in self.targets) or len({(t.collector_id,t.target_id) for t in self.targets})!=len(self.targets):
            raise ConfigError('Invalid recovery target registry')

    def plan(self,incident,action,target_id,now):
        for target in self.targets:
            if target.collector_id==incident.collector_id and target.target_id==target_id and action in target.allowed_actions:
                return RecoveryPlan(incident,action,target_id,now)
        raise ConfigError('Recovery target/action not explicitly allowed')


class ApprovedRecoveryExecutor(Protocol):
    def execute_approved(self,plan: RecoveryPlan, approval_reference: str) -> bool:
        """Future adapter must validate approval independently; no implementation here."""
        ...
