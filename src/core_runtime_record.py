"""Atomic publication of a confirmed core launch observation shared by CLI adapters.

A record is published only when the named session's panes run the CLI it names,
so a launcher can never stamp its runtime over a core of the other runtime.
"""
import argparse
import json
import math
import subprocess
from pathlib import Path

from result_publish import publish_text

CLI_NAMES = {"claude": ("claude", "claude.exe"), "codex": ("codex",)}


def publish(workspace, runtime, session, started_at):
    if runtime not in ("claude", "codex"):
        raise ValueError("unsupported core runtime")
    if not isinstance(session, str) or not 1 <= len(session) <= 200 or any(
        ord(c) < 32 or ord(c) == 127 for c in session
    ):
        raise ValueError("invalid core session")
    if not math.isfinite(started_at) or started_at <= 0:
        raise ValueError("invalid session start time")
    record = {"runtime": runtime, "session": session, "started_at": started_at}
    return publish_text(Path(workspace) / "state/core-runtime.json",
                        json.dumps(record) + "\n", mode=0o600)


def argv_runtime(args):
    """The runtime whose CLI this `ps` command line executes, directly or via an interpreter."""
    # ps joins argv with spaces and paths may contain them, so try each space-ended prefix;
    # the program part ends at the first flag, which keeps a tmux client's argv from matching.
    for end in [i for i, ch in enumerate(args) if ch == " "] + [len(args)]:
        head = args[:end]
        if " -" in head:
            break
        base = head.rsplit("/", 1)[-1]
        for runtime, names in CLI_NAMES.items():
            if base in names:
                return runtime
    return None


def _process_table():
    rows = subprocess.run(["ps", "-axo", "pid=,ppid=,args="], capture_output=True,
                          text=True, timeout=10, check=True).stdout
    children, argv = {}, {}
    for row in rows.splitlines():
        parts = row.strip().split(None, 2)
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        argv[pid] = parts[2] if len(parts) > 2 else ""
        children.setdefault(ppid, []).append(pid)
    return children, argv


def pane_runtimes(socket, session, tmux="tmux"):
    """Runtimes of the CLIs nearest each pane of <session>; a core's own subprocesses do not count."""
    listed = subprocess.run([tmux, "-S", socket, "list-panes", "-s", "-t", f"={session}",
                             "-F", "#{pane_pid}"], capture_output=True, text=True, timeout=10)
    if listed.returncode != 0:
        return set()
    children, argv = _process_table()
    found = set()
    for pane in listed.stdout.split():
        if not pane.isdigit():
            continue
        level, seen = [int(pane)], set()
        while level:
            hits = {argv_runtime(argv.get(pid, "")) for pid in level} - {None}
            if hits:
                found |= hits
                break
            seen.update(level)
            level = [c for pid in level for c in children.get(pid, ()) if c not in seen]
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("runtime", choices=("claude", "codex"))
    parser.add_argument("session")
    parser.add_argument("started_at", type=float)
    parser.add_argument("--socket", required=True, help="tmux socket of the session")
    args = parser.parse_args()
    try:
        running = pane_runtimes(str(args.socket), args.session)
    except (OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"core runtime record: cannot inspect {args.session}: {exc}\n")
    if running != {args.runtime}:
        seen = ", ".join(sorted(running)) or "no core CLI"
        parser.exit(3, f"core runtime record: {args.session} runs {seen}, not {args.runtime}; "
                       "record unchanged\n")
    try:
        publish(args.workspace, args.runtime, args.session, args.started_at)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"core runtime record: {exc}\n")


if __name__ == "__main__":
    main()
