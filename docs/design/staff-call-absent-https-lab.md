# call・absent限定の合成HTTPSラボ

記録日: 2026-11-11。[ADR-0017](../adr/ADR-0017.md)のサーバー側API検証環境。
8443の閲覧専用ラボ、8444のcall限定ラボ・UIは変更しない。
**今回の8445画面は閲覧専用**で、不在や呼出を実行する新UIは未接続である。

## 起動と確認

WSLのリポジトリルートから実行する。`omachi_tls_dir`は利用者が設定済みのリポジトリ外TLSディレクトリを指す。
共有資格情報をコマンドやURLへ入れない。32文字以上の印字可能ASCIIを非表示で入力する（空白だけは不可）。
証明書と秘密鍵は従来のstaff.localhost用を利用し、所有者・権限・リンク・ペア検証を維持する。

```bash
.venv/bin/python -B -m staff_call_absent_https_lab \
  --cert "$omachi_tls_dir/staff.localhost.pem" \
  --key "$omachi_tls_dir/staff.localhost-key.pem" \
  --check

.venv/bin/python -B -m staff_call_absent_https_lab \
  --cert "$omachi_tls_dir/staff.localhost.pem" \
  --key "$omachi_tls_dir/staff.localhost-key.pem"
```

`--check`はTLS・資格情報・ポート・合成DB準備を検証し、サーバーを起動せずDBも破棄する。
通常起動は`https://staff.localhost:8445/staff/login`。CLIのポートは8445固定で、HTTPやLANへフォールバックしない。
実DB・モデル・待受ホスト・worker・操作範囲を指定するオプションはない。
Windows側の信頼設定・名前解決は[既存HTTPS手順](staff-readonly-ui-https.md)に従い、
信頼ストア・hosts・ファイアウォールは自動変更しない。

**同じstaff.localhostのラボを同時に使わない。**
8444等で結果不明の操作があれば、旧ラボで先に照会・解決する。未確定のまま停止して別ポートへ移らない。
Cookieはポート間で共有される一方、sessionStorageとWeb Locksはorigin別であり、
8445から8444の未確定記録を読んだり排他したりできない。
一時DBは通常終了で破棄され、再生成後にはdatabase_id・write_epochも変わる。
消滅した旧DBの操作結果を新DBから回収する仕組みはない。

## サーバーの境界

- `/tmp/omachi-migration-*`の新規private一時領域にだけDBを生成する。
  空の合成v1へ既存ストアで受付を作り、その合成DBだけを既存移行機構でv2へ移行する。
  revision_required・訂正無効を明示確認し、環境変数の実DB・実モデル設定を使わない。
- 合成行はwaiting・called・absent各1件、seated3件、cancelled1件。
  予測JSONと初回応答は既存受付ストアを通して生成し、後から修復・上書きしない。
  実モデルは読込不能な一時パスを指定するため、モデル性能の検証環境ではない。
- `/staff/`のGET/HEAD、セッションPOST/DELETEを従来どおり通す。
  業務POSTは`/staff/receptions/{id}/operations`のcall・absentだけに限定する。
  HTTP／異なるHost、クエリ、重複Content-Type、圧縮本文等を拒否し、本文4KiB・受信5秒を上限にする。
  重複JSONキー、NaN/Infinity、不正UTF-8、余分なフィールド、不正な型・UUID・actionを拒否する。
- return・seat・cancel・訂正・品質操作・旧更新API・顧客用APIは拒否する。
  許可actionでも、既存Cookie・Origin・CSRFとストアの操作ID・revision・write_epochを必ず通す。
  スタッフ共有主体のままであり、個人の本人確認や品質承認権限を追加しない。
- 応答は`X-Omachi-Preview: synthetic`、`X-Omachi-Preview-Actions: call,absent`で識別する。
  既存call UIの完全一致条件を満たさないため、8445ではcall UI・ジャーナルを開始しない。
  write_enabledやallowed_actionsはストアの能力情報であり、ラボの全操作の許可を意味しない。
  検証済みUIがない今回、APIの結合試験は合成データの自動テストで行う。

