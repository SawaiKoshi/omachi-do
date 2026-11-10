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
