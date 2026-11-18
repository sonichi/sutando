#!/usr/bin/env python3
"""stale-head-post-guard — PreToolUse hook on Bash that blocks posting a GitHub PR
review or PR comment against a head that has moved since it was checked.

Posting commands it reads:
  gh api repos/<o>/<r>/pulls/<n>/reviews   with POST (`-X POST`, or implied by -f/-F/--input);
                                           a literal hex `commit_id` and `body` from -f/-F fields
                                           or --input JSON ($VAR / $(...) values are not checked)
  gh pr review <n|url> [--repo o/r]        body from --body/-b or --body-file/-F
  gh pr comment <n|url> [--repo o/r]       same
A sha claim is one of `at <sha>`, `Reviewed at <sha>`, `head <sha>`, `@ <sha>` in the
body, sha = 7-40 hex characters holding at least one digit and one letter.

The current head comes from `gh api repos/<o>/<r>/pulls/<n> --jq .head.sha` (8 s timeout).
Blocks (exit 2, both shas on stderr) when `commit_id` is not a prefix of that head, or
when a claimed sha is not, unless the body also names the head's short sha.

Allows: nothing to check, no repo named (it is never guessed), and a failed head
fetch (with a one-line stderr warning). `SUTANDO_ALLOW_STALE_POST=1` in the command
allows the post before any file is read; a body file that is not a regular file (a FIFO,
a device) is never opened blocking or read. `STALE_HEAD_POST_GUARD_DISABLE=1` turns the hook off;
`STALE_HEAD_POST_GUARD_GH` names the `gh` binary. Fail-OPEN on any internal error.

Tokenizes with the shared ``_shell_scan`` scanner; heredoc bodies are read as the
stdin of a `-` argument, or as the content of a body file the command writes itself.
"""
import json
import os
import re
import stat
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import _shell_scan  # noqa: E402  (sibling module; path set above)
except Exception:
    _shell_scan = None  # decide() then allows everything

