#!/usr/bin/env python3
"""Per-host heartbeat for sutando-core sessions.

Writes a small JSON file at `<workspace>/state/cores/<hostname>.alive` every
30 seconds while the core is up. `pid` is the CORE's pid (resolved from the
tmux pane on the recorded socket); `heartbeat_pid` is this writer's own. The
file's mtime is the cross-host "is this core still up?" signal.

Until 2026-08-01 `pid` was `os.getpid()` — the *writer's* pid — while this
docstring already claimed it was the core's. The writer is started detached by
startup.sh (PPID 1), is never killed by restart.sh, and is only started `if !
pgrep`, so it outlived every core restart: the file kept a fresh mtime with a
pid that was never the core's, and a DEAD core read as healthy. Measured on two
hosts. The beat is now gated on the core actually existing.

Why
---
Today's "is the core alive?" check reads `core-status.json` at the workspace
root — a single file written by the proactive-loop each pass. That's fine
for a single-machine install: one core, one status. The moment we want
multi-core (multiple Claude Code sessions sharing a workspace, or sutando
running on both Mac Studio + MacBook against a synced workspace), one file
can no longer represent N processes.

Per-host file at `state/cores/<hostname>.alive`:
  • Each running core writes only its own file (no contention).
  • Any process can read the directory to see who's alive across the fleet.
  • mtime is the authoritative liveness signal (younger than ~90s = alive).
  • Future lease-based scheduler consumes this to know who can pick up work.

This script is intentionally tiny and standalone — startup.sh launches it as
a background process. SIGTERM/SIGINT clean up the .alive file so a graceful
shutdown is visible immediately (vs. waiting for mtime-staleness timeout).

Usage:
  python3 src/core_heartbeat.py                  # default 30s interval
  python3 src/core_heartbeat.py --interval 10    # for tests
  python3 src/core_heartbeat.py --status busy    # set the status string

Runs forever until killed. Exit codes:
  0 — clean shutdown (SIGTERM/SIGINT received)
  Other — fatal write error (unrecoverable; supervisor should restart)
"""
from __future__ import annotations
import argparse
import json
import os
import signal
import subprocess
import socket
import sys
import time
from pathlib import Path

# Resolve workspace via the M0 helper (PR #1395 / v0.8 #1440) — the previous
# inlined env-or-legacy-default resolution wrote .alive files where no
# post-M0 reader looks (health-check + dashboard read resolve_workspace()/
# state/cores/), so every core reported dead. workspace_default is a sibling
# module (stdlib-only deps), so the old "dep-free" rationale no longer buys
# anything. Fail loud on import error: a heartbeat written to the wrong tree
# is worse than no heartbeat (supervisor restarts on crash; see module header).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_default import resolve_workspace  # noqa: E402

WORKSPACE = resolve_workspace()

CORES_DIR = WORKSPACE / "state" / "cores"


def _hostname() -> str:
    """Per-host label for the `.alive` filename. Delegates to
    `util_paths._host_label()` — the single source of truth (honors
    `$SUTANDO_HOST_LABEL`, else short hostname) — so the heartbeat label stays
    in lockstep with the `hosts/<host>/` per-host dir and survives DHCP
    hostname drift (a node whose `hostname` is a DHCP/Comcast name that flaps
    would otherwise write two divergent `<label>.alive` files). Falls back to
    the raw short hostname if util_paths is unavailable."""
    try:
        from util_paths import _host_label
        return _host_label()
    except Exception:
        return socket.gethostname().split(".")[0]


def _alive_path() -> Path:
    return CORES_DIR / f"{_hostname()}.alive"


