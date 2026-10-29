# Step 5-B: 合成DB専用のv1→v2移行検証

この段階の移行コマンドは、Pythonが生成する`/tmp/omachi-migration-*`内の
登録済み合成DBだけを対象とする。アプリは引き続きv1を使用し、このパッケージを
importしない。本番・開発・指定実験用DBへの移行、スタッフ訂正API、画面、学習用抽出は未実装。

## 実行範囲

`reception_migration.safety.temporary_workspace()`が所有者専用の一時ディレクトリと
合成DB登録ファイルを生成する。`synthetic.create_v1()`は保存コミット`f470ddf`の
凍結スキーマから合成データを作る。実Excel・モデル・元DBを読み込んで生成しない。
スキーマ定義の比較にはメモリDBも使うが、ファイルDB・バックアップ・復元先はすべてこの一時領域内。

明示コマンドを検証する例:

```python
from hashlib import sha256
import subprocess
import sys

from reception_migration.safety import temporary_workspace
from reception_migration.synthetic import create_v1

with temporary_workspace() as lab:
    database, _ = create_v1(lab)
    subprocess.run([
        sys.executable, "-B", "-m", "reception_migration",
        "--workspace", str(lab.root),
        "--database", str(database),
        "--expected-sha256", sha256(database.read_bytes()).hexdigest(),
    ], check=True)
# 終了時に合成DBを含む一時ディレクトリを削除する。
```

任意の作業ディレクトリを許可するオプション、保護対象の解除オプション、
アプリ起動時の自動移行は存在しない。

## パスの保護

- `/tmp`直下の専用名・所有者・0700権限・合成登録情報を確認。
- DBは一時領域の直下だけ。パストラバーサル・ディレクトリ別名を拒否。
- DB・登録ファイル・SQLite付随ファイルのシンボリックリンクや複数ハードリンクを拒否。
- `O_NOFOLLOW`、inode照合、アドバイザリロックを使用。
- SQLiteは既存ファイルを`mode=rw`で開く。誤指定時に新規DBを作らない。
- 必須のSHA-256をロック取得前後に照合。通常移行では既存のjournal/WAL/SHMを拒否。
- バックアップ・復元先は新規ファイル限定。上書きしない。

`var/receptions.sqlite3`、`var/dev-receptions.sqlite3`、ユーザーのバックアップ領域、
指定された`~/omachi-do-sandbox/seat-correction-test-20261029-124134-293403.sqlite3`は許可範囲外。
実ファイルにリンクを作るテストも行わず、一時領域内の代替保護対象で検証する。

この境界は誤指定防止用。同じOSユーザーが登録情報やディレクトリを故意に書き換える攻撃を
隔離するOSサンドボックスではない。任意の実データをコピーして登録する用途には使用しない。

## v2の構成

既存7テーブルを維持し、次の5テーブルを追加する。

| 対象 | 内容 |
| --- | --- |
| `receptions` | `revision INTEGER NOT NULL DEFAULT 0`。整数・非負制約 |
| `store_metadata` | DB識別子・書込み世代・`staff_write_mode=legacy`・`seat_corrections_enabled=0` |
| `reception_events` | 旧8列を完全保持。監査種別、操作ID、前後revision、訂正参照、理由、変更前後JSON |
| `staff_operations` | 操作ID、認証主体、要求、結果。ID再使用や履歴の更新・置換を拒否 |
| `data_quality_issues` | 原因訂正・対象受付・影響種別・根拠・規則版 |
| `data_quality_reviews` | 追記型のhold/exclude/clear。受付との対応・判断順を制約 |
| `data_quality_assessments` | 明示的な採否とイベント・問題・レビュー境界。未解決問題や古い根拠による採用を拒否 |
| `schema_migrations` | 移行版・実装ハッシュ・旧データ比較ハッシュ・旧ID/連番境界 |

