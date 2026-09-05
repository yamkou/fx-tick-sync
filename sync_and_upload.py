"""GitHub Actions: Dukascopy private-reference acquisition and immutable snapshots.

旧版からの主な修正
- 重複排除 `PARTITION BY 1` （全体 1 行になる）→ DISTINCT
- 90 日より古い *.parquet を一律削除するロジックを撤廃（年次アーカイブが消えていた）
- 「今から 7 日」固定 → Drive 上の最終ティック時刻から再開（cron 欠落を自己修復）
- 月跨ぎの週は実際の timestamp の月で分配
- 列名を dukascopy_python 実装（bidPrice/askPrice）に合わせ、TZ 変換を fxtick.mt_export に一本化
- ZIP パスワードを ::add-mask:: でログからマスク
- 1 銘柄でも失敗したら非 0 で終了（Actions を赤にする）

環境変数（Secrets / inputs）
  GDRIVE_PRIVATE_REFERENCE_FOLDER_ID, GDRIVE_DISTRIBUTION_FOLDER_ID,
  GDRIVE_TOKEN_JSON                            必須（旧ルートへの自動fallbackなし）
  SYNC_SYMBOLS       "XAUUSD,USDJPY" のようなカンマ区切り。省略時は全銘柄
  MAX_BACKFILL_DAYS  1 回の実行で遡る上限日数（既定 60、6 時間制限対策）
  TARGET_FORMAT      NONE のみ。このジョブからの配布要求は接続前に拒否。
  EXPORT_TZ          broker | utc（配布 ZIP の時刻）
  RECIPIENT_EMAIL, MAIL_USERNAME, MAIL_PASSWORD  メール通知（任意）
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import smtplib
import string
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

from fxtick import duck, fetcher, gdrive, mt_export
from fxtick.instruments import resolve
from fxtick.policy import ExportPurpose
from fxtick.artifacts import inspect
from fxtick.export_service import build_zip
from uuid import uuid4

UTC = timezone.utc
log = logging.getLogger("sync")

ROOT_FOLDER_ID = os.environ.get("GDRIVE_PRIVATE_REFERENCE_FOLDER_ID", "")
DISTRIBUTION_FOLDER_ID = os.environ.get("GDRIVE_DISTRIBUTION_FOLDER_ID", "")
TOKEN_JSON = os.environ.get("GDRIVE_TOKEN_JSON", "")
SYNC_SYMBOLS = [s.strip() for s in os.environ.get("SYNC_SYMBOLS", "").split(",") if s.strip()]
MAX_BACKFILL_DAYS = int(os.environ.get("MAX_BACKFILL_DAYS", "60"))
DEFAULT_LOOKBACK_DAYS = 7
TARGET_FORMAT = os.environ.get("TARGET_FORMAT", "NONE").upper()
EXPORT_TZ = os.environ.get("EXPORT_TZ", "broker").lower()
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", "").strip()
MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "")
MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")
ZIP_TTL_DAYS = 7
EXPORT_FOLDER_NAME = "Export_Shared"
ZIP_PREFIX = "TickData_"


def monthly_name(code: str, year: int, month: int) -> str:
    return f"{code}_{year:04d}_{month:02d}.parquet"


def latest_monthly_file(service, folder_id: str, code: str, year=None, month=None) -> dict | None:
    files = gdrive.list_files(service, folder_id, name_contains=f"{code}_", fields="id, name, size")
    prefix = rf"{re.escape(code)}_\d{{4}}_\d{{2}}" if year is None else rf"{re.escape(code)}_{year:04d}_{month:02d}"
    pat = re.compile(rf"^{prefix}__\d{{8}}T\d{{12}}_[a-f0-9]+\.parquet$")
    cands = [f for f in files if pat.match(f["name"])]
    return max(cands, key=lambda f: f["name"]) if cands else None


def sync_symbol(service, con, inst, tmpdir: Path, now: datetime, *, roots) -> Path | None:
    """1 銘柄の差分同期。取得した新規ティック CSV のパス（無ければ None）を返す。"""
    code = inst.code
    gdrive.assert_zone(service, roots.private_reference, gdrive.StorageZone.PRIVATE_REFERENCE, roots)
    sub_id = gdrive.get_or_create_folder(service, roots.private_reference, code)
    gdrive.assert_zone(service, sub_id, gdrive.StorageZone.PRIVATE_REFERENCE, roots)

    # 1) 再開位置の決定
    latest = latest_monthly_file(service, sub_id, code)
    start = now - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    if latest:
        local_latest = tmpdir / f"remote_{latest['name']}"
        gdrive.download_file(service, latest["id"], local_latest, zone=gdrive.StorageZone.PRIVATE_REFERENCE, roots=roots)
        last_ts = duck.max_timestamp(con, local_latest)
        if last_ts:
            start = last_ts + fetcher.ONE_MS
    floor = now - timedelta(days=MAX_BACKFILL_DAYS)
    if start < floor:
        log.warning("[%s] 遅延が %d 日を超えています。%s 以降のみ取得（複数回実行で追いつきます）", code, MAX_BACKFILL_DAYS, floor.date())
        start = floor
    if start >= now:
        log.info("[%s] 差分なし", code)
        return None

    # 2) 取得
    new_csv = tmpdir / f"{code}_new.csv"
    log.info("[%s] 取得 %s → %s", code, start.strftime("%Y-%m-%d %H:%M"), now.strftime("%Y-%m-%d %H:%M"))
    n = fetcher.download_range_to_csv(inst.dukascopy, start, now, new_csv, pause_sec=1.0)
    if n == 0:
        log.info("[%s] 新規ティックなし", code)
        return None
    log.info("[%s] %s ticks 取得", code, f"{n:,}")

    # 3) timestamp の実際の年月ごとに月次ファイルへ統合
    for year, month in duck.month_range(con, new_csv):
        name = f"{code}_{year:04d}_{month:02d}__{now:%Y%m%dT%H%M%S%f}_{uuid4().hex}.parquet"
        out = tmpdir / name
        remote = latest_monthly_file(service, sub_id, code, year, month)
        existing: list[Path] = []
        if remote:
            if latest and remote["id"] == latest["id"]:
                existing.append(tmpdir / f"remote_{latest['name']}")  # 既に DL 済み
            else:
                old = tmpdir / f"old_{name}"
                gdrive.download_file(service, remote["id"], old, zone=gdrive.StorageZone.PRIVATE_REFERENCE, roots=roots)
                existing.append(old)
        rows = duck.merge_month(con, new_csv, year, month, existing, out)
        gdrive.upload_file(service, sub_id, name, out, zone=gdrive.StorageZone.PRIVATE_REFERENCE, roots=roots)
        log.info("[%s]   %s ← %s rows %s", code, name, f"{rows:,}", "(統合)" if remote else "(新規)")
    return new_csv


def cleanup_expired_zips(service, export_folder_id: str, now: datetime) -> None:
    limit = (now - timedelta(days=ZIP_TTL_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    for f in gdrive.list_files(service, export_folder_id, name_contains=ZIP_PREFIX, fields="id, name, createdTime"):
        if f["name"].endswith(".zip") and f.get("createdTime", "9999") < limit:
            log.info("期限切れ ZIP 削除: %s", f["name"])
            gdrive.delete_file(service, f["id"])


def build_distribution(service, con, export_folder_id: str, new_csvs: dict[str, Path], tmpdir: Path, now: datetime, *, roots=None) -> None:
    for csv in new_csvs.values():
        inspect(csv).check(ExportPurpose.DISTRIBUTION)
    files: list[tuple[Path, str]] = []
    both = TARGET_FORMAT == "BOTH"
    for code, csv in new_csvs.items():
        src = duck.normalized_select(duck.source_sql(csv))
        if TARGET_FORMAT in ("MT4", "BOTH"):
            p = tmpdir / f"{code}_MT4.csv"
            mt_export.export_mt4_ticks(con, src, p, EXPORT_TZ, purpose=ExportPurpose.DISTRIBUTION)
            files.append((p, f"MT4/{p.name}" if both else p.name))
        if TARGET_FORMAT in ("MT5", "BOTH"):
            p = tmpdir / f"{code}_MT5.txt"
            mt_export.export_mt5_ticks(con, src, p, EXPORT_TZ, purpose=ExportPurpose.DISTRIBUTION)
            files.append((p, f"MT5/{p.name}" if both else p.name))
    if not files:
        log.info("配布対象なし")
        return

    pwd = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))

    # メール送信時のみパスワードをログからマスク（Summary には表示しないため）
    # メール未指定時は Summary にパスワードを表示するのでマスクしない
    if RECIPIENT_EMAIL:
        print(f"::add-mask::{pwd}")

    zip_name = f"{ZIP_PREFIX}{TARGET_FORMAT}_{EXPORT_TZ}_{now:%Y%m%d_%H%M}.zip"
    zip_path = tmpdir / zip_name
    build_zip(files, zip_path, purpose=ExportPurpose.DISTRIBUTION, password=pwd)

    file_id = gdrive.upload_file(service, export_folder_id, zip_name, zip_path, roots=roots)
    gdrive.share_anyone_reader(service, file_id, roots=roots)
    url = gdrive.distribution_url(service, file_id, roots=roots)
    expire = "自動失効なし"

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### 📦 ティックデータ配布 ({TARGET_FORMAT} / {EXPORT_TZ})\n\n")
            f.write(f"- ダウンロード: {url}\n- 有効期限: {expire}\n")
            if RECIPIENT_EMAIL:
                f.write(f"- 解凍パスワードは {RECIPIENT_EMAIL} へメール送信しました\n")
            else:
                f.write(f"- 解凍パスワード: `{pwd}`\n")
    log.info("配布 URL: %s（期限 %s）", url, expire)

    if RECIPIENT_EMAIL:
        send_email(RECIPIENT_EMAIL, service, file_id, pwd, expire, roots=roots)


def send_email(to_addr: str, service, file_id: str, pwd: str, expire: str, *, roots) -> None:
    url = gdrive.distribution_url(service, file_id, roots=roots)
    if not MAIL_USERNAME or not MAIL_PASSWORD:
        log.warning("MAIL_USERNAME / MAIL_PASSWORD 未設定のためメール送信をスキップ")
        return
    body = (
        f"ティックデータ（{TARGET_FORMAT} / {EXPORT_TZ}）の準備ができました。\n\n"
        f"■ ダウンロード URL\n{url}\n\n■ ZIP 解凍パスワード\n{pwd}\n\n"
        f"■ 有効期限\n{expire}\n"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"【データ送付】ティックデータ ({TARGET_FORMAT})"
    msg["From"] = MAIL_USERNAME
    msg["To"] = to_addr
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(MAIL_USERNAME, MAIL_PASSWORD)
        s.send_message(msg)
    log.info("メール送信完了: %s", to_addr)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if not ROOT_FOLDER_ID or not DISTRIBUTION_FOLDER_ID or not TOKEN_JSON:
        log.error("Private-reference / distribution roots and token must be explicitly configured")
        return 2
    roots = gdrive.DriveRoots(ROOT_FOLDER_ID, DISTRIBUTION_FOLDER_ID)
    if TARGET_FORMAT not in ("NONE", "MT4", "MT5", "BOTH"):
        log.error("TARGET_FORMAT が不正: %s", TARGET_FORMAT)
        return 2
    if TARGET_FORMAT != "NONE":
        log.error("Dukascopy sync is PRIVATE_REFERENCE only; distribution is prohibited")
        return 2
    if EXPORT_TZ not in mt_export.TZ_MODES:
        log.error("EXPORT_TZ が不正: %s", EXPORT_TZ)
        return 2

    service = gdrive.service_from_token_json(TOKEN_JSON)
    now = fetcher.utcnow()
    failures: list[str] = []
    new_csvs: dict[str, Path] = {}

    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        con = duck.connect(threads=2, memory_limit="4GB", temp_dir=tmpdir / "duck")
        for inst in resolve(SYNC_SYMBOLS):
            try:
                csv = sync_symbol(service, con, inst, tmpdir, now, roots=roots)
                if csv:
                    new_csvs[inst.code] = csv
            except Exception as e:
                log.exception("[%s] 失敗: %s", inst.code, e)
                failures.append(inst.code)

        # This private acquisition job never creates, cleans, shares or mails exports.
        con.close()

    if failures:
        log.error("失敗: %s", ", ".join(failures))
        return 1
    log.info("全銘柄完了")
    return 0


if __name__ == "__main__":
    sys.exit(main())

