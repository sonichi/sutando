#!/usr/bin/env python3
"""Which inbox a Claude session owes a task watcher, and the Monitor command that re-arms it.

One owner for the two hooks that ask: the Stop hook (src/check-pending-tasks.sh)
blocks a turn end on an unwatched inbox, and the SessionStart hook
(src/watcher-rearm-session-hint.sh) tells a session resumed from compaction to
re-arm before anything else. Both must name the same inbox and command.

    watcher_rearm.py target --repo R --workspace W         # inbox line, then command line
    watcher_rearm.py session-start --repo R --workspace W  # hook JSON, or nothing
    watcher_rearm.py watch-argv --repo R --workspace W     # the same command, one argument per line
    watcher_rearm.py mod-ready --repo R --workspace W      # yes: skip the Monitor; no: start it

watch-argv serves the task-watch Mod, so it watches the inbox the Stop hook checks; mod-ready
answers yes only when the Mod is installed AND a ready session watcher holds that inbox.

A pool worker is the session carrying SUTANDO_INSTANCE_ID; anything else is the core.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

WORKER_REARM = 'bash "$SUTANDO_WATCHER_CMD" "$SUTANDO_TASKS_DIR" --role session --inbox "$SUTANDO_TASKS_DIR"'


def target(repo: str, workspace: str, instance_id: Optional[str]) -> Tuple[str, str]:
    if instance_id:
        return f"{workspace}/deliveries/{instance_id}", WORKER_REARM
    tasks = f"{workspace}/tasks"
    # Absolute: the core may run from a foreign cwd (SUTANDO_CLAUDE_WORKING_DIR).
    return tasks, f'bash "{repo}/src/watch-tasks-stream.sh" --role session --inbox "{tasks}"'


def watch_argv(repo: str, workspace: str, instance_id: Optional[str], env=None) -> list:
    env = os.environ if env is None else env
    inbox, _ = target(repo, workspace, instance_id)
    if instance_id:
        cmd = env.get("SUTANDO_WATCHER_CMD") or ""
        if not cmd:
            raise ValueError("a worker's watcher command comes from SUTANDO_WATCHER_CMD")
    else:
        cmd = f"{repo}/src/watch-tasks-stream.sh"
    return ["bash", cmd, inbox, "--role", "session", "--inbox", inbox]


def session_verdict(inbox: str, state_dir: str) -> str:
    """`yes`, `no` or `unknown` from watcher_identity's role-present; only `no` is evidence."""
    script = Path(__file__).resolve().parent / "watcher_identity.py"
    try:
        out = subprocess.run(
            [sys.executable, str(script), "role-present", "session", "--inbox", inbox, "--ready", state_dir],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:  # noqa: BLE001 -- an unobservable host is not an unwatched inbox
        return "unknown"
    lines = (out.stdout or "").split()
    return lines[0] if lines and lines[0] in ("yes", "no") else "unknown"


def session_start_context(repo: str, workspace: str, instance_id: Optional[str]) -> Optional[dict]:
    inbox, command = target(repo, workspace, instance_id)
    if session_verdict(inbox, f"{workspace}/state") != "no":
        return None
    text = (
        f"SUTANDO WATCHER: no ready session-role task watcher holds {inbox}, so tasks arriving "
        "now are announced to no one. A Monitor ends at its timeout and does not survive a "
        "resume, and an expiry notice that arrived during compaction may be lost to the summary. "
        "Re-arm it NOW, before any other work: via the Monitor tool, "
        f'command: {command}, timeout_ms: 1800000, description: "Streaming task watcher". '
        "Then read any task files already waiting in that inbox."
    )
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}


def mod_ready(repo: str, workspace: str, instance_id: Optional[str], env=None,
              wait_s: float = 30.0, sleep=time.sleep) -> str:
    env = os.environ if env is None else env
    if env.get("SUTANDO_TASK_WATCH_MOD") != "1":
        return "no"
    inbox, _ = target(repo, workspace, instance_id)
    # A refused, unloaded or crashed Mod never makes this yes, so the Monitor is skipped only on proof.
    for _ in range(max(1, int(wait_s))):
        if session_verdict(inbox, f"{workspace}/state") == "yes":
            return "yes"
        sleep(1)
    return "no"


def _opts(rest):
    opts = {}
    it = iter(rest)
    for key in it:
        if key not in ("--repo", "--workspace"):
            raise ValueError(key)
        opts[key[2:]] = next(it)
    if set(opts) != {"repo", "workspace"}:
        raise ValueError("missing --repo/--workspace")
    return opts


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        cmd = args[0]
        opts = _opts(args[1:])
        if cmd not in ("target", "session-start", "watch-argv", "mod-ready"):
            raise ValueError(cmd)
    except (IndexError, ValueError, StopIteration):
        print("usage: watcher_rearm.py target|session-start|watch-argv|mod-ready --repo R --workspace W", file=sys.stderr)
        return 64
    instance_id = os.environ.get("SUTANDO_INSTANCE_ID") or None
    if cmd == "target":
        print("\n".join(target(opts["repo"], opts["workspace"], instance_id)))
        return 0
    if cmd == "watch-argv":
        try:
            print("\n".join(watch_argv(opts["repo"], opts["workspace"], instance_id)))
        except ValueError as err:
            print(f"watcher_rearm.py: {err}", file=sys.stderr)
            return 1
        return 0
    if cmd == "mod-ready":
        print(mod_ready(opts["repo"], opts["workspace"], instance_id))
        return 0
    payload = session_start_context(opts["repo"], opts["workspace"], instance_id)
    if payload is not None:
        sys.stdout.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