既存call限定ゲートのパラメータ化・許可拡張を避けるため、新入口に小さな固定ゲートを置く。
TLS・資格情報検証・閲覧専用ゲート・一時領域保護・移行・認証・業務処理は既存機構を使う。
ゲートの重複による保守コストはあるが、将来の共通化は既存ラボの回帰試験を条件に別途判断する。

## データと再送の規則

called→absentが新しい不在遷移。waiting・seated・cancelledからの不在は409で拒否する。
最新のrevision・世代でabsent→absentを要求した場合はno_changeで、受付・イベント・revisionを更新せず、
その要求の操作記録だけが追加される。構文上有効な業務拒否にも必要な拒否記録が残る。
同一操作ID・同一主体・同一本文の再送は保存結果を返すだけで、DBを再更新しない。
異なるactionを同じIDへ付け替えた場合は409で拒否し、保存結果を上書きしない。

不在は受付順・ID・番号・人数・受付日時・保存予測・受付スナップショットを維持し、未案内総数にも残る。
called_atは最後の呼出時刻を維持し、不在時刻はreception_eventsのoccurred_atに記録する。
不在時に実待ち時間や案内時刻を確定しない。将来の復帰後の案内も元の受付日時から計算し、不在時間を引かない。
既存品質問題・レビュー・承認を更新／自動解除せず、新イベントに対する判定の鮮度検査も維持する。

保存結果は履歴であり現在状態ではない。call A→absent B→古いcall A再送でも現在はabsentを維持する。
absent B→return C→古いabsent B再送でも現在はwaitingを維持する。
後者のreturnと、その後のseatはストア限定の合成結合試験であり、このラボのHTTPでは許可しない。
応答喪失・タイムアウト・照会404は失敗の証明ではない。結果照会と固定再送の原則を維持する。

## 次工程のUI接続条件

今回、既存staff.mjs／staff-call.mjs／journal／HTML／CSSを変更しない。
次工程ではcall・absent共通の一件の未確定記録と同一originのWeb Lockを設計・実装する。
ロック名は既存call名を継承し、旧タブと別名の排他を取って同時送信できる状態を作らない。
旧call v1形式・guard・期限後マーカーを無視せず、競合する旧新記録、不明版、破損では新規実行を止める。
旧記録を同じ操作の結果照会・現在context確認で解決してから新形式を初期化する方針はADR-0017に定める。
この互換復旧と、24時間、確認時・確定直前の再取得、結果照会、固定再送が揃うまで8445のUIを有効化しない。
ポート間・端末間・ストレージ完全喪失をまたぐ完全な復旧や排他を保証したものではない。

## 自動テストと手動確認

[合成テスト](../../tests/test_staff_call_absent_https_lab.py)で正常遷移・全状態の不在可否・呼出時刻維持、
不変データと品質境界、再送、異action再利用、revision／世代競合、古い操作、同時操作、
状態更新後・イベント追加後・操作保存後の故意の例外による完全ロールバックを確認する。
応答送出時の故障、セッション失効・再ログイン、厳密JSON、既存8443・8444の境界も確認する。
実TLSはテスト用一時証明書を個別に信頼し、staff.localhostの名前検証、異名拒否、Cookie属性、
call→absent、照会・再送、認証／Origin／CSRF欠落、他操作拒否、ログアウトを試験する。
OSの証明書ストアは変更しない。実TLS試験の待受は127.0.0.1の空きポートだけに限定する。

既存[スタッフJS試験](../../tests/frontend/staff.test.mjs)には、call,absentヘッダーでwaiting/called行を表示しても
業務POSTや呼出ジャーナルを始めず、閲覧専用メニューを維持する確認を追加する。

Windowsブラウザ・実タブレットは今回未確認。次を利用者が確認する。

1. 8445の証明書警告がなく、合成データの表示とログイン・ログアウトが機能する。
2. 5秒更新・一覧内スクロール・右スワイプ／三点・詳細閲覧を維持する。
3. メニューのcall・absentを含む業務操作がすべて未接続で、Networkに業務POSTが出ない。
4. 認証期限切れ・ログアウトで受付情報が消える。新ジャーナルが作成されない。
5. 他ラボは停止し、8444の未確定操作を残して切り替えていないことを確認する。

