"""Strict non-secret deployment configuration; standard library, no runtime I/O.

Loading configuration creates no directories, starts no collectors and grants no
source permissions. Relative paths are anchored to the configuration directory.
Foreign absolute paths can be inspected lexically, never silently used locally.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
import re


class ConfigError(ValueError):
    """Messages omit input values: invalid files may accidentally contain secrets."""


class Environment(str, Enum):
    DEVELOPMENT = "development"
    TESTING = "testing"
    STAGING = "staging"
    PRODUCTION = "production"


class SourceType(str, Enum):
    DUKASCOPY = "dukascopy"
    CTRADER = "ctrader"
    MT5 = "mt5"
    LOCAL = "local"


def logical_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", value) or len(value) > 63:
        raise ConfigError("Logical IDs require 1-63 lowercase ASCII letters/digits and single hyphens")
    return value


def _shape(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields.split()):
        raise ConfigError("Missing or unsupported configuration fields; credentials are not configuration")


def _enum(kind, value):
    try:
        return kind(value)
    except (ValueError, TypeError):
        raise ConfigError("Unsupported configuration enum") from None


def path_text(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ConfigError("Path must be a nonempty explicit string")
    if any(ord(c) < 32 for c in value) or any(c in value for c in ("$", "%", "~", "*", "?")):
        raise ConfigError("Implicit expansion, control characters and glob paths are not supported")
    win = PureWindowsPath(value)
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
        raise ConfigError("URLs and embedded credentials are not filesystem paths")
    if value.startswith(("\\\\?\\", "\\\\.\\")) or (win.drive and not win.is_absolute()):
        raise ConfigError("Device and drive-relative paths are not supported")
    if ".." in win.parts or ".." in PurePosixPath(value).parts:
        raise ConfigError("Parent traversal is not supported in deployment paths")
    components = win.parts[1:] if win.anchor else win.parts
    if any(any(c in part for c in '<>:"|') or part.rstrip(" .") != part
           or PureWindowsPath(part).is_reserved() for part in components):
        raise ConfigError("Nonportable or reserved path component")
    return value


def resolve_path(value: str, base: PurePath) -> PurePath:
    """Pure lexical resolution for native or explicitly selected target OS."""
    path_text(value)
    if not isinstance(base, PurePath) or not base.is_absolute():
        raise ConfigError("An absolute configuration directory is required")
    win = PureWindowsPath(value)
    if isinstance(base, PureWindowsPath):
        if value.startswith("/") and not value.startswith("//"):
            raise ConfigError("POSIX absolute path cannot be used as a Windows path")
        if win.root and not win.drive:
            raise ConfigError("Windows rooted path needs an explicit drive")
        return win if win.is_absolute() else base / win
    if win.drive or "\\" in value:
        raise ConfigError("Windows paths require a Windows target or an explicit configuration change")
    posix = PurePosixPath(value)
    return posix if posix.is_absolute() else base / posix


def native_path(value: str, base: Path) -> Path:
    """Resolve only host-compatible paths, without creating or modifying anything."""
    return Path(resolve_path(value, base.resolve())).resolve()


@dataclass(frozen=True)
class RootPaths:
    data_root: str
    temp_root: str
    log_root: str
    export_root: str
    provenance_registry: str

    def __post_init__(self):
        for value in self.__dict__.values():
            path_text(value)

    def resolve(self, base: Path):
        result = {key: native_path(value, base) for key, value in self.__dict__.items()}
        # Separate operational roots, including nesting, so temporary cleanup
        # can never encompass history, exports, logs or the legacy registry.
        roots = [result[k] for k in ("data_root", "temp_root", "log_root", "export_root")]
        for index, left in enumerate(roots):
            for right in roots[index + 1:]:
                if left == right or left in right.parents or right in left.parents:
                    raise ConfigError("Operational roots must be disjoint")
        registry = result["provenance_registry"]
        if any(registry == root or root in registry.parents for root in roots):
            raise ConfigError("Keep provenance registry outside operational roots")
        return result


@dataclass(frozen=True)
class Terminal:
    terminal_id: str
    collector_id: str
    broker: str
    path: str

    def __post_init__(self):
        for value in (self.terminal_id, self.collector_id, self.broker):
            logical_id(value)
        path_text(self.path)
        if PurePosixPath(self.path).is_absolute() and not PureWindowsPath(self.path).is_absolute():
            raise ConfigError("MT5 terminal paths must be Windows or portable relative paths")


@dataclass(frozen=True)
class StorageDestination:
    storage_id: str
    kind: str
    zone: str
    location: str

    def __post_init__(self):
        logical_id(self.storage_id)
        if self.kind not in ("local", "gdrive") or self.zone not in ("PRIVATE_REFERENCE", "QUARANTINE", "DISTRIBUTION"):
            raise ConfigError("Invalid storage destination")
        if self.kind == "local":
            path_text(self.location)
        else:
            # Symbolic destination only. Actual Drive roots stay in the existing
            # operator configuration; this cannot bypass Phase 2 Drive checks.
            logical_id(self.location)


@dataclass(frozen=True)
class Collector:
    collector_id: str
    location: str
    source_type: SourceType
    broker: str
    symbols: tuple[str, ...]
    storage_destination: str

    def __post_init__(self):
        for value in (self.collector_id, self.location, self.broker, self.storage_destination):
            logical_id(value)
        if not isinstance(self.source_type, SourceType):
            raise ConfigError("source_type must be a SourceType")
        if not isinstance(self.symbols, tuple) or not self.symbols or any(not isinstance(s, str) for s in self.symbols) or len(set(self.symbols)) != len(self.symbols):
            raise ConfigError("Symbols must be a nonempty unique tuple")
        if any(not isinstance(s, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}", s) for s in self.symbols):
            raise ConfigError("Invalid logical symbol")


@dataclass(frozen=True)
class DeploymentConfig:
    environment: Environment
    paths: RootPaths
    collectors: tuple[Collector, ...]
    terminals: tuple[Terminal, ...]
    storage: tuple[StorageDestination, ...]

    def __post_init__(self):
        if not isinstance(self.environment, Environment) or not isinstance(self.paths, RootPaths):
            raise ConfigError("Invalid deployment model")
        for entries, kind, key in ((self.collectors, Collector, "collector_id"),
                                    (self.terminals, Terminal, "terminal_id"),
                                    (self.storage, StorageDestination, "storage_id")):
            if not isinstance(entries, tuple) or any(not isinstance(v, kind) for v in entries):
                raise ConfigError("Invalid registry")
            if len({getattr(v, key) for v in entries}) != len(entries):
                raise ConfigError("Duplicate logical ID in registry")
        if not self.collectors or not self.storage:
            raise ConfigError("At least one collector and storage destination are required")
        nodes = {c.collector_id: c for c in self.collectors}
        destinations = {s.storage_id: s for s in self.storage}
        occupied = set()
        for terminal in self.terminals:
            collector = nodes.get(terminal.collector_id)
            if collector is None or collector.source_type != SourceType.MT5 or collector.broker != terminal.broker:
                raise ConfigError("Terminal must match an MT5 collector and broker")
            location = (terminal.collector_id, str(PureWindowsPath(terminal.path)).casefold())
            if location in occupied:
                raise ConfigError("Duplicate terminal installation on one collector")
            occupied.add(location)
        for collector in self.collectors:
            if collector.storage_destination not in destinations:
                raise ConfigError("Unknown storage destination")
            if collector.source_type == SourceType.MT5 and not any(t.collector_id == collector.collector_id for t in self.terminals):
                raise ConfigError("MT5 collector requires a terminal registration")

    def collector(self, collector_id):
        logical_id(collector_id)
        for collector in self.collectors:
            if collector.collector_id == collector_id:
                return collector
        raise ConfigError("Unknown collector ID")

    @classmethod
    def from_dict(cls, value):
        _shape(value, "schema_version environment paths collectors terminals storage")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ConfigError("Unsupported deployment schema")
        _shape(value["paths"], "data_root temp_root log_root export_root provenance_registry")
        for key in ("collectors", "terminals", "storage"):
            if not isinstance(value[key], list):
                raise ConfigError("Registry must be a JSON array")
        collectors = []
        for item in value["collectors"]:
            _shape(item, "collector_id location source_type broker symbols storage_destination")
            if not isinstance(item["symbols"], list):
                raise ConfigError("Symbols must be a JSON array")
            collectors.append(Collector(**{**item, "source_type": _enum(SourceType, item["source_type"]), "symbols": tuple(item["symbols"])}))
        terminals = []
        for item in value["terminals"]:
            _shape(item, "terminal_id collector_id broker path")
            terminals.append(Terminal(**item))
        storage = []
        for item in value["storage"]:
            _shape(item, "storage_id kind zone location")
            storage.append(StorageDestination(**item))
        return cls(_enum(Environment, value["environment"]), RootPaths(**value["paths"]),
                   tuple(collectors), tuple(terminals), tuple(storage))


def load_config(path: str | Path) -> DeploymentConfig:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ConfigError("Duplicate configuration key")
            result[key] = value
        return result
    try:
        with Path(path).open(encoding="utf-8") as src:
            value = json.load(src, object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise ConfigError("Invalid deployment configuration; values omitted") from None
    return DeploymentConfig.from_dict(value)