def _locality() -> dict[str, str]:
    """The core's locality — self-reported (Track 10, owner 2026-07-10).

    `kind`: ``local`` when this core runs on one of the owner's own machines
    (a normal ``startup.sh`` launch), ``cloud`` when spawned by the hosted
    spawn-user-core template. The template sets ``$SUTANDO_CORE_LOCALITY=cloud``;
    an absent or unrecognized value defaults to ``local`` (a hand-started core
    is local by construction — fail toward the safe, common case). ``host`` is
    the per-host label, so a client can render WHICH machine ("MacBook Pro
    (yours)" vs "mac-mini (yours, remote)").

    Consumed downstream by the broker presence sweep → ``space.ag2.presence`` →
    a client locality badge (the remaining two Track-10 slices). Self-reported
    v1; attestation is a Track 2/4 tie-in. Same runtime-authored-state pattern
    as ``socket`` above — the answer lives in the core's own environment.
    """
    kind = os.environ.get("SUTANDO_CORE_LOCALITY", "local").strip().lower()
    if kind not in ("local", "cloud"):
        kind = "local"
    return {"kind": kind, "host": _hostname()}


def _socket_path() -> str:
    """The tmux socket this core runs on. Mirrors start-cli.sh's resolution."""
    return os.environ.get("SUTANDO_TMUX_SOCKET", "/tmp/sutando-tmux.sock")


def core_session() -> str:
    """The tmux session the core runs in. Mirrors both launchers' default."""
    return os.environ.get("SUTANDO_TMUX_SESSION", "sutando-core")


def _tmux(sock: str, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["tmux", "-S", sock, *args],
                              capture_output=True, text=True, timeout=5)
    except Exception:
        return None


def core_pid(socket_path: str | None = None, session: str | None = None) -> int | None:
    """The pid of the CORE process, or None if the core is gone.

    Two things this must NOT do, both review-caught (qingyun-wu on #2488):

    1. **Never accept any pane on the socket.** The Codex launcher runs a
       separate `${SESSION}-watcher` session on the SAME socket
       (`src/agent/codex/cli/start-cli.sh:10,111`), and the Claude launcher
       deliberately PRESERVES sibling windows inside the core session when the
       core window dies (`src/agent/claude/cli/start-cli.sh:563-573`, the G10
       heal). A first-pane-wins lookup returns the watcher's or the sibling's
       pid and the heartbeat stays fresh over a dead core — the exact
       false-healthy class this module is fixing.
    2. **Never gate on the pane's foreground command.** `start-cli.sh:80-84`
       spells out why: a healthy core mid-tool shows the pane cmd as
       bash/python3/node, so a command match reports a live core dead.

    So: exact-match the session (`-t =name`, which is also why the launchers use
    `=` — bare `sutando-core` prefix-matches `sutando-core-watcher`), and then
    require the core PROCESS, mirroring `core_claude_pids()`: a `claude --name
    <session>` for the Claude runtime. For other runtimes fall back to panes
    scoped to that exact session, which still excludes the watcher session.
    """
    sock = socket_path or _socket_path()
    sess = session or core_session()

    has = _tmux(sock, "has-session", "-t", f"={sess}")
    if has is None or has.returncode != 0:
        return None

    # The core process itself — identity, not location.
    try:
        pg = subprocess.run(["pgrep", "-ax", "claude"],
                            capture_output=True, text=True, timeout=5)
        if pg.returncode == 0:
            for line in pg.stdout.splitlines():
                pid_s, _, args = line.partition(" ")
                if not pid_s.strip().isdigit():
                    continue
                if f"--name {sess}" in args or f"--name={sess}" in args:
                    return int(pid_s)
    except Exception:
        pass

    # Non-Claude runtime: panes of THIS session only (never `-a`).
    lp = _tmux(sock, "list-panes", "-t", f"={sess}", "-F", "#{pane_pid}")
    if lp is None or lp.returncode != 0:
        return None
    for line in lp.stdout.split():
        if line.strip().isdigit():
            return int(line.strip())
    return None


