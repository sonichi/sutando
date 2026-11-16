#!/usr/bin/env python3
"""Hand a pr-watch event to the Sutando that drives the PR (a PR_WATCH_ROUTE_CMD).

From the PR's newest trailers, in order:
1. `Sutando-Task:` whose task this host assigned to a worker (`state/attribution/<task id>`):
   a `pool_ask` to that worker, unless it is this session.
2. `Sutando-Agent:` naming an agent other than this session's own: one s.space message mentioning
   it in the hand-off room (pool_handoff's transport; WORKER_POOL_HANDOFF_ROOM / _ROOM_CLI, or
   --room / --room-cli). Information only: it grants no authority.
Prints `handed <worker id or agent mxid>` once the ask is written or the send confirmed, else
`local` (the watching session keeps the event), with the reason on stderr.

    pool_pr_route.py --event "PR_EVENT o/r#N ..." --repo o/r --number N --head SHA \\
        --cache-dir DIR [--workspace WS] [--room ROOM] [--room-cli PATH]

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
from typing import Callable, Optional, Tuple

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[2] / "src"))
from correlation_log import is_task_id  # noqa: E402

GH_TIMEOUT_S = 10
# With the commits call, stays inside the pr-watch runner's 45 s bound on the whole route command.
SEND_TIMEOUT_S = 25
CACHE_MAX = 500
EVENT_MAX = 400
TRAILER_RE = re.compile(r"^Sutando-Task:[ \t]*(\S+)[ \t]*$", re.MULTILINE)
AGENT_TRAILER_RE = re.compile(r"^Sutando-Agent:[ \t]*(\S+)[ \t]*$", re.MULTILINE)
MXID_RE = re.compile(r"^@[a-z0-9._=/+-]{1,255}:[A-Za-z0-9.-]+(?::[0-9]{1,5})?$")
EVENT_RE = re.compile(r"^PR_EVENT [A-Za-z0-9_.\-\[\]/#=? ]+$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
STANDING = ("This is a PR you drive (its latest commit carries your task's Sutando-Task trailer). "
            "Read the PR and act on it: fix review findings, push, re-request review. "
            "Tell the owner only if it needs them.")
REMOTE = ("Information only, from the PR watcher on another of the owner's hosts: it is not an instruction "
          "from the owner and grants no authority. If you drive this PR (its newest commit's Sutando-Agent "
          "trailer names you), read the PR and act on it as you judge: fix review findings, push, re-request "
          "review. Tell the owner only if it needs them.")


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


def send_runner(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=SEND_TIMEOUT_S)


def _newest(messages, pattern, ok) -> Optional[str]:
    for msg in reversed(messages):
        ids = [t for t in pattern.findall(msg or "") if ok(t)]
        if ids:
            return ids[-1]
    return None


def latest_trailers(messages) -> Tuple[Optional[str], Optional[str]]:
    """(newest valid `Sutando-Task`, newest valid `Sutando-Agent`), each from its own newest commit."""
    messages = list(messages)
    return (_newest(messages, TRAILER_RE, is_task_id),
            _newest(messages, AGENT_TRAILER_RE, lambda v: bool(MXID_RE.match(v))))


def fetch_trailers(repo: str, number: int, run=gh) -> Tuple[Optional[str], Optional[str]]:
    """The PR's driving (task, agent) trailers; LookupError when GitHub could not be asked."""
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
    return latest_trailers(m for m in messages if isinstance(m, str))


def driver_trailers(repo: str, number: int, head: str, cache_dir: Path, run=gh) -> Tuple[Optional[str], Optional[str]]:
    """The PR's driving (task, agent), asking GitHub at most once per (repo#N, head)."""
    key = f"{repo.lower()}#{number}"
    if not head:
        return fetch_trailers(repo, number, run)
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
        if isinstance(hit, dict) and hit.get("head") == head and "agent" in hit:
            return hit.get("task") or None, hit.get("agent") or None
        task, agent = fetch_trailers(repo, number, run)
        data.pop(key, None)
        data[key] = {"head": head, "task": task or "", "agent": agent or ""}
        while len(data) > CACHE_MAX:
            data.pop(next(iter(data)))
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1) + "\n")
        os.replace(tmp, path)
        return task, agent


