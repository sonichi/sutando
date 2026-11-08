#!/usr/bin/env python3
"""Merge-hold gate for github-evidence-guard.py: a PR merge waits for an announced re-read.

A hold is an announcement ("re-reading", "will re-review", "do not merge", ...) in an issue
comment or review body posted since the PR head's commit by a login with standing (a
requested reviewer, a login that has reviewed the PR, or an OWNER/MEMBER/COLLABORATOR; never
the PR author or a bot), or a hold recorded from a chat room in
<workspace>/state/merge-holds.json. It resolves when that login posts any newer review, or a
newer comment that approves or says lgtm / done / no further / clear to merge. It expires
after `env.MERGE_HOLD_TTL_HOURS` in sutando.config (default 4) or a room hold's own
`expires_at`; an expired hold is an advisory line, never a denial. One GraphQL read per
merge, 5 s budget; a failed or truncated read is UNKNOWN. `env.MERGE_HOLD_MODE` picks
`shadow` (default: log to <workspace>/logs/merge-hold-shadow.jsonl, never deny) or `enforce`
(HOLD and UNKNOWN deny unless `SUTANDO_MERGE_HOLD_OVERRIDE=<reason>` leads the merge command,
appended to <workspace>/state/guards/merge_hold_overrides.jsonl first).

CLI (exit 0 CLEAR / done, 1 HOLD, 2 UNKNOWN):
  python3 hooks/_merge_hold.py OWNER/REPO PR [--add-hold|--clear-hold --who LOGIN [--note T]]
  python3 hooks/_merge_hold.py --summary [--log PATH]
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _shell_scan  # noqa: E402

HOLD, CLEAR, UNKNOWN = "HOLD", "CLEAR", "UNKNOWN"
GH_TIMEOUT = 5
DEFAULT_TTL_HOURS = 4.0
TTL_KEY = "MERGE_HOLD_TTL_HOURS"
MODE_KEY = "MERGE_HOLD_MODE"
SHADOW, ENFORCE = "shadow", "enforce"
STANDING_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
MIN_REASON = 8
STAMP = "%Y-%m-%dT%H:%M:%SZ"

ANNOUNCE = re.compile(
    r"\bre-?read(?:ing)?\b|\breading (?:it |this )?again\b|\bwill re-?review\b"
    r"|\btaking another look\b|\bhold (?:the |this )?merge\b|\bdo\s*n[o']?t merge\b|\bplease wait\b",
    re.I)
# A re-read named as already done ("after re-reading it", "I re-read the diff") announces nothing.
REREAD = re.compile(r"\bre-?read(?:ing)?\b", re.I)
DONE_BEFORE = re.compile(r"\b(?:after|having|have|has|had|i've|we've|i|we|was|were|been|since|by|from|when|just)\s+$",
                         re.I)
RESOLVE = re.compile(r"\bapprov(?:e|ed|ing)\b|\blgtm\b|\bdone\b|\bno further\b|\bclear to merge\b", re.I)
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

OVERRIDE_VAR = "SUTANDO_MERGE_HOLD_OVERRIDE"
LOOKS_LIKE_MERGE = re.compile(r"\bpr\s+merge\b|mergePullRequest|enqueuePullRequest|/pulls/\d+/merge\b"
                              r"|\bgraphql\b.*(?:--input|=@)", re.S)
MUTATION = re.compile(r"\b(mergePullRequest|enqueuePullRequest)\b")
PR_ID_IN_QUERY = re.compile(r"pullRequestId\s*:\s*(?:\"([^\"]*)\"|\$(\w+))")
PR_URL = re.compile(r"^https://github\.com/([^/]+/[^/]+)/pull/(\d+)(?:[/?#].*)?$")
REST_MERGE = re.compile(r"^/?repos/([^/]+/[^/]+)/pulls/(\d+)/merge$")
NODE_ID = re.compile(r"^[A-Za-z0-9_=-]+$")
ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
HEREDOC = re.compile(r"(?<!<)<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
# gh flags that take a value, so the word after them is not a positional.
PR_MERGE_VALUE_FLAGS = {"-R", "--repo", "-b", "--body", "-F", "--body-file", "-t", "--subject",
                        "--match-head-commit", "-A", "--author-email"}
API_VALUE_FLAGS = {"-X", "--method", "-H", "--header", "-f", "--raw-field", "-F", "--field",
                   "--input", "-q", "--jq", "-t", "--template", "--hostname", "--cache", "-p", "--preview"}

_PR_FIELDS = """number repository { nameWithOwner } author { __typename login }
  commits(last: 1) { nodes { commit { committedDate } } }
  reviewRequests(first: 100) { nodes { requestedReviewer { ... on User { login } ... on Mannequin { login } } } }
  reviews(last: 100) { pageInfo { hasPreviousPage }
    nodes { author { __typename login } authorAssociation submittedAt body url } }
  comments(last: 100) { pageInfo { hasPreviousPage }
    nodes { author { __typename login } authorAssociation createdAt body url } }"""
BY_NUMBER = ("query($o: String!, $n: String!, $pr: Int!) { repository(owner: $o, name: $n) { pullRequest(number: $pr) { "
             + _PR_FIELDS + " } } }")
BY_NODE = "query($id: ID!) { node(id: $id) { ... on PullRequest { " + _PR_FIELDS + " } } }"

Gh = Callable[[List[str]], str]


class GhError(RuntimeError):
    """The GitHub read failed, timed out, or returned what this cannot read."""


class StateError(RuntimeError):
    """The room-hold file or the TTL setting exists but cannot be read."""


def _target(repo=None, pr=None, node=None, problem="", override="", kind="pr-merge"):
    return {"repo": repo, "pr": pr, "node": node, "problem": problem, "override": override, "kind": kind}


def _flag_value(tail, i):
    """(value, next index) for the flag at tail[i], handling --flag=value."""
    w = tail[i]
    if w.startswith("--") and "=" in w:
        return w.split("=", 1)[1], i + 1
    return (tail[i + 1] if i + 1 < len(tail) else ""), i + 2


def _pr_merge(tail, override):
    repo, ref, i = None, None, 0
    if "--disable-auto" in tail:
        return []
    while i < len(tail):
        w = tail[i]
        name = w.split("=", 1)[0] if w.startswith("--") else w
        if name in PR_MERGE_VALUE_FLAGS:
            val, i = _flag_value(tail, i)
            if name in ("-R", "--repo"):
                repo = val
            continue
        if not w.startswith("-") and ref is None:
            ref = w
        i += 1
    if ref and PR_URL.match(ref):
        m = PR_URL.match(ref)
        return [_target(m[1], int(m[2]), override=override)]
    if ref and ref.lstrip("#").isdigit():
        if repo and re.fullmatch(r"[^/\s]+/[^/\s]+", repo):
            return [_target(repo, int(ref.lstrip("#")), override=override)]
        return [_target(problem=f"`gh pr merge {ref}` names no repository; add --repo OWNER/REPO",
                        override=override)]
    return [_target(problem="`gh pr merge` names no PR number or URL; name both the PR and --repo OWNER/REPO",
                    override=override)]


def _read_body(path, cwd):
    """Text of a `--input` / `@file` argument, or None when it cannot be read."""
    if path == "-":
        return None
    try:
        with open(os.path.join(cwd or os.getcwd(), os.path.expanduser(path))) as f:
            return f.read(1 << 20)
    except (OSError, UnicodeDecodeError):
        return None


def _api(tail, override, cwd=None):
    endpoint, method, fields, has_input, i = None, None, {}, False, 0
    input_path = None
    while i < len(tail):
        w = tail[i]
        name = w.split("=", 1)[0] if w.startswith("--") else w
        if name in API_VALUE_FLAGS:
            val, i = _flag_value(tail, i)
            if name in ("-X", "--method"):
                method = val.upper()
            elif name in ("-f", "--raw-field", "-F", "--field"):
                k, _, v = val.partition("=")
                fields[k] = v
            elif name == "--input":
                has_input, input_path = True, val
            continue
        if not w.startswith("-") and endpoint is None:
            endpoint = w
        i += 1
    if endpoint is None:
        return []
    m = REST_MERGE.match(endpoint)
    if m:
        writes = method not in (None, "GET") or (method is None and (fields or has_input))
        return [_target(m[1], int(m[2]), override=override, kind="rest-merge")] if writes else []
    if endpoint != "graphql":
        return []
    query, kind = fields.get("query", ""), "graphql"
    unreadable = "a graphql body read from a file this guard cannot read; inline the query"
    if has_input:
        body = _read_body(input_path, cwd)
        try:
            doc = json.loads(body) if body is not None else None
            query = doc.get("query", "")
            fields.update({k: str(v) for k, v in (doc.get("variables") or {}).items()})
        except (ValueError, AttributeError, TypeError):
            return [_target(problem=unreadable, override=override, kind=kind)]
    elif query.startswith("@"):
        body = _read_body(query[1:], cwd)
        if body is None:
            return [_target(problem=unreadable, override=override, kind=kind)]
        query = body
    if not isinstance(query, str) or not MUTATION.search(query):
        return []
    kind = "graphql-" + MUTATION.search(query).group(1)
    if "$(" in query or "`" in query:
        return [_target(problem="the merge mutation's query is computed at run time", override=override, kind=kind)]
    out = []
    for lit, var in PR_ID_IN_QUERY.findall(query):
        node = lit if lit else fields.get(var, "")
        if node and NODE_ID.match(node):
            out.append(_target(node=node, override=override, kind=kind))
        else:
            out.append(_target(problem="the merge mutation's pullRequestId is not a literal node id",
                               override=override, kind=kind))
    return out or [_target(problem="a merge mutation with no pullRequestId this guard can read",
                           override=override, kind=kind)]


def strip_heredocs(command):
    """Drop heredoc bodies: they are data, and the scanner reads their lines as commands."""
    out, pending = [], []
    for line in command.split("\n"):
        if pending:
            dash, word = pending[0]
            if (line.lstrip("\t") if dash else line) == word:
                pending.pop(0)
            continue
        out.append(line)
        pending = [(m[1] == "-", m[3]) for m in HEREDOC.finditer(line)]
    return "\n".join(out)


def merge_targets(command, depth=0, cwd=None):
    """Every merge the command would run, as target dicts; ValueError when it does not lex."""
    command = strip_heredocs(command)
    segs = _shell_scan.segments(command)
    if not segs and command.strip():
        raise ValueError("command does not lex")
    out = []
    for words in segs:
        texts = [w.text for w in words]
        override = ""
        while texts and (ASSIGN.match(texts[0]) or os.path.basename(texts[0]) == "env"):
            m = ASSIGN.match(texts[0])
            if m and m[1] == OVERRIDE_VAR:
                override = m[2].strip()
            texts = texts[1:]
        if not texts:
            continue
        head = os.path.basename(texts[0])
        if depth < 3 and (head == "eval" or (head in SHELLS and "-c" in texts[1:])):
            if head == "eval":
                inner = " ".join(texts[1:])
            else:
                rest = texts[texts.index("-c") + 1:]
                inner = rest[0] if rest else ""
            for t in merge_targets(inner, depth + 1, cwd):
                t["override"] = t["override"] or override
                out.append(t)
            continue
        if head != "gh":
            continue
        tail = texts[1:]
        if tail[:2] == ["pr", "merge"]:
            out += _pr_merge(tail[2:], override)
        elif tail[:1] == ["api"]:
            out += _api(tail[1:], override, cwd)
    return out



def run_gh(args: List[str]) -> str:
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=GH_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise GhError(f"gh did not answer within {GH_TIMEOUT}s") from e
    except OSError as e:
        raise GhError(f"gh could not run: {e}") from e
    if p.returncode != 0:
        raise GhError(f"gh api graphql: {p.stderr.strip()[:200]}")
    return p.stdout


def _ts(value) -> dt.datetime:
    t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(STAMP)


def fetch(target: dict, gh: Gh) -> Tuple[str, int, dt.datetime, dict, List[dict]]:
    """(repo, pr, head commit time, PR facts, human comments and reviews oldest first): one gh call."""
    if target.get("node"):
        args = ["api", "graphql", "-f", "query=" + BY_NODE, "-f", "id=" + target["node"]]
    else:
        owner, name = target["repo"].split("/", 1)
        args = ["api", "graphql", "-f", "query=" + BY_NUMBER, "-f", "o=" + owner, "-f", "n=" + name,
                "-F", f"pr={int(target['pr'])}"]
    try:
        data = json.loads(gh(args))["data"]
        pull = data["node"] if target.get("node") else data["repository"]["pullRequest"]
        repo, pr = pull["repository"]["nameWithOwner"], int(pull["number"])
        since = _ts(pull["commits"]["nodes"][-1]["commit"]["committedDate"])
        facts = {"author": ((pull.get("author") or {}).get("login") or "ghost").lower(),
                 "requested": {(n.get("requestedReviewer") or {}).get("login", "").lower()
                               for n in pull["reviewRequests"]["nodes"]} - {""}}
        ev = []
        for kind, conn, at in (("comment", "comments", "createdAt"), ("review", "reviews", "submittedAt")):
            nodes = [n for n in pull[conn]["nodes"] if n.get(at)]
            if pull[conn]["pageInfo"]["hasPreviousPage"] and (not nodes or min(_ts(n[at]) for n in nodes) >= since):
                raise GhError(f"more than 100 {conn} since the head commit; older ones were not read")
            for n in nodes:
                author = n.get("author") or {}
                if author.get("__typename") == "Bot" or str(author.get("login", "")).lower().endswith("[bot]"):
                    continue
                ev.append({"who": author.get("login") or "ghost", "at": _ts(n[at]), "kind": kind,
                           "body": n.get("body") or "", "url": n.get("url") or "",
                           "assoc": n.get("authorAssociation") or ""})
    except (ValueError, KeyError, TypeError, IndexError, AttributeError) as e:
        raise GhError(f"unreadable GitHub response: {e!r}") from e
    return repo, pr, since, facts, sorted(ev, key=lambda e: e["at"])


def announces(body: str) -> bool:
    """Does the body announce a pending re-read or hold? Quoted (`>`) lines are someone else's words."""
    text = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith(">"))
    for m in ANNOUNCE.finditer(text):
        if not (REREAD.fullmatch(m.group(0)) and DONE_BEFORE.search(text[max(0, m.start() - 30):m.start()])):
            return True
    return False


