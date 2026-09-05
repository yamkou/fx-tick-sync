"""Explicit synthetic adapter: never connects to a terminal or market service."""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SourceObservation:
    connected: bool
    tick: bool
    write_failure: bool = False


class SourceAdapter(Protocol):
    def poll(self, now) -> SourceObservation: ...
    def close(self): ...


class FakeSourceAdapter:
    MODES = ('normal', 'disconnect', 'stale-tick', 'write-failure', 'reconnect', 'exception')

    def __init__(self, mode='normal'):
        self.mode = mode
        self.closed = False

    def poll(self, now):
        if self.closed or self.mode not in self.MODES:
            raise RuntimeError('Invalid synthetic adapter state')
        if self.mode == 'exception':
            raise RuntimeError('Synthetic collector exception')
        return SourceObservation(self.mode != 'disconnect',
                                 self.mode in ('normal', 'reconnect', 'write-failure'),
                                 self.mode == 'write-failure')

    def close(self):
        self.closed = True
