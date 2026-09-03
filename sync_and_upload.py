
import os
import sys
import json
import tempfile
from datetime import datetime, timedelta
import io
import dukascopy_python
import pandas as pd
import duckdb
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# 保持期間（日数）：これより古い月別ファイルはGoogleドライブから自動削除して容量を維持
RETENTION_DAYS = 90  # 過去約3ヶ月分を保持（好みに応じて変更可能）

TARGET_DRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID")
TOKEN_JSON_STR = os.environ.get("GDRIVE_TOKEN_JSON")

if not TARGET_DRIVE_FOLDER_ID or not TOKEN_JSON_STR:
    print("エラー: Secretsが設定されていません。")
    sys.exit(1)

SCOPES = ['https://www.googleapis.com/auth/drive']
creds = Credentials.from_authorized_user_info(json.loads(TOKEN_JSON_STR), SCOPES)
service = build('drive', 'v3', credentials=creds)

TARGET_INSTRUMENTS = {
    "XAUUSD": {"symbol": "XAU/USD"},
    "USDJPY": {"symbol": "USD/JPY"},
    "EURUSD": {"symbol": "EUR/USD"},
    "GBPUSD": {"symbol": "GBP/USD"},
    "EURGBP": {"symbol": "EUR/GBP"},
    "US30":   {"symbol": "USA30.IDX/USD"},
}

def get_or_create_subfolder(parent_id, folder_name):
    query = f"name = '{folder_name}' and '{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    res = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    files = res.get('files', [])
    if files:
        return files[0]['id']
    file_metadata = {'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
    folder = service.files().create(body=file_metadata, fields='id').execute()
    return folder.get('id')

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
        file_metadata = {'name': file_name, 'parents': [folder_id]}
        service.files().create(body=file_metadata, media_body=media, fields='id').execute()

def cleanup_old_files(subfolder_id, folder_name):
    """古いParquetファイルを検索し、指定期間（RETENTION_DAYS）を超えていれば削除"""
    limit_date = (datetime.utcnow() - timedelta(days=RETENTION_DAYS)).strftime('%Y-%m-%dT%H:%M:%S')
    query = f"'{subfolder_id}' in parents and name contains '{folder_name}_' and name contains '.parquet' and createdTime < '{limit_date}' and trashed = false"
    res = service.files().list(q=query, spaces='drive', fields='files(id, name, createdTime)').execute()
    
    for f in res.get('files', []):
        print(f"  -> 容量整理: 古い月別ファイル {f['name']} を削除します")
        service.files().delete(fileId=f['id']).execute()

def run_sync():
    now = datetime.utcnow()
    start_date = now - timedelta(days=7)
    month_str = now.strftime("%Y_%m")  # 例: 2026_09
    con = duckdb.connect()

    with tempfile.TemporaryDirectory() as tmpdir:
        for folder_name, config in TARGET_INSTRUMENTS.items():
            symbol = config["symbol"]
            subfolder_id = get_or_create_subfolder(TARGET_DRIVE_FOLDER_ID, folder_name)
            
            target_parquet_name = f"{folder_name}_{month_str}.parquet"
            new_csv_path = os.path.join(tmpdir, f"new_{folder_name}.csv")
            existing_parquet_path = os.path.join(tmpdir, f"old_{target_parquet_name}")
            merged_parquet_path = os.path.join(tmpdir, target_parquet_name)

            print(f"\n[{folder_name}] 直近データ取得: {start_date.strftime('%Y-%m-%d')} ~ {now.strftime('%Y-%m-%d')}")
            try:
                df = dukascopy_python.fetch(
                    instrument=symbol,
                    interval=dukascopy_python.INTERVAL_TICK,
                    offer_side=dukascopy_python.OFFER_SIDE_BID,
                    start=start_date,
                    end=now,
                )

                if df is not None and not df.empty and len(df) > 0:
                    print(f"[{folder_name}] 取得件数: {len(df):,} 行")
                    df.to_csv(new_csv_path)

                    existing = find_file_info(subfolder_id, target_parquet_name)
                    if existing:
                        # 既存の当月ファイルがある場合はダウンロードして統合マージ
                        download_file(existing['id'], existing_parquet_path)
                        # 重複タイムスタンプを除外しつつ合体
                        con.execute(f"""
                            COPY (
                                SELECT * FROM (
                                    SELECT * FROM read_parquet('{existing_parquet_path}')
                                    UNION ALL
                                    SELECT * FROM read_csv_auto('{new_csv_path}')
                                )
                                QUALIFY ROW_NUMBER() OVER (PARTITION BY 1 ORDER BY 1) = 1
                            ) TO '{merged_parquet_path}' (FORMAT PARQUET, COMPRESSION snappy);
                        """)
                    else:
                        con.execute(f"COPY (SELECT * FROM read_csv_auto('{new_csv_path}')) TO '{merged_parquet_path}' (FORMAT PARQUET, COMPRESSION snappy);")

                    # ドライブに上書き保存
                    upload_or_overwrite(subfolder_id, target_parquet_name, merged_parquet_path)
                    print(f"[{folder_name}] 月別ファイル更新完了: {target_parquet_name}")

                    # 一時ファイルの削除
                    for p in [new_csv_path, existing_parquet_path, merged_parquet_path]:
                        if os.path.exists(p): os.remove(p)

                else:
                    print(f"[{folder_name}] スキップ: 新規データなし")

                # 古い月ファイルのクリーンアップ
                cleanup_old_files(subfolder_id, folder_name)

            except Exception as e:
                print(f"[{folder_name}] エラー: {e}")

if __name__ == "__main__":
    run_sync()