## 2026-11-11の検証結果

- Python全件: `.venv/bin/python -B -m pytest -q -W error` → **1,826 passed in 205.15s**。
  従来1,710件と新ラボ116件が成功し、skip・失敗はない。
  新ラボ単独も **116 passed in 31.66s**。sandbox内のTestClient停止は中断し、許可された環境で再実行した。
  書込み対象は一時領域の合成データだけで、実TLS試験はloopbackの一時ポートで行った。
- JavaScript全件: **352件成功**。`node tests/frontend/reception.test.mjs`（111件）、
  `node tests/frontend/staff.test.mjs`（187件）、`node tests/frontend/staff-call-journal.test.mjs`（54件）。
  スタッフ試験へ8445相当のヘッダーでwaiting/called行が閲覧専用となる2件を追加した。
  既存callの成功通知、復旧、5秒更新を含め全件成功した。
- waiting→called→absentでrevisionは0→1→2、監査イベントは追記のみ。
  不在前後の呼出時刻、ID・番号・受付順・受付日時・人数、予測・スナップショット・初回応答・日別採番・品質テーブルを保持した。
  未案内一覧の元の順番と総数3組を維持し、実案内前の実待ち時間はNULLのままである。
  calledは不在へ変更可能、waiting／seated／cancelledは409、absentはno_changeとなる。
- 同一ID再送・照会ではDBバイトが不変。異actionへのID再利用は保存結果を上書きせず409。
  新規のrevision／世代不一致では状態・状態変更イベントを維持し、必要な拒否記録だけが残った。
  call A→absent B→A再送でabsentを維持した。store限定のreturn C後のB再送もwaitingを維持した。
  後続のstore限定seatでは元の受付から2分で計算し、return／seatのHTTP許可は追加していない。
- 不在の状態更新後・イベント追記後・操作保存後に例外を注入し、DB全体のバイトが操作前と一致した。
  同じ固定IDで回復でき、call・absentの同時同一IDでは重複イベントなし、異ID・同revisionでは一方だけ状態変更した。
  応答送出時の例外後も、照会と固定再送で二重実行せず回復した。
- 実TLSでは名前・信頼検証、異名拒否、Secure/HttpOnly/SameSite=Strict、call→absent、
  固定再送・過去のcall受領証と現在absentの区別、結果照会、認証・Origin・CSRF欠落、他操作拒否、ログアウトを確認した。
  WindowsブラウザやOSの実際の信頼ストアで確認した結果ではない。
- 8443の業務POST拒否と8444のcall限定を追加の境界試験でも確認した。
  既存ラボ・UI・認証・API・ストア・品質保護のソースは変更していない。
- 開始時125ファイルのSHA-256比較で、既存の変更はADR索引とスタッフJSテストの2件だけ。
  残り123件は一致し、実DB2件、バックアップ2件、指定実験DB、RFモデル、Excelの7ファイルを含む。
  新規はラボ・Pythonテスト・ADR-0017・本資料の4件。
- Python構文、ADR0001〜0017の連番、文書リンク、新規ファイルの空白、`git diff --check`は正常。
  commit・push・PR・merge・実DB移行・実DBモード変更は行っていない。

今回、APIを独立検証できる段階までを完了した。
2操作共通ジャーナル・旧形式復旧・排他と確認フローを実装する次工程へ進めるが、
それらが完成するまで8445の書込みUIを有効化せず、実店舗運用も承認済みとは扱わない。

## 2026-11-12追記: 共通操作基盤（ブラウザ未接続）

ADR-0017の既存判断に沿って、共通ジャーナル・排他・結果照会・旧記録互換を実装した。
新ADRは追加しない。上の2026-11-11の「未実装」は当時の記録として保持する。
**8445の画面は引き続き閲覧専用**である。新モジュールの配信ルート、staff.mjsからのimport、
ボタンやHTML/CSSへの接続は追加していない。既存8444のソースも変更しない。

