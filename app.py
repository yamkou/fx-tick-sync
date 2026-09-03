import os
import json
import tempfile
import zipfile
import re
import streamlit as st
import duckdb
from datetime import datetime
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

st.set_page_config(page_title="FX/Tick Data Downloader", layout="centered")

# --- Secretsの読み込み ---
TARGET_DRIVE_FOLDER_ID = st.secrets.get("GDRIVE_FOLDER_ID")
TOKEN_JSON_STR = st.secrets.get("GDRIVE_TOKEN_JSON")
ACCESS_PASSWORD = st.secrets.get("APP_PASSWORD", "secret123")

if not TARGET_DRIVE_FOLDER_ID or not TOKEN_JSON_STR:
    st.error("Google Drive APIの設定が見つかりません。")
    st.stop()

# --- Google Drive API 接続 ---
@st.cache_resource
def get_drive_service():
    creds_info = json.loads(TOKEN_JSON_STR)
    creds = Credentials.from_authorized_user_info(creds_info, ['https://www.googleapis.com/auth/drive'])
    return build('drive', 'v3', credentials=creds)

service = get_drive_service()

# --- 認証画面 ---
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    st.title("🔒 ティックデータ ダウンロード")
    pwd_input = st.text_input("アクセスパスワードを入力してください", type="password")
    if st.button("ログイン"):
        if pwd_input == ACCESS_PASSWORD:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("パスワードが正しくありません。")
    st.stop()

# --- メイン画面 ---
st.title("📊 ヒストリカル・ティックデータ 一括生成")
st.caption("GoogleドライブのParquetから、MT4/MT5最適分割ファイルへオンデマンド変換して出力します。")

# 30銘柄リスト
INSTRUMENTS = [
    # 貴金属・指数
    "XAUUSD", "US30",
    # ドルストレート
    "USDJPY", "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF",
    # クロス円
    "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY",
    # ユーロクロス
    "EURGBP", "EURAUD", "EURNZD", "EURCAD", "EURCHF",
    # ポンドクロス
    "GBPAUD", "GBPNZD", "GBPCAD", "GBPCHF",
    # その他クロス
    "AUDNZD", "AUDCAD", "AUDCHF", "NZDCAD", "NZDCHF", "CADCHF"
]
selected_symbol = st.selectbox("1. 銘柄を選択", INSTRUMENTS)

@st.cache_data(ttl=300)
def get_available_files(symbol):
    q = f"name = '{symbol}' and '{TARGET_DRIVE_FOLDER_ID}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    res = service.files().list(q=q, fields='files(id)').execute()
    items = res.get('files', [])
    if not items:
        return {}
    folder_id = items[0]['id']

    q_files = f"'{folder_id}' in parents and name contains '.parquet' and trashed = false"
    res_files = service.files().list(q=q_files, fields='files(id, name)').execute()
    files = res_files.get('files', [])
    return {f['name']: f['id'] for f in sorted(files, key=lambda x: x['name'])}

file_dict = get_available_files(selected_symbol)

if not file_dict:
    st.warning(f"{selected_symbol} のデータがGoogleドライブにまだ存在しません。")
    st.stop()

all_filenames = list(file_dict.keys())

def extract_year(fname):
    m = re.search(r'_(\d{4})[_\.]', fname)
    return int(m.group(1)) if m else None

files_with_year = [(fname, extract_year(fname)) for fname in all_filenames if extract_year(fname) is not None]
files_with_year.sort(key=lambda x: x[0])

unique_years = sorted(list(set(y for _, y in files_with_year)))
earliest_year = unique_years[0] if unique_years else 2020
latest_year = unique_years[-1] if unique_years else datetime.utcnow().year

PERIOD_OPTIONS = [
    "直近 3年（1本）",
    "直近 5年（1本）",
    "直近 10年（2分割：前半5年＋後半5年）",
    f"すべての期間（3分割：{earliest_year}年〜{latest_year}年を3等分）",
    "単一ファイル指定（カスタム）"
]
selected_period = st.selectbox("2. 取得期間を選択", PERIOD_OPTIONS)

split_groups = []

if "単一ファイル" in selected_period:
    chosen = st.selectbox("対象ファイルを選択", sorted(all_filenames, reverse=True))
    split_groups.append(("Single", [chosen]))

elif "直近 3年" in selected_period:
    target_yrs = [latest_year - i for i in range(3)]
    matched = [f for f, y in files_with_year if y in target_yrs]
    split_groups.append(("Last3Y", matched if matched else all_filenames[-3:]))

elif "直近 5年" in selected_period:
    target_yrs = [latest_year - i for i in range(5)]
    matched = [f for f, y in files_with_year if y in target_yrs]
    split_groups.append(("Last5Y", matched if matched else all_filenames[-5:]))

elif "直近 10年" in selected_period:
    target_yrs = [latest_year - 9 + i for i in range(10)]
    half1_yrs = target_yrs[:5]
    half2_yrs = target_yrs[5:]
    split_groups.append(("10Y_Part1", [f for f, y in files_with_year if y in half1_yrs]))
    split_groups.append(("10Y_Part2", [f for f, y in files_with_year if y in half2_yrs]))

