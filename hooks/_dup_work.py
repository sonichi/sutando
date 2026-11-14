#!/usr/bin/env python3
"""Duplicate-work check: before a branch, a PR or a review, name existing PRs that may cover it.

Policy for `github-evidence-guard.py --dup-check`. Triggers: `git checkout -b|-B`, `git switch
-c|-C|--create`, `git worktree add -b|-B`, `gh pr create`, `gh pr review`. Each trigger makes one
`gh api graphql` read (5 s budget) that carries every search:

- ticket ids (`rm[a-z0-9]{10,14}`, `#NNNN`) from the branch, title or body: any state, reported
  first and never truncated;
- keywords from the branch name and title: open PRs and PRs closed in the last 30 days;
- for `pr create` and `pr review`, the 25 most recently updated open PRs with their files,
  compared with the local `git diff --name-only` (create) or the reviewed PR's files (review).

It never blocks. A match is advisory context; a failed read is UNKNOWN plus one line. One row
per trigger goes to <workspace>/logs/dup-work.jsonl. `env.DUP_WORK_CHECK: off` in
sutando.config.local.json turns it off.

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
CLOSED_DAYS = 30
MAX_KW = 6
RECENT_OPEN = 25
KW_FIRST = 20
TICKET_FIRST = 100
SHOWN = 5
TICKET_RM = re.compile(r"\b(rm[a-z0-9]{10,14})\b")
TICKET_NUM = re.compile(r"#(\d{2,6})\b")
CLOSES_NUM = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d{2,6})\b", re.I)
PR_URL = re.compile(r"^https://github\.com/([^/]+/[^/]+)/pull/(\d+)(?:[/?#].*)?$")
GITHUB_REMOTE = re.compile(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")
NOISE_FILES = re.compile(r"(^|/)(CHANGELOG[^/]*|package-lock\.json|pnpm-lock\.yaml|yarn\.lock|uv\.lock|poetry\.lock)$")
STOP = set("""fix fixes feat feature chore docs doc refactor test tests ci build perf style wip tmp temp
update add adds remove change changes and the for with from into onto main master dev prod release
codex claude sutando pr branch new old v1 v2 v3 bump use via of to in on at by is a an
followup follow up hotfix patch minor misc""".split())
HUNK = re.compile(r"^@@ [^@]* @@ ?(.*)$")
DIFF_FILE = re.compile(r"^\+\+\+ b/(.*)$")
MAX_FN = 3
PR_FIELDS = "number title state headRefName closedAt url"
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
    stripped, docs = _shell_scan.heredocs(command)
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
            body = "\n".join([_flag(t, ("-b", "--body")) or ""] + [d for _, d in docs])
            body_file = _flag(t, ("-F", "--body-file"))
            if body_file and body_file != "-":
                with contextlib.suppress(OSError):
                    body += "\n" + Path(where, body_file).read_text(errors="replace")[:200_000]
            head = _flag(t, ("-H", "--head"))
            out.append({"trigger": "pr-create", "branch": head.split(":", 1)[-1] if head else None,
                        "title": _flag(t, ("-t", "--title")) or "", "body": body,
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


def keywords(*texts) -> List[str]:
    out: List[str] = []
    for t in texts:
        for tok in re.split(r"[^A-Za-z0-9]+", (t or "").lower()):
            if len(tok) < 3 or tok in STOP or tok.isdigit() or TICKET_RM.fullmatch(tok) or tok in out:
                continue
            out.append(tok)
    return out[:MAX_KW]


def tickets(branch: str = "", title: str = "", body: str = "") -> List[str]:
    """Row ids anywhere; `#NNNN` in the branch or title, or after closes/fixes/resolves in the body."""
    out: List[str] = []
    for t in (branch, title, body):
        out += [m for m in TICKET_RM.findall((t or "").lower()) if m not in out]
    nums = TICKET_NUM.findall(f"{branch or ''} {title or ''}") + CLOSES_NUM.findall(body or "")
    out += ["#" + n for n in dict.fromkeys(nums) if "#" + n not in out]
    return out


def kw_hits(kws: List[str], node: dict) -> List[str]:
    """Keywords in a PR's title or branch; a shared prefix of 4+ chars counts (confirm ~ confirmation)."""
    hay = [t for t in re.split(r"[^a-z0-9]+", f"{node.get('title') or ''} {node.get('headRefName') or ''}".lower())
           if len(t) >= 3]
    return sorted(k for k in kws if any(k == t or (min(len(k), len(t)) >= 4 and (k.startswith(t) or t.startswith(k)))
                                        for t in hay))


