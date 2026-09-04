import os
import sys
import io
import json
from datetime import datetime, timezone, timedelta
import dukascopy_python
import duckdb
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from instruments import TARGET_INSTRUMENTS

GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID")
GDRIVE_TOKEN_JSON = os.environ.get("GDRIVE_TOKEN_JSON")

if not GDRIVE_FOLDER_ID or not GDRIVE_TOKEN_JSON:
    print("環境変数 GDRIVE_FOLDER_ID または GDRIVE_TOKEN_JSON が未設定です。")
    sys.exit(1)

con = duckdb.connect()
con.execute("SET TimeZone='UTC';")

def get_drive_service():
    creds_dict = json.loads(GDRIVE_TOKEN_JSON)
    creds = Credentials.from_authorized_user_info(creds_dict)
    return build('drive', 'v3', credentials=creds)

def get_or_create_folder(service, folder_name, parent_id):
    query = f"name = '{folder_name}' and '{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    res = service.files().list(q=query, fields='files(id)').execute()
    files = res.get('files', [])
    if files:
        return files[0]['id']
    meta = {'name': folder_name, 'parents': [parent_id], 'mimeType': 'application/vnd.google-apps.folder'}
    folder = service.files().create(body=meta, fields='id').execute()
    return folder.get('id')

def find_file(service, filename, parent_id):
    query = f"name = '{filename}' and '{parent_id}' in parents and trashed = false"
    res = service.files().list(q=query, fields='files(id, name)').execute()
    files = res.get('files', [])
    return files[0] if files else None

def sync_weekly_data():
    service = get_drive_service()
    now_utc = datetime.now(timezone.utc)
    # 直近8日分（通信トラブル等の取りこぼしを防ぐため余裕を持たせる）
    fetch_start = (now_utc - timedelta(days=8)).replace(hour=0, minute=0, second=0, microsecond=0)
    fetch_end = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    current_year = now_utc.year

    print(f"=== 週次同期開始: {fetch_start.strftime('%Y-%m-%d')} -> {fetch_end.strftime('%Y-%m-%d')} ===")

    for folder_name, cfg in TARGET_INSTRUMENTS.items():
        symbol = cfg["symbol"]
        print(f"\n[{folder_name}] データ取得中...")

        try:
            df = dukascopy_python.fetch(
                instrument=symbol,
                interval=dukascopy_python.INTERVAL_TICK,
                offer_side=dukascopy_python.OFFER_SIDE_BID,
                start=fetch_start.replace(tzinfo=None),
                end=fetch_end.replace(tzinfo=None),
            )
        except Exception as e:
            print(f"  取得エラー: {e}")
            continue

        if df is None or df.empty:
            print("  新規データなし")
            continue

        df.index.name = "timestamp"
        target_folder_id = get_or_create_folder(service, folder_name, GDRIVE_FOLDER_ID)
        parquet_filename = f"{folder_name}_{current_year}_ticks.parquet"
        existing_file = find_file(service, parquet_filename, target_folder_id)

        # 一時CSVへ書き出し
        tmp_csv = f"tmp_{folder_name}.csv"
        df.to_csv(tmp_csv, header=True)

        merged_parquet = f"merged_{folder_name}.parquet"

        if existing_file:
            print(f"  Drive上の既存ファイルとマージ中: {parquet_filename}")
            existing_local = f"exist_{folder_name}.parquet"
            req = service.files().get_media(fileId=existing_file['id'])
            with open(existing_local, "wb") as f:
                downloader = MediaIoBaseDownload(f, req)
                done = False
                while not done:
                    _, done = downloader.next_chunk()

            # DISTINCTによる完全重複排除結合
            merge_query = f"""
            COPY (
                SELECT DISTINCT * FROM (
                    SELECT * FROM read_parquet('{existing_local}')
                    UNION ALL
                    SELECT * FROM read_csv_auto('{tmp_csv}')
                ) ORDER BY timestamp ASC
            ) TO '{merged_parquet}' (FORMAT PARQUET, COMPRESSION snappy);
            """
            con.execute(merge_query)
            if os.path.exists(existing_local):
                os.remove(existing_local)
        else:
            print(f"  新規作成: {parquet_filename}")
            create_query = f"""
            COPY (
                SELECT DISTINCT * FROM read_csv_auto('{tmp_csv}')
                ORDER BY timestamp ASC
            ) TO '{merged_parquet}' (FORMAT PARQUET, COMPRESSION snappy);
            """
            con.execute(create_query)

        # Driveへアップロード（上書きまたは新規作成）
        media = MediaIoBaseUpload(io.FileIO(merged_parquet, 'rb'), mimetype='application/octet-stream', resumable=True)
        if existing_file:
            service.files().update(fileId=existing_file['id'], media_body=media).execute()
            print(f"  Drive更新完了: {parquet_filename}")
        else:
            meta = {'name': parquet_filename, 'parents': [target_folder_id]}
            service.files().create(body=meta, media_body=media).execute()
            print(f"  Drive新規保存完了: {parquet_filename}")

        # クリーンアップ
        if os.path.exists(tmp_csv):
            os.remove(tmp_csv)
        if os.path.exists(merged_parquet):
            os.remove(merged_parquet)

    print("\nすべての週次同期が正常に完了しました。")

if __name__ == "__main__":
    sync_weekly_data()
