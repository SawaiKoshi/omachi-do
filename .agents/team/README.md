# AIチーム（agent-team）の使い方

ユーザーは **プランの承認・指摘の取捨・マージの判断** だけを行い、残りは AI のチームが進めます。

```
 ユーザー（判断だけ）        統括（Claude・T3 Code の親スレッド）        子スレッド（タスクごとに worktree）
 ─────────────────        ─────────────────────────────        ──────────────────────────────
                           1 Issue を拾う（agent:ready）
                           2 worktree と子スレッドを作る ─────────▶ 3 プランだけ書いて止まる（Claude）
                           4 衝突を調べ承認シートを作る ◀─────────┘
 5 ★プランを承認 ◀──────────┘
   └─ 差し戻し ─────────────────────────────────────────────────▶ 3 へ戻る
                           6 同じスレッドを実装用モデルへ ────────▶ 7 実装 → ゲート → draft PR（Claude）
                                                                    8 徹底レビュー（Codex・読むだけ）
 9 ★指摘を直す／見送る ◀──── 指摘をまとめて中継 ◀──────────────────┘
   └─ 答え（原文）──────────▶ 中継 ───────────────────────────────▶ 10 直して PR を更新 → ready
                           11 gh で PR・SHA・結果を確認 ◀─────────┘
12 ★マージなどを決める ◀────┘
```

## ファイル

| 場所 | 中身 |
|------|------|
| `.agents/skills/agent-team/` | 統括のスキル（手順・依頼文・品質確認） |
| `.agents/skills/team-plan/` `team-implement/` `team-review/` | 子スレッドのスキル |
| `.claude/skills/*/SKILL.md` | Claude Code 用の入口（本体は `.agents/skills/`） |
| `.agents/team/profile.md` | このリポジトリの役割・モデル・承認が要る場所・例外ケース |
| `.agents/team/gate.sh` | PR 前に必ず通す検証 |
| `.agents/team/check-secrets.sh` | 差分に秘密情報が混ざっていないかの確認（値は表示しない） |
| `.agents/team/new-worktree.sh` | T3 Code が worktree を作れないときの予備 |
| `.agents/team/hooks/guard.py` | Claude Code の安全ガード（破壊的な操作・秘密情報の読み出しを止める） |
| `.claude/settings.json` | 安全ガードの登録 |
| `.github/ISSUE_TEMPLATE/agent-task.md` | タスクの書き方 |

## ここから始める

1. **T3 Code を最新にする**：エージェントがスレッドを作れる「T3 Code MCP」と `delegate_task` は v0.1.0（Orchestrator V2）以降の機能です。T3 Code の設定で、Claude Code から T3 Code MCP に接続します（接続方法は T3 Code のドキュメントに従ってください）。接続しなくても「手動モード」で使えます。
2. **ラベルを作る**（リポジトリごとに1回、ご自身で実行）：

   ```sh
   gh label create agent:ready --color 0E8A16 --description "AIチームが拾ってよいタスク"
   gh label create agent:in-progress --color FBCA04 --description "AIチームが進行中"
   gh label create agent:needs-human --color D93F0B --description "ユーザーの判断待ち"
   ```

3. **安全ガードが効くか確かめる**：T3 Code の Claude Code スレッドで、次を実行するよう頼みます。「安全ガード」と表示されて止まれば有効です（止まらなかった場合も、一時フォルダーが消えるだけです）。

   ```
   mkdir -p /tmp/agent-guard-test && rm -rf /tmp/agent-guard-test を実行して、結果をそのまま教えて
   ```

   止まらなかったら、T3 Code の Claude Code がプロジェクトの `.claude/settings.json` を読んでいない可能性があります。統括に進む前に教えてください。ガード自体の自己テストは `python3 .agents/team/hooks/selftest.py` です。
4. **Codex のレビュー用設定**：レビュー役の Codex は、サンドボックスを read-only にして使います（Claude 用の安全ガードは Codex には効かないため、Codex 側はサンドボックスと承認設定で守ります）。
5. **最初のタスク**：小さくて失敗しても困らないもの（文言の修正、テストの追加など）を Issue テンプレートで作り、`agent:ready` を付けます。T3 Code でこのリポジトリの新しいスレッドを Claude（Opus）で開き、`/agent-team` と送ります。
6. 慣れてきたら、同時に2タスクまで並べる。T3 Code のスケジュール機能で「手順1〜4だけ実行して承認シートを作る」を朝に回すこともできます。

## 安全ガードが止めるもの

フォルダーの再帰削除、force push・main への push・ブランチ削除、`git reset --hard`・`git clean`・変更の破棄、`--no-verify`、PR のマージ・クローズ、ワークフローの実行、秘密情報・設定の操作、トークンの表示、DB の削除系 SQL、環境変数の一覧表示、`.env`・鍵ファイルの読み出し、依存パッケージの追加、`sudo`、ダウンロードしたスクリプトの直接実行、安全設定・フック・ゲート自体の変更。

止められた操作が本当に必要なときは、内容を確かめてご自身で実行してください。ガードやゲートを変えたいときも、ご自身で編集してください（AI は変更できません）。