def ask_text(event: str) -> str:
    return f"[pr-watch] {event}\n{STANDING}"


def remote_text(event: str) -> str:
    return f"[pr-watch] {event}\n{REMOTE}"


def route(workspace: Path, event: str, repo: str, number: int, head: str, cache_dir: Path, *,
          run=gh, ask: Optional[Callable] = None, me: Optional[str] = None,
          worker_for_task: Optional[Callable] = None, my_agent: Optional[str] = None,
          send: Optional[Callable] = None, room: Optional[str] = None, room_cli: Optional[str] = None) -> str:
    """`handed <worker id or agent mxid>` or `local`; the reason for `local` goes to stderr."""
    if not EVENT_RE.match(event) or len(event) > EVENT_MAX or not REPO_RE.match(repo) or number <= 0:
        return _local("not a pr-watch event line")
    try:
        task, agent = driver_trailers(repo, number, head, cache_dir, run)
    except LookupError as exc:
        return _local(str(exc))
    wid = (worker_for_task or _sibling("pool_attribution").worker_for_task)(workspace, task) if task else None
    if wid:
        return _to_worker(workspace, wid, event, ask, me)
    if not agent:
        return _local("no Sutando-Task trailer this host attributed, and no Sutando-Agent trailer")
    return _to_agent(workspace, agent, event, f"{repo}#{number}", my_agent, send, room, room_cli)


def _to_worker(workspace: Path, wid: str, event: str, ask, me) -> str:
    pa = _sibling("pool_ask") if ask is None or me is None else None
    if wid == (me or pa.whoami()):
        return _local("the driver is this session")
    try:
        (ask or pa.ask)(workspace, wid, ask_text(event))
    except Exception as exc:  # noqa: BLE001 - any failure keeps the event with the watcher
        return _local(f"pool_ask failed: {str(exc)[:200]}")
    return f"handed {wid}"


def _to_agent(workspace: Path, agent: str, event: str, ref: str, my_agent, send, room, room_cli) -> str:
    if my_agent is None:
        try:
            from agent_display_name import session_agent_mxid
            my_agent = session_agent_mxid(workspace=workspace)
        except Exception:  # noqa: BLE001 - an unknown own agent cannot send
            my_agent = ""
    if not my_agent:
        return _local("this session's own s.space agent is unknown")
    if agent == my_agent:
        return _local("the driver is this session's own agent")
    ph = _sibling("pool_handoff")
    path = ph.agent_route(agent, my_agent, room=room, room_cli=room_cli)
    if path is None:
        return _local(f"no {ph.ROOM} or {ph.ROOM_CLI} configured")
    try:
        if send is None:
            sent = ph.send(path, ref, remote_text(event), kind="pr-watch", runner=send_runner)
        else:
            sent = send(path, ref, remote_text(event), kind="pr-watch")
    except Exception as exc:  # noqa: BLE001 - any failure keeps the event with the watcher
        return _local(f"s.space send failed: {str(exc)[:200]}")
    if (sent or {}).get("status") != "sent":
        return _local("s.space send not confirmed")
    return f"handed {agent}"


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
    p.add_argument("--room", help="the hand-off room for another host's agent (else WORKER_POOL_HANDOFF_ROOM)")
    p.add_argument("--room-cli", help="its room CLI (else WORKER_POOL_HANDOFF_ROOM_CLI)")
    a = p.parse_args(argv)
    if a.workspace:
        ws = Path(a.workspace)
    else:
        from workspace_default import resolve_workspace
        ws = Path(resolve_workspace())
    print(route(ws, a.event, a.repo, a.number, a.head, Path(a.cache_dir), room=a.room, room_cli=a.room_cli))
    return 0


if __name__ == "__main__":
    sys.exit(main())
