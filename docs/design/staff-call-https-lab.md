# 呼出限定の合成HTTPSラボ

記録日: 2026-11-09。[ADR-0015](../adr/ADR-0015.md)の検証環境。
通常の`staff_https_lab.py`は変更せず、閲覧専用・legacy・訂正無効を維持する。
今回の新入口もUIは閲覧専用であり、ボタンから呼出を実行する機能はない。

## 起動

WSLでリポジトリのルートから実行する。`omachi_tls_dir`は既存のリポジトリ外TLSディレクトリを指す変数。
実DB・モデル指定、HTTP・LAN公開、汎用書込み許可のオプションはない。

```bash
.venv/bin/python -B -m staff_call_https_lab \
  --cert "$omachi_tls_dir/staff.localhost.pem" \
  --key "$omachi_tls_dir/staff.localhost-key.pem" \
  --check

.venv/bin/python -B -m staff_call_https_lab \
  --cert "$omachi_tls_dir/staff.localhost.pem" \
  --key "$omachi_tls_dir/staff.localhost-key.pem"
```

開発用共有資格情報を非表示で入力する（32文字以上の印字可能ASCII、空白だけは不可）。
引数・URL・ソースへ資格情報を記入しない。ブラウザは`https://staff.localhost:8444/staff/login`。
証明書は従来と同じstaff.localhost用を使える。秘密鍵はWSL利用者所有・0600、証明書・鍵は通常の単一リンクファイル。
Windowsの信頼設定・名前解決は[既存HTTPS手順](staff-readonly-ui-https.md)に従う。
自動的に信頼ストア・hosts・ファイアウォールを変更せず、検証を無効化しない。

同ホストのCookieはポートで分離されない。8443の通常ラボ等は停止してから利用する。
サーバーは127.0.0.1だけで待ち受け、WindowsへのWSL転送・証明書信頼は利用者が確認する。
`--check`はサーバーを起動せず、生成・移行した合成DBも終了時に破棄する。

## 許可する範囲

- 一時領域の新規合成v1だけを生成・v2移行し、そこでのみrevision_requiredにする。訂正は無効。
  移行ストレス試験用fixtureの予測JSONはAPI応答契約を保証しないため、その既存行は使わない。
  空の合成v1へ既存ストアで受付・状態を生成してから移行する。
  予測は既存ストアのモデル未設定時の正規応答で、保存後の修復・上書きは行わない。
- `/staff/`のGET/HEADとセッションPOST/DELETEを従来どおり利用する。
- `POST /staff/receptions/{id}/operations`の厳密なcall要求だけを既存アプリへ通す。
  本文はoperation_id、action、expected_revision、write_epochの4項目。
  ログインCookie・同一Origin・CSRFが必須。共有トークンを代わりに付けてもCookie認証を回避できない。
- 他操作、旧更新API、訂正・品質のPOST、顧客用APIは拒否する。読取りに汎用allowed_actionsがあっても権限は増えない。
- モデルには一時領域の不存在パスを指定する。環境変数による実DB・モデル指定を使わない。
  時刻と人数は合成fixtureであり、実店舗データではない。

## 結果不明時の検証

自動試験では成功応答を利用しない状況から、同じ操作IDの照会・固定再送で元の結果を得ることを確認する。
案内・取消等への権限拡大や、本番での復旧完了を意味しない。
結果照会の200は取得成功で、保存されたresponse_status・outcomeも確認する。
現在の状態はoperation-contextから別途取得する。404、応答喪失、認証切れだけでは失敗と断定しない。
期限切れの再ログイン後も同じ操作を照会し、新IDを発行して自動再試行しない。
UIへの操作接続とsessionStorageの保存は未実装。復旧方針の詳細はADR-0015を参照する。

## 手動確認と残る範囲

Windowsブラウザで新入口のHTTPS・合成表示・ログイン・閲覧・ログアウトを確認する。
呼出UIはないため、メニューが実行不能のままであることも確認する。
開発者ツールへ秘密情報や操作スクリプトを貼り付ける手順は提供しない。
callの書込み・拒否・再送は合成API試験で確認し、確認画面を含むブラウザ操作は次工程とする。
店舗LAN・個人認証・複数worker・実DB移行・訂正有効化・LINEは対象外。

## 自動検証結果

2026-11-09の今回実行結果:

- 新規ラボ試験: **79 passed in 17.73s**。
- Python全件: **1,692 passed in 158.96s**（既存1,613件＋追加79件）。
  `.venv/bin/python -B -m pytest -q -W error -p no:cacheprovider -o faulthandler_timeout=30`。
  sandbox内ではTestClient起動時に停止したため中断し、許可された環境で再実行した。
- JavaScript全件: **231 passed / 0 failed**。
  `node tests/frontend/reception.test.mjs`（111件）、`node tests/frontend/staff.test.mjs`（120件）。
- 実TLSでは一時証明書を個別のクライアント信頼対象として使い、staff.localhostの名前も検証した。
  CookieのSecure/HttpOnly/SameSite=Strict、CSRFなしの拒否、call成功・再送・照会、seat拒否、ログアウトを確認した。
  OSの信頼ストア・実証明書・hostsは変更していない。
- waiting/absentからcallでrevisionが1増え、旧イベントを残して一つのイベント・操作記録が増える。
  同一再送はDBバイト不変。同時の異なる操作ID・同じ期待revisionは一方だけ成功する。
  スナップショット・予測JSON・初回応答・日別採番は保持し、受付順・受付日時・人数を維持した。
  呼出で案内時刻・実待ち時間は作られない。
- イベント追記後／操作結果保存後の故意の例外は503となり、DBバイト全体が操作前と一致した。
  保存後の実際の応答送出例外でも、結果照会・同じIDの再送で一度だけの確定を確認した。
  期限切れ・再ログイン後の照会、誤った世代・古いrevision、他Host・ネイティブトークンによる回避拒否も確認した。
- 開始時の既存115ファイル中、変更はADR索引の追記だけ。残り114ファイルのSHA-256は一致した。
  実DB2件、バックアップ2件、指定実験DB、RFモデル、Excelの7ファイルを含む。
  通常ラボ・既存API・認証・品質保護・予測・前処理・評価・顧客UI・スタッフUIは変更していない。
- ADR番号0001〜0015、新規・索引の相対リンク42件、`git diff --check`と新規ファイルの空白検査は正常。
  commit・push・PR・mergeは実行していない。

Windowsブラウザでの新入口の確認、UIからの確認画面・送信・sessionStorage復旧、複数タブの復旧は未検証。
今回のAPI試験成功だけで、その実装や実店舗利用を承認済みとはしない。