### 追加ファイル

- `web/staff/assets/staff-operation-journal.mjs`: v2の一件枠、固定保存、旧v1互換読取り。
- `web/staff/assets/staff-operation-contract.mjs`: 能力、context、操作別受領証、現在状態との照合。
- `web/staff/assets/staff-operations.mjs`: 画面から独立した確認・確定・照会・明示再送と共通ロック。
- `tests/frontend/staff-operation-journal.test.mjs`: 保存障害、期限、旧新競合・旧記録解決の試験。
- `tests/frontend/staff-operations.test.mjs`: 操作横断、同時操作、認証、通信・復旧、状態巻戻し防止の試験。
- `tests/test_staff_operation_ui_contract.py`: 新規一時合成DBの実API応答とJS検証の直列化契約。

既存ファイルの変更は本設計資料だけとする。サーバー、認証、ストア、予測、モデル、品質コードは変更しない。

### 保存形式と旧形式

v2のキーは`omachi.staff.operation.pending.v2`、guardは`omachi.staff.operation.guard.v2`。
pendingは`version: 2`、`state: pending`、`operation_id`、`reception_id`、`database_id`、
`action`、`request`、`created_at`、`expires_at`を持つ。requestは次の固定4項目だけである。

```json
{
  "operation_id": "操作UUID",
  "action": "call または absent",
  "expected_revision": 4,
  "write_epoch": "世代UUID"
}
```

上記UUID文字列は説明用で、実際はnil以外の小文字UUIDだけを受け入れる。
guardを先に保存し、本文とguardを正確に読み戻してからPOSTする。
未知の版・余分な項目・重複JSONキー・破損・guardの欠落・既知の本文の変更では停止する。
同じ一件枠にcallかabsentのどちらか一つだけを保持し、未解決中は別action・別IDへ差し替えない。
認証Cookie・共有資格情報・CSRF・受付詳細・人数・予測・snapshot・応答全文は永続保存しない。

24時間到達または時計逆行では、`version/state/operation_id/reception_id/database_id/action/reason`だけの
manualマーカーへ縮小する。固定本文・revision・write_epochを捨て、再送を禁止する。
経過時間を未実行・失敗の証拠にはせず、同じ操作の結果照会と手動確認を続けられる。
縮小保存に失敗しても新規操作を許可しない。
解決済み受領証と現在contextはメモリ上に分離して持ち、未確定ジャーナルへ保存しない。
ログアウト・認証世代変更後は旧セッションで得た受領証や受付詳細をstatusへ返さない。

旧キー`omachi.staff.call.pending.v1`・`omachi.staff.call.guard.v1`と形式版1は変更しない。
旧モジュールによる厳密検証を再利用し、pending/manualを**callとして、その場所で照会のみ**行う。
旧pendingをv2へ自動変換せず、旧要求をabsentへ読み替えない。
旧結果と最新contextが対応すると確認して旧記録を解決した後に、v2の空枠を初期化する。
旧記録の復旧は安全側の照会専用で、旧固定本文のPOST再送は共通基盤から許可しない。
旧新の未確定記録の共存、未確定guardだけの残存、不明版・破損では両方を保持して停止する。
観測済みの記録が消失しても新しい空枠へ移らない。

### 共通ロックと操作API

ロック名はADRどおり`omachi.synthetic.staff.call.v1`を継承する。
`exclusive`・`ifAvailable`で利用タブが保持し、終了時に解放する。
call・absentでロックを分けない。旧callタブとの競合と、新共通タブ同士の競合を試験する。
Web Locks非対応・拒否・取得不能では新規POSTと固定再送を止めるが、保存済み操作の照会は可能である。
Web Locksはブラウザの同一originに限る。8444と8445の排他は提供しない。
sessionStorageもポート間で共有しない。タブ喪失・完全消去・別端末までの恒久排他や完全復旧は保証しない。
複数ラボを同時利用せず、未確定操作を旧ラボで解決してから切り替える従来の制約を維持する。

