# fx-tick-sync（GitHub リポジトリ側）

このフォルダの中身をそのまま `github.com/yamkou/fx-tick-sync` の main に置く。
**データファイルや認証ファイルはここには入れない**（.gitignore で除外済み）。

```
.github/workflows/tick_sync.yml   毎週土曜 07:00 JST に sync_and_upload.py を実行
fxtick/                           共通モジュール（Google Drive FX 側にも同じものを置く。GitHub 側を正とする）
sync_and_upload.py                Actions: Dukascopy 差分取得 → Drive の <CODE>_<YYYY>_<MM>.parquet を統合更新 →（任意）暗号化 ZIP 配布
app_cloud.py                      Streamlit Community Cloud: Drive の Parquet を MT4/MT5 形式に変換して ZIP ダウンロード
requirements.txt                  Streamlit Cloud 用（Cloud はこのファイル名しか読まない）
requirements-actions.txt          Actions 用（軽量）
```

## Secrets（Settings → Secrets and variables → Actions）
| 名前 | 値 |
|---|---|
| `GDRIVE_FOLDER_ID` | Drive の `FX` フォルダ ID（URL 末尾） |
| `GDRIVE_TOKEN_JSON` | PC の `%USERPROFILE%\.fxtick\token.json` の中身をそのまま貼る |
| `MAIL_USERNAME` / `MAIL_PASSWORD` | ZIP 配布のメール通知用（Gmail アプリパスワード）。使わなければ不要 |

## Streamlit Cloud の Secrets（app_cloud.py をデプロイする場合）
`GDRIVE_FOLDER_ID` / `GDRIVE_TOKEN_JSON` / `APP_PASSWORD` の 3 つ。`APP_PASSWORD` が無いと起動しない（旧版の既定 "secret123" は廃止）。

## 手動実行（Actions → Weekly Tick Data Sync → Run workflow）
- `target_format`: NONE / MT4 / MT5 / BOTH（配布 ZIP を作るか）
- `export_tz`: broker（冬GMT+2/夏GMT+3）/ utc
- `recipient_email`: 指定するとパスワードをメール送付。空なら Step Summary に表示（ログではマスク）
- `sync_symbols`: `XAUUSD,USDJPY` のように絞る。空で全銘柄

## 注意
- OAuth 同意画面が「テスト」状態だとリフレッシュトークンが 7 日で失効する。「本番」に公開しておく。
- `fxtick/` を修正したら Google Drive FX 側の `fxtick/` にもコピーする（中身は完全に同一）。
