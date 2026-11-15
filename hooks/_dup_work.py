#!/usr/bin/env python3
"""Duplicate-work check: before a branch, a PR or a review, name another author's open PR that
changes the same function.

Policy for `github-evidence-guard.py --dup-check`. Triggers: `git checkout -b|-B`, `git switch
-c|-C|--create`, `git worktree add -b|-B`, `gh pr create`, `gh pr review`. The local side is the
working tree against its base (branch), `origin/<base>...HEAD` (create) or the reviewed PR's diff
(review). When it changes a function in a non-common file, one `gh api graphql` read (5 s budget)
lists the 25 most recently updated open PRs with their files; the top 3 by shared files are diffed
and their `@@` hunk-header functions compared.

Only one thing is flagged: an OPEN PR by another author that changes the same function in the
same file. The author's own PRs, a promote or back-port of the same change, and common files
(lockfiles, changelogs, docs, JSON, manifests) never count. It never blocks. A failed read with
no hit is UNKNOWN plus one line. One row per trigger goes to <workspace>/logs/dup-work.jsonl.
`env.DUP_WORK_CHECK: off` in sutando.config.local.json turns it off.

  python3 hooks/_dup_work.py --summary [--log PATH]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
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
from typing import Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _shell_scan  # noqa: E402

SWITCH_KEY = "DUP_WORK_CHECK"
GH_TIMEOUT = 5.0
GIT_TIMEOUT = 2.0
RECENT_OPEN = 25
SHOWN = 5
PR_URL = re.compile(r"^https://github\.com/([^/]+/[^/]+)/pull/(\d+)(?:[/?#].*)?$")
GITHUB_REMOTE = re.compile(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")
# Files many unrelated PRs touch, and data/prose whose hunk headers are not functions.
COMMON_FILES = re.compile(r"(^|/)(CHANGELOG[^/]*|README[^/]*|package-lock\.json|pnpm-lock\.yaml|yarn\.lock|uv\.lock"
                          r"|poetry\.lock|go\.sum|ENGINE_MANIFEST\.json|[^/]*\.lock)$"
                          r"|\.(?:md|mdx|rst|txt|json|jsonl|csv)$", re.I)
PROMOTE_WORDS = re.compile(r"^(?:chore\((?:promote|back-?port)\)|(?:promote|back-?port|cherry-?pick)\b)[:\s]*"
                           r"|\s*\(#\d+\)$", re.I)
HUNK = re.compile(r"^@@ [^@]* @@ ?(.*)$")
DIFF_FILE = re.compile(r"^\+\+\+ b/(.*)$")
FUNC_LINE = re.compile(r"[A-Za-z_$]")  # git's default funcname rule (no diff driver)
MAX_FN = 3
PR_FIELDS = "number title state headRefName baseRefName author{login} url"
GH_VALUE_FLAGS = {"-R", "--repo", "-b", "--body", "-F", "--body-file", "-t", "--title", "-H", "--head",
                  "-B", "--base", "-a", "--assignee", "-l", "--label", "-m", "--milestone", "-p", "--project",
                  "-r", "--reviewer", "-T", "--template"}

Run = Callable[..., subprocess.CompletedProcess]


def _flag(tail: List[str], names) -> Optional[str]:
    for i, a in enumerate(tail):
        if a in names and i + 1 < len(tail):
            return tail[i + 1]
        for n in names:
            if n.startswith("--") and a.startswith(n + "="):
                return a.split("=", 1)[1]
    return None


def _positional(tail: List[str]) -> Optional[str]:
    skip = False
    for a in tail:
        if skip:
            skip = False
        elif a.startswith("-"):
            skip = a in GH_VALUE_FLAGS
        else:
            return a
    return None


def _git_branch(tail: List[str]) -> Optional[str]:
    if tail[:1] == ["checkout"] or tail[:2] == ["worktree", "add"]:
        return _flag(tail, ("-b", "-B"))
    if tail[:1] == ["switch"]:
        return _flag(tail, ("-c", "-C", "--create", "--force-create"))
    return None


def triggers(command: str, cwd: Optional[str]) -> List[dict]:
    """Every trigger in a command line, with the directory it runs in (`cd X &&`, `git -C X`)."""
    stripped, _docs = _shell_scan.heredocs(command)
    try:
        segs = _shell_scan.segments(stripped)
    except ValueError:
        return []
    out, here = [], cwd or os.getcwd()
    for seg in segs:
        w = [x.text for x in seg]
        while w and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w[0]):
            w = w[1:]
        if not w:
            continue
        if w[0] == "cd" and len(w) > 1:
            here = os.path.join(here, os.path.expanduser(w[1]))
            continue
        prog, tail, where = os.path.basename(w[0]), w[1:], here
        if prog == "git":
            while tail[:1] in (["-C"], ["-c"]) and len(tail) > 1:
                if tail[0] == "-C":
                    where = os.path.join(where, os.path.expanduser(tail[1]))
                tail = tail[2:]
            name = _git_branch(tail)
            if name:
                out.append({"trigger": "branch", "branch": name, "cwd": where})
        elif prog == "gh" and tail[:2] == ["pr", "create"]:
            t = tail[2:]
            head = _flag(t, ("-H", "--head"))
            out.append({"trigger": "pr-create", "branch": head.split(":", 1)[-1] if head else None,
                        "title": _flag(t, ("-t", "--title")) or "",
                        "repo": _flag(t, ("-R", "--repo")), "base": _flag(t, ("-B", "--base")), "cwd": where})
        elif prog == "gh" and tail[:2] == ["pr", "review"]:
            t, repo, pr = tail[2:], None, None
            pos = _positional(t)
            m = PR_URL.match(pos or "")
            if m:
                repo, pr = m[1], int(m[2])
            elif pos and pos.isdigit():
                pr = int(pos)
            out.append({"trigger": "review", "pr": pr, "repo": repo or _flag(t, ("-R", "--repo")), "cwd": where})
    return out


def build_query(repo: str, t: dict):
    q = {"ro": f"repo:{repo} is:pr is:open sort:updated-desc"}
    parts = [f"ro:search(type:ISSUE,query:$ro,first:{RECENT_OPEN})"
             f"{{issueCount nodes{{... on PullRequest{{{PR_FIELDS} files(first:100){{nodes{{path}}}}}}}}}}"]
    decl, args = ["$ro:String!"], ["-f", f"ro={q['ro']}"]
    if t["trigger"] == "review":
        owner, name = repo.split("/", 1)
        decl += ["$o:String!", "$n:String!", "$pr:Int!"]
        parts.append("me:repository(owner:$o,name:$n){pullRequest(number:$pr){title headRefName baseRefName author{login} files(first:100){nodes{path}}}}")
        args += ["-f", f"o={owner}", "-f", f"n={name}", "-F", f"pr={t['pr']}"]
    parts.append("viewer{login}")
    return f"query({','.join(decl)}){{{' '.join(parts)}}}", args


def _git(run: Run, cwd: str, *args) -> Optional[str]:
    try:
        p = run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def _origin_repo(run: Run, cwd: str) -> Optional[str]:
    m = GITHUB_REMOTE.search(_git(run, cwd, "remote", "get-url", "origin") or "")
    return m[1] if m else None


def local_diff(run: Run, cwd: str, base: Optional[str], worktree: bool = False):
    """(changed files, {file: functions}) against the base, from local git only; None when unreadable.
    `worktree` compares the working tree (uncommitted edits included) with the merge base."""
    base = base or (_git(run, cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD") or "origin/main")
    base = base if base.startswith("origin/") else "origin/" + base
    rng = ["--merge-base", base] if worktree else [f"{base}...HEAD"]
    names = _git(run, cwd, "diff", "--name-only", *rng)
    text = _git(run, cwd, "diff", "-U3", *rng) if names is not None else None  # same context as `gh pr diff`
    if text is None:
        return None, {}
    return set(filter(None, names.splitlines())), hunk_functions(text)


class GhError(RuntimeError):
    """The search failed, timed out, or answered with something this cannot read."""


def pr_diff(run: Run, repo: str, number: int, timeout: float) -> Optional[str]:
    if timeout < 0.3:
        return None
    try:
        p = run(["gh", "pr", "diff", str(number), "-R", repo], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def search(run: Run, query: str, args: List[str], timeout: float = GH_TIMEOUT) -> dict:
    try:
        p = run(["gh", "api", "graphql", "-f", f"query={query}", *args], capture_output=True, text=True,
                timeout=max(0.3, timeout))
    except subprocess.TimeoutExpired as e:
        raise GhError(f"gh did not answer within {GH_TIMEOUT:.0f}s") from e
    except OSError as e:
        raise GhError(f"gh could not run: {e}") from e
    if p.returncode != 0:
        raise GhError((p.stderr or "gh api graphql failed").strip().splitlines()[-1][:160])
    try:
        data = json.loads(p.stdout)["data"]
    except (ValueError, KeyError, TypeError) as e:
        raise GhError("unreadable search response") from e
    if not isinstance(data, dict):
        raise GhError("unreadable search response")
    return data


def _nodes(data: dict, alias: str) -> List[dict]:
    return [n for n in ((data.get(alias) or {}).get("nodes") or []) if isinstance(n, dict) and n.get("number")]


def hunk_functions(diff_text: Optional[str]) -> Dict[str, set]:
    """{file: {enclosing function of each changed line}}, independent of the diff's context size.
    The `@@` header seeds the function; a context or added line that git's default rule would name a
    function (starts with a letter, `_` or `$`) takes over for the lines below it in the post-image."""
    res: Dict[str, set] = {}
    cur = fn = None
    in_hunk = False
    for line in (diff_text or "").splitlines():
        if line.startswith("diff --git "):
            cur, in_hunk = None, False
            continue
        m = DIFF_FILE.match(line)
        if m and not in_hunk:
            cur = m[1]
            res.setdefault(cur, set())
            continue
        m = HUNK.match(line)
        if m:
            in_hunk, fn = bool(cur), (m[1].strip()[:120] or None)
            continue
        if not in_hunk or not line or line[0] not in " +-":
            continue
        body = line[1:]
        if line[0] in " +" and FUNC_LINE.match(body):
            fn = body.strip()[:120]
        if line[0] in "+-" and fn:
            res[cur].add(fn)
    return res


def _login(n: dict) -> Optional[str]:
    return ((n.get("author") or {}).get("login") or "").lower() or None


def promote_pair(me: dict, n: dict) -> bool:
    """The same change on another base: one head branch, or one title bar a promote/back-port tag."""
    if me.get("branch") and me["branch"] == n.get("headRefName"):
        return True
    if me.get("base") and me["base"] == n.get("baseRefName"):
        return False
    norm = lambda x: PROMOTE_WORDS.sub("", (x or "").strip()).strip().lower()
    return bool(norm(me.get("title"))) and norm(me.get("title")) == norm(n.get("title"))


def match(t: dict, data: dict, mine: set, me: Optional[dict] = None) -> List[dict]:
    """Open PRs by another author sharing a non-common file; self, own branch and promote pairs excluded."""
    me = me or {}
    out = []
    for n in _nodes(data, "ro"):
        if n["number"] == t.get("pr") or (t.get("branch") and n.get("headRefName") == t["branch"]):
            continue
        if n.get("state", "OPEN") != "OPEN" or (me.get("author") and _login(n) == me["author"]) or promote_pair(me, n):
            continue
        shared = sorted(f for f in mine & {f.get("path") for f in ((n.get("files") or {}).get("nodes") or []) if f}
                        if not COMMON_FILES.search(f))
        if shared:
            out.append({"number": n["number"], "title": (n.get("title") or "")[:100], "state": n.get("state"),
                        "branch": n.get("headRefName"), "url": n.get("url"), "shared_files": shared[:10],
                        "shared_file_count": len(shared)})
    out.sort(key=lambda x: -x["shared_file_count"])
    return out


def function_overlap(files: List[dict], my_fns: Dict[str, set], fetch: Callable[[int], Optional[str]]) -> int:
    """Mark the top file-overlap rows that change the same enclosing function (bounded diff reads).
    Returns how many candidate diffs could not be read."""
    pool = files[:MAX_FN]
    if not (my_fns and pool):
        return 0
    with cf.ThreadPoolExecutor(max_workers=MAX_FN) as ex:
        diffs = list(ex.map(lambda x: (x, fetch(x["number"])), pool))
    for x, d in diffs:
        if d is None:
            continue
        theirs = hunk_functions(d)
        fns = sorted({fn for f in x["shared_files"] for fn in my_fns.get(f, set()) & theirs.get(f, set())})
        if fns:
            x["functions"] = fns[:5]
    return sum(d is None for _, d in diffs)


def _line(x: dict, why: str) -> str:
    return f"  #{x['number']} [{x['state']}] {x['title']} (branch {x['branch']}) - {why}"


def advice(t: dict, repo: str, fn: List[dict], file_only: int) -> str:
    what = {"branch": f"new branch `{t.get('branch')}`", "pr-create": f"PR from `{t.get('branch') or 'HEAD'}`",
            "review": f"review of #{t.get('pr')}"}[t["trigger"]]
    lines = [f"Duplicate-work check (advisory; nothing was blocked): the {what} changes a function that another "
             f"author's open PR in {repo} also changes.", "Same function changed in an open PR:"]
    lines += [_line(x, "changes " + ", ".join(f"`{f}`" for f in x["functions"][:3])) for x in fn[:SHOWN]]
    if file_only:
        lines.append(f"  ({file_only} other open PR(s) touch the same files, no other signal.)")
    lines.append(f"Read these before continuing. Off switch: env.{SWITCH_KEY}=off in sutando.config.local.json.")
    return "\n".join(lines)


def _src(repo_root) -> None:
    src = os.path.join(str(repo_root), "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def log_path(repo_root) -> Path:
    _src(repo_root)
    # The resolver can print migration notices; a hook's stderr reaches the model.
    with contextlib.redirect_stderr(io.StringIO()):
        from workspace_default import resolve_workspace
        return Path(resolve_workspace(migrate=False)) / "logs" / "dup-work.jsonl"


def enabled(repo_root) -> bool:
    _src(repo_root)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            from sutando_config import config_get
            raw = config_get(SWITCH_KEY, "on", Path(repo_root))
    except Exception:
        return True
    return str(raw).strip().lower() not in ("off", "0", "false", "no")


def _log(path: Optional[Path], row: dict) -> None:
    if path is None:
        return
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def _code_fns(fns: Dict[str, set]) -> Dict[str, set]:
    return {f: v for f, v in fns.items() if v and not COMMON_FILES.search(f)}


def _one(t: dict, run: Run) -> tuple:
    """(verdict, text, detail) for one trigger."""
    deadline = time.monotonic() + GH_TIMEOUT
    cwd = t["cwd"]
    repo = t.get("repo") or _origin_repo(run, cwd)
    if t["trigger"] == "pr-create" and not t.get("branch"):
        head = _git(run, cwd, "rev-parse", "--abbrev-ref", "HEAD")
        t["branch"] = head if head and head != "HEAD" else None
    detail = {"repo": repo, "branch": t.get("branch"), "pr": t.get("pr")}
    if not repo or "/" not in repo:
        return "UNKNOWN", "no GitHub repository (no --repo and no github.com origin remote)", detail
    if t["trigger"] == "review" and not t.get("pr"):
        return "UNKNOWN", "the reviewed PR is not named by number or URL", detail
    own = ""
    if t["trigger"] != "review":
        mine, my_fns = local_diff(run, cwd, t.get("base"), worktree=t["trigger"] == "branch")
        if mine is None:
            return "UNKNOWN", "the local diff against the base could not be read", detail
        detail["local_file_count"] = len(mine)
        if not _code_fns(my_fns):
            return "OK", "", dict(detail, reason="no changed function in a non-common file")
    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        mine_diff = (ex.submit(pr_diff, run, repo, t["pr"], deadline - time.monotonic())
                     if t["trigger"] == "review" else None)
        query, args = build_query(repo, t)
        data = search(run, query, args, deadline - time.monotonic())
        if mine_diff is not None:
            own = mine_diff.result()
            my_fns = hunk_functions(own)
    if t["trigger"] == "review":
        me = ((data.get("me") or {}).get("pullRequest") or {})
        if not me:
            raise GhError(f"PR #{t['pr']} not found in {repo}")
        mine = {f.get("path") for f in ((me.get("files") or {}).get("nodes") or []) if f}
        who = {"author": _login(me), "title": me.get("title"), "branch": me.get("headRefName"),
               "base": me.get("baseRefName")}
    else:
        who = {"author": ((data.get("viewer") or {}).get("login") or "").lower() or None, "title": t.get("title"),
               "branch": t.get("branch"), "base": t.get("base")}
    files = match(t, data, mine, who)
    unread = function_overlap(files, _code_fns(my_fns), lambda n: pr_diff(run, repo, n, deadline - time.monotonic()))
    if t["trigger"] == "review" and own is None and files:
        unread += 1
    fn = [x for x in files if x.get("functions")]
    file_only = [x["number"] for x in files if not x.get("functions")]
    undiffed = max(0, len(files) - MAX_FN) if _code_fns(my_fns) else 0
    detail.update({"function": [x["number"] for x in fn], "file_only": file_only, "unread_diffs": unread,
                   "undiffed": undiffed,
                   "local_file_count": len(mine)})
    why = ([f"{unread} diff read(s) failed or timed out"] if unread else []) + (
        [f"{undiffed} same-file open PR(s) beyond the top {MAX_FN} were not diffed"] if undiffed else [])
    gap = f"Function comparison incomplete: {'; '.join(why)}, so same-function overlap may be missed." if why else ""
    if not fn:
        return ("UNKNOWN", gap.rstrip("."), detail) if gap else ("OK", "", detail)
    return "FLAG", advice(t, repo, fn, len(file_only)) + ("\n" + gap if gap else ""), detail


def check(command: str, cwd: Optional[str], repo_root, run: Optional[Run] = None) -> str:
    """Advisory text for the model, or '' when nothing matched. Never raises, never denies."""
    ts = triggers(command, cwd)
    if not ts:
        return ""
    if not repo_root:
        return "Duplicate-work check could not run (repo root not configured); search existing PRs yourself."
    if not enabled(repo_root):
        return ""
    run = run or subprocess.run
    try:
        path = log_path(repo_root)
    except Exception:
        path = None
    texts = []
    for t in ts:
        t0 = time.monotonic()
        try:
            verdict, text, detail = _one(t, run)
        except Exception as e:  # GhError or anything else: say so, never go quiet
            reason = str(e) if isinstance(e, GhError) else f"{type(e).__name__}: {e}"
            verdict, text, detail = "UNKNOWN", reason[:200], {}
        if verdict == "UNKNOWN":
            detail["reason"] = text
            text = (f"Duplicate-work check could not run ({text}); search open and recent PRs yourself "
                    "before continuing.")
        _log(path, dict(detail, ts=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                        trigger=t["trigger"], verdict=verdict, elapsed_ms=round((time.monotonic() - t0) * 1e3, 1)))
        if text:
            texts.append(text)
    return "\n\n".join(texts)


def summarize(path: Path) -> str:
    rows = []
    with contextlib.suppress(OSError):
        for line in path.read_text().splitlines():
            with contextlib.suppress(ValueError):
                rows.append(json.loads(line))
    by_v: Dict[str, int] = {}
    by_t: Dict[str, int] = {}
    for r in rows:
        by_v[r.get("verdict", "?")] = by_v.get(r.get("verdict", "?"), 0) + 1
        by_t[r.get("trigger", "?")] = by_t.get(r.get("trigger", "?"), 0) + 1
    ms = sorted(float(r["elapsed_ms"]) for r in rows if isinstance(r.get("elapsed_ms"), (int, float)))

    def pct(p):
        return f"{ms[min(len(ms) - 1, int(round(p / 100 * (len(ms) - 1))))]:.1f}" if ms else "-"

    return "\n".join([f"dup-work: {len(rows)} checks in {path}",
                      "  verdicts " + "  ".join(f"{k} {v}" for k, v in sorted(by_v.items())),
                      "  triggers " + "  ".join(f"{k} {v}" for k, v in sorted(by_t.items())),
                      f"  elapsed_ms p50 {pct(50)}  p95 {pct(95)}"])


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--summary", action="store_true", help="counts by verdict and trigger, p50/p95 elapsed_ms")
    ap.add_argument("--log", help="log to summarise (default <workspace>/logs/dup-work.jsonl)")
    a = ap.parse_args(argv)
    if not a.summary:
        ap.error("only --summary is a CLI action; the check runs from the hook")
    root = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root
    print(summarize(Path(a.log) if a.log else log_path(root)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
