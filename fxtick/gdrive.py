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
from dataclasses import dataclass
from enum import Enum
import tempfile
import shutil

from .artifacts import inspect, sidecar, IntegrityError
from .policy import ExportPurpose


class StorageZone(str, Enum):
    PRIVATE_REFERENCE = "PRIVATE_REFERENCE"
    DISTRIBUTION = "DISTRIBUTION"


@dataclass(frozen=True)
class DriveRoots:
    private_reference: str
    distribution: str

    def __post_init__(self):
        if not self.private_reference or not self.distribution or self.private_reference == self.distribution:
            raise IntegrityError("Separate explicit private-reference and distribution roots are required")


def assert_zone(service, folder_id, zone, roots):
    """Verify ancestry and private ACLs; folder names never grant permission."""
    if not isinstance(roots, DriveRoots) or not isinstance(zone, StorageZone):
        raise IntegrityError("Explicit storage zone configuration required")
    expected = roots.private_reference if zone == StorageZone.PRIVATE_REFERENCE else roots.distribution
    forbidden = roots.distribution if zone == StorageZone.PRIVATE_REFERENCE else roots.private_reference
    seen = set()
    current = folder_id
    while current:
        if current in seen or current == forbidden:
            raise IntegrityError("Overlapping or cyclic Drive storage roots")
        seen.add(current)
        info = service.files().get(fileId=current, fields="id,parents,mimeType,trashed").execute(num_retries=RETRIES)
        if info.get("trashed") or info.get("mimeType") != FOLDER_MIME:
            raise IntegrityError("Invalid storage folder")
        if zone == StorageZone.PRIVATE_REFERENCE:
            page = None
            while True:
                permissions = service.permissions().list(fileId=current, pageToken=page,
                    fields="nextPageToken,permissions(type,role)").execute(num_retries=RETRIES)
                if any(p.get("type") != "user" or p.get("role") != "owner"
                       for p in permissions.get("permissions", [])):
                    raise IntegrityError("Private reference folder must be owner-only, including ancestors")
                if not permissions.get("permissions"):
                    raise IntegrityError("Private ACL is unknown")
                page = permissions.get("nextPageToken")
                if not page:
                    break
        parents = info.get("parents", [])
        if len(parents) > 1:
            raise IntegrityError("Ambiguous Drive ancestry")
        current = parents[0] if parents else None
    if expected not in seen:
        raise IntegrityError("Destination is outside the configured zone")

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
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def service_from_token_json(token_json: str, scope: str = DEFAULT_SCOPE):
    """GitHub Secrets 等に入れた token.json の中身（文字列）から service を作る。"""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
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
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

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


def _download_raw(service, file_id: str, dest_path: str | os.PathLike) -> None:
    from googleapiclient.http import MediaIoBaseDownload
    request = service.files().get_media(fileId=file_id)
    with open(dest_path, "xb") as f:
        downloader = MediaIoBaseDownload(f, request, chunksize=32 * 1024 * 1024)
        done = False
        while not done:
            _, done = downloader.next_chunk(num_retries=RETRIES)


def _remote_info(service, file_id, zone, roots):
    info = service.files().get(fileId=file_id,
        fields="id,name,parents,appProperties,version,trashed").execute(num_retries=RETRIES)
    props = info.get("appProperties", {})
    if info.get("trashed") or props.get("fxtick_zone") != zone.value or not props.get("fxtick_manifest") or "version" not in info:
        raise IntegrityError("Remote artifact has missing/invalid provenance binding")
    if len(info.get("parents", [])) != 1:
        raise IntegrityError("Ambiguous remote location")
    assert_zone(service, info["parents"][0], zone, roots)
    return info


def download_file(service, file_id, dest_path, *, zone, roots):
    if not isinstance(zone, StorageZone):
        raise IntegrityError("Explicit zone required")
    before = _remote_info(service, file_id, zone, roots)
    dest_path = Path(dest_path)
    if dest_path.exists() or sidecar(dest_path).exists():
        raise FileExistsError("Download requires a new destination")
    with tempfile.TemporaryDirectory() as td:
        snapshot = Path(td) / ("download" + dest_path.suffix)
        _download_raw(service, file_id, snapshot)
        _download_raw(service, before["appProperties"]["fxtick_manifest"], sidecar(snapshot))
        artifact = inspect(snapshot)
        artifact.check(ExportPurpose.LOCAL_TEST if zone == StorageZone.PRIVATE_REFERENCE else ExportPurpose.DISTRIBUTION)
        if artifact.sha256 != before["appProperties"].get("fxtick_sha256") or before != _remote_info(service, file_id, zone, roots):
            raise IntegrityError("Remote artifact changed or hash binding disagrees")
        with snapshot.open("rb") as src, dest_path.open("xb") as dest:
            shutil.copyfileobj(src, dest)
        with sidecar(snapshot).open("rb") as src, sidecar(dest_path).open("xb") as dest:
            shutil.copyfileobj(src, dest)
    return inspect(dest_path)


