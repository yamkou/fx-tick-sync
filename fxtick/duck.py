"""DuckDB まわりの共通処理。

設計方針
- セッション TimeZone は必ず UTC に固定する（Windows 既定＝Asia/Tokyo で
  CAST(ts AS TIMESTAMP) すると JST になってしまう事故を防ぐ）。
- CSV は型を明示して読む（read_csv_auto による推定ぶれを排除）。
- Parquet は一時ファイルで構築し、新規出力へ排他的に保存する（上書き禁止）。
- 重複排除は DISTINCT（全列一致）。PARTITION BY 定数は使わない。
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from .artifacts import inspect, seal, sidecar, PARQUET_KEY, canonical, IntegrityError, is_parquet
from .query import Query, require_query

# 保存スキーマ（dukascopy_python の tick 出力と同じ列名・順序）
TICK_SCHEMA: dict[str, str] = {
    "timestamp": "TIMESTAMPTZ",
    "bidPrice": "DOUBLE",
    "askPrice": "DOUBLE",
    "bidVolume": "DOUBLE",
    "askVolume": "DOUBLE",
}


def connect(
    threads: int | None = None,
    memory_limit: str | None = None,
    temp_dir: str | os.PathLike | None = None,
) -> duckdb.DuckDBPyConnection:
    """UTC 固定・スピル先付きの DuckDB 接続を返す。"""
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute("SET preserve_insertion_order = false")  # ソート時のメモリ削減
    if threads:
        con.execute(f"SET threads = {int(threads)}")
    if memory_limit:
        con.execute(f"SET memory_limit = '{memory_limit}'")
    tmp = Path(temp_dir) if temp_dir else Path(tempfile.gettempdir()) / "fxtick_duckdb_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory = '{sql_str(tmp)}'")
    return con


def sql_str(value: str | os.PathLike) -> str:
    """SQL 文字列リテラル用エスケープ（パス区切りも / に統一）。"""
    return str(value).replace("\\", "/").replace("'", "''")


def source_sql(path: str | os.PathLike, *, ledger=None) -> Query:
    """Parquet / CSV パス → 型付き読み取り式。"""
    artifact = inspect(path, ledger=ledger)
    p = str(artifact.path)
    if is_parquet(artifact.path):
        return Query(f"read_parquet('{sql_str(p)}')", (artifact,))
    cols = ", ".join(f"'{k}': '{v}'" for k, v in TICK_SCHEMA.items())
    return Query(f"read_csv('{sql_str(p)}', header = true, columns = {{{cols}}})", (artifact,))


def normalized_select(source: str) -> str:
    """どの世代のファイルでも同じ型に揃える SELECT（列名は保存スキーマ準拠が前提）。"""
    if not isinstance(source, Query):
        raise IntegrityError("Normalization requires bound inputs")
    return source.wrap(
        "SELECT CAST(timestamp AS TIMESTAMPTZ) AS timestamp, "
        "CAST(bidPrice AS DOUBLE) AS bidPrice, CAST(askPrice AS DOUBLE) AS askPrice, "
        "CAST(bidVolume AS DOUBLE) AS bidVolume, CAST(askVolume AS DOUBLE) AS askVolume "
        f"FROM {source}"
    )


def union_sources(paths: list[str | os.PathLike], *, ledger=None) -> Query:
    queries = [normalized_select(source_sql(p, ledger=ledger)) for p in paths]
    return Query(" UNION ALL ".join(f"({q})" for q in queries), tuple(a for q in queries for a in q.inputs))


def count_rows(con: duckdb.DuckDBPyConnection, path: str | os.PathLike) -> int:
    return con.execute(f"SELECT count(*) FROM {source_sql(path)}").fetchone()[0]


def max_timestamp(con: duckdb.DuckDBPyConnection, path: str | os.PathLike) -> datetime | None:
    """ファイル内の最終ティック時刻（UTC, tz-aware）。空なら None。"""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    row = con.execute(f"SELECT max(timestamp) FROM ({normalized_select(source_sql(path))})").fetchone()
    ts = row[0] if row else None
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def write_parquet(con: duckdb.DuckDBPyConnection, select_sql: str, out_path: str | os.PathLike) -> int:
    """由来を保持して新規 Parquet へ保存（DISTINCT + UTC timestamp 順）。"""
    lineage = require_query(select_sql)
    out_path = Path(out_path)
    if out_path.exists() or sidecar(out_path).exists():
        raise FileExistsError("Choose a new output; historical artifacts are never overwritten")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(suffix=".parquet", dir=out_path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    fd, annotated_name = tempfile.mkstemp(suffix=".parquet", dir=out_path.parent)
    os.close(fd)
    annotated = Path(annotated_name)
    try:
        con.execute(
            f"COPY (SELECT DISTINCT * FROM ({select_sql}) ORDER BY timestamp) "
            f"TO '{sql_str(tmp)}' (FORMAT PARQUET, COMPRESSION SNAPPY)"
        )
        import pyarrow.parquet as pq
        # Stream batches: do not materialize a historical dataset in memory.
        with pq.ParquetFile(tmp) as src:
            metadata = dict(src.schema_arrow.metadata or {})
            metadata[PARQUET_KEY] = canonical(lineage.payload()).encode()
            schema = src.schema_arrow.with_metadata(metadata)
            with pq.ParquetWriter(annotated, schema, compression="snappy") as writer:
                for batch in src.iter_batches():
                    writer.write_batch(batch)
        require_query(select_sql)
        # Exclusive creation protects existing data even if a path appears meanwhile.
        import shutil
        with annotated.open("rb") as src, out_path.open("xb") as dest:
            shutil.copyfileobj(src, dest)
        seal(out_path, lineage)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
    finally:
        tmp.unlink(missing_ok=True)
        annotated.unlink(missing_ok=True)
    return count_rows(con, out_path)


def merge_to_parquet(
    con: duckdb.DuckDBPyConnection,
    sources: list[str | os.PathLike],
    out_path: str | os.PathLike,
) -> int:
    """複数の Parquet/CSV を重複排除しつつ 1 つの Parquet に統合。"""
    if not sources:
        raise FileNotFoundError("統合対象のファイルがありません")
    # Missing inputs must fail, never silently remove a restrictive parent.
    query = union_sources(sources)
    return write_parquet(con, query.wrap(f"SELECT * FROM ({query})"), out_path)


def csv_to_parquet(con: duckdb.DuckDBPyConnection, csv_path: str | os.PathLike, parquet_path: str | os.PathLike) -> int:
    return merge_to_parquet(con, [csv_path], parquet_path)


def month_range(con: duckdb.DuckDBPyConnection, path: str | os.PathLike) -> list[tuple[int, int]]:
    """ファイルに含まれる (年, 月) の一覧（UTC 基準）。"""
    rows = con.execute(
        "SELECT DISTINCT year(timezone('UTC', timestamp))::INT, month(timezone('UTC', timestamp))::INT "
        f"FROM ({normalized_select(source_sql(path))}) ORDER BY 1, 2"
    ).fetchall()
    return [(int(y), int(m)) for y, m in rows]


def month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1, tzinfo=timezone.utc)
    return start, end


def filter_month_sql(select_sql: str, year: int, month: int) -> str:
    """select_sql の結果を UTC の (year, month) に絞る。"""
    s, e = month_bounds(year, month)
    if not isinstance(select_sql, Query):
        raise IntegrityError("Month filter requires bound inputs")
    return select_sql.wrap(
        f"SELECT * FROM ({select_sql}) WHERE timestamp >= TIMESTAMPTZ '{s:%Y-%m-%d %H:%M:%S}+00' "
        f"AND timestamp < TIMESTAMPTZ '{e:%Y-%m-%d %H:%M:%S}+00'"
    )


def merge_month(con: duckdb.DuckDBPyConnection, new_path: str | os.PathLike, year: int, month: int,
                existing: list[str | os.PathLike], out_path: str | os.PathLike) -> int:
    """new_path から該当月だけを抜き出し、既存月次ファイル群と重複排除統合して out_path へ。"""
    queries = [normalized_select(source_sql(p)) for p in existing]
    queries.append(filter_month_sql(normalized_select(source_sql(new_path)), year, month))
    query = Query("SELECT * FROM (" + " UNION ALL ".join(f"({q})" for q in queries) + ")",
                  tuple(a for q in queries for a in q.inputs))
    return write_parquet(con, query, out_path)
