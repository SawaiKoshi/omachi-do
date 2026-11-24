#!/usr/bin/env bash
# タスク用の git worktree を作る（T3 Code が worktree を作れないときの予備）。
# 使い方: .agents/team/new-worktree.sh <Issue番号> <slug>
#   例:   .agents/team/new-worktree.sh 42 wait-time-rounding
# 作るもの: ブランチ agent/<番号>-<slug>（origin/main から）と
#           <リポジトリの親>/<リポジトリ名>.worktrees/<番号>-<slug>
# 既にあるブランチ・フォルダーは上書きしない。
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "使い方: $0 <Issue番号> <slug>" >&2
  exit 64
fi
issue="$1"
slug="$2"
if ! [[ "$issue" =~ ^[0-9]+$ ]]; then
  echo "Issue番号は数字で指定してください: $issue" >&2
  exit 64
fi
if ! [[ "$slug" =~ ^[a-z0-9][a-z0-9-]{0,40}$ ]]; then
  echo "slug は英小文字・数字・ハイフンで41文字以内にしてください: $slug" >&2
  exit 64
fi

# worktree の中から実行しても、元のリポジトリを基準にする
common_dir="$(git rev-parse --path-format=absolute --git-common-dir)"
main_root="$(dirname "$common_dir")"
repo_name="$(basename "$main_root")"
branch="agent/${issue}-${slug}"
target="$(dirname "$main_root")/${repo_name}.worktrees/${issue}-${slug}"

if git -C "$main_root" show-ref --verify --quiet "refs/heads/$branch"; then
  echo "ブランチ $branch は既にあります。作り直さずに止まります。" >&2
  exit 1
fi
if [ -e "$target" ]; then
  echo "フォルダー $target は既にあります。作り直さずに止まります。" >&2
  exit 1
fi

git -C "$main_root" fetch origin main
mkdir -p "$(dirname "$target")"
git -C "$main_root" worktree add -b "$branch" "$target" origin/main
# origin/main を追跡させず、push 先は同名のブランチにする
git -C "$target" branch --unset-upstream >/dev/null 2>&1 || true

echo
echo "worktree: $target"
echo "branch:   $branch"