def write_beat(status: str = "running") -> None:
    """Write one heartbeat record. Atomic-via-tmp-then-rename so a concurrent
    reader never sees a partial file."""
    CORES_DIR.mkdir(parents=True, exist_ok=True)
    target = _alive_path()
    cpid = core_pid()
    payload = {
        "host": _hostname(),
        # The CORE's pid — what this file has always claimed to carry. Falls
        # back to the writer's own pid ONLY when tmux cannot be consulted, so a
        # missing/!broken tmux degrades to the pre-2026-08-01 behaviour rather
        # than blanking a field readers may come to depend on.
        "pid": cpid if cpid is not None else os.getpid(),
        "heartbeat_pid": os.getpid(),
        "started_at": _STARTED_AT,
        "last_beat_at": time.time(),
        "status": status,
        # The tmux socket THIS core actually runs on. Recorded here — in the
        # core's own environment — so it is the authoritative, runtime-authored
        # answer to "which socket?" for readers that cannot reconstruct the
        # launch env (e.g. `sutando-config.sh runtime` invoked by the desktop
        # app, whose ambient SUTANDO_TMUX_SOCKET points at a *different* bundled
        # socket). Mirrors start-cli.sh's resolution exactly.
        "socket": os.environ.get("SUTANDO_TMUX_SOCKET", "/tmp/sutando-tmux.sock"),
        # Self-reported locality (Track 10): {kind: local|cloud, host}. Additive
        # and informational — mtime remains the liveness signal — so readers
        # that don't know the field are unaffected.
        "locality": _locality(),
        "schema_version": 3,
    }
    tmp = target.with_suffix(".alive.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(target)


_STARTED_AT: float = time.time()
_SHUTDOWN_REQUESTED = False


def _handle_signal(signum: int, frame) -> None:
    """Mark shutdown so the loop exits at the top of the next sleep; also
    unlink the .alive file so peers see this core leave immediately rather
    than wait for mtime staleness."""
    global _SHUTDOWN_REQUESTED
    _SHUTDOWN_REQUESTED = True
    try:
        _alive_path().unlink(missing_ok=True)
    except Exception:  # pragma: no cover — best-effort cleanup
        pass


def run_forever(interval: float = 30.0, status: str = "running") -> int:
    """Heartbeat loop. Returns the exit code (0 on graceful shutdown)."""
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    # The gate ARMS on first observation and is re-checked every beat — it is
    # NOT decided once up front. `startup.sh:632` launches this process BEFORE
    # the core launcher runs, so on a cold boot there is no core yet; deciding
    # once would leave the gate disarmed forever and a core that came up and
    # then died would keep a fresh `.alive` (review-caught, qingyun-wu on #2488,
    # reproduced with core_pid=None at start: three beats, .alive still present).
    # Fail-open is preserved: never having seen a core means never stopping.
    saw_core = False
    while not _SHUTDOWN_REQUESTED:
        present = core_pid() is not None
        saw_core = saw_core or present
        if saw_core and not present:
            print("core_heartbeat: core pane is gone — stopping beat and "
                  "removing .alive so readers see it leave", file=sys.stderr, flush=True)
            try:
                _alive_path().unlink(missing_ok=True)
            except Exception:
                pass
            return 0
        try:
            write_beat(status=status)
        except Exception as e:
            # Don't die on transient FS hiccups — log + retry next tick.
            print(f"core_heartbeat: write failed: {e}", file=sys.stderr, flush=True)
        # Sleep in small slices so SIGTERM is responsive (signal handler
        # sets the flag; we check it between slices instead of blocking
        # for the full `interval`).
        slept = 0.0
        slice_s = min(1.0, interval)
        while slept < interval and not _SHUTDOWN_REQUESTED:
            time.sleep(slice_s)
            slept += slice_s
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--interval", type=float, default=30.0, help="seconds between beats (default: 30)")
    p.add_argument("--status", type=str, default="running", help="status string written into the .alive file")
    p.add_argument("--once", action="store_true", help="write a single beat and exit (for tests/debugging)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.once:
        write_beat(status=args.status)
        return 0
    # Anonymous, opt-out product telemetry: one event per real core boot so
    # maintainers can count active installs (OSS + desktop). No-op when opted
    # out or no key is configured. Never blocks; see src/telemetry.py + TELEMETRY.md.
    try:  # pragma: no cover — fire-and-forget glue; telemetry logic tested in tests/telemetry.test.py
        from telemetry import capture  # sibling module (src/ already on sys.path)

        capture("core_started", {"interval_s": args.interval})
    except Exception:  # pragma: no cover — telemetry must never break the core
        pass
    return run_forever(interval=args.interval, status=args.status)


if __name__ == "__main__":
    sys.exit(main())
