
"""Streamlit distribution UI: only content-attested, policy-approved Drive data.

st.secrets に別々の private-reference / distribution root と token/password を設定する。
APP_PASSWORD 未設定時はフェイルクローズ（旧版の既定 "secret123" は廃止）。
"""
from __future__ import annotations

import hmac
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from fxtick import duck, gdrive, mt_export
from fxtick.instruments import ALL_CODES
from fxtick.policy import ExportPurpose
from fxtick.export_service import build_zip, streamlit_download

st.set_page_config(page_title="FX Tick Data Downloader", layout="centered")

ROOT_FOLDER_ID = st.secrets.get("GDRIVE_DISTRIBUTION_FOLDER_ID")
PRIVATE_FOLDER_ID = st.secrets.get("GDRIVE_PRIVATE_REFERENCE_FOLDER_ID")
TOKEN_JSON = st.secrets.get("GDRIVE_TOKEN_JSON")
APP_PASSWORD = st.secrets.get("APP_PASSWORD")
MAX_ATTEMPTS = 5

if not ROOT_FOLDER_ID or not PRIVATE_FOLDER_ID or not TOKEN_JSON or not APP_PASSWORD:
    st.error("設定不足: private-reference / distribution の別ルート、token、APP_PASSWORD が必要です。")
    st.stop()
ROOTS = gdrive.DriveRoots(PRIVATE_FOLDER_ID, ROOT_FOLDER_ID)

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
    gdrive.assert_zone(service, ROOT_FOLDER_ID, gdrive.StorageZone.DISTRIBUTION, ROOTS)
    folders = gdrive.list_files(service, ROOT_FOLDER_ID, name=code, mime=gdrive.FOLDER_MIME, fields="id, name")
    if not folders:
        return {}
    files = gdrive.list_files(service, folders[0]["id"], name_contains=".parquet", fields="id, name")
    return gdrive.eligible_distribution_files(service,
        [f for f in sorted(files, key=lambda x: x["name"]) if FILE_RE.match(f["name"])], roots=ROOTS)


def get_years(file_ids: dict[str, str]) -> list[int]:
    return sorted({int(FILE_RE.match(n)["year"]) for n in file_ids})


def split_years_into_chunks(years: list[int], chunk_size: int = 5) -> list[list[int]]:
    """年リストを chunk_size 年ごとに分割（最後は端数OK）。"""
    chunks = []
    for i in range(0, len(years), chunk_size):
        chunks.append(years[i:i + chunk_size])
    return chunks


# ---------------- メイン画面 ----------------
st.title("📊 ヒストリカル・ティックデータ 一括ダウンロード")
st.caption("Google Drive 上の Parquet を MT4 / MT5 形式へ変換し、5年区切りの ZIP でダウンロードします。")

# 1. 銘柄選択（複数可）
selected_symbols = st.multiselect("1. 銘柄を選択（複数可）", ALL_CODES, default=["XAUUSD"])
if not selected_symbols:
    st.info("銘柄を1つ以上選択してください。")
    st.stop()

# 各銘柄のファイル一覧を取得
all_file_ids: dict[str, dict[str, str]] = {}
all_years: dict[str, list[int]] = {}
for code in selected_symbols:
    fids = list_symbol_files(code)
    if fids:
        all_file_ids[code] = fids
        all_years[code] = get_years(fids)

if not all_file_ids:
    st.warning("選択した銘柄のデータが Drive にまだありません。")
    st.stop()

# 2. 期間選択
PERIOD_OPTIONS = {
    "直近 5 年": 5,
    "直近 10 年": 10,
    "全期間": 0,
}
period_label = st.selectbox("2. 期間", list(PERIOD_OPTIONS))
period_n = PERIOD_OPTIONS[period_label]

# 3. タイムゾーン（JST 追加）
tz_mode = st.radio(
    "3. 時刻",
    list(mt_export.TZ_LABELS.keys()),
    format_func=lambda k: mt_export.TZ_LABELS[k],
    index=0,  # 既定をブローカー時間に
    horizontal=True,
)

# 4. 形式
fmt = st.radio("4. 形式", ["MT5（タブ区切りティック）", "MT4（ティック CSV）", "両方"], horizontal=True)
make_mt5 = fmt.startswith("MT5") or fmt == "両方"
make_mt4 = fmt.startswith("MT4") or fmt == "両方"