基盤の`prepare(action, id)`は最新contextを読み、改変不能な確認用オブジェクトを返す。
`confirm(ticket)`は同じオブジェクト・認証世代と最新contextを再確認してから、UUID発行・固定保存・POSTを行う。
受付・revision・DB・世代・設定・許可操作等が変われば送信しない。
同時実行はメモリ上のbusyでも抑え、確認オブジェクトの複製を実行権限として受け付けない。
応答にはsyntheticおよび`call,absent`の完全一致ヘッダー、contextにはv2・revision_required・
write_enabled・訂正無効・妥当なDB/世代・受付情報が必要である。
この基盤をimportしただけではストレージ、ロック、通信は開始しない。

### 操作別検証と復旧

callは既存`validCallReceipt`をそのまま使い、waiting/absentからの新規呼出だけを確認可能とする。
absentの新規変更はcalledだけから許可し、成功受領証の状態はabsent、called_atは必須とする。
送信直前の行と、番号・受付順・日時・人数・IDと呼出時刻の保持を照合し、
入力にsnapshot/予測が含まれる場合はそれらも変更されていないことを照合する。
実際のoperation-contextはsnapshot/予測を含まないため、その保存不変性はサーバー側の既存合成DB試験が担保する。
案内・取消時刻や実待ち時間の新規確定を不在成功として受け入れない。
no_changeとstate_changedはrevision増分も分けて検証し、保存済み拒否も独立した確定結果として扱う。

`lookup()`はDBの一致を確認し、`GET /staff/operations/{operation_id}`を照会する。
照会HTTP 200だけでは成功扱いせず、action・受付・操作ID・response_status・outcome・成功行または拒否を検証する。
404、汎用409、形式不正、通信喪失、15秒タイムアウトはpendingを維持する。
各POST・照会は独立したAbortControllerを持ち、一覧の5秒ポーリングから中断されない。
認証中断後に遅れて届いた応答を解決に使わない。

`retry()`は人が明示した場合だけ呼ぶための入口で、自動再送はしない。
再ログイン／再生成した基盤では先に結果照会が必要である。
期限内のv2、同じDB/世代、ロック所有、利用可能な認証と新CSRF、未受領の結果という条件でのみ、
元のID・元のaction・元のrevision・元の本文を送る。現在revisionが進んでも本文を修正しない。
確定受領証を得た後にcontext読取りが失敗した場合は、再POSTせず照会を続ける。

確定受領証の後でも現在contextを再取得する。同じDB/世代、現在revisionが受領証以上であることを確認し、
同じrevisionなら状態・呼出時刻等の一致も確認してからジャーナルを解決する。
後続変更で現在状態が進んでいれば、古いcalled/absentを現在行へ書き戻さない。
基盤は受領証と現在contextを別々に返し、一覧やDOMを変更しない。
UI接続時には全ページ一覧の再取得・鮮度確認がさらに必要であり、
context照合完了だけで「最新一覧確認済み」の成功通知を出してはならない。

### UI有効化前に残る作業

1. 新モジュールの配信ルートと、検証済み能力に限定した8445画面への明示接続。
2. call/absent別の確認文言・状態別ボタン・フォーカス・通知と、5秒更新を含む画面の結合試験。
3. 確定後の全ページ一覧取得と、取得失敗・古い一覧に対する「最新状態確認待ち」の表示。
4. 実ブラウザのsessionStorage/Web Locks、再読み込み・再ログイン・複数タブと実タッチの確認。
5. 未確定記録を失った場合や旧合成DBが消滅した場合の手動照合手順の確認。

実店舗認証、別端末の完全排他、実DBへの有効化、return/seat/cancel、LINEは今回の対象外である。


### 2026-11-12の検証結果

- Python全件: `.venv/bin/python -B -m pytest -q -W error` → **1,834 passed in 190.58s**。
  既存1,826件と新規のAPI/JS契約8件が成功し、失敗・skipなし。
- JavaScript全件: `node --test tests/frontend/*.test.mjs` → **485件成功**、失敗・skipなし。
  既存352件と新規133件（journal 68件、操作基盤65件）。
  ロック・ストレージ・通信・時計は注入した試験環境であり、実ブラウザの検証結果ではない。
