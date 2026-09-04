"""DuckDB まわりの共通処理。

設計方針
- セッション TimeZone は必ず UTC に固定する（Windows 既定＝Asia/Tokyo で
  CAST(ts AS TIMESTAMP) すると JST になってしまう事故を防ぐ）。
- CSV は型を明示して読む（read_csv_auto による推定ぶれを排除）。
- Parquet 書き出しは .tmp → os.replace のアトミック置換。
- 重複排除は DISTINCT（全列一致）。PARTITION BY 定数は使わない。
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb

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


def source_sql(path: str | os.PathLike) -> str:
    """Parquet / CSV パス → 型付き読み取り式。"""
    p = str(path)
    if p.lower().endswith(".parquet"):
        return f"read_parquet('{sql_str(p)}')"
    cols = ", ".join(f"'{k}': '{v}'" for k, v in TICK_SCHEMA.items())
    return f"read_csv('{sql_str(p)}', header = true, columns = {{{cols}}})"


def normalized_select(source: str) -> str:
    """どの世代のファイルでも同じ型に揃える SELECT（列名は保存スキーマ準拠が前提）。"""
    return (
        "SELECT CAST(timestamp AS TIMESTAMPTZ) AS timestamp, "
        "CAST(bidPrice AS DOUBLE) AS bidPrice, CAST(askPrice AS DOUBLE) AS askPrice, "
        "CAST(bidVolume AS DOUBLE) AS bidVolume, CAST(askVolume AS DOUBLE) AS askVolume "
        f"FROM {source}"
    )


def union_sources(paths: list[str | os.PathLike]) -> str:
    parts = [f"({normalized_select(source_sql(p))})" for p in paths]
    return " UNION ALL ".join(parts)


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
    """SELECT 結果を Parquet に書き出し（DISTINCT + timestamp 順、アトミック置換）。行数を返す。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    try:
        con.execute(
            f"COPY (SELECT DISTINCT * FROM ({select_sql}) ORDER BY timestamp) "
            f"TO '{sql_str(tmp)}' (FORMAT PARQUET, COMPRESSION SNAPPY)"
        )
        os.replace(tmp, out_path)  # Windows でも既存ファイルを原子的に置換
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
    return count_rows(con, out_path)


def merge_to_parquet(
    con: duckdb.DuckDBPyConnection,
    sources: list[str | os.PathLike],
    out_path: str | os.PathLike,
) -> int:
    """複数の Parquet/CSV を重複排除しつつ 1 つの Parquet に統合。"""
    sources = [s for s in sources if os.path.exists(s) and os.path.getsize(s) > 0]
    if not sources:
        raise FileNotFoundError("統合対象のファイルがありません")
    # 出力先が入力に含まれていても、COPY は tmp に書くので安全
    return write_parquet(con, f"SELECT * FROM ({union_sources(sources)})", out_path)


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
    return (
        f"SELECT * FROM ({select_sql}) WHERE timestamp >= TIMESTAMPTZ '{s:%Y-%m-%d %H:%M:%S}+00' "
        f"AND timestamp < TIMESTAMPTZ '{e:%Y-%m-%d %H:%M:%S}+00'"
    )


def merge_month(con: duckdb.DuckDBPyConnection, new_path: str | os.PathLike, year: int, month: int,
                existing: list[str | os.PathLike], out_path: str | os.PathLike) -> int:
    """new_path から該当月だけを抜き出し、既存月次ファイル群と重複排除統合して out_path へ。"""
    parts = [f"({normalized_select(source_sql(p))})" for p in existing if os.path.exists(p) and os.path.getsize(p) > 0]
    parts.append(f"({filter_month_sql(normalized_select(source_sql(new_path)), year, month)})")
    return write_parquet(con, "SELECT * FROM (" + " UNION ALL ".join(parts) + ")", out_path)