def build_query(repo: str, t: dict, kws: List[str], tks: List[str], since: str, with_files: bool):
    scope = f"repo:{repo} is:pr"
    q: Dict[str, str] = {}
    if tks:
        q["tk"] = f"{scope} " + " OR ".join(x.lstrip("#") for x in tks)
    if kws:
        terms = " OR ".join(kws)
        q["ko"] = f"{scope} is:open {terms}"
        q["kc"] = f"{scope} is:closed closed:>={since} {terms}"
    if with_files:
        q["ro"] = f"{scope} is:open sort:updated-desc"
    parts, args = [], []
    for alias, query in q.items():
        n = {"tk": TICKET_FIRST, "ro": RECENT_OPEN}.get(alias, KW_FIRST)
        files = " files(first:100){nodes{path}}" if alias == "ro" else ""
        parts.append(f"{alias}:search(type:ISSUE,query:${alias},first:{n})"
                     f"{{issueCount nodes{{... on PullRequest{{{PR_FIELDS}{files}}}}}}}")
        args += ["-f", f"{alias}={query}"]
    decl = [f"${a}:String!" for a in q]
    if t["trigger"] == "review":
        owner, name = repo.split("/", 1)
        decl += ["$o:String!", "$n:String!", "$pr:Int!"]
        parts.append("me:repository(owner:$o,name:$n){pullRequest(number:$pr){headRefName files(first:100){nodes{path}}}}")
        args += ["-f", f"o={owner}", "-f", f"n={name}", "-F", f"pr={t['pr']}"]
    return f"query({','.join(decl)}){{{' '.join(parts)}}}", args, q


def _git(run: Run, cwd: str, *args) -> Optional[str]:
    try:
        p = run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def _origin_repo(run: Run, cwd: str) -> Optional[str]:
    m = GITHUB_REMOTE.search(_git(run, cwd, "remote", "get-url", "origin") or "")
    return m[1] if m else None


