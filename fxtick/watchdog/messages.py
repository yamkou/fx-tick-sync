"""Small typed smartphone messages; never forward raw exception or market data."""
from dataclasses import dataclass
from datetime import datetime
from ..config import ConfigError
from ..collectors.health import utc_time
from ..collectors.monitoring import Check, Severity


@dataclass(frozen=True)
class AlertObservation:
    last_heartbeat: datetime | None = None
    last_tick: datetime | None = None
    last_write: datetime | None = None
    threshold_seconds: int | None = None

    def __post_init__(self):
        for name in ('last_heartbeat','last_tick','last_write'):
            object.__setattr__(self,name,utc_time(getattr(self,name),optional=True))
        if self.threshold_seconds is not None and (type(self.threshold_seconds) is not int or self.threshold_seconds<=0):
            raise ConfigError('Invalid alert threshold')

    def to_dict(self):
        return {name:(value.isoformat() if isinstance(value,datetime) else value) for name,value in self.__dict__.items()}

    @classmethod
    def from_dict(cls,data):
        if not isinstance(data,dict) or set(data)!=set(cls.__dataclass_fields__):
            raise ConfigError('Invalid alert observation')
        try:
            return cls(**{name:(datetime.fromisoformat(value) if name!='threshold_seconds' and value is not None else value)
                          for name,value in data.items()})
        except (ValueError,TypeError):
            raise ConfigError('Invalid alert observation') from None


def duration(seconds):
    seconds=seconds or 0
    hours,remaining=divmod(seconds,3600); minutes,seconds=divmod(remaining,60)
    return (f'{hours}h' if hours else '')+f'{minutes}m{seconds:02d}s'


def format_notification(event):
    node=event.incident.collector_id
    if event.severity==Severity.RECOVERY:
        title='ONLINE' if event.incident.check==Check.HEARTBEAT else event.incident.check.value.upper()+' RECOVERED'
        return (f'[RECOVERY] {node} {title}\nCollector: {node}\nCheck: {event.incident.check.value}\n'
                f'Downtime: {duration(event.outage_seconds)}\nRecovered: {event.occurred_at.strftime("%Y-%m-%d %H:%M:%S UTC")}')
    down=event.incident.check==Check.HEARTBEAT and event.severity==Severity.CRITICAL
    title='DOWN' if down else event.incident.check.value.upper()
    observation=getattr(event,'observation',None) or AlertObservation()
    def stamp(value): return value.strftime('%Y-%m-%d %H:%M:%S UTC') if value else 'UNKNOWN'
    reason={Check.HEARTBEAT:'heartbeat timeout',Check.LAST_TICK:'tick stale',Check.LAST_WRITE:'write stale',
            Check.DISK:'disk capacity/access anomaly',Check.SOURCE:'source connection anomaly',
            Check.TERMINAL:'terminal process anomaly',Check.COLLECTOR:'collector health anomaly'}[event.incident.check]
    activity={Check.LAST_TICK:observation.last_tick,Check.LAST_WRITE:observation.last_write}
    if event.incident.check in activity and activity[event.incident.check] is None:
        reason=event.incident.check.value+' observation unavailable'
    elif observation.threshold_seconds:
        reason+=f' >= {observation.threshold_seconds} sec'
    return (f'[{event.severity.value}] {node} {title}\nCollector: {node}\n'
            f'Last heartbeat: {stamp(observation.last_heartbeat)}\nLast tick: {stamp(observation.last_tick)}\n'
            f'Last write: {stamp(observation.last_write)}\nReason: {reason}')
