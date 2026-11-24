#!/usr/bin/env bash
# guard.py を起動する入口（.claude/settings.json から呼ばれる）。
# Windows（Git Bash）と WSL・Linux のどちらでも動くよう、本物の Python 3 を探して使う。
# Windows の python3 は Microsoft Store の案内（本物の Python ではない）のことがあるため、
# 実際に起動できるかを確かめてから使う。
# 判定できないとき（Python が見つからない・ガードが異常終了した）は、通さずに止める（終了コード2）。
input="$(cat)"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for candidate in python3 python "py -3"; do
  # shellcheck disable=SC2086
  if $candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
    # shellcheck disable=SC2086
    printf '%s' "$input" | $candidate -I "$here/guard.py"
    status=$?
    if [ "$status" -eq 0 ]; then
      exit 0
    fi
    if [ "$status" -ne 2 ]; then
      echo "【安全ガード】判定中にエラーが起きたため（終了コード $status）、念のため止めました。ユーザーに報告してください。" >&2
    fi
    exit 2
  fi
done

echo "【安全ガード】Python 3 が見つからないため安全確認ができず、念のため止めました。ユーザーに報告してください。" >&2
exit 2