elif "すべての期間" in selected_period:
    n = len(unique_years)
    step = n / 3.0
    g1_yrs = set(unique_years[:int(round(step))])
    g2_yrs = set(unique_years[int(round(step)):int(round(step * 2))])
    g3_yrs = set(unique_years[int(round(step * 2)):])

    split_groups.append(("All_Part1", [f for f, y in files_with_year if y in g1_yrs]))
    split_groups.append(("All_Part2", [f for f, y in files_with_year if y in g2_yrs]))
    split_groups.append(("All_Part3", [f for f, y in files_with_year if y in g3_yrs]))

st.markdown("##### 📁 出力構成")
for label, flist in split_groups:
    st.write(f"- **{label}**: 対象ファイル数 {len(flist)} 件")

target_format = st.radio("3. 出力フォーマットを選択", [
    "MT4 (CSV形式)",
    "MT5 (タブ区切り形式)",
    "両方（MT4 + MT5 同梱）"
])

# --- 変換＆ダウンロード処理 ---
if st.button("🚀 データを生成してZIPダウンロード"):
    with st.spinner("ドライブからデータを取得し、最適分割・変換を行っています..."):
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                con = duckdb.connect()
                generated_files = []

                total_steps = sum(len(flist) for _, flist in split_groups)
                current_step = 0
                progress = st.progress(0.0)

                for grp_label, flist in split_groups:
                    if not flist:
                        continue

                    group_parquets = []
                    for idx, fname in enumerate(flist):
                        fid = file_dict[fname]
                        lp = os.path.join(tmpdir, f"{grp_label}_{idx}.parquet")
                        req = service.files().get_media(fileId=fid)
                        with open(lp, 'wb') as f:
                            downloader = MediaIoBaseDownload(f, req)
                            done = False
                            while not done:
                                _, done = downloader.next_chunk()
                        group_parquets.append(lp)
                        current_step += 1
                        progress.progress(current_step / total_steps * 0.6)

                    p_pattern = os.path.join(tmpdir, f"{grp_label}_*.parquet")
                    cols = [c[0] for c in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{p_pattern}')").fetchall()]
                    time_col = next((c for c in cols if 'time' in c.lower()), cols[0])

                    make_mt4 = "MT4" in target_format or "両方" in target_format
                    make_mt5 = "MT5" in target_format or "両方" in target_format

                    if make_mt4:
                        mt4_name = f"{selected_symbol}_{grp_label}_MT4.csv"
                        mt4_path = os.path.join(tmpdir, mt4_name)
                        con.execute(f"""
                            COPY (
                                SELECT 
                                    strftime(CAST({time_col} AS TIMESTAMP), '%Y.%m.%d %H:%M:%S.%g') AS dt,
                                    bid,
                                    ask,
                                    0 AS vol,
                                    0 AS dummy
                                FROM read_parquet('{p_pattern}')
                                ORDER BY {time_col}
                            ) TO '{mt4_path}' (FORMAT CSV, HEADER FALSE);
                        """)
                        arc_path = f"MT4/{mt4_name}" if "両方" in target_format else mt4_name
                        generated_files.append((mt4_path, arc_path))

                    if make_mt5:
                        mt5_name = f"{selected_symbol}_{grp_label}_MT5.txt"
                        mt5_path = os.path.join(tmpdir, mt5_name)
                        con.execute(f"""
                            COPY (
                                SELECT 
                                    strftime(CAST({time_col} AS TIMESTAMP), '%Y.%m.%d') AS "<DATE>",
                                    strftime(CAST({time_col} AS TIMESTAMP), '%H:%M:%S.%g') AS "<TIME>",
                                    bid AS "<BID>",
                                    ask AS "<ASK>",
                                    1 AS "<FLAGS>",
                                    0 AS "<VOLUME>"
                            FROM read_parquet('{p_pattern}')
                            ORDER BY {time_col}
                        ) TO '{mt5_path}' (FORMAT CSV, DELIMITER '\t', HEADER TRUE);
                        """)
                        arc_path = f"MT5/{mt5_name}" if "両方" in target_format else mt5_name
                        generated_files.append((mt5_path, arc_path))

                    for lp in group_parquets:
                        if os.path.exists(lp): os.remove(lp)

                progress.progress(0.85)

                format_tag = "BOTH" if "両方" in target_format else ("MT4" if "MT4" in target_format else "MT5")
                period_clean = selected_period.split('（')[0].replace(" ", "")
                zip_name = f"{selected_symbol}_{period_clean}_{format_tag}.zip"
                zip_path = os.path.join(tmpdir, zip_name)

                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for fpath, arc_name in generated_files:
                        zf.write(fpath, arcname=arc_name)

                progress.progress(1.0)

                with open(zip_path, 'rb') as f:
                    zip_bytes = f.read()

                st.success(f"✅ 生成完了！ 合計 {len(generated_files)} 本のファイルが格納されました。")
                st.download_button(
                    label=f"📥 {zip_name} をダウンロード",
                    data=zip_bytes,
                    file_name=zip_name,
                    mime="application/zip"
                )

        except Exception as e:
            st.error(f"処理エラーが発生しました: {e}")
