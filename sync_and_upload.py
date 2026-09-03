import os
import sys
import json
import tempfile
import secrets
import string
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
import dukascopy_python
import pandas as pd
import duckdb
import pyzipper
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# 保持期間（日数）：これより古い月別ファイルはGoogleドライブから自動削除して容量維持
RETENTION_DAYS = 90

# 環境変数・Secretsの取得
TARGET_DRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID")
TOKEN_JSON_STR = os.environ.get("GDRIVE_TOKEN_JSON")
MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "")
MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")
TARGET_FORMAT = os.environ.get("TARGET_FORMAT", "NONE")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", "")
ZIP_PASSWORD_IN = os.environ.get("ZIP_PASSWORD", "")

if not TARGET_DRIVE_FOLDER_ID or not TOKEN_JSON_STR:
    print("エラー: Google Drive Secretsが未設定です。")
    sys.exit(1)

# Google Drive API クライアント初期化
SCOPES = ['https://www.googleapis.com/auth/drive']
creds = Credentials.from_authorized_user_info(json.loads(TOKEN_JSON_STR), SCOPES)
service = build('drive', 'v3', credentials=creds)

# 30銘柄の完全マッピング定義
TARGET_INSTRUMENTS = {
    # 貴金属・指数
    "XAUUSD": {"symbol": "XAU/USD"},
    "US30":   {"symbol": "USA30.IDX/USD"},
    # ドルストレート
    "USDJPY": {"symbol": "USD/JPY"},
    "EURUSD": {"symbol": "EUR/USD"},
    "GBPUSD": {"symbol": "GBP/USD"},
    "AUDUSD": {"symbol": "AUD/USD"},
    "NZDUSD": {"symbol": "NZD/USD"},
    "USDCAD": {"symbol": "USD/CAD"},
    "USDCHF": {"symbol": "USD/CHF"},
    # クロス円
    "EURJPY": {"symbol": "EUR/JPY"},
    "GBPJPY": {"symbol": "GBP/JPY"},
    "AUDJPY": {"symbol": "AUD/JPY"},
    "NZDJPY": {"symbol": "NZD/JPY"},
    "CADJPY": {"symbol": "CAD/JPY"},
    "CHFJPY": {"symbol": "CHF/JPY"},
    # ユーロクロス
    "EURGBP": {"symbol": "EUR/GBP"},
    "EURAUD": {"symbol": "EUR/AUD"},
    "EURNZD": {"symbol": "EUR/NZD"},
    "EURCAD": {"symbol": "EUR/CAD"},
    "EURCHF": {"symbol": "EUR/CHF"},
    # ポンドクロス
    "GBPAUD": {"symbol": "GBP/AUD"},
    "GBPNZD": {"symbol": "GBP/NZD"},
    "GBPCAD": {"symbol": "GBP/CAD"},
    "GBPCHF": {"symbol": "GBP/CHF"},
    # その他クロス
    "AUDNZD": {"symbol": "AUD/NZD"},
    "AUDCAD": {"symbol": "AUD/CAD"},
    "AUDCHF": {"symbol": "AUD/CHF"},
    "NZDCAD": {"symbol": "NZD/CAD"},
    "NZDCHF": {"symbol": "NZD/CHF"},
    "CADCHF": {"symbol": "CAD/CHF"},
}

