
import os
import sys
import json
import tempfile
from datetime import datetime, timedelta
import dukascopy_python
import pandas as pd
import duckdb
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

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

def run_sync():
    now = datetime.utcnow()
    start_date = now - timedelta(days=7)
    con = duckdb.connect()

    with tempfile.TemporaryDirectory() as tmpdir:
        for folder_name, config in TARGET_INSTRUMENTS.items():
            symbol = config["symbol"]
            subfolder_id = get_or_create_subfolder(TARGET_DRIVE_FOLDER_ID, folder_name)
            
            week_str = now.strftime("%Y_w%U")
            parquet_name = f"{folder_name}_{week_str}.parquet"
            csv_path = os.path.join(tmpdir, f"{folder_name}.csv")
            parquet_path = os.path.join(tmpdir, parquet_name)

            print(f"[{folder_name}] 取得開始: {start_date.strftime('%Y-%m-%d')} ~ {now.strftime('%Y-%m-%d')}")
            try:
                df = dukascopy_python.fetch(
                    instrument=symbol,
                    interval=dukascopy_python.INTERVAL_TICK,
                    offer_side=dukascopy_python.OFFER_SIDE_BID,
                    start=start_date,
                    end=now,
                )

                if df is not None and not df.empty:
                    df.to_csv(csv_path)
                    
                    con.execute(f"COPY (SELECT * FROM read_csv_auto('{csv_path}')) TO '{parquet_path}' (FORMAT PARQUET, COMPRESSION snappy);")
                    
                    print(f"[{folder_name}] 送信中: {parquet_name}")
                    media = MediaFileUpload(parquet_path, resumable=True)
                    file_metadata = {'name': parquet_name, 'parents': [subfolder_id]}
                    service.files().create(body=file_metadata, media_body=media, fields='id').execute()
                    print(f"[{folder_name}] アップロード完了")
                    
                    if os.path.exists(csv_path): os.remove(csv_path)
                    if os.path.exists(parquet_path): os.remove(parquet_path)
                else:
                    print(f"[{folder_name}] データなし")
            except Exception as e:
                print(f"[{folder_name}] エラー: {e}")

if __name__ == "__main__":
    run_sync()