旧イベントは`legacy_v1`とし、操作ID・操作者・旧revisionを推測で補わない。
移行時に品質問題・レビュー・採否確認は追加しない。採否記録の不在は未評価であり、
学習許可ではない。訂正イベントの追加は無効化トリガーが拒否する。

監査レコード・スナップショット・予測・初回応答は更新・削除を禁止し、
`INSERT OR REPLACE`による迂回も防ぐ。DB管理者が制約を削除する行為までは防げない。

## トランザクションと保持の確認

接続は`isolation_level=None`とし、`executescript()`を使わない。
外部キーはBEGIN前にOFFを確認し、`BEGIN IMMEDIATE`で単一の書込みトランザクションを開始。
トランザクション中のON/OFFが無効になるSQLiteの挙動もテストする。

新テーブル作成→明示列コピー→旧テーブル削除→正式名へ変更の順で再構築する。
旧テーブルを先にリネームしない。旧`sqlite_sequence`を保持し、`MAX(id)`で代用しない。
インデックス・制約・トリガーの完全な定義を照合し、`foreign_key_check`と`integrity_check`を実施。
移行記録と`user_version=2`を同じトランザクションで確定する。

比較は旧全列、行集合、SQLiteのstorage class、NULL、実数の`float.hex()`表現、
JSONの元TEXT、受付順、日別採番、イベントID、連番を対象とする。
保存済み予測・初回応答を解析して再生成せず、実待ち時間も移行中に再計算しない。
既存履歴の妥当性確認では日時差を検査するが、値の修復はしない。

空の正常v1は移行できる。バージョン0の空ファイル、外部キー違反、不完全な受付データ、
イベント不整合、未知スキーマ、不完全v2、二重移行は変更せず拒否する。

## 障害・復元

12か所の工程境界で例外を注入し、空v1・データ入りv1の両方について、
旧スキーマ・全データ・連番・バージョンと元ファイル全体の一致を検証する。

別プロセスでは小さいSQLiteページキャッシュを使い、未コミットページが実際に本体へ
書き出された状態を含めてSIGKILLする。SQLiteに回復させた後、旧状態との一致を確認する。
残るcold journalを手作業で削除せず、必要に応じてSQLite Backup APIで新しい一時ファイルへ写す。

コミット直後のSIGKILLでは完全なv2が残ることを検証する。コミット後の検証エラーは
`PostCommitValidationError`として隔離が必要と報告し、「ロールバック済み」と扱わない。
電源断やストレージ故障の再現試験ではない。

移行前バックアップをSQLite Backup APIで新規一時DBへ復元し、全旧内容・型・連番を照合。
復元コピーでは既存v1コードによる新規受付、案内、同一キー再送、採番の継続も確認する。
バックアップと保護対象へ復元しない。

## テスト

```sh
.venv/bin/python -B -m pytest -q -W error tests/test_reception_migration.py
.venv/bin/python -B -m pytest -q -W error --ignore=tests/test_reception_migration.py
node tests/frontend/reception.test.mjs
```

品質テーブルのテストに使用する過去の訂正履歴は、ガード設定前に作る独立した合成fixture。
訂正APIを実装・呼出ししたり、`seat_corrections_enabled`を1にするものではない。
実移行の保持検証に、このfixture生成経路は使わない。

## 次工程との境界

現アプリの`SCHEMA_VERSION`は1のまま。v1コードは移行済みv2を拒否し、
このパッケージのv2検証処理はv1や定義が欠けたv2を拒否する。
v2上の新規受付・案内は合成SQLでのスキーマ検証であり、v2対応の実アプリ統合ではない。

実DBへ進む前に、v2ストアの実装、全スタッフ操作の冪等性・revision・世代確認、
訂正と品質記録の原子的保存、影響対象の列挙、レビュー権限、抽出時の再検証を別途実装する。
古いeligible記録があっても新問題で採用できないよう、将来の抽出側は最新状態を再確認する必要がある。
既存Excel・RF・前処理・評価・モデル・公開API・顧客UIは本工程の変更対象に含めない。