def download_bytes(service, file_id: str, *, roots) -> bytes:
    info = _remote_info(service, file_id, StorageZone.DISTRIBUTION, roots)
    with tempfile.TemporaryDirectory() as td:
        artifact = download_file(service, file_id, Path(td) / ("download" + Path(info.get("name", "")).suffix),
                                 zone=StorageZone.DISTRIBUTION, roots=roots)
        from .export_service import download_payload
        return download_payload(artifact)


def upload_file(service, folder_id: str, name: str, local_path: str | os.PathLike,
                replace: bool = False, *, zone=StorageZone.DISTRIBUTION, roots=None) -> str:
    """Immutable upload with content, policy and ancestry checks before API writes."""
    artifact = inspect(local_path)
    if not isinstance(zone, StorageZone):
        raise IntegrityError("Explicit zone required")
    purpose = ExportPurpose.LOCAL_TEST if zone == StorageZone.PRIVATE_REFERENCE else ExportPurpose.DISTRIBUTION
    artifact.check(purpose)
    if replace:
        raise IntegrityError("Historical remote files must not be overwritten")
    assert_zone(service, folder_id, zone, roots)
    if find_file(service, folder_id, name):
        raise FileExistsError("Remote name already exists; choose a new snapshot name")
    from googleapiclient.http import MediaFileUpload
    # Upload a verified private snapshot, not a mutable caller path.
    with tempfile.TemporaryDirectory() as td:
        snapshot = Path(td) / artifact.path.name
        shutil.copyfile(artifact.path, snapshot)
        shutil.copyfile(sidecar(artifact.path), sidecar(snapshot))
        verified = inspect(snapshot)
        verified.check(purpose)
        if verified.sha256 != artifact.sha256 or verified.lineage != artifact.lineage:
            raise IntegrityError("Upload input changed")
        assert_zone(service, folder_id, zone, roots)
        manifest = service.files().create(body={"name": name + ".provenance.json", "parents": [folder_id]},
            media_body=MediaFileUpload(str(sidecar(snapshot)), resumable=True), fields="id").execute(num_retries=RETRIES)["id"]
        verified.check(purpose)
        assert_zone(service, folder_id, zone, roots)
        meta = {"name": name, "parents": [folder_id], "appProperties": {
            "fxtick_zone": zone.value, "fxtick_manifest": manifest, "fxtick_sha256": verified.sha256}}
        return service.files().create(body=meta,
            media_body=MediaFileUpload(str(snapshot), resumable=True, chunksize=32 * 1024 * 1024),
            fields="id").execute(num_retries=RETRIES)["id"]


def delete_file(service, file_id: str) -> None:
    service.files().delete(fileId=file_id).execute(num_retries=RETRIES)


def share_anyone_reader(service, file_id: str, *, roots=None) -> str:
    """「リンクを知っている全員が閲覧可」にしてダウンロード URL を返す。"""
    validate_distribution_remote(service, file_id, roots=roots)
    service.permissions().create(fileId=file_id, body={"type": "anyone", "role": "reader"}).execute(num_retries=RETRIES)
    info = service.files().get(fileId=file_id, fields="webContentLink, webViewLink").execute(num_retries=RETRIES)
    return info.get("webContentLink") or info.get("webViewLink")


def validate_distribution_remote(service, file_id, *, roots):
    """Re-download/re-evaluate the exact remote artifact before sharing/link delivery."""
    before = _remote_info(service, file_id, StorageZone.DISTRIBUTION, roots)
    name = before.get("name", "")
    with tempfile.TemporaryDirectory() as td:
        artifact = download_file(service, file_id, Path(td) / ("artifact" + Path(name).suffix),
            zone=StorageZone.DISTRIBUTION, roots=roots)
        artifact.check(ExportPurpose.DISTRIBUTION)
        if before != _remote_info(service, file_id, StorageZone.DISTRIBUTION, roots):
            raise IntegrityError("Remote artifact changed before publication")


def distribution_url(service, file_id, *, roots):
    validate_distribution_remote(service, file_id, roots=roots)
    info = service.files().get(fileId=file_id, fields="webContentLink,webViewLink").execute(num_retries=RETRIES)
    return info.get("webContentLink") or info.get("webViewLink")


def eligible_distribution_files(service, files, *, roots):
    result = {}
    for info in files:
        try:
            validate_distribution_remote(service, info["id"], roots=roots)
        except (PermissionError, ValueError, OSError):
            continue
        result[info["name"]] = info["id"]
    return result
