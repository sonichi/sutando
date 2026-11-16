#!/usr/bin/env python3
"""Hand a pr-watch event to the worker that drives the PR (a PR_WATCH_ROUTE_CMD).

The driver is the worker the PR's latest `Sutando-Task:` trailer's task was assigned
to (`state/attribution/<task id>`). Prints `handed <worker id>` after a `pool_ask` to
it, or `local` when the watching session should keep the event: no trailer, no
attribution, the driver is this session, or the ask failed.

    pool_pr_route.py --event "PR_EVENT o/r#N ..." --repo o/r --number N --head SHA \\
        --cache-dir DIR [--workspace WS]

One commits call per (repo#N, head), cached in --cache-dir; a failed call is not cached.
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
TRAILER_RE = re.compile(r"^Sutando-Task:[ \t]*(\S+)[ \t]*$", re.MULTILINE)
EVENT_RE = re.compile(r"^PR_EVENT [A-Za-z0-9_.\-\[\]/#=? ]+$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
STANDING = ("This is a PR you drive (its latest commit carries your task's Sutando-Task trailer). "
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


def latest_task(messages) -> Optional[str]:
    """The task id of the newest message carrying a valid `Sutando-Task:` trailer."""
    for msg in reversed(list(messages)):
        ids = [t for t in TRAILER_RE.findall(msg or "") if is_task_id(t)]
        if ids:
            return ids[-1]
    return None


def fetch_task(repo: str, number: int, run=gh) -> Optional[str]:
    """The driving task id from the PR's commits; LookupError when GitHub could not be asked."""
    try:
        r = run(["api", f"repos/{repo}/pulls/{number}/commits?per_page=100", "--paginate",
                 "--jq", ".[] | .commit.message | @json"], GH_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        raise LookupError("commits call did not finish")
    if r.returncode != 0:
        raise LookupError(f"commits call exited {r.returncode}")
    try:
        messages = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
    except ValueError:
        raise LookupError("commits call returned unparsable output")
    return latest_task(m for m in messages if isinstance(m, str))


def driver_task(repo: str, number: int, head: str, cache_dir: Path, run=gh) -> Optional[str]:
    """The PR's driving task id, asking GitHub at most once per (repo#N, head)."""
    key = f"{repo.lower()}#{number}"
    if not head:
        return fetch_task(repo, number, run)
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
        if isinstance(hit, dict) and hit.get("head") == head:
            return hit.get("task") or None
        task = fetch_task(repo, number, run)
        data.pop(key, None)
        data[key] = {"head": head, "task": task or ""}
        while len(data) > CACHE_MAX:
            data.pop(next(iter(data)))
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1) + "\n")
        os.replace(tmp, path)
        return task


def ask_text(event: str) -> str:
    return f"[pr-watch] {event}\n{STANDING}"


def route(workspace: Path, event: str, repo: str, number: int, head: str, cache_dir: Path, *,
          run=gh, ask: Optional[Callable] = None, me: Optional[str] = None,
          worker_for_task: Optional[Callable] = None) -> str:
    """`handed <worker id>` or `local`; the reason for `local` goes to stderr."""
    if not EVENT_RE.match(event) or len(event) > EVENT_MAX or not REPO_RE.match(repo) or number <= 0:
        return _local("not a pr-watch event line")
    try:
        task = driver_task(repo, number, head, cache_dir, run)
    except LookupError as exc:
        return _local(str(exc))
    if not task:
        return _local("no Sutando-Task trailer on the PR")
    wid = (worker_for_task or _sibling("pool_attribution").worker_for_task)(workspace, task)
    if not wid:
        return _local(f"{task} has no worker attribution")
    pa = _sibling("pool_ask") if ask is None or me is None else None
    if wid == (me or pa.whoami()):
        return _local("the driver is this session")
    try:
        (ask or pa.ask)(workspace, wid, ask_text(event))
    except Exception as exc:  # noqa: BLE001 - any failure keeps the event with the watcher
        return _local(f"pool_ask failed: {str(exc)[:200]}")
    return f"handed {wid}"


def _local(why: str) -> str:
    print(f"pool_pr_route: local: {why}", file=sys.stderr)
    return "local"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--event", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--number", type=int, required=True)
    p.add_argument("--head", default="")
    p.add_argument("--cache-dir", required=True)
    p.add_argument("--workspace")
    a = p.parse_args(argv)
    if a.workspace:
        ws = Path(a.workspace)
    else:
        from workspace_default import resolve_workspace
        ws = Path(resolve_workspace())
    print(route(ws, a.event, a.repo, a.number, a.head, Path(a.cache_dir)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
