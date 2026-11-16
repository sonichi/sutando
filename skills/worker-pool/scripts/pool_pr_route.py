#!/usr/bin/env python3
"""Hand a pr-watch event to the worker that drives the PR (a PR_WATCH_ROUTE_CMD).

Only an event about one of our own PRs is handed off: an author/head event (not a review request,
a dismissal or a `reason=reviewed` update), on a PR whose author is one of the watch's `--authors`
and whose head branch lives in the base repository. Its driver is the worker this host assigned the
task named by the newest commit's `Sutando-Task:` trailer (`state/attribution/<task id>`). A live
worker other than this session gets a `pool_ask`; `handed <worker id>` is printed only when that
ask was delivered (a file sentinel, or a confirmed room post). Anything else prints `local`, with
the reason on stderr, and the watching session keeps the event.

    pool_pr_route.py --event "PR_EVENT o/r#N ..." --repo o/r --number N --head SHA \\
        --authors a,b --cache-dir DIR [--workspace WS]

One GitHub call per (repo#N, head), cached in --cache-dir; a failed call is not cached.
"""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[2] / "src"))
from correlation_log import is_task_id  # noqa: E402

GH_TIMEOUT_S = 10
CACHE_MAX = 500
EVENT_MAX = 400
TRAILER_RE = re.compile(r"^Sutando-Task:[ \t]*(\S+)[ \t]*$")
EVENT_RE = re.compile(r"^PR_EVENT [A-Za-z0-9_.\-\[\]/#=? ]+$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# Events about someone else's PR: ours to review, never ours to drive.
NOT_OURS = ("review_requested", "review_dismissed")
QUERY = ("query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
         "pullRequest(number:$number){author{login} headRepository{nameWithOwner} "
         "commits(last:100){nodes{commit{message}}}}}}")
STANDING = ("This is a PR you drive (its newest commit carries your task's Sutando-Task trailer). "
            "Read the PR and act on it: fix review findings, push, re-request review. "
            "Tell the owner only if it needs them.")


def _sibling(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def gh(args, timeout):
    return subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=timeout)


def trailer_task(message: str) -> Optional[str]:
    """The valid `Sutando-Task:` id in the message's trailer block (its last paragraph), or None."""
    paragraphs = [p for p in re.split(r"\n[ \t]*\n", (message or "").strip()) if p.strip()]
    lines = paragraphs[-1].splitlines() if len(paragraphs) > 1 else []
    ids = [m.group(1) for m in (TRAILER_RE.match(line.strip()) for line in lines) if m]
    return ids[-1] if ids and is_task_id(ids[-1]) else None


def latest_task(messages) -> Optional[str]:
    """The task id of the newest commit whose trailer block carries a valid `Sutando-Task:`."""
    for msg in reversed(list(messages)):
        task = trailer_task(msg)
        if task:
            return task
    return None


def fetch_pr(repo: str, number: int, run=gh) -> dict:
    """{task, author, head_repo} for the PR; LookupError when GitHub could not be asked."""
    owner, name = repo.split("/", 1)
    try:
        r = run(["api", "graphql", "-f", f"query={QUERY}", "-F", f"owner={owner}", "-F", f"name={name}",
                 "-F", f"number={number}"], GH_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        raise LookupError("GitHub call did not finish")
    if r.returncode != 0:
        raise LookupError(f"GitHub call exited {r.returncode}")
    try:
        pr = json.loads(r.stdout)["data"]["repository"]["pullRequest"]
        nodes = pr["commits"]["nodes"]
    except (ValueError, KeyError, TypeError):
        raise LookupError("GitHub call returned an unexpected answer")
    messages = [((n or {}).get("commit") or {}).get("message") for n in nodes or []]
    return {"task": latest_task(m for m in messages if isinstance(m, str)) or "",
            "author": str(((pr.get("author") or {}).get("login")) or ""),
            "head_repo": str(((pr.get("headRepository") or {}).get("nameWithOwner")) or "")}


def pr_facts(repo: str, number: int, head: str, cache_dir: Path, run=gh) -> dict:
    """fetch_pr's answer, asking GitHub at most once per (repo#N, head)."""
    key = f"{repo.lower()}#{number}"
    if not head:
        return fetch_pr(repo, number, run)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / "pr-driver.json"
    # Held across the fetch so two events at one head make one call.
    with open(cache_dir / "pr-driver.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text())
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        hit = data.get(key)
        if isinstance(hit, dict) and hit.get("head") == head and "head_repo" in hit:
            return hit
        facts = dict(fetch_pr(repo, number, run), head=head)
        data.pop(key, None)
        data[key] = facts
        while len(data) > CACHE_MAX:
            data.pop(next(iter(data)))
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1) + "\n")
        os.replace(tmp, path)
        return facts


def ask_text(event: str) -> str:
    return f"[pr-watch] {event}\n{STANDING}"


def delivered(out, wid: str) -> bool:
    """Whether pool_ask's result shows the ask reached `wid`: a confirmed room post, or a sentinel
    in its folder (this call's or the watcher pass that routed it first)."""
    if not isinstance(out, dict):
        return False
    if out.get("via"):
        return out.get("status") == "sent" and bool(out.get("event_id"))
    r = out.get("route") or {}
    return wid in (r.get("delivered") or []) or wid in (r.get("already") or [])


def route(workspace: Path, event: str, repo: str, number: int, head: str, authors, cache_dir: Path, *,
          run=gh, ask: Optional[Callable] = None, me: Optional[str] = None,
          worker_for_task: Optional[Callable] = None, alive: Optional[Callable] = None) -> str:
    """`handed <worker id>` or `local`; the reason for `local` goes to stderr."""
    if not EVENT_RE.match(event) or len(event) > EVENT_MAX or not REPO_RE.match(repo) or number <= 0:
        return _local("not a pr-watch event line")
    words = event.split()
    if len(words) < 3 or words[2] in NOT_OURS or "reason=reviewed" in words:
        return _local("not an event about our own PR")
    ours = {a.lower() for a in authors if a and a != "*"}
    if not ours:
        return _local("no watched author to check the PR's author against")
    try:
        facts = pr_facts(repo, number, head, cache_dir, run)
    except LookupError as exc:
        return _local(str(exc))
    if facts.get("author", "").lower() not in ours or facts.get("head_repo", "").lower() != repo.lower():
        return _local("the PR is not one of ours (author or head repository)")
    task = facts.get("task")
    if not task:
        return _local("no Sutando-Task trailer on the PR's newest commits")
    wid = (worker_for_task or _sibling("pool_attribution").worker_for_task)(workspace, task)
    if not wid:
        return _local(f"{task} has no worker attribution on this host")
    pa = _sibling("pool_ask") if ask is None or me is None or alive is None else None
    if wid == (me or pa.whoami()):
        return _local("the driver is this session")
    if not (alive or _alive)(workspace, wid):
        return _local("the driving worker's session is not alive")
    try:
        out = (ask or pa.ask)(workspace, wid, ask_text(event))
    except Exception as exc:  # noqa: BLE001 - any failure keeps the event with the watcher
        return _local(f"pool_ask failed: {str(exc)[:200]}")
    if not delivered(out, wid):
        # An unconfirmed room post may still have landed; it is not sent again.
        return _local("the ask's delivery was not confirmed")
    return f"handed {wid}"


def _alive(workspace: Path, wid: str) -> bool:
    return any(r["id"] == wid and r.get("alive") is True for r in _sibling("pool_ask").who(workspace))


def _local(why: str) -> str:
    print(f"pool_pr_route: local: {why}", file=sys.stderr)
    return "local"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--event", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--number", type=int, required=True)
    p.add_argument("--head", default="")
    p.add_argument("--authors", default="", help="the watch's authors, comma-separated")
    p.add_argument("--cache-dir", required=True)
    p.add_argument("--workspace")
    a = p.parse_args(argv)
    if a.workspace:
        ws = Path(a.workspace)
    else:
        from workspace_default import resolve_workspace
        ws = Path(resolve_workspace())
    authors = [x.strip() for x in a.authors.split(",") if x.strip()]
    print(route(ws, a.event, a.repo, a.number, a.head, authors, Path(a.cache_dir)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