- 新規契約試験は取消済み受付を通常一覧へ露出させず、一時合成DBからread-onlyでIDだけ取得し、
  認証付きcontextと実受領証をJS側で検証した。
- 開始時のリポジトリ内126ファイルをSHA-256比較し、本資料を除く125ファイルが一致した。
  実DB2件・RFモデル・Excelの4件を含む。
  外部バックアップ2件と指定実験DB1件は、最終全件テストの前後で別途SHA-256一致を確認した。
  保護対象7件は一致し、保護ディレクトリ内のファイル集合も変化していない。
- 既存8443/8444の入口・UI・旧journal、8445のラボ、認証/API/ストア/予測/品質コードは変更なし。
  8445の閲覧専用動作は既存JS試験でも維持した。
- 新規ファイルを含む構文・改行・空白検査と`git diff --check`は正常。
  ブランチ`feature/seat-correction`、HEAD`f26d85d`を維持し、indexは空。
  commit・push・PR・merge、実DB移行・モード変更は行っていない。

### 2026-11-12追記: P2認証失効時の基盤内fail-closed

最終レビューで、`authFailure`を省略／無処理にした場合、401/403を受けても外部の
`security().active`がtrueのまま残り、`status()`が過去の受付詳細と受領証を返す問題を再現した。
外部通知の成功に情報閉鎖を依存させず、共通操作基盤の中で失効を確定するよう修正する。
ADR-0010・0016・0017の認証失敗時の閉鎖と固定要求保持を満たす安全性修正であり、
権限・業務API・通常の状態遷移の設計は変更しない。

#### 失効と非同期応答

- 現在の認証世代のリクエストが401/403を返した時点で、JSON本文の読込みや
  ラボ能力ヘッダーの検証より先に、インスタンス内の`authExpired`をtrueにする。
  `phase`は`auth_expired`となり、応答世代を進める。
- 確認用context・確認チケットの有効性・表示用受領証・現在context・照会済み認証世代を閉じる。
  `status()`はreceipt/currentをnullで返し、call/absentのprepare・confirm・retry・lookupを停止する。
  新しいUUIDやPOSTを発行しない。保持済みチケットのcontext getterもnullとなる。
  一度失効したインスタンスは、外部のactive/versionの変更やコールバック完了では再開しない。
- 失効処理はジャーナルの解決・削除・本文変更を一切行わない。通知前に閉鎖を完了し、
  通知関数の省略、無処理、Promise待機・拒否、同期例外で失効状態が変わらないようにする。
  操作成功／未実行／失敗は401/403だけでは確定しない。
  通常の24時間到達による最小マーカーへの縮小は、既存ジャーナル規則どおり別に働く。
- 各通信は開始時の外部認証世代と内部応答世代を捕捉する。
  fetch完了時とJSON本文完了時の両方で、有効性・世代・インスタンスの生存を検査する。
  失効・停止・認証世代変更の後に届いた200で、表示情報や確認を復活させたりジャーナルを解決したりしない。
  古い世代の401で、新しい認証世代を誤って失効させることも避ける。
- 同じインスタンス内の操作通信は従来どおりbusyで直列化する。
  将来UIを接続する際、外側のセッション／一覧ポーリングが401/403を検出した場合は、
  その通信に対応するインスタンスの`expireAuthentication()`へ通知する。
  これも同じ不可逆の閉鎖処理で、進行中のPOST／照会を最新表示の復元に使わせない。
  この通知は冪等で、再認証／再送の入口ではない。今回、既存UIやポーリングへの配線は追加しない。

既に呼出側がコピーした値や描画済みDOMを、基盤から遡って消すことはできない。
UIは失効を受けたら自分の表示・コピーも破棄する必要がある。
基盤は失効後の公開APIからの再取得と、遅延応答による復活を禁止する。

#### 再認証と安全な再初期化

自動解除や新しい認証方式は追加しない。失効したインスタンスは使い捨てとし、次の順で復旧する。

