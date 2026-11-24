#!/usr/bin/env bash
# origin/main からの差分（コミット済み＋未コミット）で追加された行に、
# 秘密情報らしき文字列がないか調べる。見つけても値は表示せず、ファイル名と行番号だけを出す。
# 使い方: .agents/team/check-secrets.sh [比較の起点。省略時は origin/main]
set -euo pipefail

base="${1:-origin/main}"
cd "$(git rev-parse --show-toplevel)"

if ! git rev-parse --verify --quiet "$base" >/dev/null; then
  echo "[secrets] 比較の起点 $base が見つかりません。git fetch origin main を実行してください。" >&2
  exit 2
fi
merge_base="$(git merge-base "$base" HEAD)"

python3 -I - "$merge_base" <<'PY'
import re
import subprocess
import sys

merge_base = sys.argv[1]
patterns = [
    ("秘密鍵", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("OpenAI系のキー", r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}"),
    ("GitHubトークン", r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}"),
    ("AWSアクセスキー", r"\bAKIA[0-9A-Z]{16}\b"),
    ("Googleのキー", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("Slackトークン", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("Stripeのキー", r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}"),
    ("代入された秘密の値", r"(?i)\b[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API_KEY|APIKEY)[A-Z0-9_]*\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+=.]{12,}"),
]
compiled = [(label, re.compile(rx)) for label, rx in patterns]

# 作業ツリーと merge-base の差分（未コミットを含む）
diff = subprocess.run(
    ["git", "diff", "--no-color", "--unified=0", merge_base, "--"],
    check=True, capture_output=True, text=True, errors="replace",
).stdout
untracked = subprocess.run(
    ["git", "ls-files", "--others", "--exclude-standard", "-z"],
    check=True, capture_output=True, text=True,
).stdout.split("\0")

findings = []
bad_files = []
path = None
line_no = 0
for raw in diff.splitlines():
    if raw.startswith("+++ "):
        path = raw[6:] if raw.startswith("+++ b/") else None
        continue
    if raw.startswith("@@"):
        m = re.search(r"\+(\d+)", raw)
        line_no = int(m.group(1)) if m else 0
        continue
    if raw.startswith("+") and path:
        for label, rx in compiled:
            if rx.search(raw[1:]):
                findings.append((path, line_no, label))
                break
        line_no += 1

def is_env_file(p):
    name = p.rsplit("/", 1)[-1]
    return (name == ".env" or name.startswith(".env.")) and name != ".env.example"

changed = subprocess.run(
    ["git", "diff", "--name-only", merge_base, "--"],
    check=True, capture_output=True, text=True,
).stdout.splitlines()
for p in changed + [u for u in untracked if u]:
    if is_env_file(p) or p.endswith((".pem", ".p12", ".pfx", ".key")):
        bad_files.append(p)

for p in [u for u in untracked if u]:
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            for i, text in enumerate(fh, 1):
                for label, rx in compiled:
                    if rx.search(text):
                        findings.append((p, i, label + "（未追跡ファイル）"))
                        break
    except (IsADirectoryError, FileNotFoundError):
        pass

if findings or bad_files:
    print("[secrets] 秘密情報の疑いがあります（値は表示しません）。", file=sys.stderr)
    for p, i, label in findings:
        print(f"  {p}:{i}  {label}", file=sys.stderr)
    for p in bad_files:
        print(f"  {p}  秘密情報を入れるファイルの種類です", file=sys.stderr)
    print("  誤検出なら、その理由をPRの確認結果に書き、ユーザーの判断を待ってください。", file=sys.stderr)
    sys.exit(1)
print("[secrets] 問題なし")
PY
