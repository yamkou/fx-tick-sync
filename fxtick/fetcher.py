"""Dukascopy からのティック取得（UTC 固定・月チャンク・CSV 追記）。

重要: dukascopy_python は start/end に naive datetime を渡すと
`datetime.timestamp()` によって **PC のローカル時刻（日本なら JST）** として
解釈する。そのため本モジュールは tz-aware UTC 以外を受け付けない。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

import dukascopy_python
import pandas as pd
from dateutil.relativedelta import relativedelta
import hashlib

from .artifacts import Lineage, IntegrityError, new_dukascopy, inspect, derive, seal, sidecar
from .policy import ExportPurpose

log = logging.getLogger(__name__)

UTC = timezone.utc
TICK_COLUMNS = ["bidPrice", "askPrice", "bidVolume", "askVolume"]
ONE_MS = timedelta(milliseconds=1)


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(dt: datetime, name: str = "datetime") -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"{name} は tz-aware（UTC）で指定してください。naive はローカル時刻扱いになります。")
    return dt.astimezone(UTC)


def empty_frame() -> pd.DataFrame:
    df = pd.DataFrame(columns=TICK_COLUMNS, dtype="float64")
    df.index = pd.DatetimeIndex([], tz="UTC", name="timestamp")
    return df


def fetch_ticks(
    instrument: str,
    start: datetime,
    end: datetime,
    retries: int = 3,
    backoff_sec: float = 5.0,
) -> pd.DataFrame:
    """[start, end) のティックを取得。列順・tz を正規化して返す。失敗時は RuntimeError。"""
    start, end = ensure_utc(start, "start"), ensure_utc(end, "end")
    if start >= end:
        return empty_frame()

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            df = dukascopy_python.fetch(
                instrument=instrument,
                interval=dukascopy_python.INTERVAL_TICK,
                offer_side=dukascopy_python.OFFER_SIDE_BID,
                start=start,
                end=end,
            )
            if df is None or df.empty:
                return empty_frame()
            df = df[TICK_COLUMNS].copy()
            idx = pd.DatetimeIndex(df.index)
            idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
            df.index = idx
            df.index.name = "timestamp"
            # ライブラリは end と同時刻のティックを含めるので end 排他に揃える
            result = df[(df.index >= start) & (df.index < end)].copy()
            result.attrs["fxtick_lineage"] = Lineage(new_dukascopy())
            result.attrs["fxtick_sha256"] = hashlib.sha256(result.to_csv().encode()).hexdigest()
            return result
        except Exception as e:  # ネットワーク・JSON パース等
            last_err = e
            log.warning("fetch 失敗 (%s/%s) %s %s→%s: %s", attempt, retries, instrument, start, end, e)
            time.sleep(backoff_sec * attempt)
    raise RuntimeError(f"fetch を {retries} 回失敗: {instrument} {start}→{end}") from last_err


def iter_chunks(start: datetime, end: datetime, months: int = 1) -> Iterator[tuple[datetime, datetime]]:
    cur = start
    while cur < end:
        nxt = min(cur + relativedelta(months=months), end)
        yield cur, nxt
        cur = nxt


def append_csv(df: pd.DataFrame, path: str | os.PathLike) -> None:
    """CSV へ追記（初回のみヘッダ）。書き込み後 fsync。"""
    if df.empty:
        return
    lineage = df.attrs.get("fxtick_lineage")
    if not isinstance(lineage, Lineage) or df.attrs.get("fxtick_sha256") != hashlib.sha256(df.to_csv().encode()).hexdigest():
        raise IntegrityError("Acquisition frame has missing or changed provenance/content")
    lineage.check(ExportPurpose.LOCAL_TEST)
    has_data = os.path.exists(path) and os.path.getsize(path) > 0
    if os.path.exists(path) and not has_data:
        raise FileExistsError("Refusing to modify an existing empty file")
    if has_data:
        previous = inspect(path)
        previous.check(ExportPurpose.LOCAL_TEST)
        lineage = derive((previous.lineage, lineage))
    with open(path, "a" if has_data else "x", newline="", encoding="utf-8") as f:
        df.to_csv(f, header=not has_data)
        f.flush()
        os.fsync(f.fileno())
    # Interrupted append leaves a hash mismatch, never a permissive stale record.
    if has_data:
        sidecar(path).unlink()
    seal(path, lineage)


def download_range_to_csv(
    instrument: str,
    start: datetime,
    end: datetime,
    csv_path: str | os.PathLike,
    pause_sec: float = 2.0,
    progress: Callable[[str], None] | None = None,
) -> int:
    """[start, end) を月ごとに取得して CSV へ追記。取得ティック総数を返す。

    途中でチャンクが失敗した場合は、そこまでの追記を残したまま例外を送出する
    （再開は最終時刻を明示し、別の新規 CSV へ取得する。既存 CSV は変更しない）。
    """
    start, end = ensure_utc(start, "start"), ensure_utc(end, "end")
    if os.path.exists(csv_path) or sidecar(csv_path).exists():
        raise FileExistsError("Acquisition requires a new CSV; preserve existing history")
    say = progress or (lambda s: None)
    total = 0
    for c_start, c_end in iter_chunks(start, end):
        say(f"  取得 {c_start:%Y-%m-%d %H:%M} → {c_end:%Y-%m-%d %H:%M} ...")
        df = fetch_ticks(instrument, c_start, c_end)
        append_csv(df, csv_path)
        total += len(df)
        say(f"    {len(df):,} ticks")
        if c_end < end:
            time.sleep(pause_sec)
    return total