def get_or_create_subfolder(parent_id, folder_name):
    query = f"name = '{folder_name}' and '{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    res = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    files = res.get('files', [])
    if files:
        return files[0]['id']
    meta = {'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
    return service.files().create(body=meta, fields='id').execute().get('id')

def find_file_info(parent_folder_id, file_name):
    query = f"name = '{file_name}' and '{parent_folder_id}' in parents and trashed = false"
    res = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    files = res.get('files', [])
    return files[0] if files else None

def download_file(file_id, dest_path):
    request = service.files().get_media(fileId=file_id)
    with open(dest_path, 'wb') as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

def upload_or_overwrite(folder_id, file_name, local_filepath):
    existing = find_file_info(folder_id, file_name)
    media = MediaFileUpload(local_filepath, resumable=True)
    if existing:
        print(f"  -> 既存の月別ファイル({file_name})を最新データで上書き統合")
        service.files().update(fileId=existing['id'], media_body=media).execute()
    else:
        print(f"  -> 当月の新規ファイル({file_name})を作成")
        meta = {'name': file_name, 'parents': [folder_id]}
        service.files().create(body=meta, media_body=media, fields='id').execute()

def create_public_share_link(file_id):
    service.permissions().create(
        fileId=file_id,
        body={'type': 'anyone', 'role': 'reader'}
    ).execute()
    file_info = service.files().get(fileId=file_id, fields='webContentLink, webViewLink').execute()
    return file_info.get('webContentLink') or file_info.get('webViewLink')

def cleanup_old_parquets(subfolder_id, folder_name):
    limit_date = (datetime.utcnow() - timedelta(days=RETENTION_DAYS)).strftime('%Y-%m-%dT%H:%M:%S')
    query = f"'{subfolder_id}' in parents and name contains '{folder_name}_' and name contains '.parquet' and createdTime < '{limit_date}' and trashed = false"
    res = service.files().list(q=query, spaces='drive', fields='files(id, name, createdTime)').execute()
    for f in res.get('files', []):
        print(f"  -> 古い月別Parquetを削除: {f['name']}")
        service.files().delete(fileId=f['id']).execute()

def cleanup_expired_zip(folder_id):
    expire_time = (datetime.utcnow() - timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%S')
    query = f"'{folder_id}' in parents and name contains '.zip' and createdTime < '{expire_time}' and trashed = false"
    res = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    for f in res.get('files', []):
        print(f"  -> 7日経過した共有ZIPを削除: {f['name']}")
        service.files().delete(fileId=f['id']).execute()

def send_email(to_addr, download_url, password, expire_date_str, fmt_label):
    if not MAIL_USERNAME or not MAIL_PASSWORD:
        print("メール通知スキップ: MAIL_USERNAME または MAIL_PASSWORD が未設定です。")
        return

    subject = f"【データ送付】ティックデータ ({fmt_label}) のダウンロード案内"
    body = f"""お世話になっております。

ご指定のティックデータ（{fmt_label}形式）の準備が完了いたしました。
以下のリンクより暗号化ZIPファイルをダウンロードの上、パスワードを入力して解凍してください。

■ ダウンロードURL:
{download_url}

■ ZIP解凍パスワード:
{password}

■ ダウンロード期限:
{expire_date_str}（※期限を過ぎると自動的にアクセスできなくなります）

※本メールにお心当たりのない場合は破棄をお願い申し上げます。
"""
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = subject
    msg['From'] = MAIL_USERNAME
    msg['To'] = to_addr

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(MAIL_USERNAME, MAIL_PASSWORD)
        server.send_message(msg)
    print(f"メール送信完了: {to_addr}")

def convert_to_mt_formats(raw_csv_path, mt4_path=None, mt5_path=None):
    df = pd.read_csv(raw_csv_path)
    time_col = [c for c in df.columns if 'time' in c.lower()][0]
    df['dt'] = pd.to_datetime(df[time_col])

    if mt4_path:
        df_mt4 = pd.DataFrame({
            'datetime': df['dt'].dt.strftime('%Y.%m.%d %H:%M:%S.%f').str[:-3],
            'bid': df['bid'],
            'ask': df['ask'],
            'volume': 0
        })
        df_mt4.to_csv(mt4_path, index=False, header=False)

    if mt5_path:
        df_mt5 = pd.DataFrame({
            'date': df['dt'].dt.strftime('%Y.%m.%d'),
            'time': df['dt'].dt.strftime('%H:%M:%S.%f').str[:-3],
            'bid': df['bid'],
            'ask': df['ask'],
            'volume': 1
        })
        df_mt5.to_csv(mt5_path, sep='\t', index=False, header=True)

def run_pipeline():
    now = datetime.utcnow()
    start_date = now - timedelta(days=7)
    month_str = now.strftime("%Y_%m")
    con = duckdb.connect()

    export_folder_id = get_or_create_subfolder(TARGET_DRIVE_FOLDER_ID, "Export_Shared")
    cleanup_expired_zip(export_folder_id)

    files_to_zip = []

    with tempfile.TemporaryDirectory() as tmpdir:
        for folder_name, config in TARGET_INSTRUMENTS.items():
            symbol = config["symbol"]
            subfolder_id = get_or_create_subfolder(TARGET_DRIVE_FOLDER_ID, folder_name)

            target_parquet_name = f"{folder_name}_{month_str}.parquet"
            new_csv = os.path.join(tmpdir, f"{folder_name}_new.csv")
            parquet_path = os.path.join(tmpdir, target_parquet_name)

            print(f"\n[{folder_name}] データ取得中: {symbol} ({start_date.strftime('%Y-%m-%d')} ~ {now.strftime('%Y-%m-%d')})")
            try:
                df = dukascopy_python.fetch(
                    instrument=symbol,
                    interval=dukascopy_python.INTERVAL_TICK,
                    offer_side=dukascopy_python.OFFER_SIDE_BID,
                    start=start_date,
                    end=now,
                )

                if df is not None and not df.empty and len(df) > 0:
                    print(f"[{folder_name}] 取得成功: {len(df):,} 行")
                    df.to_csv(new_csv)

                    # DuckDBによる既存ファイルとの重複排除マージ
                    existing = find_file_info(subfolder_id, target_parquet_name)
                    if existing:
                        old_parquet = os.path.join(tmpdir, f"old_{target_parquet_name}")
                        download_file(existing['id'], old_parquet)
                        con.execute(f"""
                            COPY (
                                SELECT * FROM (
                                    SELECT * FROM read_parquet('{old_parquet}')
                                    UNION ALL
                                    SELECT * FROM read_csv_auto('{new_csv}')
                                )
                                QUALIFY ROW_NUMBER() OVER (PARTITION BY 1 ORDER BY 1) = 1
                            ) TO '{parquet_path}' (FORMAT PARQUET, COMPRESSION snappy);
                        """)
                    else:
                        con.execute(f"COPY (SELECT * FROM read_csv_auto('{new_csv}')) TO '{parquet_path}' (FORMAT PARQUET, COMPRESSION snappy);")

                    upload_or_overwrite(subfolder_id, target_parquet_name, parquet_path)

                    # 暗号化ZIP配布用ファイルの生成
                    if TARGET_FORMAT in ['MT4', 'BOTH']:
                        mt4_csv = os.path.join(tmpdir, f"{folder_name}_MT4.csv")
                        convert_to_mt_formats(new_csv, mt4_path=mt4_csv)
                        files_to_zip.append((mt4_csv, f"MT4/{folder_name}_MT4.csv" if TARGET_FORMAT == 'BOTH' else f"{folder_name}_MT4.csv"))

                    if TARGET_FORMAT in ['MT5', 'BOTH']:
                        mt5_csv = os.path.join(tmpdir, f"{folder_name}_MT5.txt")
                        convert_to_mt_formats(new_csv, mt5_path=mt5_csv)
                        files_to_zip.append((mt5_csv, f"MT5/{folder_name}_MT5.txt" if TARGET_FORMAT == 'BOTH' else f"{folder_name}_MT5.txt"))

                else:
                    print(f"[{folder_name}] スキップ: 有効な実データなし")

                cleanup_old_parquets(subfolder_id, folder_name)

            except Exception as e:
                print(f"[{folder_name}] エラー: {e}")

        # 配布用暗号化ZIP生成・リンク発行
        if files_to_zip and TARGET_FORMAT != 'NONE':
            print(f"\n暗号化ZIPを作成中...")
            zip_filename = f"TickData_{TARGET_FORMAT}_{now.strftime('%Y%m%d_%H%M')}.zip"
            zip_path = os.path.join(tmpdir, zip_filename)

            pwd = ZIP_PASSWORD_IN if ZIP_PASSWORD_IN else ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10))

            with pyzipper.AESZipFile(zip_path, 'w', compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES_256) as zf:
                zf.setpassword(pwd.encode('utf-8'))
                for local_f, arc_name in files_to_zip:
                    zf.write(local_f, arcname=arc_name)

            media = MediaFileUpload(zip_path, resumable=True)
            meta = {'name': zip_filename, 'parents': [export_folder_id]}
            uploaded_file = service.files().create(body=meta, media_body=media, fields='id').execute()
            file_id = uploaded_file.get('id')

            download_url = create_public_share_link(file_id)
            expire_date = (now + timedelta(days=7)).strftime('%Y/%m/%d %H:%M UTC')

            # GitHub Actions Step Summaryへの出力
            github_summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
            summary_content = f"""
### 📦 データダウンロード案内（有効期限: 7日間）
- **対象形式**: `{TARGET_FORMAT}`
- **ダウンロードURL**: [ダウンロードはこちら]({download_url})
- **解凍パスワード**: `{pwd}`
- **有効期限**: `{expire_date}`
"""
            if github_summary_path:
                with open(github_summary_path, "a", encoding="utf-8") as f:
                    f.write(summary_content)

            print("\n" + "="*50)
            print(f"【ダウンロードURL】: {download_url}")
            print(f"【ZIP解凍パスワード】: {pwd}")
            print(f"【有効期限】: {expire_date}")
            print("="*50 + "\n")

            if RECIPIENT_EMAIL:
                send_email(RECIPIENT_EMAIL, download_url, pwd, expire_date, TARGET_FORMAT)
            else:
                print("※メールアドレス指定なしのため、画面リンク発行のみ完了しました。")

if __name__ == "__main__":
    run_pipeline()
