# fx-tick-sync（GitHub リポジトリ側）

Phase 2 は `codex/dukascopy-isolation` 上で開発中です。main への反映・実運用の切替は別途行います。
**データファイルや認証ファイルはここには入れない・commit しないでください。**

Phase 2 の設定、旧履歴の明示登録、LOCAL_TEST 手順、配布境界と未実行テストは
[docs/PHASE2_BOUNDARIES.md](docs/PHASE2_BOUNDARIES.md) を参照してください。
Dukascopy は PRIVATE_REFERENCE / LOCAL_TEST 専用です。UNKNOWN は両用途で拒否します。

```
.github/workflows/tick_sync.yml   毎週土曜 07:00 JST に sync_and_upload.py を実行
fxtick/                           共通モジュール（Google Drive FX 側にも同じものを置く。GitHub 側を正とする）
sync_and_upload.py                Actions: Dukascopy 差分取得 → private-reference Drive の新規月次スナップショット
app_cloud.py                      Streamlit: 配布承認・内容照合済みの distribution Drive データのみ変換・ダウンロード
register_legacy.py                所有者が明示指定した旧履歴を別台帳へ登録（元データ変更なし）
local_export.py                   登録済み旧履歴／新規データから LOCAL_TEST 出力を新規生成
requirements.txt                  Streamlit Cloud 用（Cloud はこのファイル名しか読まない）
requirements-actions.txt          Actions 用（軽量）
```

## Secrets（Settings → Secrets and variables → Actions）
| 名前 | 値 |
|---|---|
| `GDRIVE_PRIVATE_REFERENCE_FOLDER_ID` | 別途明示設定する owner-only の参照用ルート |
| `GDRIVE_DISTRIBUTION_FOLDER_ID` | private-reference と分離した配布用ルート |
| `GDRIVE_TOKEN_JSON` | PC の `%USERPROFILE%\.fxtick\token.json` の中身をそのまま貼る |

旧 `GDRIVE_FOLDER_ID` への自動 fallback はありません。weekly sync は配布・公開共有・メールを行いません。

## Streamlit Cloud の Secrets（app_cloud.py をデプロイする場合）
上記の別々の2ルート、`GDRIVE_TOKEN_JSON`、`APP_PASSWORD` が必要です。
実在ソースの配布承認は未設定なので、既定ではダウンロード対象はありません。

## 手動実行（Actions → Weekly Tick Data Sync → Run workflow）
- 配布形式・メールの入力は廃止し、`TARGET_FORMAT=NONE` 固定です。
- `sync_symbols`: `XAUUSD,USDJPY` のように絞る。空で全銘柄

## 注意
- OAuth 同意画面が「テスト」状態だとリフレッシュトークンが 7 日で失効する。「本番」に公開しておく。
- `fxtick/` を修正したら Google Drive FX 側の `fxtick/` にもコピーする（中身は完全に同一）。

今回、Drive 側へのコードコピー・データ操作・既存履歴の登録は実施していません。
private cloud retention の法的可否は別途レビュー対象です。
