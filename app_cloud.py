"""Streamlit Community Cloud 用: Drive 上の Parquet を MT4/MT5 形式に変換して ZIP ダウンロード。

st.secrets に GDRIVE_FOLDER_ID / GDRIVE_TOKEN_JSON / APP_PASSWORD を設定する。
APP_PASSWORD 未設定時はフェイルクローズ（旧版の既定 "secret123" は廃止）。
"""
from __future__ import annotations

import hmac
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from fxtick import duck, gdrive, mt_export
from fxtick.instruments import ALL_CODES

st.set_page_config(page_title="FX Tick Data Downloader", layout="centered")

ROOT_FOLDER_ID = st.secrets.get("GDRIVE_FOLDER_ID")
TOKEN_JSON = st.secrets.get("GDRIVE_TOKEN_JSON")
APP_PASSWORD = st.secrets.get("APP_PASSWORD")
MAX_ATTEMPTS = 5

if not ROOT_FOLDER_ID or not TOKEN_JSON or not APP_PASSWORD:
    st.error("設定不足: GDRIVE_FOLDER_ID / GDRIVE_TOKEN_JSON / APP_PASSWORD を Secrets に設定してください。")
    st.stop()

# ---------------- 認証 ----------------
st.session_state.setdefault("authenticated", False)
st.session_state.setdefault("attempts", 0)

if not st.session_state.authenticated:
    st.title("🔒 ティックデータ ダウンロード")
    if st.session_state.attempts >= MAX_ATTEMPTS:
        st.error("試行回数の上限に達しました。ページを再読み込みしてください。")
        st.stop()
    pwd = st.text_input("アクセスパスワード", type="password")
    if st.button("ログイン"):
        if hmac.compare_digest(pwd.encode(), str(APP_PASSWORD).encode()):
            st.session_state.authenticated = True
            st.rerun()
        st.session_state.attempts += 1
        st.error("パスワードが正しくありません。")
    st.stop()


# ---------------- Drive ----------------
@st.cache_resource
def get_service():
    return gdrive.service_from_token_json(TOKEN_JSON)


service = get_service()

FILE_RE = re.compile(r"^(?P<code>[A-Z0-9]+)_(?P<year>\d{4})(?:_(?P<month>\d{2})|_ticks)\.parquet$")


@st.cache_data(ttl=300)
def list_symbol_files(code: str) -> dict[str, str]:
    folders = gdrive.list_files(service, ROOT_FOLDER_ID, name=code, mime=gdrive.FOLDER_MIME, fields="id, name")
    if not folders:
        return {}
    files = gdrive.list_files(service, folders[0]["id"], name_contains=".parquet", fields="id, name")
    return {f["name"]: f["id"] for f in sorted(files, key=lambda x: x["name"]) if FILE_RE.match(f["name"])}


st.title("📊 ヒストリカル・ティックデータ 一括生成")
st.caption("Google Drive 上の Parquet を MT4 / MT5 形式へオンデマンド変換して ZIP でダウンロードします。")

symbol = st.selectbox("1. 銘柄", ALL_CODES)
file_ids = list_symbol_files(symbol)
if not file_ids:
    st.warning(f"{symbol} のデータが Drive にまだありません。")
    st.stop()

years_of = {n: int(FILE_RE.match(n)["year"]) for n in file_ids}
unique_years = sorted(set(years_of.values()))
earliest, latest = unique_years[0], unique_years[-1]

PERIODS = {
    "直近 3 年（1 本）": [list(range(latest - 2, latest + 1))],
    "直近 5 年（1 本）": [list(range(latest - 4, latest + 1))],
    "直近 10 年（前半 5 年 / 後半 5 年 の 2 本）": [list(range(latest - 9, latest - 4)), list(range(latest - 4, latest + 1))],
    f"全期間 {earliest}〜{latest}（3 分割）": None,
    "単一ファイル": "single",
}
period_label = st.selectbox("2. 期間", list(PERIODS))
tz_label = st.radio("3. 時刻", ["ブローカー時間（冬GMT+2 / 夏GMT+3）", "UTC"], horizontal=True)
tz_mode = "broker" if tz_label.startswith("ブローカー") else "utc"
fmt = st.radio("4. 形式", ["MT5（タブ区切りティック）", "MT4（ティック CSV）", "両方"], horizontal=True)
make_mt5 = fmt.startswith("MT5") or fmt == "両方"
make_mt4 = fmt.startswith("MT4") or fmt == "両方"

groups: list[tuple[str, list[str]]] = []
spec = PERIODS[period_label]
if spec == "single":
    chosen = st.selectbox("対象ファイル", sorted(file_ids, reverse=True))
    groups.append(("Single", [chosen]))
elif spec is None:
    n = len(unique_years)
    cuts = [round(n / 3), round(2 * n / 3)]
    parts = [unique_years[:cuts[0]], unique_years[cuts[0]:cuts[1]], unique_years[cuts[1]:]]
    for i, yrs in enumerate(parts, 1):
        groups.append((f"All_Part{i}", [f for f, y in years_of.items() if y in set(yrs)]))
else:
    for i, yrs in enumerate(spec, 1):
        label = f"Part{i}" if len(spec) > 1 else "Range"
        groups.append((f"{label}_{yrs[0]}-{yrs[-1]}", [f for f, y in years_of.items() if y in set(yrs)]))
groups = [(g, fl) for g, fl in groups if fl]

st.markdown("##### 出力構成")
for g, fl in groups:
    st.write(f"- **{g}**: {len(fl)} ファイル")

if st.button("🚀 生成して ZIP ダウンロード"):
    prog = st.progress(0.0)
    total = sum(len(fl) for _, fl in groups) or 1
    done = 0
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            con = duck.connect(threads=2, memory_limit="1GB", temp_dir=tmp / "duck")
            outputs: list[tuple[Path, str]] = []
            for g, fl in groups:
                local = []
                for i, name in enumerate(fl):
                    p = tmp / f"{g}_{i}.parquet"
                    gdrive.download_file(service, file_ids[name], p)
                    local.append(p)
                    done += 1
                    prog.progress(done / total * 0.6)
                src = f"SELECT * FROM ({duck.union_sources(local)})"
                if make_mt4:
                    out = tmp / f"{symbol}_{g}_{tz_mode}_MT4.csv"
                    mt_export.export_mt4_ticks(con, src, out, tz_mode)
                    outputs.append((out, f"MT4/{out.name}" if fmt == "両方" else out.name))
                if make_mt5:
                    out = tmp / f"{symbol}_{g}_{tz_mode}_MT5.txt"
                    mt_export.export_mt5_ticks(con, src, out, tz_mode)
                    outputs.append((out, f"MT5/{out.name}" if fmt == "両方" else out.name))
                for p in local:
                    p.unlink(missing_ok=True)
            prog.progress(0.85)

            tag = "BOTH" if fmt == "両方" else ("MT4" if make_mt4 else "MT5")
            zip_name = f"{symbol}_{re.sub(r'[^0-9A-Za-z]+', '', period_label.split('（')[0]) or 'custom'}_{tz_mode}_{tag}.zip"
            zip_path = tmp / zip_name
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for p, arc in outputs:
                    zf.write(p, arcname=arc)
            prog.progress(1.0)
            data = zip_path.read_bytes()
        st.success(f"生成完了: {len(outputs)} ファイル / {len(data) / 1e6:,.1f} MB")
        st.download_button(f"📥 {zip_name}", data=data, file_name=zip_name, mime="application/zip")
    except Exception as e:
        st.error(f"処理エラー: {e}")
