# Step 5-B: スタッフブラウザ認証・操作対象の読取り

今回追加するのは認証基盤と読取りAPIである。スタッフ画面、ログイン画面、LINE連携は含めない。
受付の状態遷移、スキーマ、移行コード、予測・学習処理、顧客用画面は変更しない。
実DBの移行、モード変更、訂正機能の有効化は行わない。

## 認証の境界

従来の `X-Staff-Token` はネイティブクライアント向けに維持する。
ブラウザセッションは既定で無効で、アプリ生成時の
`create_app(staff_browser_origin="https://staff.localhost[:port]", ...)` で明示的に構成する。
共有トークンの設定も必要。実際のURLには省略記号や角括弧を含めない。
この引数はDBの設定・スキーマ・訂正フラグを変更しない。

同じホストのポート違いではCookieを分離できないため、顧客の `localhost` / `127.0.0.1`
とは別の `staff.localhost` を使う。このホストでは `/staff/` のみ提供し、顧客HTML・JS・公開APIを提供しない。
顧客画面からスタッフ画面のセッションを開始・参照するCORS許可も追加しない。

| API | 認証・用途 |
| --- | --- |
| `POST /staff/session` | HTTPSのスタッフオリジン、同一Origin、共有X-Staff-Tokenを必須にしてログイン |
| `GET /staff/session` | Cookieを検証し、CSRFトークン・残り有効秒数・共有認証主体を取得 |
| `DELETE /staff/session` | Cookie + Origin + CSRFを検証し、サーバー側セッションとCookieを破棄 |

ログイン資格情報はヘッダーからのみ受け付ける。URL・JSON応答・DB・監査主体へトークンやその指紋を保存しない。
将来のログイン画面では利用者が入力した値を送信中だけ扱い、HTML/配布JSへの埋込みや
localStorage/sessionStorageへの保存、コンソール出力、アクセスログへのヘッダー記録を行わない。
ログイン画面や資格情報の配布手順は次工程の対象で、今回は作成しない。

サーバー側のプロセスメモリにセッションを持ち、256ビット乱数の不透明Cookieを発行する。
サーバーの検索キーはCookieのSHA-256で、Cookieは `__Host-omachi-staff`、
`Secure; HttpOnly; SameSite=Strict; Path=/`、Domainなしとする。セッションIDはJSONに返さない。
CSRFは別の256ビット乱数で、認証済み応答から取得してメモリ内だけで扱う。

絶対期限1時間、アイドル期限15分。ポーリングしても絶対期限を延長しない。
ログイン成功時に以前のセッションを失効し、セッション固定を防ぐ。
最大256セッション、ログイン試行は成功・失敗合計で1分10回までとし、上限時は新規発行しない。
再起動・ログアウト・期限切れでは認証を拒否する。セッション無効時にヘッダー認証へ戻さない。
資格情報・Origin・CSRF・Cookieの重複も拒否する。

