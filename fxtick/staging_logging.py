"""Fixed event vocabulary, UTC timestamps and bounded rotating staging logs.

Never accepts free-form message text, exception objects or credential-bearing args.
One process owns each directory; multiple processes need separate directories.
"""
from enum import Enum
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import time


class Event(str, Enum):
    PREFLIGHT_PASS = 'preflight-pass'
    PREFLIGHT_BLOCKED = 'preflight-blocked'
    DRY_RUN = 'dry-run'
    STARTED = 'started'
    STOPPED = 'stopped'
    FAILED = 'failed'


class StagingLogs:
    def __init__(self, directory, max_bytes=5*1024*1024, backups=5):
        if type(max_bytes) is not int or max_bytes < 64 or type(backups) is not int or not 1 <= backups <= 20:
            raise ValueError('Invalid rotation bounds')
        self.handlers = {}
        formatter = logging.Formatter('%(asctime)sZ %(levelname)s %(message)s', '%Y-%m-%dT%H:%M:%S')
        formatter.converter = time.gmtime
        try:
            for name in ('collector','watchdog','heartbeat','error'):
                for suffix in ('', *(f'.{i}' for i in range(1, backups+1))):
                    target = Path(directory)/(name+'.log'+suffix)
                    if target.is_symlink() or target.resolve().parent != Path(directory).resolve():
                        raise ValueError('Log path must not redirect elsewhere')
                handler = RotatingFileHandler(Path(directory)/(name+'.log'), maxBytes=max_bytes,
                                              backupCount=backups, encoding='utf-8', delay=True)
                handler.setFormatter(formatter)
                self.handlers[name] = handler
        except Exception:
            self.close()
            raise

    def emit(self, channel, event):
        if channel not in self.handlers or not isinstance(event, Event):
            raise ValueError('Only defined staging log events are accepted')
        level = logging.ERROR if event in (Event.FAILED, Event.PREFLIGHT_BLOCKED) else logging.INFO
        record = logging.LogRecord('staging', level, '', 0, event.value, (), None)
        self.handlers[channel].handle(record)
        if level == logging.ERROR and channel != 'error':
            self.handlers['error'].handle(record)

    def close(self):
        for handler in self.handlers.values():
            handler.close()

    def __enter__(self): return self
    def __exit__(self, *unused): self.close()
