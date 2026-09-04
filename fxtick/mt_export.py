"""MT4 / MT5 向けエクスポート（唯一のタイムゾーン変換定義を持つ）。

ブローカー時間（冬 GMT+2 / 夏 GMT+3）の導出:
    ニューヨーク現地時刻 + 7 時間
  EST(UTC-5)+7 = UTC+2、EDT(UTC-4)+7 = UTC+3 となり DST 判定が不要になる。
  ※ 旧コードの `timezone('America/New_York', ts)::VARCHAR LIKE '%-04:00'` は
    常に偽（timezone() は naive TIMESTAMP を返しオフセットが付かない）。

JST（日本時間 UTC+9）:
    日本は夏時間が無いので timezone('Asia/Tokyo', ts) で常に UTC+9。
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

import duckdb
import numpy as np

from .duck import normalized_select, sql_str

TZ_MODES = ("broker", "utc", "jst")

TZ_LABELS = {
    "broker": "ブローカー時間（冬GMT+2 / 夏GMT+3）",
    "utc": "UTC（GMT+0）",
    "jst": "日本時間（JST / UTC+9）",
}

_TIME_EXPR = {
    "broker": "timezone('America/New_York', CAST(timestamp AS TIMESTAMPTZ)) + INTERVAL 7 HOUR",
    "utc": "timezone('UTC', CAST(timestamp AS TIMESTAMPTZ))",
    "jst": "timezone('Asia/Tokyo', CAST(timestamp AS TIMESTAMPTZ))",
}

# MT5 のティック FLAGS: TICK_FLAG_BID(2) | TICK_FLAG_ASK(4)
MT5_FLAGS_BID_ASK = 6


def time_expr(tz_mode: str) -> str:
    if tz_mode not in _TIME_EXPR:
        raise ValueError(f"tz_mode は {TZ_MODES} のいずれか: {tz_mode}")
    return _TIME_EXPR[tz_mode]


def _ticks_cte(source_select: str, tz_mode: str, digits: int) -> str:
    """t (naive TIMESTAMP, 指定 TZ), bidPrice, askPrice（digits 桁に丸め）, bidVolume を返す副問い合わせ。

    価格は double → 1e-6 単位の整数から復元されており、浮動小数ノイズ
    （2000.2060000000001 等）が乗ることがあるため、桁数で丸めて出力する。
    """
    d = int(digits)
    return (
        f"SELECT {time_expr(tz_mode)} AS t, "
        f"round(bidPrice, {d}) AS bidPrice, round(askPrice, {d}) AS askPrice, bidVolume "
        f"FROM ({source_select})"
    )


def export_mt5_ticks(con: duckdb.DuckDBPyConnection, source_select: str, out_path: str | os.PathLike,
                     tz_mode: str = "broker", digits: int | None = None) -> None:
    """MT5「銘柄 > カスタム > ティックをインポート」形式（タブ区切り、ミリ秒）。"""
    digits = digits if digits is not None else infer_digits(con, source_select)
    tab = chr(9)
    con.execute(f"""
        COPY (
            SELECT
                strftime(t, '%Y.%m.%d')       AS "<DATE>",
                strftime(t, '%H:%M:%S.%g')    AS "<TIME>",
                bidPrice                      AS "<BID>",
                askPrice                      AS "<ASK>",
                0                             AS "<LAST>",
                0                             AS "<VOLUME>",
                {MT5_FLAGS_BID_ASK}           AS "<FLAGS>"
            FROM ({_ticks_cte(source_select, tz_mode, digits)})
            ORDER BY t
        ) TO '{sql_str(out_path)}' (FORMAT CSV, DELIMITER '{tab}', HEADER TRUE)
    """)


def export_mt4_ticks(con: duckdb.DuckDBPyConnection, source_select: str, out_path: str | os.PathLike,
                     tz_mode: str = "broker", digits: int | None = None) -> None:
    """汎用ティック CSV（Tickstory / CSV2FXT 系ツール向け）: `yyyy.mm.dd HH:MM:SS.fff,bid,ask,volume`"""
    digits = digits if digits is not None else infer_digits(con, source_select)
    con.execute(f"""
        COPY (
            SELECT
                strftime(t, '%Y.%m.%d %H:%M:%S.%g') AS dt,
                bidPrice, askPrice,
                0 AS volume
            FROM ({_ticks_cte(source_select, tz_mode, digits)})
            ORDER BY t
        ) TO '{sql_str(out_path)}' (FORMAT CSV, HEADER FALSE)
    """)


def infer_digits(con: duckdb.DuckDBPyConnection, source_select: str, sample: int = 50000) -> int:
    """価格の小数桁数を推定: サンプルの 99.9% 以上が round(x, d) == x を満たす最小の d（1〜6）。

    浮動小数ノイズに惑わされないよう文字列長ではなく丸め一致率で判定する。
    """
    checks = ", ".join(
        f"avg(CASE WHEN abs(round(bidPrice, {d}) - bidPrice) < 1e-9 THEN 1 ELSE 0 END)" for d in range(1, 7)
    )
    ratios = con.execute(f"SELECT {checks} FROM (SELECT bidPrice FROM ({source_select}) LIMIT {int(sample)})").fetchone()
    for d, r in zip(range(1, 7), ratios):
        if r is not None and r >= 0.999:
            return d
    return 5


# ---- HST (MT4 build 600+ / version 401) -------------------------------------
# ヘッダ 148 bytes: int version; char copyright[64]; char symbol[12]; int period; int digits;
#                   int timesign; int last_sync; int unused[13]
# レコード 60 bytes: int64 time; double open, high, low, close; int64 tick_volume; int spread; int64 real_volume
_HST_HEADER_FMT = "<i64s12siiii13i"
_HST_RECORD_DTYPE = np.dtype([
    ("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
    ("tick_volume", "<i8"), ("spread", "<i4"), ("real_volume", "<i8"),
])
assert struct.calcsize(_HST_HEADER_FMT) == 148
assert _HST_RECORD_DTYPE.itemsize == 60


def export_hst(con: duckdb.DuckDBPyConnection, source_select: str, out_path: str | os.PathLike,
               symbol: str, period_min: int, digits: int, tz_mode: str = "broker") -> int:
    """ティックを period_min 分足に集約して HST(401) を書き出す。バー数を返す。

    open/close は timestamp 順を明示（ORDER BY なしの first/last は並列実行で不定）。
    """
    res = con.execute(f"""
        SELECT
            CAST(epoch(time_bucket(INTERVAL '{int(period_min)} minutes', t)) AS BIGINT) AS bar_time,
            first(bidPrice ORDER BY t) AS o,
            max(bidPrice)              AS h,
            min(bidPrice)              AS l,
            last(bidPrice ORDER BY t)  AS c,
            CAST(count(*) AS BIGINT)   AS v
        FROM ({_ticks_cte(source_select, tz_mode, digits)})
        GROUP BY 1
        ORDER BY 1
    """).fetchnumpy()

    n = len(res["bar_time"])
    rec = np.empty(n, dtype=_HST_RECORD_DTYPE)
    rec["time"] = res["bar_time"]
    rec["open"] = res["o"]
    rec["high"] = res["h"]
    rec["low"] = res["l"]
    rec["close"] = res["c"]
    rec["tick_volume"] = res["v"]
    rec["spread"] = 0
    rec["real_volume"] = 0

    header = struct.pack(
        _HST_HEADER_FMT,
        401,
        b"(C)opyright 2003, MetaQuotes Software Corp.",
        symbol.encode("ascii", "ignore")[:11],
        int(period_min),
        int(digits),
        0, 0,
        *([0] * 13),
    )
    out_path = Path(out_path)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(header)
        f.write(rec.tobytes())
    os.replace(tmp, out_path)
    return n


def hst_filename(symbol: str, period_min: int) -> str:
    return f"{symbol}{int(period_min)}.hst"
