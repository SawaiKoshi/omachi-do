#!/usr/bin/env bash
# おまちどう：PR を出す前・修正のたびに実行する検証。すべて成功したら終了コード0。
# 使い方: .agents/team/gate.sh
# Python は OMACHI_PYTHON → この worktree の .venv → 元のリポジトリの .venv の順に探す。
# （worktree には .venv がないため、元のリポジトリの仮想環境を使う）
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
cd "$root"
main_root="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
base="$(git merge-base origin/main HEAD)"

echo "== 1/6 秘密情報の混入"
.agents/team/check-secrets.sh

echo "== 2/6 空白エラー"
git diff --check "$base" --

echo "== 3/6 実データ・モデル成果物の混入"
if git diff --name-only --diff-filter=A "$base" -- \
  | grep -E '\.(xlsx|xls|csv|db|sqlite3?|joblib|pkl|pickle)$'; then
  echo "上のファイルは実データ・DB・モデルの成果物の可能性があります。追加しないでください。" >&2
  exit 1
fi
echo "問題なし"

echo "== 4/6 Python のテスト"
py="${OMACHI_PYTHON:-}"
if [ -z "$py" ]; then
  for candidate in "$root/.venv/bin/python" "$main_root/.venv/bin/python"; do
    if [ -x "$candidate" ]; then py="$candidate"; break; fi
  done
fi
if [ -z "$py" ]; then
  echo "仮想環境（.venv）が見つかりません。ユーザーに確認してください（依存の導入はユーザーが行います）。" >&2
  exit 2
fi
"$py" -m pytest -q -W error -p no:cacheprovider

echo "== 5/6 フロントエンドのテスト"
for test_file in tests/frontend/*.test.mjs; do
  node "$test_file" > /dev/null
  echo "ok $test_file"
done

echo "== 6/6 JavaScript の構文"
while IFS= read -r js; do
  node --check "$js"
done < <(git ls-files 'web/*.mjs' 'web/*.js')
echo "問題なし"

dirty="なし"
if [ -n "$(git status --porcelain)" ]; then dirty="あり（コミット前の結果です）"; fi
echo
echo "ゲート成功：SHA $(git rev-parse --short HEAD)、未コミットの変更 $dirty"