ESCAPE = "SUTANDO_ALLOW_STALE_POST=1"
FETCH_TIMEOUT = 8
FILE_CAP = 1 << 20
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
HEX_RE = re.compile(r"^[0-9a-f]{7,40}$")
REVIEWS_RE = re.compile(r"^/?repos/([^/{}\s]+)/([^/{}\s]+)/pulls/(\d+)/reviews/?(?:\?.*)?$")
PR_URL_RE = re.compile(r"^https?://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)(?:[/?#].*)?$")
REPO_RE = re.compile(r"^(?:https?://github\.com/)?([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")
CLAIM_RE = re.compile(r"(?i)(?:\b(?:at|head)|@)[ \t]+`?([0-9a-f]{7,40})\b`?")
HEREDOC_RE = re.compile(r"<<-?[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
FIELD_OPTS = ("--raw-field", "--field", "-f", "-F")
PREFIX_WORDS = {"sudo", "command", "exec", "nohup", "time", "env", "then", "do", "else", "!", "{"}


class Post:
    """One posting command: where it posts and what it claims."""

    def __init__(self, kind, repo, number):
        self.kind, self.repo, self.number = kind, repo, number
        self.commit_id = None
        self.body = ""


def heredocs(cmd):
    """(command without heredoc bodies, [(opener line, body), ...])."""
    out, docs, pending = [], [], []
    for line in cmd.split("\n"):
        if pending:
            tag, opener, buf = pending[0]
            if line.strip() == tag:
                docs.append((opener, "\n".join(buf)))
                pending.pop(0)
            else:
                buf.append(line)
            continue
        out.append(line)
        pending.extend((m.group(2), line, []) for m in HEREDOC_RE.finditer(line))
    return "\n".join(out), docs


def _read(path, cwd):
    if not path or path == "-":
        return None
    path = os.path.expanduser(path)
    if not os.path.isabs(path) and cwd:
        path = os.path.join(cwd, path)
    # O_NONBLOCK + S_ISREG: a FIFO the same command feeds would otherwise block the hook.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, FILE_CAP).decode("utf-8", errors="replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def _source(value, cwd, docs, stdin_marker):
    """Text behind a file or stdin argument: the file, else a heredoc the command feeds it."""
    if value == "-":
        hits = [b for opener, b in docs if stdin_marker in opener]
        return "\n".join(hits) if hits else None
    text = _read(value, cwd)
    if text is not None:
        return text
    base = os.path.basename(value)
    hits = [b for opener, b in docs if base and base in opener]
    return "\n".join(hits) if hits else None


def _opt(words, i, names):
    """(name, value, next index) when words[i] is one of `names` as `--x v`, `--x=v` or `-xv`."""
    w = words[i]
    for n in names:
        if w == n:
            return n, (words[i + 1] if i + 1 < len(words) else ""), i + 2
        if n.startswith("--") and w.startswith(n + "="):
            return n, w[len(n) + 1:], i + 1
        if not n.startswith("--") and w.startswith(n) and len(w) > 2:
            return n, w[2:], i + 1
    return None, None, None


def _with_docs(body, docs):
    """A body built by `$(cat <<EOF ...)` lost its text to heredoc removal; put it back."""
    return body + "\n" + "\n".join(b for _, b in docs) if "<<" in body else body


def _command(seg):
    words = [w.text for w in seg]
    while words and (ASSIGN_RE.match(words[0]) or words[0] in PREFIX_WORDS):
        words = words[1:]
    return words


def parse_api(words, cwd, docs):
    target = next((REVIEWS_RE.match(w) for w in words if REVIEWS_RE.match(w)), None)
    if not target:
        return None
    method, posting, fields, i = None, False, {}, 2
    while i < len(words):
        name, v, nxt = _opt(words, i, ("--method", "-X", "--input") + FIELD_OPTS)
        if not name:
            i += 1
            continue
        i = nxt
        if name in ("--method", "-X"):
            method = v.upper()
            continue
        posting = True
        if name == "--input":
            text = _source(v, cwd, docs, "--input")
            try:
                data = json.loads(text) if text else None
            except ValueError:
                data = None
            if isinstance(data, dict):
                fields.update({k: data[k] for k in ("commit_id", "body") if isinstance(data.get(k), str)})
            continue
        key, sep, val = v.partition("=")
        if sep and key in ("commit_id", "body"):
            if name in ("-F", "--field") and val.startswith("@"):
                val = _source(val[1:], cwd, docs, "@") or ""
            fields[key] = val
    if (method or ("POST" if posting else "GET")) != "POST":
        return None
    post = Post("review", "%s/%s" % (target.group(1), target.group(2)), target.group(3))
    commit_id = fields.get("commit_id", "").strip().lower()
    post.commit_id = commit_id if HEX_RE.match(commit_id) else None
    post.body = _with_docs(fields.get("body", ""), docs)
    return post


def parse_pr(words, cwd, docs):
    kind = words[2]
    repo, number, body, i = None, None, "", 3
    while i < len(words):
        name, v, nxt = _opt(words, i, ("--repo", "-R", "--body-file", "-F", "--body", "-b"))
        if name in ("--repo", "-R"):
            m = REPO_RE.match(v)
            repo = "%s/%s" % (m.group(1), m.group(2)) if m else None
        elif name in ("--body-file", "-F"):
            body += "\n" + (_source(v, cwd, docs, "gh ") or "")
        elif name:
            body += "\n" + v
        elif number is None and not words[i].startswith("-"):
            m = PR_URL_RE.match(words[i])
            if m:
                repo, number = repo or "%s/%s" % (m.group(1), m.group(2)), m.group(3)
            elif words[i].isdigit():
                number = words[i]
        i = nxt or i + 1
    if not number:
        return None
    post = Post(kind, repo, number)
    post.body = _with_docs(body, docs)
    return post


def posts(cmd, cwd=None):
    """Every PR-posting command in `cmd`, parsed."""
    flat, docs = heredocs(cmd)
    out = []
    for seg in _shell_scan.segments(flat):
        words = _command(seg)
        if not words or os.path.basename(words[0]) != "gh" or len(words) < 3:
            continue
        if words[1] == "api":
            p = parse_api(words, cwd, docs)
        elif words[1] == "pr" and words[2] in ("review", "comment"):
            p = parse_pr(words, cwd, docs)
        else:
            p = None
        if p:
            out.append(p)
    return out


def claims(body):
    return [m.group(1).lower() for m in CLAIM_RE.finditer(body or "")
            if re.search(r"[0-9]", m.group(1)) and re.search(r"[a-fA-F]", m.group(1))]


def fetch_head(repo, number):
    """The PR's current head sha, or None when it cannot be read."""
    gh = os.environ.get("STALE_HEAD_POST_GUARD_GH") or "gh"
    try:
        p = subprocess.run([gh, "api", "repos/%s/pulls/%s" % (repo, number), "--jq", ".head.sha"],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=FETCH_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = p.stdout.strip().lower()
    return sha if p.returncode == 0 and SHA_RE.match(sha) else None


def check(post, head):
    """Block messages for one post against its PR's current head."""
    msgs = []
    if post.commit_id and not head.startswith(post.commit_id):
        msgs.append("commit_id %s" % post.commit_id)
    stale = [s for s in claims(post.body) if not head.startswith(s)]
    if stale and head[:7] not in post.body.lower():
        msgs.extend("claimed sha %s" % s for s in dict.fromkeys(stale))
    return ["stale-head-post-guard blocked: %s#%s %s, but its head is now %s; re-read the PR at the "
            "new head before posting (or add %s to the command)." % (post.repo, post.number, m, head, ESCAPE)
            for m in msgs]


def decide(raw, fetch=None):
    """(exit_code, stdout_text, stderr_text) for one hook invocation."""
    if os.environ.get("STALE_HEAD_POST_GUARD_DISABLE") == "1" or _shell_scan is None:
        return 0, "", ""
    data = json.loads(raw)
    if data.get("tool_name", "Bash") != "Bash":
        return 0, "", ""
    cmd = (data.get("tool_input") or {}).get("command") or ""
    if not isinstance(cmd, str) or "gh" not in cmd or not cmd.strip():
        return 0, "", ""
    if any(w.text == ESCAPE and not w.quoted for w in _shell_scan.words(heredocs(cmd)[0])):
        return 0, "", ""
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else None
    todo = [p for p in posts(cmd, cwd) if p.repo and (p.commit_id or claims(p.body))]
    if not todo:
        return 0, "", ""
    fetch = fetch or fetch_head
    heads, blocks, warns = {}, [], []
    for p in todo:
        key = (p.repo, p.number)
        if key not in heads:
            heads[key] = fetch(*key)
        if heads[key] is None:
            warns.append("stale-head-post-guard: could not read the head of %s#%s; post allowed unchecked."
                         % key)
            continue
        blocks.extend(check(p, heads[key]))
    if blocks:
        return 2, "", "\n".join(blocks + warns) + "\n"
    return 0, "", ("\n".join(dict.fromkeys(warns)) + "\n") if warns else ""


def main():
    try:
        code, out, err = decide(sys.stdin.read())
    except Exception:
        sys.exit(0)
    if out:
        sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    sys.exit(code)


if __name__ == "__main__":
    main()
