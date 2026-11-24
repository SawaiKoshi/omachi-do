# おまちどう の作業ルール

## プロジェクト

- 飲食店の待ち時間予測。データ品質監査・前処理・モデル比較、予測API、受付・スタッフ画面、SQLiteの受付記録。詳細は `README.md`。
- 判断の記録は `docs/adr/`、操作手順と確認結果は `README.md` に残す。文書は日本語で書く。

## テストとデータ

- テストは既存の仮想環境で `.venv/bin/python -m pytest -q -W error`。フロントエンドは `node tests/frontend/<名前>.test.mjs`。
- テストは合成データと `tmp_path` だけを使う。元のExcelなど実データを読み込むコマンド（`step1_audit.py` など）は、ユーザーの指示なしに実行しない。
- 依存パッケージ（`requirements-*.txt`）の追加・更新と導入はユーザーが行う。

## AIチーム（agent-team、2026-11-25導入）

- ユーザーはプランの承認・レビュー指摘の取捨・マージの判断を行う。統括・プラン・実装はClaude、レビューはCodexが担当する（`.agents/team/profile.md`）。使い方は `.agents/team/README.md`。
- タスクは `agent:ready` の付いたIssue。1タスクに1つのworktreeとブランチ `agent/<Issue番号>-<名前>` を使い、ほかのタスクのブランチに書き込まない。
- PRを出す前と修正のたびに `.agents/team/gate.sh` を通す。テストや確認を省略・弱めない。存在を確かめていないファイル・関数・パッケージを前提にしない。
- エージェントはマージ、mainへのpush、force pushをしない。安全ガード・ゲート（`.claude/settings.json`、`.agents/team/hooks/`、`.agents/team/gate.sh`）はユーザーだけが変更する。
- 秘密情報（`.env`、APIキー、トークン、環境変数の値）を読まない・表示しない・ログやPRに書かない。
