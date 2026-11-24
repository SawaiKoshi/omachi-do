#!/usr/bin/env python3
"""Claude Code の PreToolUse フック：破壊的な操作と秘密情報の読み出しを止める。

.claude/settings.json から呼ばれる。標準入力にツール呼び出しのJSONが届く。
止めるときは終了コード2で理由を標準エラーに出す（Claude Code はそれをエージェントに返す）。
迂回用の設定や環境変数は用意しない。止められた操作は、ユーザーが内容を確かめて自分で行う。
自動で修正・実行・承認することはしない。
"""

import json
import os
import re
import shlex
import sys

ASK_USER = (
    "このコマンドはユーザーの明示的な承認が必要なため、安全ガードが止めました。"
    "迂回や言い換えはせず、コマンドと必要な理由をユーザーに示し、ユーザー自身に実行してもらってください。"
)

SEPARATORS = {"&&", "||", ";", "|", "&", ";;", "(", ")", "|&"}
WRAPPERS = {"command", "builtin", "exec", "nohup", "time", "nice", "stdbuf", "then", "do", "else", "if", "while", "!"}
SHELLS = {"bash", "sh", "zsh", "dash"}
DB_CLIENTS = {"sqlite3", "psql", "mysql", "mariadb", "duckdb"}
SCRIPT_RUNNERS = {"python", "python3", "node", "deno", "bun", "ruby", "perl"}
PROTECTED_CONFIG = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".agents/team/hooks",
    ".agents/team/gate.sh",
    ".agents/team/check-secrets.sh",
)
PROTECTED_REASON = "安全設定・検証の仕組み（.claude/settings.json、フック、gate.sh）の変更はユーザーが行います"
SECRET_WORD = re.compile(r"(?i)(SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIAL)")


def is_secret_path(path):
    p = path.replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    if name in (".env.example", ".env.sample", ".env.template"):
        return False
    if name == ".env" or name.startswith(".env."):
        return True
    if name in (".git-credentials", ".netrc", ".npmrc", ".pypirc") or name.startswith("id_rsa") or name.startswith("id_ed25519"):
        return True
    if name.endswith((".pem", ".key", ".p12", ".pfx")):
        return True
    return "/.ssh/" in p or p.startswith(".ssh/") or p.startswith("~/.ssh")


def tokenize(command):
    text = command.replace("\r", "").replace("\n", " ; ")
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return text.split()


def segments(tokens):
    current = []
    for tok in tokens:
        if tok in SEPARATORS:
            if current:
                yield current
            current = []
        else:
            current.append(tok)
    if current:
        yield current