def resolves(e: dict) -> bool:
    if e["kind"] == "review":
        return True
    return not announces(e["body"]) and bool(RESOLVE.search(e["body"]))


def _age(delta: dt.timedelta) -> str:
    mins = int(delta.total_seconds() // 60)
    return f"{mins // 60}h{mins % 60:02d}m old"


def open_holds(since: dt.datetime, facts: dict, ev: List[dict], room_holds: List[dict], ttl: dt.timedelta,
               now: dt.datetime) -> Tuple[List[dict], List[dict]]:
    """(live holds, holds that expired unanswered)."""
    live, expired = [], []
    reviewed = {e["who"].lower() for e in ev if e["kind"] == "review"}
    for i, e in enumerate(ev):
        who = e["who"].lower()
        if e["at"] < since or who == facts["author"] or not announces(e["body"]):
            continue
        if who not in facts["requested"] and who not in reviewed and e["assoc"] not in STANDING_ASSOCIATIONS:
            continue
        if any(later["who"].lower() == who and resolves(later) for later in ev[i + 1:]):
            continue
        (live if now < e["at"] + ttl else expired).append(dict(e, expires=e["at"] + ttl))
    for h in room_holds:
        if h.get("cleared_at"):
            continue
        said, who = _ts(h["said_at"]), str(h["who"]).lower()
        if any(e["who"].lower() == who and e["at"] > said and resolves(e) for e in ev):
            continue
        until = _ts(h["expires_at"]) if h.get("expires_at") else said + ttl
        hold = {"who": h["who"], "at": said, "kind": "room hold", "expires": until,
                "body": h.get("note", ""), "url": h.get("url") or "merge-holds.json"}
        (live if now < until else expired).append(hold)
    return live, expired


def _src(repo_root) -> str:
    src = os.path.join(str(repo_root), "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    return src


def workspace(repo_root) -> Path:
    _src(repo_root)
    # The resolver can print migration notices; a hook's stderr reaches the model.
    with contextlib.redirect_stderr(io.StringIO()):
        from workspace_default import resolve_workspace
        return Path(resolve_workspace(migrate=False))


def state_path(repo_root) -> Path:
    return workspace(repo_root) / "state" / "merge-holds.json"


def override_log_path(repo_root) -> Path:
    return workspace(repo_root) / "state" / "guards" / "merge_hold_overrides.jsonl"


def shadow_log_path(repo_root) -> Path:
    return workspace(repo_root) / "logs" / "merge-hold-shadow.jsonl"


def gate_mode(repo_root) -> str:
    """`shadow` (default: decide and log, never deny) or `enforce`; any other value enforces."""
    _src(repo_root)
    with contextlib.redirect_stderr(io.StringIO()):
        from sutando_config import config_get
        raw = config_get(MODE_KEY, SHADOW, Path(repo_root))
    return SHADOW if str(raw).strip().lower() == SHADOW else ENFORCE


def ttl_hours(repo_root) -> float:
    _src(repo_root)
    with contextlib.redirect_stderr(io.StringIO()):
        from sutando_config import config_get
        raw = config_get(TTL_KEY, str(DEFAULT_TTL_HOURS), Path(repo_root))
    try:
        hours = float(raw)
    except (TypeError, ValueError) as e:
        raise StateError(f"{TTL_KEY}={raw!r} is not a number of hours") from e
    if not hours > 0:
        raise StateError(f"{TTL_KEY}={raw!r} must be positive")
    return hours


def read_holds(path: Path) -> List[dict]:
    if not path.exists():
        return []
    try:
        holds = json.loads(path.read_text())
        if not isinstance(holds, list):
            raise ValueError("top level is not a list")
        for h in holds:
            if not isinstance(h, dict) or not {"repo", "pr", "who", "said_at"} <= h.keys():
                raise ValueError(f"malformed hold {h!r}"[:120])
            _ts(h["said_at"])
            if h.get("expires_at"):
                _ts(h["expires_at"])
            int(h["pr"])
    except (OSError, ValueError, TypeError) as e:
        raise StateError(f"cannot read {path}: {e}") from e
    return holds


def _write_holds(path: Path, holds: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(holds, indent=2) + "\n")
    os.replace(tmp, path)


def _same_pr(h: dict, repo: str, pr: int) -> bool:
    return str(h.get("repo", "")).lower() == repo.lower() and int(h.get("pr", -1)) == pr


def add_hold(path: Path, repo: str, pr: int, who: str, note: str = "", url: str = "", expires_at: str = "") -> dict:
    holds = read_holds(path)
    hold = {"repo": repo, "pr": pr, "who": who, "said_at": _now(), "note": note}
    if url:
        hold["url"] = url
    if expires_at:
        hold["expires_at"] = _ts(expires_at).strftime(STAMP)
    holds.append(hold)
    _write_holds(path, holds)
    return hold


def clear_hold(path: Path, repo: str, pr: int, who: str) -> int:
    holds, n = read_holds(path), 0
    for h in holds:
        if _same_pr(h, repo, pr) and str(h["who"]).lower() == who.lower() and not h.get("cleared_at"):
            h["cleared_at"] = _now()
            n += 1
    _write_holds(path, holds)
    return n


def check(target: dict, holds_file: Path, ttl: float, gh: Optional[Gh] = None,
          now: Optional[dt.datetime] = None) -> Dict:
    """{status, repo, pr, holds, advisories, reason}; never raises, anything unread is UNKNOWN."""
    label = target.get("node") or f"{target.get('repo')}#{target.get('pr')}"
    try:
        if not target.get("node") and not REPO_RE.match(str(target.get("repo") or "")):
            raise GhError("repository is not OWNER/REPO")
        all_holds = read_holds(holds_file)
        repo, pr, since, facts, ev = fetch(target, gh or run_gh)
        local = [h for h in all_holds if _same_pr(h, repo, pr)]
        now = now or dt.datetime.now(dt.timezone.utc)
        found, expired = open_holds(since, facts, ev, local, dt.timedelta(hours=ttl), now)
    except (GhError, StateError) as e:
        return _unknown(label, str(e), target)
    except Exception as e:
        return _unknown(label, f"check failed: {e!r}", target)
    notes = [f"EXPIRED hold by {h['who']} announced {h['at']:{STAMP}} ({_age(now - h['at'])}); re-ask them {h['url']}"
             for h in expired]
    lines = [f"HOLD {repo}#{pr}: {h['who']} at {h['at']:{STAMP}} ({h['kind']}, expires {h['expires']:{STAMP}}) "
             f"{h['url']}" for h in found]
    return {"status": HOLD if lines else CLEAR, "label": f"{repo}#{pr}", "repo": repo, "pr": pr, "holds": lines,
            "advisories": notes, "holders": [h["who"] for h in found], "expired_by": [h["who"] for h in expired],
            "reason": f"no open announced re-read since {since:{STAMP}}"}


def _unknown(label: str, reason: str, target: dict) -> Dict:
    return {"status": UNKNOWN, "label": label, "repo": target.get("repo"), "pr": target.get("pr"), "holds": [],
            "advisories": [], "holders": [], "expired_by": [], "reason": reason}


def record_override(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps({"ts": _now(), **entry}, default=str) + "\n")


OVERRIDE_HINT = (f"To merge deliberately anyway, prefix the merge command with {OVERRIDE_VAR}='<reason>' "
                 f"(at least {MIN_REASON} characters; logged).")


def _unknown_reason(problems: List[str]) -> str:
    return ("merge-hold: cannot verify merge holds (" + "; ".join(problems) + "). Re-run, or record an override. "
            + OVERRIDE_HINT)


def _hold_reason(lines: List[str]) -> str:
    return ("merge-hold: a reviewer announced a re-read that has not landed.\n" + "\n".join(lines) +
            "\nWait for that reviewer's newer review, or a newer comment that approves or says LGTM / done / "
            "no further / clear to merge; the hold also lapses at its expiry. A room hold clears with "
            "`python3 hooks/_merge_hold.py OWNER/REPO PR --clear-hold --who LOGIN` once they answer. " + OVERRIDE_HINT)


def _log_rows(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")


def _row(t: dict, v: Optional[Dict], elapsed_ms: float, mode: str) -> dict:
    v = v or _unknown("", t.get("problem", ""), t)
    verdict = v["status"]
    if verdict == CLEAR and v["expired_by"]:
        verdict = "EXPIRED"
    holder = v["holders"] if verdict == HOLD else v["expired_by"] if verdict == "EXPIRED" else []
    return {"ts": _now(), "repo": v.get("repo") or t.get("repo"), "pr": v.get("pr") or t.get("pr"), "verdict": verdict,
            "holder": ",".join(holder), "elapsed_ms": round(elapsed_ms, 1), "kind": t.get("kind", ""),
            "mode": mode, "override": bool(t.get("override"))}


def gate(command: str, cwd: Optional[str], repo_root: Optional[str], gh: Optional[Gh] = None,
         now: Optional[dt.datetime] = None) -> Tuple[str, str]:
    """("allow" | "deny", reason or context) for one Bash command; ("allow", "") when it merges nothing.

    In shadow mode the decision is logged to <workspace>/logs/merge-hold-shadow.jsonl and never denies.
    """
    if not LOOKS_LIKE_MERGE.search(command):
        return "allow", ""
    try:
        targets = merge_targets(command, cwd=cwd)
    except ValueError:
        targets = [_target(problem="the command did not parse", kind="unparsed")]
    if not targets:
        return "allow", ""
    try:
        mode = gate_mode(repo_root) if repo_root else ENFORCE
    except Exception:
        mode = ENFORCE
    verdict, text, rows = _decide(command, targets, repo_root, gh, now, mode)
    try:
        if repo_root:
            _log_rows(shadow_log_path(repo_root), rows)
    except Exception:
        pass
    return ("allow", "") if mode == SHADOW else (verdict, text)


def _decide(command: str, targets: List[dict], repo_root: Optional[str], gh: Optional[Gh],
            now: Optional[dt.datetime], mode: str) -> Tuple[str, str, List[dict]]:
    held, problems, advisories, rows = [], [], [], []
    try:
        if not repo_root:
            raise StateError("repo root not configured (--repo or SUTANDO_REPO_ROOT)")
        holds_file, ttl, log = state_path(repo_root), ttl_hours(repo_root), override_log_path(repo_root)
    except Exception as e:
        rows = [_row(t, _unknown("", str(e), t), 0.0, mode) for t in targets]
        return "deny", _unknown_reason([f"merge-hold settings unavailable: {e}"]), rows
    for t in targets:
        if t["problem"]:
            problems.append(t["problem"])
            rows.append(_row(t, None, 0.0, mode))
            continue
        start = time.monotonic()
        v = check(t, holds_file, ttl, gh, now)
        rows.append(_row(t, v, (time.monotonic() - start) * 1e3, mode))
        advisories += v["advisories"]
        if v["status"] == HOLD:
            held += v["holds"]
        elif v["status"] != CLEAR:
            problems.append(f"{v['label']}: {v['reason']}")
    note = "\n".join("merge-hold advisory: " + a for a in advisories)
    if not held and not problems:
        return "allow", note, rows
    reasons = [t["override"] for t in targets if len(t["override"]) >= MIN_REASON]
    if len(reasons) != len(targets):
        return "deny", _hold_reason(held) if held else _unknown_reason(problems), rows
    if mode == SHADOW:
        return "allow", "", rows
    try:
        record_override(log, {"targets": [{k: t[k] for k in ("repo", "pr", "node")} for t in targets],
                              "holds": held, "problems": problems, "reason": reasons[0], "command": command[:300]})
    except Exception as e:
        return "deny", _unknown_reason(problems + held + [f"override not recorded: {e}"]), rows
    return "allow", (f"merge-hold: merge allowed past {'a live hold' if held else 'an unverifiable check'} on a "
                     f"recorded override ({reasons[0]}); logged to {log}." + ("\n" + note if note else "")), rows


def summarize(path: Path) -> str:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r.get("verdict", "?")] = counts.get(r.get("verdict", "?"), 0) + 1
    ms = sorted(float(r.get("elapsed_ms", 0)) for r in rows if r.get("verdict") != UNKNOWN or r.get("elapsed_ms"))

    def pct(p: float) -> str:
        return f"{ms[max(0, -(-len(ms) * p // 100) - 1)]:.1f}" if ms else "n/a"

    order = [HOLD, "EXPIRED", CLEAR, UNKNOWN] + sorted(set(counts) - {HOLD, "EXPIRED", CLEAR, UNKNOWN})
    lines = [f"{len(rows)} merge-command decision(s) in {path}"]
    lines += [f"  {k:<8} {counts[k]}" for k in order if k in counts]
    lines.append(f"  elapsed_ms p50 {pct(50)}  p95 {pct(95)}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Record or clear a merge hold announced in a chat room.")
    ap.add_argument("repo", nargs="?")
    ap.add_argument("pr", nargs="?", type=int)
    ap.add_argument("--summary", action="store_true", help="summarise the shadow log: counts and p50/p95 elapsed_ms")
    ap.add_argument("--log", type=Path, help="shadow log (default: <workspace>/logs/merge-hold-shadow.jsonl)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--add-hold", action="store_true")
    mode.add_argument("--clear-hold", action="store_true")
    ap.add_argument("--who")
    ap.add_argument("--note", default="")
    ap.add_argument("--url", default="", help="link to the room message that announced it")
    ap.add_argument("--expires-at", default="", help="ISO time the room hold lapses (default: the TTL)")
    ap.add_argument("--repo-root", default=os.environ.get("SUTANDO_REPO_ROOT") or ".",
                    help="the Sutando checkout whose workspace holds the file (default: .)")
    ap.add_argument("--state-file", type=Path, help="hold file (default: <workspace>/state/merge-holds.json)")
    a = ap.parse_args(argv)
    if a.summary:
        try:
            print(summarize(a.log or shadow_log_path(a.repo_root)))
        except (OSError, ValueError, ImportError) as e:
            print(f"UNKNOWN: {e}")
            return 2
        return 0
    if not a.repo or a.pr is None:
        ap.error("OWNER/REPO and PR are required unless --summary")
    try:
        path = a.state_file or state_path(a.repo_root)
        if a.add_hold or a.clear_hold:
            if not a.who:
                print("REFUSED: --who is required")
                return 2
            if a.add_hold:
                add_hold(path, a.repo, a.pr, a.who, a.note, a.url, a.expires_at)
                print(f"HOLD RECORDED {a.repo}#{a.pr} by {a.who}")
            else:
                print(f"CLEARED {clear_hold(path, a.repo, a.pr, a.who)} hold(s) on {a.repo}#{a.pr} by {a.who}")
            return 0
        v = check({"repo": a.repo, "pr": a.pr, "node": None}, path, ttl_hours(a.repo_root))
    except (StateError, OSError, ValueError, ImportError) as e:
        print(f"UNKNOWN: {e}")
        return 2
    print("\n".join((v["holds"] or [f"{v['status']} {v['label']}: {v['reason']}"]) + v["advisories"]))
    return {CLEAR: 0, HOLD: 1}.get(v["status"], 2)


if __name__ == "__main__":
    sys.exit(main())
