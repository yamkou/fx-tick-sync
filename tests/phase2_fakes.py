"""Small explicit fakes. These do NOT validate DuckDB/PyArrow/NumPy behavior."""
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
import importlib
import json
import re
import sys


class FakeCon:
    def __init__(self):
        self.sql = []

    def execute(self, sql):
        self.sql.append(sql)
        match = re.search(r"TO '((?:[^']|'')*)'", sql)
        if match:
            Path(match[1].replace("''", "'")).write_bytes(b"synthetic conversion\n")
        return self

    def fetchone(self):
        return (2,)


@contextmanager
def fake_engines():
    duck = ModuleType("duckdb")
    duck.connect = lambda: FakeCon()
    numpy = ModuleType("numpy")
    numpy.dtype = lambda *args: SimpleNamespace(itemsize=60)
    arrow = ModuleType("pyarrow")
    parquet = ModuleType("pyarrow.parquet")
    parquet.read_metadata = lambda path: SimpleNamespace(metadata={
        k.encode(): v.encode() for k, v in json.loads(Path(path).read_text()).get("metadata", {}).items()})
    class Schema:
        metadata = {}
        def with_metadata(self, metadata):
            result = Schema()
            result.metadata = metadata
            return result
    class Reader:
        schema_arrow = Schema()
        def __init__(self, path): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_batches(self): return iter(("synthetic rows",))
    class Writer:
        def __init__(self, path, schema, **kwargs): self.path, self.schema = path, schema
        def __enter__(self): return self
        def write_batch(self, batch): pass
        def __exit__(self, *args):
            Path(self.path).write_text(json.dumps({"metadata": {k.decode(): v.decode() for k, v in self.schema.metadata.items()}}))
    parquet.ParquetFile, parquet.ParquetWriter = Reader, Writer
    arrow.parquet = parquet
    duka = ModuleType("dukascopy_python")
    pandas = ModuleType("pandas")
    dateutil = ModuleType("dateutil")
    relative = ModuleType("dateutil.relativedelta")
    relative.relativedelta = lambda **kwargs: None
    modules = {"duckdb": duck, "numpy": numpy, "pyarrow": arrow, "pyarrow.parquet": parquet,
               "dukascopy_python": duka, "pandas": pandas, "dateutil": dateutil,
               "dateutil.relativedelta": relative}
    names = ("fxtick.duck", "fxtick.mt_export", "fxtick.fetcher", "sync_and_upload")
    saved = {name: sys.modules.pop(name) for name in names if name in sys.modules}
    import fxtick
    attrs = {n: getattr(fxtick, n) for n in ("duck", "mt_export", "fetcher") if hasattr(fxtick, n)}
    for name in attrs:
        delattr(fxtick, name)
    try:
        with patch.dict(sys.modules, modules):
            yield
    finally:
        for name in names:
            sys.modules.pop(name, None)
        sys.modules.update(saved)
        for name in ("duck", "mt_export", "fetcher"):
            if hasattr(fxtick, name): delattr(fxtick, name)
        for name, value in attrs.items(): setattr(fxtick, name, value)


class Request:
    def __init__(self, value): self.value = value
    def execute(self, **kwargs): return self.value


class FakeDrive:
    """Deterministic API state; records every write; no Google modules or network."""
    def __init__(self):
        self.nodes = {
            "private": {"id": "private", "parents": [], "mimeType": "application/vnd.google-apps.folder"},
            "distribution": {"id": "distribution", "parents": [], "mimeType": "application/vnd.google-apps.folder"},
        }
        self.acls = {}
        self.writes = []
        self.mode = "files"

    def files(self): self.mode = "files"; return self
    def permissions(self): self.mode = "permissions"; return self
    def get(self, fileId, **kwargs): return Request(self.nodes[fileId].copy())
    def list(self, **kwargs):
        if self.mode == "permissions":
            return Request({"permissions": self.acls.get(kwargs["fileId"], [{"type": "user", "role": "owner"}])})
        return Request({"files": []})
    def create(self, **kwargs):
        self.writes.append((self.mode, kwargs))
        return Request({"id": "new-" + str(len(self.writes))})
    def update(self, **kwargs): raise AssertionError("Updates forbidden")
    def delete(self, **kwargs): raise AssertionError("Deletes forbidden")
