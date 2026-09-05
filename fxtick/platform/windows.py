"""Windows terminal contract, not an implementation of process/account control."""
import os
from pathlib import Path
from typing import Protocol

from ..config import Terminal, native_path


class WindowsOnlyError(RuntimeError):
    pass


def terminal_path(terminal: Terminal, config_directory: Path) -> Path:
    if os.name != "nt":
        raise WindowsOnlyError("MT5 terminal runtime requires Windows; Core remains available")
    return native_path(terminal.path, config_directory)


class TerminalAdapter(Protocol):
    def process_alive(self, terminal: Terminal) -> bool | None: ...
    # Actual start/stop, MT5 integration and process probes are Phase 3B+.
