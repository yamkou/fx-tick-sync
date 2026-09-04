"""Google Drive API v3 薄いラッパー。

- クエリ文字列は必ずエスケープ（' と \\）
- 一覧は pageToken でページング
- 全 execute() に num_retries を付与
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPE_FULL = "https://www.googleapis.com/auth/drive"
SCOPE_APP_FILES = "https://www.googleapis.com/auth/drive.file"  # このアプリが作ったファイルのみ

# 既存の FX フォルダ（別クライアントで作成済み）へアクセスするには SCOPE_FULL が必要。
# 新規に構築し直せるなら SCOPE_APP_FILES に落とすのが安全。環境変数で切替。
DEFAULT_SCOPE = os.environ.get("GDRIVE_SCOPE", SCOPE_FULL)

FOLDER_MIME = "application/vnd.google-apps.folder"
RETRIES = 3


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _build(creds: Credentials):
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def service_from_token_json(token_json: str, scope: str = DEFAULT_SCOPE):
    """GitHub Secrets 等に入れた token.json の中身（文字列）から service を作る。"""
    info = json.loads(token_json)
    creds = Credentials.from_authorized_user_info(info, [scope])
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
    return _build(creds)


def service_from_local_files(token_path: str | os.PathLike, client_secret_path: str | os.PathLike, scope: str = DEFAULT_SCOPE):
    """ローカル PC 用。token.json が無い/期限切れならブラウザで認可して保存する。

    token.json / client_secret.json は **Drive 同期フォルダの外**（既定 ~/.fxtick/）に置く。
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_path = Path(token_path)
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), [scope])
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), [scope])
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        try:
            os.chmod(token_path, 0o600)
        except OSError:
            pass
    return _build(creds)


def list_files(service, parent_id: str, name: str | None = None, name_contains: str | None = None,
               mime: str | None = None, fields: str = "id, name, size, createdTime, modifiedTime") -> list[dict[str, Any]]:
    conds = [f"'{_q(parent_id)}' in parents", "trashed = false"]
    if name is not None:
        conds.append(f"name = '{_q(name)}'")
    if name_contains is not None:
        conds.append(f"name contains '{_q(name_contains)}'")
    if mime is not None:
        conds.append(f"mimeType = '{_q(mime)}'")
    q = " and ".join(conds)
    out: list[dict[str, Any]] = []
    token = None
    while True:
        res = service.files().list(
            q=q, spaces="drive", pageSize=1000, pageToken=token,
            fields=f"nextPageToken, files({fields})",
        ).execute(num_retries=RETRIES)
        out.extend(res.get("files", []))
        token = res.get("nextPageToken")
        if not token:
            break
    return out


def find_file(service, parent_id: str, name: str) -> dict[str, Any] | None:
    files = list_files(service, parent_id, name=name)
    return files[0] if files else None


def get_or_create_folder(service, parent_id: str, name: str) -> str:
    found = list_files(service, parent_id, name=name, mime=FOLDER_MIME, fields="id, name")
    if found:
        return found[0]["id"]
    meta = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
    return service.files().create(body=meta, fields="id").execute(num_retries=RETRIES)["id"]


def download_file(service, file_id: str, dest_path: str | os.PathLike) -> None:
    request = service.files().get_media(fileId=file_id)
    with open(dest_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request, chunksize=32 * 1024 * 1024)
        done = False
        while not done:
            _, done = downloader.next_chunk(num_retries=RETRIES)


def download_bytes(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request, chunksize=32 * 1024 * 1024)
    done = False
    while not done:
        _, done = downloader.next_chunk(num_retries=RETRIES)
    return buf.getvalue()


def upload_file(service, folder_id: str, name: str, local_path: str | os.PathLike, replace: bool = True) -> str:
    """同名があれば update（replace=True）、無ければ create。file id を返す。"""
    media = MediaFileUpload(str(local_path), resumable=True, chunksize=32 * 1024 * 1024)
    existing = find_file(service, folder_id, name)
    if existing and replace:
        service.files().update(fileId=existing["id"], media_body=media).execute(num_retries=RETRIES)
        return existing["id"]
    meta = {"name": name, "parents": [folder_id]}
    return service.files().create(body=meta, media_body=media, fields="id").execute(num_retries=RETRIES)["id"]


def delete_file(service, file_id: str) -> None:
    service.files().delete(fileId=file_id).execute(num_retries=RETRIES)


def share_anyone_reader(service, file_id: str) -> str:
    """「リンクを知っている全員が閲覧可」にしてダウンロード URL を返す。"""
    service.permissions().create(fileId=file_id, body={"type": "anyone", "role": "reader"}).execute(num_retries=RETRIES)
    info = service.files().get(fileId=file_id, fields="webContentLink, webViewLink").execute(num_retries=RETRIES)
    return info.get("webContentLink") or info.get("webViewLink")