1. 失効表示を閉じ、古い基盤を`stop()`する。stopも応答世代を進め、表示用メモリを閉じて
   Web Lockを解放するが、未確定ジャーナルには触れない。
2. 呼出側が既存のスタッフログインフローで再認証し、新しいHttpOnly Cookieの発行と、
   同じHTTPSオリジンの`GET /staff/session`成功を確認する。
   既存のセッション応答検証を再利用し、外部認証世代を更新して、新CSRFをメモリだけに保持する。
   コールバックが完了したことやactiveフラグの変更だけを認証成立の証拠にしない。
3. 認証確認後に新しい`createStaffOperations`を作る。古いチケット・受領証・contextを渡さない。
   初期化そのものでは通信・POST・UUID発行・自動再送を行わない。
4. pending/manualがあれば、まず既存操作IDで`lookup()`する。
   新インスタンスでも、新規操作は未確定枠に阻まれ、先行照会なしのretryも禁止される。
   認証付きoperation-contextでDB識別子・現在世代を確認し、保存結果を取得後にもう一度現在contextを照合する。
   pendingは保存時の世代との一致も必須。manualでは旧世代／本文を既に捨てているため再送せず、
   現在の妥当なmetadataとDB・受付・action・結果・現在revisionの照合で照会だけを行う。
5. 404・通信失敗・DB／世代不一致では未確定を維持する。旧v1はcallのまま照会専用で扱う。
   結果と現在状態を確認できた場合だけ既存規則で記録を解決し、UI接続後は全ページ一覧も再取得する。
   再認証・初期化だけで、新しいIDによる操作や固定POSTを自動実行しない。

新インスタンスへ誤って古い認証フラグを渡しても、実際のセッションが無効なら最初のcontextが401/403となり、
再び閉鎖する。古いインスタンスの遅い応答は、新インスタンスの表示や認証を変更しない。
新しい復帰用APIや、失効したインスタンスを再開するスイッチは設けない。

#### 回帰検証

修正前にP2用の12件を追加し、従来65件成功・追加12件失敗を確認した。
修正後は通知省略／無処理／非同期／同期例外／Promise拒否／外部認証世代変更のすべてで閉鎖する。
追加の試験では、call/absentのPOST・照会前context・結果照会・結果後contextの401/403、
能力ヘッダーなし・拒否本文を読まないこと、固定ジャーナル不変、UUID追加なしを確認する。
外部ポーリングの401/403後に遅れる200ヘッダー・200本文・現在context本文も破棄する。
旧v1 pending/manual、新インスタンスでの既存ID復旧、DB／世代変更、無効セッションでの再初期化、
停止済み旧インスタンスの遅い401を含む。

今回の修正後の検証結果:

- Python全件: `.venv/bin/python -B -m pytest -q -W error` → **1,834 passed in 100.06s**。
  実TLSを使う既存試験を含み、失敗・skipなし。書込みは既存試験の一時合成DBに限定した。
- JavaScript全件: `tests/frontend/*.test.mjs`の5ファイルをそれぞれ`node --test`で実行し、
  **533件成功**、失敗・skipなし。内訳はreception 111、staff 187、旧call journal 54、
  共通journal 68、共通operations 113。共通operationsへ今回48件を追加した。
  8444の既存呼出・復旧・通知と、8445の閲覧専用契約も維持した。
- 作業開始時135ファイルのSHA-256と比較し、今回の差分は共通operations・対応JS試験・本資料の3件のみ。
  他132件は一致し、実DB2件・バックアップ2件・指定実験DB・Excel・RFモデルの保護対象7件も一致した。
  既存の未コミット7ファイルを保持し、新たなファイル追加、ステージング、commit・pushは行っていない。
  HEADとindexも作業開始時のまま。`git diff --check`は正常。
- 失効通知と遅延応答は注入した通信・時計・ストレージによる回帰試験であり、
  実ブラウザの再ログイン／タブ間操作を検証した結果ではない。
  共通基盤のUI接続時には、セッション検証、失効通知の配線、描画済み情報の破棄、
  同じIDの照会からの復旧をWindowsブラウザでも確認する必要がある。
