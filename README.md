# fx-tick-sync（GitHub リポジトリ側）

Phase 2 は `codex/dukascopy-isolation` 上で開発中です。main への反映・実運用の切替は別途行います。
**データファイルや認証ファイルはここには入れない・commit しないでください。**

Phase 2 の設定、旧履歴の明示登録、LOCAL_TEST 手順、配布境界と未実行テストは
[docs/PHASE2_BOUNDARIES.md](docs/PHASE2_BOUNDARIES.md) を参照してください。
Dukascopy は PRIVATE_REFERENCE / LOCAL_TEST 専用です。UNKNOWN は両用途で拒否します。

Phase 3A のクロスプラットフォーム設定、Collector/Terminal ID、VPS移管手順、
外部監視・通知の契約は [docs/PHASE3A_FOUNDATION.md](docs/PHASE3A_FOUNDATION.md) にあります。
`collector_plan.py` は設定を検証するだけで、収集・MT5起動・通知は行いません。
`local_export.py --config ...` は任意設定です。従来の引数形式も維持しています。

Phase 3B の内部監視、外部heartbeat、SQLite状態保存、通知adapterは
[docs/PHASE3B_MONITORING.md](docs/PHASE3B_MONITORING.md) を参照してください。
`python -S -B monitor_demo.py` は外部通信なしの合成障害・復旧デモです。
実MT5/cTrader接続、公開受信endpoint、LINE実送信はまだ配備していません。

Phase 3C のHMAC認証付きHTTP入口、Monitor自己監視、通知・復旧準備は
[docs/PHASE3C_PRODUCTION_MONITORING.md](docs/PHASE3C_PRODUCTION_MONITORING.md) を参照してください。
`monitor_server.py --config configs/production-monitor.example.json --check` は設定検証のみです。
本番起動には承認済みSecret、WSGI環境、TLS proxy、boot/sequence運用の準備が必要です。

Phase 3D のオフライン検証結果・不足依存・後日の専用venv構築コマンド・実機チェックリストは
[docs/PHASE3D_READINESS.md](docs/PHASE3D_READINESS.md)、配置テンプレートと導入手順は
[deployment/README.md](deployment/README.md) を参照してください。オフライン検証完了は本番稼働の承認ではありません。

Phase 3E では専用 `.venv` で旧SKIP 7件とWaitress localhost統合を検証しました。
再構築コマンド・固定バージョン・結果は [docs/PHASE3E_ENVIRONMENT.md](docs/PHASE3E_ENVIRONMENT.md) を参照してください。

Phase 4A のMT5×1 staging配置、preflight、bootstrap、secret境界、rollback手順は
[deployment/windows-staging/README.md](deployment/windows-staging/README.md) を参照してください。
Phase 4B-0 では正式な `python -m fxtick.collector` と永続senderをfake sourceへ接続しました。
[起動・停止・再起動・preflight手順](docs/PHASE4B0_COLLECTOR_RUNTIME.md)を参照してください。実MT5/cTrader接続は未実装です。

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