セッションでのPOST/DELETEには同一Originと `X-CSRF-Token` を必須とし、SameSiteだけに依存しない。
ログインにはCookie用CSRFはまだないため、同一Originとカスタム資格情報ヘッダーを要求する。
認証応答は `Cache-Control: no-store`。これは
[OWASPのセッション管理](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)
と [CSRF対策](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
に基づく構成である。

監査主体は従来と同じ `shared_staff_token / shared-staff`。
個々のスタッフ名を本人確認済みと扱わない。ブラウザセッションから品質レビュー・評価のPOSTはすべて拒否し、
`clear` / `eligible` を許可しない。品質参照の `training_use=blocked` も維持する。

## 開発環境と店舗運用

現行のloopback接続制限を維持する。HTTPではブラウザセッションを使えず、Secure属性を外す例外は設けない。
WSL2で試す場合も、名前解決、信頼済みローカルTLS証明書、HTTPSによる直接接続が必要。
TLS終端の設定・証明書作成・ファイアウォール変更はこの工程では実施しない。
`Forwarded` / `X-Forwarded-*` によるスタッフ接続は拒否し、プロキシ信頼を追加しない。
ASGIサーバー側でも転送ヘッダー処理を無効にする（Uvicornなら `--no-proxy-headers`）。

今回のセッションは単一プロセス用で、複数workerの共有ストアを提供しない。
プロセス再起動は全セッション失効になり、未完了操作は再ログイン後に保存済み操作IDで結果照会する。
複数workerや店舗LANへの公開は、このまま有効化せず、専用ホスト・TLS・ネットワーク境界・
共有セッションストア・失効運用・ログ設定を別途レビューする。loopback制限だけを解除して公開しない。
共有資格情報、共有のログイン試行上限による可用性の制約は残る。

## 新しい読取りAPI

| API | 応答 |
| --- | --- |
| `GET /staff/receptions?view=active` | waiting / called / absentを元のreception_order昇順で取得 |
| `GET /staff/receptions?view=recently_seated` | 現在有効な案内完了を案内event_id降順で取得 |
| `GET /staff/receptions/{id}/operation-context` | 1受付の現在状態・revision・有効案内イベントと共通メタデータ |

全ルートで毎回スタッフ認証が必要。既存の `/queue`、スタッフ詳細・履歴・結果照会APIは応答形式を維持する。
新しい応答はカラムのホワイトリストから組み立て、保存予測JSON・監査操作の本文・認証情報は含めない。
顧客APIに新しい内部情報を追加しない。

共通 `metadata` は `schema_version, database_id, write_epoch, staff_write_mode,
seat_corrections_enabled, write_enabled, disabled_reason, view_version`。
受付行はID、日付付き番号、受付日時、受付順、大人・子供・合計人数、状態、状態日時、実待ち時間、
revision、allowed_actions、valid_seating_eventに限定する。

`valid_seating_event` は現在状態がseatedで、最新イベントもseated、元の状態が
waiting/called/absent、案内時刻が現在値と一致する場合に返す。
event_id、from_status、to_status、occurred_at、correctableを返す。
訂正済みの古い案内を対象として提示せず、再案内後は新しいイベントIDを返す。
矛盾する現在値は自動修復せず拒否する。過去の無効化された案内は既存の監査履歴で参照する。

`allowed_actions` は既存の状態遷移表から導出し、同じ状態にする操作を表示しない。
v2かつrevision_requiredでのみ操作を提示する。訂正にはさらに訂正フラグと有効イベントを要求する。
v1とlegacyでは読取りのみとし、v1はrevision / database_id / write_epochをnullとして操作を提示しない。
不明スキーマや途中でのDB識別子・世代の変化は既存の検証で拒否し、移行・追従・自動修復しない。

一覧の既定件数は50、最大100。最近の案内は実案内時刻から24時間が既定で、hoursは1〜168。
受付日では絞らず、日付をまたぐ受付を落とさない。同一時刻の順序はevent_idで区別する。
activeには日付の下限を設けず、未案内が古い場合も残す。

ページングはHMAC付きカーソルで、受付順または案内イベントIDを使う。
各ページのメタデータ・受付・最新イベントを同じ読取りトランザクションで取得する。
カーソルは取得条件・最初の時点・全体最大イベントID・DB設定に結び付け、5分で失効する。
ページ途中で受付イベントやモード・訂正フラグが変わると409となり、先頭から再取得する。
破損・改変・期限切れ・条件変更は422。プロセス再起動で旧カーソルも無効になる。
カーソルは認証の代用ではなく、続きの取得でもCookie/ヘッダーを検証する。
受付更新が続く繁忙時にはページングをやり直す可能性がある。将来必要なら履歴時点の一覧方式を別設計する。

## 既存の保護付き操作との接続

ブラウザでは既存の `/staff/receptions/{id}/operations` と `/seating-corrections` を使う。
ブラウザ認証で旧 `/receptions/{id}/{action}` へフォールバックする経路は拒否する。
v1・legacyではセッションによる保護付きPOSTも拒否する。
通常5操作には `require_revision_mode=True` を渡し、書込ロック内でもモードを検証する。
既存スキーマのrevision_requiredからlegacyへの逆戻り禁止制約も維持する。
ネイティブのX-Staff-Token経路は従来のv1/v2互換性を維持する。

操作の前にoperation-contextを取得し、表示した対象・状態を確認する。
409は自動でrevisionを書き換えて再実行せず、最新状態を表示して人の判断を待つ。
通信失敗は既存操作IDと同じ本文を保持し、結果照会または同じIDで再送する。
操作結果は過去の結果であり、現在値は読取りAPIで別途取得する。
ログアウト・期限切れ・再起動後も、新しいIDで未確定操作を再実行しない。
今回、ブラウザ側の未完了操作管理はまだ実装していない。

受付状態・revision・イベント・操作結果・訂正品質問題の原子性は既存ストアで保証する。
セッション用テーブル、スキーマ変更、状態遷移の追加は行わない。
品質保留の解除や学習候補化も行わない。

## 検証と次工程

書込みテストはすべてtemporary_workspace内の合成DBのみで行う。
新規テストではCookie/CSRF/Origin/Host/期限/再ログイン/失効、権限、v1/v2、
旧経路拒否、再送・照会・古いrevision・並行更新・ロールバック、範囲・ページング・
同時刻と日付またぎ、未知スキーマ、公開APIへの漏洩を検証する。
WALの合成DBでは読取り途中に更新を確定させ、単一応答に新旧の状態が混ざらないことも確認する。

必須シナリオは18:00受付001、18:10誤案内、18:10:30受付002、18:11訂正、18:25再案内。
最近の案内から有効イベントを選び、訂正後は受付001→002の順を回復する。
古い案内要求の再送は保存結果のみを返し、001は待機のまま。
再案内時は25分を記録し、新しい訂正対象イベントを返す。
002のsnapshot_context/prediction_gateと両受付の元のスナップショット・予測を保持する。

スタッフUI工程では、HTTPSの実ブラウザでCookieとCSRF、ログイン・失効・通信喪失時の操作ID保持を確認する。
右スワイプ/三点メニューは開くだけ、番号は詳細を開く、案内・取消・訂正には確認を設ける。
メニュー表示中に情報が更新された場合も再確認し、訂正は有効イベントIDと理由を送る。
案内直後の訂正導線と最近の案内履歴は仮設計として実機検証する。
スワイプ、個別スタッフ認証、店舗LAN運用、LINE通知はこの工程では実装しない。

## 今回の実行結果

- 全Python: `1537 passed`（既存1456件 + 新規81件、`-W error`）。
- JavaScript: `111 passed / 0 failed`。
- 認証・読取り・既存スタッフAPIの関連テストは初回243件すべて成功し、追加した対象なし/不正IDも全件実行で成功。
- 001→誤案内→002→訂正→再案内のブラウザ認証付き結合試験、古い案内要求の再送、25分の実待ちを確認。
- 障害注入で受付・イベント・操作台帳がDBファイルのバイト列まで戻ること、同時操作は200/409になることを確認。
- 実DB2件、バックアップ2件、指定実験DB、Excel、モデル、推論/前処理/評価、顧客UIの計21ファイルについて作業前後のSHA-256一致を確認。
- 実機ブラウザのTLS・Cookie・スワイプ試験は未実施。実DB設定変更、commit/push/PR/mergeは行っていない。