def local_diff(run: Run, cwd: str, base: Optional[str]):
    """(changed files, {file: functions}) of HEAD against its base, from local git only."""
    base = base or (_git(run, cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD") or "origin/main")
    base = base if base.startswith("origin/") else "origin/" + base
    names = _git(run, cwd, "diff", "--name-only", f"{base}...HEAD")
    if names is None:
        return None, {}
    return set(filter(None, names.splitlines())), hunk_functions(_git(run, cwd, "diff", "-U0", f"{base}...HEAD"))


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
    """{file: {hunk-header context}}: the enclosing function git names in each `@@` line."""
    res: Dict[str, set] = {}
    cur = None
    for line in (diff_text or "").splitlines():
        m = DIFF_FILE.match(line)
        if m:
            cur = m[1]
            res.setdefault(cur, set())
            continue
        m = HUNK.match(line)
        if m and cur and m[1].strip():
            res[cur].add(m[1].strip()[:120])
    return res


def match(t: dict, data: dict, kws: List[str], mine: Optional[set], since: str) -> dict:
    """Candidate PRs by reason, self and own branch excluded; `files` holds file-overlap rows."""
    own_branch, own_pr = t.get("branch"), t.get("pr")

    def mine_too(n):
        return n["number"] == own_pr or (own_branch and n.get("headRefName") == own_branch)

    def row(n, **extra):
        return dict({"number": n["number"], "title": (n.get("title") or "")[:100], "state": n.get("state"),
                     "branch": n.get("headRefName"), "url": n.get("url")}, **extra)

    out: Dict[str, list] = {"ticket": [], "keyword": [], "files": []}
    out["ticket"] = [row(n) for n in _nodes(data, "tk") if not mine_too(n)]
    seen = {x["number"] for x in out["ticket"]}
    need = max(2, (len(kws) + 1) // 2)
    for alias in ("ko", "kc"):
        for n in _nodes(data, alias):
            if mine_too(n) or n["number"] in seen or (alias == "kc" and (n.get("closedAt") or "") < since):
                continue
            hits = kw_hits(kws, n)
            if len(hits) >= need:
                seen.add(n["number"])
                out["keyword"].append(row(n, keywords=hits))
    for n in _nodes(data, "ro") if mine else []:
        if mine_too(n):
            continue
        shared = sorted(mine & {f.get("path") for f in ((n.get("files") or {}).get("nodes") or []) if f})
        if shared and not all(NOISE_FILES.search(f) for f in shared):
            out["files"].append(row(n, shared_files=shared[:10], shared_file_count=len(shared)))
    out["keyword"].sort(key=lambda x: -len(x["keywords"]))
    out["files"].sort(key=lambda x: -x["shared_file_count"])
    out["ticket_total"] = (data.get("tk") or {}).get("issueCount")
    return out


def function_overlap(found: dict, my_fns: Dict[str, set], fetch: Callable[[int], Optional[str]]) -> None:
    """Mark the top file-overlap rows that change the same enclosing function (bounded diff reads)."""
    pool = [x for x in found["files"] if x["number"] not in {k["number"] for k in found["ticket"]}][:MAX_FN]
    if not (my_fns and pool):
        return
    with cf.ThreadPoolExecutor(max_workers=MAX_FN) as ex:
        diffs = list(ex.map(lambda x: (x, fetch(x["number"])), pool))
    for x, d in diffs:
        theirs = hunk_functions(d)
        fns = sorted({fn for f in x["shared_files"] for fn in my_fns.get(f, set()) & theirs.get(f, set())})
        if fns:
            x["functions"] = fns[:5]


def ranked(found: dict) -> dict:
    """Ticket hits, then function overlap, then keyword hits; file-only overlap is just a count."""
    tk = {x["number"] for x in found["ticket"]}
    files = {x["number"]: x for x in found["files"]}
    fn = [x for x in found["files"] if x.get("functions") and x["number"] not in tk]
    fn_n = {x["number"] for x in fn}
    kw = []
    for x in found["keyword"]:
        if x["number"] not in fn_n:
            kw.append(dict(x, shared_file_count=(files.get(x["number"]) or {}).get("shared_file_count", 0)))
    kw_n = {x["number"] for x in kw}
    file_only = [n for n in files if n not in tk | fn_n | kw_n]
    return {"ticket": found["ticket"], "function": fn, "keyword": kw, "file_only": file_only,
            "ticket_total": found.get("ticket_total")}


def _line(x: dict, why: str) -> str:
    return f"  #{x['number']} [{x['state']}] {x['title']} (branch {x['branch']}) - {why}"


def advice(t: dict, repo: str, r: dict, tk_query: str) -> str:
    what = {"branch": f"new branch `{t.get('branch')}`", "pr-create": f"PR from `{t.get('branch') or 'HEAD'}`",
            "review": f"review of #{t.get('pr')}"}[t["trigger"]]
    lines = [f"Duplicate-work check (advisory; nothing was blocked): the {what} may repeat existing work in {repo}."]
    if r["ticket"]:
        lines.append("Ticket-id hits (any state; every hit is listed):")
        lines += [_line(x, "mentions the same ticket id") for x in r["ticket"]]
        if (r.get("ticket_total") or 0) > len(r["ticket"]) + 1:
            lines.append(f"  ... {r['ticket_total']} PRs in total: gh search prs '{tk_query}'")
    if r["function"]:
        lines.append("Same function changed in an open PR:")
        lines += [_line(x, "changes " + ", ".join(f"`{f}`" for f in x["functions"][:3])) for x in r["function"][:SHOWN]]
    if r["keyword"]:
        lines.append("Keyword overlap (open, or closed in the last 30 days):")
        lines += [_line(x, "keywords " + ", ".join(x["keywords"])
                        + (f"; also {x['shared_file_count']} shared file(s)" if x["shared_file_count"] else ""))
                  for x in r["keyword"][:SHOWN]]
    if r["file_only"]:
        lines.append(f"  ({len(r['file_only'])} other open PR(s) touch the same files, no other signal.)")
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


def _one(t: dict, run: Run) -> tuple:
    """(verdict, text, detail) for one trigger."""
    deadline = time.monotonic() + GH_TIMEOUT
    cwd = t["cwd"]
    repo = t.get("repo") or _origin_repo(run, cwd)
    if t["trigger"] == "pr-create" and not t.get("branch"):
        head = _git(run, cwd, "rev-parse", "--abbrev-ref", "HEAD")
        t["branch"] = head if head and head != "HEAD" else None
    kws = keywords(t.get("branch"), t.get("title")) if t["trigger"] != "review" else []
    tks = tickets(t.get("branch") or "", t.get("title") or "", t.get("body") or "")
    weak = ("No ticket id in the branch name, so the duplicate check is weaker."
            if t["trigger"] != "review" and not tickets(t.get("branch") or "") else "")
    detail = {"repo": repo, "branch": t.get("branch"), "pr": t.get("pr"), "keywords": kws, "tickets": tks}
    if not repo or "/" not in repo:
        return "UNKNOWN", "no GitHub repository (no --repo and no github.com origin remote)", detail
    if t["trigger"] == "review" and not t.get("pr"):
        return "UNKNOWN", "the reviewed PR is not named by number or URL", detail
    mine, my_fns = local_diff(run, cwd, t.get("base")) if t["trigger"] == "pr-create" else (None, {})
    with_files = t["trigger"] == "review" or bool(mine)
    if not (kws or tks or with_files):
        return "OK", weak, dict(detail, reason="nothing to search")
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=CLOSED_DAYS)).date().isoformat()
    query, args, q = build_query(repo, t, kws, tks, since, with_files)
    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        mine_diff = (ex.submit(pr_diff, run, repo, t["pr"], deadline - time.monotonic())
                     if t["trigger"] == "review" else None)
        data = search(run, query, args, deadline - time.monotonic())
        if mine_diff is not None:
            my_fns = hunk_functions(mine_diff.result())
    if t["trigger"] == "review":
        me = ((data.get("me") or {}).get("pullRequest") or {})
        if not me:
            raise GhError(f"PR #{t['pr']} not found in {repo}")
        mine = {f.get("path") for f in ((me.get("files") or {}).get("nodes") or []) if f}
    found = match(t, data, kws, mine, since)
    function_overlap(found, my_fns, lambda n: pr_diff(run, repo, n, deadline - time.monotonic()))
    r = ranked(found)
    detail.update({k: [x["number"] for x in r[k]] for k in ("ticket", "function", "keyword")})
    detail.update({"file_only": r["file_only"], "local_file_count": None if mine is None else len(mine)})
    if not (r["ticket"] or r["function"] or r["keyword"]):
        return "OK", weak, detail
    return "FLAG", advice(t, repo, r, q.get("tk", "")) + ("\n" + weak if weak else ""), detail


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
