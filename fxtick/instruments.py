"""銘柄定義（全スクリプト共通の唯一の定義箱）。

code       : フォルダ名・ファイル名の接頭辞（例 XAUUSD）
dukascopy  : dukascopy_python に渡す instrument 文字列
start_year : ローカル一括取得の開始年
digits     : MT4/MT5 エクスポート時の既定桁数（None ならデータから推定）
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    code: str
    dukascopy: str
    start_year: int
    digits: int | None = None


_LIST: list[Instrument] = [
    # --- 暗号資産 ---
    Instrument("BTCUSD", "BTC/USD", 2017),
    Instrument("ETHUSD", "ETH/USD", 2018),
    # --- 貴金属・株価指数 ---
    Instrument("XAUUSD", "XAU/USD", 2011, 3),
    Instrument("US30", "USA30.IDX/USD", 2013),
    # --- ドルストレート ---
    Instrument("USDJPY", "USD/JPY", 2006, 3),
    Instrument("EURUSD", "EUR/USD", 2006, 5),
    Instrument("GBPUSD", "GBP/USD", 2006, 5),
    Instrument("AUDUSD", "AUD/USD", 2006, 5),
    Instrument("NZDUSD", "NZD/USD", 2006, 5),
    Instrument("USDCAD", "USD/CAD", 2006, 5),
    Instrument("USDCHF", "USD/CHF", 2006, 5),
    # --- クロス円 ---
    Instrument("EURJPY", "EUR/JPY", 2006, 3),
    Instrument("GBPJPY", "GBP/JPY", 2006, 3),
    Instrument("AUDJPY", "AUD/JPY", 2006, 3),
    Instrument("NZDJPY", "NZD/JPY", 2006, 3),
    Instrument("CADJPY", "CAD/JPY", 2006, 3),
    Instrument("CHFJPY", "CHF/JPY", 2006, 3),
    # --- ユーロクロス ---
    Instrument("EURGBP", "EUR/GBP", 2006, 5),
    Instrument("EURAUD", "EUR/AUD", 2006, 5),
    Instrument("EURNZD", "EUR/NZD", 2006, 5),
    Instrument("EURCAD", "EUR/CAD", 2006, 5),
    Instrument("EURCHF", "EUR/CHF", 2006, 5),
    # --- ポンドクロス ---
    Instrument("GBPAUD", "GBP/AUD", 2006, 5),
    Instrument("GBPNZD", "GBP/NZD", 2006, 5),
    Instrument("GBPCAD", "GBP/CAD", 2006, 5),
    Instrument("GBPCHF", "GBP/CHF", 2006, 5),
    # --- その他クロス ---
    Instrument("AUDNZD", "AUD/NZD", 2006, 5),
    Instrument("AUDCAD", "AUD/CAD", 2006, 5),
    Instrument("AUDCHF", "AUD/CHF", 2006, 5),
    Instrument("NZDCAD", "NZD/CAD", 2006, 5),
    Instrument("NZDCHF", "NZD/CHF", 2006, 5),
    Instrument("CADCHF", "CAD/CHF", 2006, 5),
]

INSTRUMENTS: dict[str, Instrument] = {i.code: i for i in _LIST}
ALL_CODES: list[str] = list(INSTRUMENTS)


def resolve(codes: list[str] | None) -> list[Instrument]:
    """コード一覧 → Instrument 一覧。None/空なら全銘柄。未知コードは ValueError。"""
    if not codes:
        return list(_LIST)
    unknown = [c for c in codes if c not in INSTRUMENTS]
    if unknown:
        raise ValueError(f"未定義の銘柄コード: {unknown}（定義は fxtick/instruments.py）")
    return [INSTRUMENTS[c] for c in codes]