# 出力構成のプレビュー
st.markdown("---")
st.markdown("##### 📁 出力構成プレビュー")
total_files = 0
plan: dict[str, list[tuple[str, list[str]]]] = {}  # code -> [(chunk_label, [filenames])]
for code in sorted(all_file_ids):
    years = all_years[code]
    if period_n > 0:
        latest = max(years)
        years = [y for y in years if y > latest - period_n]
    chunks = split_years_into_chunks(years, chunk_size=5)
    fids = all_file_ids[code]
    years_of = {n: int(FILE_RE.match(n)["year"]) for n in fids}
    groups = []
    for chunk in chunks:
        label = f"{chunk[0]}-{chunk[-1]}" if len(chunk) > 1 else str(chunk[0])
        matched = [f for f, y in years_of.items() if y in set(chunk)]
        if matched:
            groups.append((label, matched))
            total_files += 1
    plan[code] = groups
    chunks_str = " / ".join(f"{g[0]}" for g in groups)
    st.write(f"- **{code}**: {len(years)}年分 → {len(groups)} 本（{chunks_str}）")

st.caption(f"合計 {total_files} 本の ZIP ファイル × {'MT4+MT5' if fmt == '両方' else ('MT5' if make_mt5 else 'MT4')} 形式")

# 生成＆ダウンロード
if st.button("🚀 生成して ZIP ダウンロード"):
    prog = st.progress(0.0)
    step = 0
    total_steps = sum(sum(len(fl) for _, fl in groups) for groups in plan.values()) or 1

    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            con = duck.connect(threads=2, memory_limit="1GB", temp_dir=tmp / "duck")
            all_zips = []

            for code in sorted(plan):
                for chunk_label, filenames in plan[code]:
                    # Parquet をダウンロード
                    local_parquets = []
                    for i, fname in enumerate(filenames):
                        p = tmp / f"{code}_{chunk_label}_{i}.parquet"
                        gdrive.download_file(service, all_file_ids[code][fname], p,
                            zone=gdrive.StorageZone.DISTRIBUTION, roots=ROOTS)
                        local_parquets.append(p)
                        step += 1
                        prog.progress(step / total_steps * 0.6)

                    src = duck.union_sources(local_parquets)
                    src = src.wrap(f"SELECT * FROM ({src})")

                    # 変換
                    outputs: list[tuple[Path, str]] = []
                    if make_mt4:
                        out = tmp / f"{code}_{chunk_label}_{tz_mode}_MT4.csv"
                        mt_export.export_mt4_ticks(con, src, out, tz_mode, purpose=ExportPurpose.DISTRIBUTION)
                        outputs.append((out, f"MT4/{out.name}" if fmt == "両方" else out.name))
                    if make_mt5:
                        out = tmp / f"{code}_{chunk_label}_{tz_mode}_MT5.txt"
                        mt_export.export_mt5_ticks(con, src, out, tz_mode, purpose=ExportPurpose.DISTRIBUTION)
                        outputs.append((out, f"MT5/{out.name}" if fmt == "両方" else out.name))

                    # ZIP 作成（5年区切り1本ずつ）
                    tag = "BOTH" if fmt == "両方" else ("MT4" if make_mt4 else "MT5")
                    zip_name = f"{code}_{chunk_label}_{tz_mode}_{tag}.zip"
                    zip_path = tmp / zip_name
                    archive = build_zip(outputs, zip_path, purpose=ExportPurpose.DISTRIBUTION)
                    all_zips.append(archive)

                    # 一時ファイル削除
                    for p in local_parquets:
                        p.unlink(missing_ok=True)
                    for p, _ in outputs:
                        p.unlink(missing_ok=True)

            prog.progress(1.0)

            st.success(f"生成完了: {len(all_zips)} 個の ZIP")
            st.markdown("---")
            for archive in all_zips:
                streamlit_download(st, archive,
                    label=f"📥 {archive.path.name}（{archive.size / 1e6:,.1f} MB）",
                    key=archive.path.name)

    except Exception as e:
        st.error(f"処理エラー: {e}")
