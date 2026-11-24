# AIチームの設定：おまちどう（SawaiKoshi/omachi-do）

飲食店の待ち時間予測。データ品質監査・前処理・モデル比較、予測API、受付・スタッフ画面、SQLite の受付記録。

## 役割とモデル

| 役割 | プロバイダー | モデル・推論の強さ | 権限 |
|------|--------------|--------------------|------|
| 統括 | Claude Code | Opus・high | 読む・`gh`（Issue のラベル、PR の確認）。コードは書かない |
| プラン | Claude Code | Opus・いちばん強い設定 | 読むだけ |
| 実装 | Claude Code（プランと同じスレッド） | Opus・medium | 自分の worktree の編集、コミット、自分のブランチへの push、draft PR |
| レビュー | Codex | 最新のコーディング向けモデル・high | read-only サンドボックス。読むだけ |

- モデル名は T3 Code のモデル選択に表示されている名前を使う。ここに書いた名前がなければ、いちばん近いものを選び、統括の報告に書く。
- Codex が使えないとき：レビューは Claude の別スレッドで行い、PR 本文にそう書く。Claude が使えないとき：実装は Codex（workspace-write、承認は都度確認）で行う。

## ゲート

```sh
.agents/team/gate.sh
```

秘密情報、空白エラー、実データ・モデル成果物の混入、`pytest -q -W error`、`tests/frontend/*.test.mjs`、`web/` の JavaScript の構文。

## 触る前にユーザーの承認が要る場所

- `reception_migration/`、`reception_schema.py`、`reception_store.py` の保存形式（スキーマ移行）
- `requirements-*.txt`（依存の追加・更新。インストールもユーザーが行う）
- 実データ（`*.xlsx` など）、`var/`、`models/`、学習済みモデル。**実データを読み込むコマンド（`step1_audit.py` など）を実行しない**
- 認証・HTTPS の入口（`staff_*https_lab.py`、`staff_sessions.py`）とポート・待受設定
- 決まっている業務規則・品質境界を変えること（`docs/adr/` の採用済み ADR と食い違う変更）

## 例外ケースの観点（レビューで1つずつ確かめる）

- 時刻：23:00 を超える受付・案内、案内が受付より前（時刻逆転）、日付またぎ、分単位の丸め
- 人数：大人＋子供と合計の不一致、0人・負の数・小数・非常に大きい値
- 取消・重複・欠損・変換できない値（`valid` 以外の状態の扱い）
- SQLite：同時の書き込み、再送・二重送信、途中で失敗したときの記録
- API：不正な入力、存在しない ID、認証なしのアクセス
- 画面：通信失敗・再送・一覧の更新、スマホ幅、キーボード操作
- 入力の DataFrame を書き換えていないこと（不変性）

## 進め方の決まりごと

- 同時進行の上限：2タスク
- 判断の記録は `docs/adr/ADR-xxxx.md`、操作手順は `README.md`
- テストは合成データと `tmp_path` だけを使う（既存の方針）
