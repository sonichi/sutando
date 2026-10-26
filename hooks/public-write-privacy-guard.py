#!/usr/bin/env python3
"""public-write-privacy-guard — PreToolUse[Bash] hook that refuses a `gh` write
to a non-private repo when the text it would publish names something private.

Covered writes: `gh pr create|edit|comment|review`, `gh issue create|comment|edit`,
`gh release create|edit`, and `gh api` POST/PATCH/PUT to a repo's issues or
pulls (and GraphQL mutations). Scanned: --title/--body/--notes, their -file
forms, `$(cat FILE)` values, heredoc/here-string stdin, and `gh api` fields.
What counts as private is src/public_write_privacy.py; this hook only parses.

Fails CLOSED: a body it cannot read (missing file, stdin it cannot see, a value
that expands at run time, a line that does not lex) is refused, as is a public
write on a host with no denylist. The escape for a name already public is the
allowlist file, not an environment variable.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))
import _shell_scan  # noqa: E402
import public_write_privacy as policy  # noqa: E402

TAG = "[public-write-privacy-guard]"
VERBS = {
    "pr": {"create", "edit", "comment", "review"},
    "issue": {"create", "comment", "edit"},
    "release": {"create", "edit"},
}
INLINE_FLAGS = {"--body", "-b", "--title", "-t", "--notes", "-n"}
FILE_FLAGS = {"--body-file", "-F", "--notes-file"}
REPO_FLAGS = {"-R", "--repo"}
API_VALUE_FLAGS = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "-H", "--header",
                   "--input", "-q", "--jq", "-t", "--template", "--hostname", "--cache", "-p",
                   "--preview"}
API_FIELD_FLAGS = {"-f", "-F", "--field", "--raw-field"}
WRAPPERS = {"env", "command", "exec", "nohup", "time", "sudo"}
SHELLS = {"bash", "sh", "zsh", "dash"}
GH_URL = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/(?:pull|issues)/\d+")
API_REPO = re.compile(r"^(?:https://api\.github\.com)?/?repos/([^/]+/[^/]+)/(issues|pulls)(?:/|$)")
WHOLE_CAT = re.compile(r"""^"?\$\(\s*(?:cat\s+|<\s*)(['"]?)([^'"\s$`()]+)\1\s*\)"?$""")
HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
REDIRECT = re.compile(r"^(?:\d|&)?>>?(.*)$")
GH_WRITE_HINT = re.compile(r"\bgh\b[\s\S]*\b(?:pr|issue|release|api)\b")


class Unreadable(Exception):
    pass


def split_heredocs(command: str):
    """(command without heredoc bodies, [bodies]). A body can hold an unbalanced
    quote that would make the whole line unlexable, so it is lifted out first."""
    lines = command.split("\n")
    kept, bodies, pending, current = [], [], [], []
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if pending:
            strip_tabs, tag = pending[0]
            if (line.lstrip("\t") if strip_tabs else line) == tag:
                pending.pop(0)
                bodies.append("\n".join(current))
                current = []
            else:
                current.append(line)
            continue
        kept.append(line)
        for m in HEREDOC.finditer(line.replace("<<<", "   ")):
            pending.append((m.group(1) == "-", m.group(3)))
    if pending:
        raise Unreadable("a heredoc is never terminated")
    return "\n".join(kept), bodies


def _literal_or_raise(word) -> str:
    raw = word.raw
    if raw.startswith("$'"):
        return word.text
    quote, escaped = "", False
    for idx, ch in enumerate(raw):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and quote != "'":
            escaped = True
            continue
        if ch in "'\"" and (not quote or quote == ch):
            quote = "" if quote else ch
            continue
        if quote != "'" and (ch == "`" or (ch == "$" and re.match(r"[\w{(@*#?!$-]", raw[idx + 1:idx + 2] or " "))):
            raise Unreadable(f"`{raw[:60]}` expands at run time, so its text cannot be checked")
    return word.text


class Ctx:
    def __init__(self, cwd, bodies, writes):
        self.cwd = cwd
        self.bodies = bodies          # heredoc + here-string text in this command
        self.writes = writes          # files this command itself writes


def _resolve(path_text: str, cwd):
    p = Path(os.path.expanduser(path_text))
    if not p.is_absolute():
        if cwd is None:
            raise Unreadable(f"relative path `{path_text}` after an unresolvable `cd`")
        p = Path(cwd) / p
    return Path(os.path.normpath(str(p)))


def _read_file(word, ctx, label):
    text = _literal_or_raise(word)
    if text == "-":
        return _stdin(ctx, label)
    path = _resolve(text, ctx.cwd)
    if path in ctx.writes:
        if not ctx.bodies:
            raise Unreadable(f"{label} `{text}` is written by this same command; write it in a "
                             "separate step so its content can be checked")
        return list(ctx.bodies)
    try:
        return [path.read_text(encoding="utf-8", errors="replace")]
    except OSError as e:
        raise Unreadable(f"cannot read {label} `{text}` ({e.strerror or e})")


def _stdin(ctx, label):
    if not ctx.bodies:
        raise Unreadable(f"{label} reads stdin, which this guard cannot see; pass --body-file <path>")
    return list(ctx.bodies)


def _inline(word, ctx):
    m = WHOLE_CAT.match(word.raw)
    if m:
        return _read_file(_shell_scan.Word(raw=m.group(2), text=m.group(2)), ctx, "file")
    return [_literal_or_raise(word)]


def _named(word, names):
    return any(word.basename_is(n) for n in names)


def _strip_prefix(seg):
    while seg and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", seg[0].text)
                   or _named(seg[0], WRAPPERS)):
        seg = seg[1:]
    return seg


def _flag_value(seg, i):
    """(flag, value Word, next index) for seg[i], splitting `--flag=value`."""
    w = seg[i]
    if w.text.startswith("--") and "=" in w.text:
        flag, _, val = w.text.partition("=")
        raw_val = w.raw.split("=", 1)[1] if "=" in w.raw else val
        return flag, _shell_scan.Word(raw=raw_val, text=val, quoted=w.quoted), i + 1
    if i + 1 < len(seg):
        return w.text, seg[i + 1], i + 2
    return w.text, None, i + 1


def _gh_action(seg, ctx):
    """None, or (repo, label, [texts]) for a publishing gh invocation."""
    tail = seg[1:]
    texts_words = [w.text for w in tail]
    if "api" in texts_words[:4]:
        return _api_action(tail[texts_words.index("api") + 1:], ctx)
    sub = None
    for j in range(len(texts_words) - 1):
        if texts_words[j] in VERBS and texts_words[j + 1] in VERBS[texts_words[j]]:
            sub = (texts_words[j], texts_words[j + 1])
            break
    if not sub:
        return None
    repo, texts, i = None, [], 0
    while i < len(tail):
        w = tail[i]
        flag = w.text.split("=", 1)[0] if w.text.startswith("--") else w.text
        if flag in REPO_FLAGS or flag in INLINE_FLAGS or flag in FILE_FLAGS:
            flag, val, i = _flag_value(tail, i)
            if val is None:
                raise Unreadable(f"`{flag}` has no value")
            if flag in REPO_FLAGS:
                repo = _literal_or_raise(val)
            elif flag in INLINE_FLAGS:
                texts += _inline(val, ctx)
            else:
                texts += _read_file(val, ctx, flag)
            continue
        m = GH_URL.search(w.text)
        if m and repo is None:
            repo = m.group(1)
        i += 1
    return repo, f"gh {sub[0]} {sub[1]}", texts


def _api_action(args, ctx):
    method, endpoint, fields, texts, i = None, None, False, [], 0
    while i < len(args):
        w = args[i]
        if w.text.startswith("-X") and len(w.text) > 2:
            method = w.text[2:].upper()
            i += 1
            continue
        flag = w.text.split("=", 1)[0] if w.text.startswith("--") else w.text
        if flag in API_VALUE_FLAGS:
            flag, val, i = _flag_value(args, i)
            if val is None:
                continue
            if flag in ("-X", "--method"):
                method = val.text.upper()
            elif flag in API_FIELD_FLAGS:
                fields = True
                key, _, value = val.text.partition("=")
                raw_value = val.raw.split("=", 1)[1] if "=" in val.raw else value
                vword = _shell_scan.Word(raw=raw_value, text=value, quoted=val.quoted)
                if flag in ("-F", "--field") and value.startswith("@"):
                    target = value[1:]
                    texts += _read_file(_shell_scan.Word(raw=target, text=target), ctx, f"field {key}")
                else:
                    texts.append(_literal_or_raise(vword))
            elif flag == "--input":
                fields = True
                texts += _read_file(val, ctx, "--input")
            continue
        if endpoint is None and not w.text.startswith("-"):
            endpoint = w.text
        i += 1
    method = method or ("POST" if fields else "GET")
    if method not in ("POST", "PATCH", "PUT") or endpoint is None:
        return None
    m = API_REPO.match(endpoint)
    if m:
        return m.group(1), f"gh api {method} {m.group(2)}", texts
    if endpoint.strip("/") == "graphql" and any("mutation" in t for t in texts):
        return None, "gh api graphql mutation", texts
    return None


def _writes(segments, cwd):
    out = set()
    for seg in segments:
        for i, w in enumerate(seg):
            m = REDIRECT.match(w.text)
            target = None
            if m and w.text[:1] in ">&0123456789" and ">" in w.text:
                target = m.group(1) or (seg[i + 1].text if i + 1 < len(seg) else None)
            elif i > 0 and seg[0].basename_is("tee") and not w.text.startswith("-"):
                target = w.text
            if target and cwd is not None and "$" not in target:
                out.add(_resolve(target, cwd))
    return out


def actions(command: str, cwd, depth: int = 0):
    """Every publishing gh invocation in `command`: (repo, label, [texts])."""
    stripped, bodies = split_heredocs(command)
    segments = _shell_scan.segments(stripped)
    if not segments and stripped.strip():
        if GH_WRITE_HINT.search(command):
            raise Unreadable("the command does not parse as shell")
        return []
    herestrings = []
    for seg in segments:
        for i, w in enumerate(seg):
            if w.text.startswith("<<<"):
                rest = w.text[3:] or (seg[i + 1].text if i + 1 < len(seg) else "")
                herestrings.append(rest)
    ctx = Ctx(cwd, bodies + herestrings, _writes(segments, cwd))
    found = []
    for seg in segments:
        seg = _strip_prefix(seg)
        if not seg:
            continue
        if seg[0].basename_is("cd"):
            target = seg[1].text if len(seg) > 1 else os.path.expanduser("~")
            ctx.cwd = None if ("$" in target or ctx.cwd is None and not target.startswith(("/", "~"))) \
                else str(_resolve(target, ctx.cwd or "/"))
            ctx.writes = _writes(segments, ctx.cwd) if ctx.cwd else ctx.writes
            continue
        if _named(seg[0], SHELLS | {"eval"}) and depth < 3:
            inner = [w.text for w in seg[1:] if not w.text.startswith("-")]
            if seg[0].basename_is("eval"):
                inner = [" ".join(w.text for w in seg[1:])]
            for text in inner[:1]:
                if "gh" in text:
                    found += actions(text, ctx.cwd, depth + 1)
            continue
        if seg[0].basename_is("gh", fold=True):
            act = _gh_action(seg, ctx)
            if act:
                found.append(act)
    return found


def decide(command: str, cwd, workspace: Path, lookup=None):
    """None to allow, or the refusal text."""
    if not isinstance(command, str) or "gh" not in command:
        return None
    try:
        acts = actions(command, cwd)
    except Unreadable as e:
        return (f"PUBLIC-WRITE PRIVACY GATE: refused — {e}. A body this guard cannot read is "
                f"treated as unsafe to publish. {TAG}")
    if not acts:
        return None
    pdir = policy.privacy_dir(workspace)
    kwargs = {"lookup": lookup} if lookup else {}
    deny_terms = None
    problems = []
    for repo, label, texts in acts:
        if not any(t.strip() for t in texts):
            continue
        vis = policy.repo_visibility(repo, pdir / policy.VISIBILITY_CACHE_NAME, **kwargs)
        if vis == "PRIVATE":
            continue
        if deny_terms is None:
            deny_terms = policy.load_terms(pdir / policy.DENYLIST_NAME)
            if deny_terms is None:
                return (f"PUBLIC-WRITE PRIVACY GATE: refused — no private-term denylist at "
                        f"{pdir / policy.DENYLIST_NAME}, so this public write cannot be checked. "
                        f"Seed it with `python3 scripts/seed-public-denylist.py`, then retry. {TAG}")
            allow = [t.text for t in (policy.load_terms(pdir / policy.ALLOWLIST_NAME) or [])]
            allow += policy.own_identities()
        hits = policy.scan("\n".join(texts), deny_terms, allow)
        if hits:
            where = repo or "a repo this guard could not resolve (pass --repo OWNER/NAME)"
            problems.append(f"{label} to {where} ({(vis or 'visibility unknown').lower()}): "
                            + "; ".join(f"`{h.matched}` [{h.category}]" for h in hits))
    if not problems:
        return None
    return ("PUBLIC-WRITE PRIVACY GATE: refused — the text would publish private details where "
            "anyone can read them, and GitHub keeps the first version in edit history even after "
            "an edit.\n" + "\n".join(problems) + "\nDescribe them by function instead (\"a private "
            "backend repo\", \"agent A\", \"a test room\", \"<repo>/path\"). If a term is already "
            f"public, add it to {pdir / policy.ALLOWLIST_NAME}. {TAG}")


def main(argv):
    workspace = None
    if "--workspace" in argv:
        workspace = Path(argv[argv.index("--workspace") + 1])
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command") or ""
    if "gh" not in command:
        return 0
    try:
        workspace = workspace or policy.resolve_workspace_dir()
        reason = decide(command, payload.get("cwd") or os.getcwd(), workspace)
    except Exception as e:  # a gh command we failed to check is not a cleared one
        reason = f"PUBLIC-WRITE PRIVACY GATE: refused — the guard failed ({type(e).__name__}: {e}). {TAG}"
        if not GH_WRITE_HINT.search(command):
            return 0
    if reason:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