def strip_prefix(seg):
    """先頭の環境変数代入やラッパー（command, timeout 10 など）を外して本体のコマンドにする。"""
    i = 0
    while i < len(seg):
        tok = seg[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            i += 1
        elif tok in WRAPPERS:
            i += 1
        elif tok == "env" and i + 1 < len(seg) and seg[i + 1] not in ("-0",) and (seg[i + 1].startswith("-") or "=" in seg[i + 1]):
            i += 1
            while i < len(seg) and (seg[i].startswith("-") or "=" in seg[i]):
                i += 1
        elif tok == "timeout":
            i += 1
            while i < len(seg) and (seg[i].startswith("-") or re.match(r"^\d", seg[i])):
                i += 1
        elif tok == "xargs":
            i += 1
            while i < len(seg) and seg[i].startswith("-"):
                i += 1
        else:
            break
    return seg[i:]


def short_flags(args):
    flags = set()
    for a in args:
        if a.startswith("-") and not a.startswith("--") and len(a) > 1:
            flags.update(a[1:])
    return flags


def git_sub(args):
    """git の後ろのグローバルオプション（-C path, -c k=v など）を飛ばしてサブコマンドと残りを返す。"""
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            return a, args[i + 1:]
    return None, []


def check_git(args):
    if any("hookspath" in a.lower() for a in args):
        return "Git フックの設定変更"
    sub, rest = git_sub(args)
    if sub is None:
        return None
    flags = short_flags(rest)
    if sub == "push":
        if any(a.startswith("--force") or a in ("--mirror", "--delete", "--prune", "--no-verify") for a in rest) or flags & {"f", "d"}:
            return "force push・ブランチ削除・検査の省略を伴う push"
        for a in rest:
            if a.startswith("+") or a.startswith(":"):
                return "強制や削除を意味する refspec での push"
            target = a.split(":")[-1]
            if target in ("main", "master", "HEAD:main", "refs/heads/main", "refs/heads/master"):
                return "main への直接の push"
    if sub == "reset" and "--hard" in rest:
        return "git reset --hard（未コミットの変更を消す）"
    if sub == "clean":
        return "git clean（未追跡ファイルを消す）"
    if sub == "checkout" and ("--" in rest or "." in rest):
        return "git checkout によるファイルの変更の破棄"
    if sub == "restore" and "--staged" not in rest:
        return "git restore によるファイルの変更の破棄"
    if sub == "branch" and (flags & {"D", "d"} or "--delete" in rest):
        return "ブランチの削除"
    if sub == "worktree" and rest[:1] == ["remove"] and (flags & {"f"} or "--force" in rest):
        return "worktree の強制削除"
    if sub == "commit" and ("--no-verify" in rest or "n" in flags):
        return "検査を飛ばすコミット（--no-verify）"
    if sub in ("filter-branch", "filter-repo", "replace"):
        return "履歴の書き換え"
    if sub == "stash" and rest[:1] in (["clear"], ["drop"]):
        return "stash の削除"
    if sub == "update-ref" and ("-d" in rest or "--stdin" in rest):
        return "参照の削除"
    if sub == "config" and any("hookspath" in a.lower() for a in rest):
        return "Git フックの設定変更"
    return None


def check_gh(args):
    sub = args[0] if args else ""
    sub2 = args[1] if len(args) > 1 else ""
    if sub == "pr" and sub2 in ("merge", "close"):
        return f"gh pr {sub2}（マージ・クローズはユーザーが決める）"
    if sub == "issue" and sub2 in ("delete", "transfer"):
        return f"gh issue {sub2}"
    if sub == "repo" and sub2 in ("delete", "edit", "archive", "rename", "transfer"):
        return f"gh repo {sub2}"
    if sub == "release":
        return "リリースの操作"
    if sub == "workflow" and sub2 in ("run", "enable", "disable"):
        return "ワークフロー（公開・デプロイを含む）の操作"
    if sub == "run" and sub2 in ("rerun", "cancel", "delete"):
        return "Actions の実行の操作"
    if sub in ("secret", "variable", "ssh-key", "gpg-key"):
        return f"gh {sub}（秘密情報・設定）"
    if sub == "auth" and (sub2 == "token" or "--show-token" in args or "-t" in args):
        return "トークンの表示"
    if sub == "api":
        joined = " ".join(args)
        if re.search(r"(?i)(-X|--method)\s*(DELETE|PUT|PATCH)", joined) or "/merge" in joined:
            return "gh api による削除・変更・マージ"
    return None


def check_install(cmd, args):
    first = args[0] if args else ""
    if cmd in ("pip", "pip3") and first in ("install", "uninstall"):
        return "依存パッケージの追加・削除"
    if cmd in ("python", "python3") and args[:2] in (["-m", "pip"],) and len(args) > 2 and args[2] in ("install", "uninstall"):
        return "依存パッケージの追加・削除"
    if cmd in ("npm", "pnpm", "yarn", "bun") and first in ("install", "i", "add", "remove", "uninstall", "rm"):
        if any(not a.startswith("-") for a in args[1:]) or first in ("add", "remove", "uninstall", "rm"):
            return "依存パッケージの追加・削除"
    if cmd in ("uv", "poetry", "pipx") and first in ("add", "remove", "install") or (cmd == "uv" and args[:2] == ["pip", "install"]):
        return "依存パッケージの追加・削除"
    if cmd in ("apt", "apt-get", "brew", "snap", "winget", "choco") and first in ("install", "remove", "purge", "uninstall"):
        return "システムへのソフトウェアの導入・削除"
    return None


def check_segment(seg):
    seg = strip_prefix(seg)
    if not seg:
        return None
    cmd = os.path.basename(seg[0])
    args = seg[1:]

    if cmd == "sudo":
        return "sudo（管理者権限での実行）"
    if cmd in SHELLS and "-c" in args:
        idx = args.index("-c")
        if idx + 1 < len(args):
            return check_command(args[idx + 1])
    if cmd == "eval":
        return check_command(" ".join(args))
    if cmd == "rm" and (short_flags(args) & {"r", "R"} or "--recursive" in args):
        return "フォルダーの再帰削除（rm -r / rm -rf）"
    if cmd == "find" and ("-delete" in args or ("-exec" in args and "rm" in args)):
        return "find による一括削除"
    if cmd in ("dd", "mkfs", "shred", "wipefs") or cmd.startswith("mkfs."):
        return f"{cmd}（ディスクやファイルの破壊的な操作）"
    if cmd in ("chmod", "chown") and (short_flags(args) & {"R"} or "--recursive" in args):
        return "権限・所有者の再帰的な変更"
    if cmd == "git":
        return check_git(args)
    if cmd == "gh":
        return check_gh(args)
    reason = check_install(cmd, args)
    if reason:
        return reason
    if cmd in DB_CLIENTS or (cmd in SCRIPT_RUNNERS and ("-c" in args or "-e" in args)):
        text = " ".join(args)
        if re.search(r"(?i)\b(DROP\s+(TABLE|DATABASE|SCHEMA|INDEX|VIEW)|TRUNCATE\b|DELETE\s+FROM|ALTER\s+TABLE\s+\S+\s+DROP)\b", text):
            return "データベースの削除・破壊的な変更"
    if cmd == "printenv" or (cmd == "env" and not args) or (cmd in ("set", "export", "declare") and args in ([], ["-p"], ["-x"])):
        return "環境変数の一覧表示（秘密情報が含まれる可能性）"
    if cmd in ("echo", "printf") and any("$" in a and SECRET_WORD.search(a) for a in args):
        return "秘密情報を含む環境変数の表示"
    if any(is_secret_path(a) for a in args if not a.startswith("-")):
        return "秘密情報のファイル（.env・鍵など）へのアクセス"
    if cmd in ("source", ".") and args and is_secret_path(args[0]):
        return "秘密情報のファイルの読み込み"
    return None


def touches_protected(token):
    t = token.replace("\\", "/")
    return any(p in t for p in PROTECTED_CONFIG)


def writes_protected(seg):
    """保護対象のファイルへの書き込み・削除・移動・リダイレクトを見つける。"""
    for i, tok in enumerate(seg):
        if tok in (">", ">>", ">|", "&>", "&>>") and i + 1 < len(seg) and touches_protected(seg[i + 1]):
            return True
    body = strip_prefix(seg)
    if not body:
        return False
    cmd = os.path.basename(body[0])
    args = body[1:]
    if not any(touches_protected(a) for a in args):
        return False
    if cmd in ("tee", "mv", "cp", "rm", "truncate", "chmod", "chown", "ln", "install", "touch", "unlink"):
        return True
    if cmd in ("sed", "perl") and any(a.startswith("-i") for a in args):
        return True
    if cmd in SCRIPT_RUNNERS and ("-c" in args or "-e" in args):
        return True
    if cmd == "git":
        sub, _ = git_sub(args)
        return sub in ("rm", "mv", "checkout", "restore", "apply")
    return False


def check_command(command):
    if re.search(r"(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b", command):
        return "ダウンロードしたスクリプトをそのまま実行する操作"
    for seg in segments(tokenize(command)):
        if writes_protected(seg):
            return PROTECTED_REASON
        reason = check_segment(seg)
        if reason:
            return reason
    # $( ... ) やバッククォートの中も念のため調べる
    for inner in re.findall(r"\$\(([^()]*)\)|`([^`]*)`", command):
        text = inner[0] or inner[1]
        if text and text != command:
            reason = check_command(text)
            if reason:
                return reason
    return None


def check_file_tool(tool_name, tool_input):
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path") or ""
    if not path:
        return None
    if is_secret_path(path):
        return "秘密情報のファイル（.env・鍵など）へのアクセス"
    norm = path.replace("\\", "/")
    if tool_name != "Read" and any(norm.endswith(p) or ("/" + p + "/") in norm or norm.startswith(p + "/") for p in PROTECTED_CONFIG):
        return PROTECTED_REASON
    if tool_name != "Read" and "/.git/" in norm:
        return ".git フォルダーの直接編集"
    return None


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print("安全ガード：入力を読めなかったため、念のため止めました。", file=sys.stderr)
        return 2
    tool = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}
    if tool == "Bash":
        reason = check_command(str(tool_input.get("command", "")))
    elif tool in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        reason = check_file_tool(tool, tool_input)
    else:
        reason = None
    if reason:
        print(f"【安全ガード】{reason}。{ASK_USER}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # 想定外の失敗では通さずに止める
        print(f"【安全ガード】判定中にエラーが起きたため、念のため止めました（{type(exc).__name__}）。ユーザーに報告してください。", file=sys.stderr)
        sys.exit(2)
