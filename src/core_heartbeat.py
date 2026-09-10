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
import re
import signal
import subprocess
import shutil
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
from tmux_probe import classify as _classify_session_probe  # noqa: E402

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


_MAX_FIELD = 64
_CLIENT_TTL_S = 10.0
_CLIENT_CACHE: dict[str, tuple[float, str]] = {}


def _tmux_candidates() -> list[str]:
    seen: list[str] = []
    for c in (shutil.which("tmux"), os.environ.get("SUTANDO_TMUX_BIN")):
        if c and c not in seen:
            seen.append(c)
    return seen


def _same_socket(reported: str, sock: str) -> bool:
    try:
        return bool(reported) and os.path.realpath(reported) == os.path.realpath(sock)
    except Exception:
        return False


def _client_for(sock: str) -> str | None:
    """The first candidate that PROVABLY talked to this server: it must list the server's own
    socket path back. A hit is kept for one beat; a miss is retried on every call."""
    hit = _CLIENT_CACHE.get(sock)
    if hit and time.monotonic() - hit[0] < _CLIENT_TTL_S:
        return hit[1]
    for binary in _tmux_candidates():
        try:
            r = subprocess.run([binary, "-S", sock, "list-sessions", "-F", "#{socket_path}"],
                               capture_output=True, text=True, timeout=2)
        except Exception:
            continue
        lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        if r.returncode == 0 and lines and all(_same_socket(ln, sock) for ln in lines):
            _CLIENT_CACHE[sock] = (time.monotonic(), binary)
            return binary
    return None


def _tmux_backend(sock: str | None = None, sess: str | None = None) -> dict:
    """A client verified to speak to THIS server about the OBSERVED session — it must answer
    with the server's socket path and the session name, so exit 0 alone proves nothing."""
    sock = sock or _socket_path()
    sess = sess or _observed_session(sock)
    seen = _tmux_candidates()
    chosen, server_version = None, None
    for binary in seen:
        try:
            # Bounded (a hung binary must not eat the beat). The server proves itself by socket path; tmux
            # renders #{session_name} EMPTY under -t, so the session is RESOLVED by has-session, not rendered.
            r = subprocess.run([binary, "-S", sock, "display-message", "-p", "-t", f"={sess}",
                                "#{version}|#{socket_path}"], capture_output=True, text=True, timeout=2)
            parts = (r.stdout or "").strip().split("|")
            if not (r.returncode == 0 and len(parts) >= 2 and parts[0] and " " not in parts[0]
                    and _same_socket(parts[1], sock)):
                continue
            h = subprocess.run([binary, "-S", sock, "has-session", "-t", f"={sess}"],
                               capture_output=True, text=True, timeout=2)
        except Exception:
            continue
        if h.returncode == 0:
            chosen, server_version = binary, parts[0][:_MAX_FIELD]
            break
    version = None
    if chosen:
        try:
            r = subprocess.run([chosen, "-V"], capture_output=True, text=True, timeout=2)
            out = (r.stdout or r.stderr or "").strip()
            if r.returncode == 0 and out.lower().startswith("tmux "):
                version = out.split(None, 1)[1][:_MAX_FIELD]
        except Exception:
            version = None
    return {"backend": "tmux", "tmux_binary": chosen, "tmux_version": version,
            "tmux_server_version": server_version, "tmux_verified": chosen is not None,
            "tmux_candidates": seen}


def core_session() -> str:
    """The tmux session the core runs in, per the env/default contract.
    NOT authoritative: this is an unverified claim from the environment, not a
    confirmed live session (the Claude launcher honors SUTANDO_TMUX_SESSION as
    of claude/cli/start-cli.sh, but an exported value can still name a session
    that never started). Prefer _observed_session() for anything recorded."""
    return os.environ.get("SUTANDO_TMUX_SESSION", "sutando-core")


def _observed_session(sock: str) -> str:
    """Inside tmux display-message is authoritative; outside, the contract name is only
    a claim — validated against a live pid and real sessions, else returned unverified."""
    if os.environ.get("TMUX"):
        r = _tmux(sock, "display-message", "-p", "#{session_name}")
        if r is not None and r.returncode == 0:
            name = (r.stdout or "").strip()
            if name:
                return name
    # $TMUX is inherited when startup.sh runs inside the pane, but absent when it
    # runs outside one — there the env value is a claim, so verify it.
    candidate = core_session()
    if core_pid(sock, candidate) is not None:
        return candidate
    r = _tmux(sock, "list-sessions", "-F", "#{session_name}")
    if r is not None and r.returncode == 0:
        for name in (r.stdout or "").split():
            if name != candidate and core_pid(sock, name) is not None:
                return name
    return candidate


def _tmux(sock: str, *args: str) -> subprocess.CompletedProcess | None:
    """Every probe goes through the client that can reach this server; only when none can is
    the PATH tmux used, so its refusal is classified rather than mistaken for an absent core."""
    binary = _client_for(sock) or "tmux"
    try:
        return subprocess.run([binary, "-S", sock, *args],
                              capture_output=True, text=True, timeout=5)
    except Exception:
        _CLIENT_CACHE.pop(sock, None)
        return None


def _argv_names_session(args: str, sess: str) -> bool:
    """True only if argv names EXACTLY this session via --name.

    Substring matching is wrong here and was a live false-healthy path
    (review-caught, qingyun-wu on #2488): `"--name sutando-core" in args` is
    also satisfied by `claude --name sutando-core-watcher`, so a prefixed
    sibling session kept this host's `.alive` fresh over a dead core — the exact
    class of bug this module exists to remove. Compare the whole token instead:
    the next token after a bare `--name`, or the value after `--name=`.
    """
    toks = args.split()
    for i, tok in enumerate(toks):
        if tok == "--name":
            if i + 1 < len(toks) and toks[i + 1] == sess:
                return True
        elif tok.startswith("--name=") and tok[len("--name="):] == sess:
            return True
    return False


# The last has-session tri-state core_pid() observed. run_forever() resets it
# before each read so a stubbed core_pid (tests) counts as an observed answer.
_LAST_SESSION_PROBE: bool | None = False


def _session_present(sock: str, sess: str) -> bool | None:
    """True / False / None for the exact session, via the shared classifier."""
    global _LAST_SESSION_PROBE
    has = _tmux(sock, "has-session", "-t", f"={sess}")
    _LAST_SESSION_PROBE = (None if has is None
                           else _classify_session_probe(has.returncode, has.stderr))
    return _LAST_SESSION_PROBE


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

    if not _session_present(sock, sess):
        return None

    # SESSION-SCOPED FIRST, then the process-name sweep as a fallback.
    #
    # The order matters and used to be the other way round. `pgrep -x claude`
    # enumerates every process of that name ON THE WHOLE MACHINE — it has no
    # notion of the tmux socket or the session. Verified on a peer host
    # (Sutando-Pro, #2580): `pgrep -x claude` returned a 16-day-old
    # `claude --resume ...` running under Terminal, parented
    # zsh -> login -> Terminal -> launchd, not on the tmux socket at all.
    # `_argv_names_session` rejected it, so the outcome was correct — but that
    # made the argv test the ONLY thing standing between the resolver and an
    # unrelated pid, and a stray `claude --name sutando-core` anywhere on the
    # box (a second core on a DIFFERENT socket, a leftover, a copy-pasted
    # command) would have been accepted and written into `.alive` as this
    # host's core.
    #
    # Asking tmux for the panes of THIS exact session cannot make that mistake:
    # the candidates are bounded by the socket and the session before identity
    # is even considered. So it goes first, and the machine-wide sweep only runs
    # if the session-scoped lookup found nothing (a core that is not its pane's
    # root process — e.g. launched behind a wrapper).
    #
    # Both #2488 guards still hold in either branch: candidates are never "any
    # pane on the socket" (`-t =<sess>` is exact, so `<sess>-watcher` is
    # excluded), and identity always comes from argv, never from the pane's
    # foreground command — a healthy core mid-tool shows bash/python3/node.
    try:
        # `-s` = every pane in the SESSION. Without it `list-panes` reports only
        # the CURRENT WINDOW's panes, so a core sitting in a non-selected sibling
        # window is invisible and this branch finds nothing — then the pgrep
        # fallback cannot see a version-named executable either, and the
        # resolver returns None for a live core. Sibling windows are not
        # hypothetical: the Claude launcher deliberately preserves them inside
        # the core session (`start-cli.sh:563-573`, the G10 heal), so whichever
        # window is selected decides whether the core is findable.
        # Review-caught, qingyun-wu on #2581, with an exact reproduction:
        #     list-panes    -t "=sutando-core" -> sibling
        #     list-panes -s -t "=sutando-core" -> core, sibling
        # `-t "={sess}"` still scopes to the EXACT session, so #2488's "never any
        # pane on the socket" guard is unaffected — `-s` widens across windows
        # WITHIN this session, never across sessions.
        lp = _tmux(sock, "list-panes", "-s", "-t", f"={sess}", "-F", "#{pane_pid}")
        if lp is not None and lp.returncode == 0:
            for pid_s in lp.stdout.split():
                if not pid_s.isdigit():
                    continue
                ps = subprocess.run(["ps", "-o", "args=", "-p", pid_s],
                                    capture_output=True, text=True, timeout=5)
                if ps.returncode != 0:
                    continue
                if _argv_names_session(ps.stdout.strip(), sess):
                    return int(pid_s)
    except Exception:
        pass

    # Fallback: the process-name sweep.
    #
    # `pgrep -a` is NOT portable: on Linux it prints "PID argv", but on
    # macOS/BSD `-a` means "include ancestors" and the output is bare PIDs.
    # Parsing argv out of it therefore matched nothing on macOS and this whole
    # identity branch silently fell through to the pane path (review-caught,
    # qingyun-wu on #2488; reproduced on this host: `pgrep -ax claude` printed
    # two bare PIDs, one of them an ancestor of pgrep itself).
    #
    # So: `pgrep -x` for the exact process NAME (no ancestors, no argv), then
    # ask `ps` for each pid's argv. `pgrep -f` is deliberately avoided — it
    # matches the invoking shell's own argv and self-matches.
    #
    # Note this branch cannot find the core on a versioned install at all:
    # `pgrep -x` matches the kernel accounting name, and Claude Code runs from
    # `~/.local/share/claude/versions/<ver>`, so `ucomm` is `<ver>`, not
    # `claude` (`ps -o comm=` shows `claude` because that is argv[0]). It is
    # kept for installs whose executable really is named `claude`, and for
    # cores that are not their pane's root process.
    try:
        pg = subprocess.run(["pgrep", "-x", "claude"],
                            capture_output=True, text=True, timeout=5)
        if pg.returncode == 0:
            for pid_s in pg.stdout.split():
                if not pid_s.isdigit():
                    continue
                ps = subprocess.run(["ps", "-o", "args=", "-p", pid_s],
                                    capture_output=True, text=True, timeout=5)
                if ps.returncode != 0:
                    continue
                if _argv_names_session(ps.stdout.strip(), sess):
                    return int(pid_s)
    except Exception:
        pass

    # A Claude session with no matching `claude --name <sess>` process is DEAD,
    # not "fall back to whatever pane is left" (review-caught, john-the-dev on
    # #2488). The pane fallback exists for NON-Claude runtimes; applied to a
    # Claude session it resurrects a corpse — a sibling/shell pane pid is
    # returned, `.alive` keeps beating, and peers plus the scheduler treat a
    # dead core as live indefinitely, suppressing takeover.
    #
    # Deliberately narrow: only an AFFIRMATIVE "claude" identification skips the
    # fallback. An undeterminable runtime keeps the previous behaviour, so this
    # cannot turn a healthy non-Claude host into a permanent false death.
    if (_session_runtime(sock, sess) or "").lower() == "claude":
        return None

    # Non-Claude runtime: panes of THIS session only (never `-a`), across all its
    # windows (`-s`) for the same reason as the identity branch above — without
    # it, a core in a non-selected window is invisible and this returns None for
    # a live core. Same one-token correction, same guard: `-t "={sess}"` keeps it
    # scoped to the exact session.
    lp = _tmux(sock, "list-panes", "-s", "-t", f"={sess}", "-F", "#{pane_pid}")
    if lp is None or lp.returncode != 0:
        return None
    for line in lp.stdout.split():
        if line.strip().isdigit():
            return int(line.strip())
    return None


def _session_runtime(sock: str, sess: str) -> "str | None":
    """Which runtime this tmux session was launched as, or None if undeterminable.

    Prefers the session's OWN environment (`tmux show-environment`), which
    `src/agent/start-cli.sh` sets at session creation — runtime-authored state,
    the same pattern `write_beat` already trusts for the socket. Falls back to
    the repo config only if the session cannot answer.

    Returns None rather than guessing: callers must treat unknown as "no
    discrimination possible" and keep their pre-existing behaviour.
    """
    r = _tmux(sock, "show-environment", "-t", f"={sess}", "SUTANDO_CORE_RUNTIME")
    if r is not None and r.returncode == 0:
        line = r.stdout.strip()
        if line.startswith("SUTANDO_CORE_RUNTIME=") and not line.startswith("-"):
            val = line.split("=", 1)[1].strip()
            if val:
                return val
    # Config fallback. Import `resolve_core_runtime` directly rather than
    # shelling out to `scripts/sutando-config.sh` — that shell-out had to walk
    # two levels up from `__file__` to locate the script, which is the repo-root
    # walk `scripts/lint-workspace-resolution.sh` rejects (and rightly: it
    # breaks when `src/` is reached through an app-bundle symlink). This module
    # already lives in `src/`, next to `sutando_config`, so the import needs no
    # walking at all — and it drops a subprocess from every beat.
    # (`src/` is already on sys.path — line ~62 inserts it for
    # `workspace_default`, using a single `.parent`, which is not the banned form.)
    try:
        from sutando_config import resolve_core_runtime
        val = (resolve_core_runtime() or "").strip()
        if val:
            return val.splitlines()[0].strip()
    except Exception:
        pass
    return None


def write_beat(status: str = "running") -> None:
    """Write one heartbeat record. Atomic-via-tmp-then-rename so a concurrent
    reader never sees a partial file."""
    if _SIGNALLED:
        return  # the handler already unlinked .alive; a beat in flight must not republish it
    CORES_DIR.mkdir(parents=True, exist_ok=True)
    target = _alive_path()
    sock = _socket_path()
    observed_session = _observed_session(sock)
    # The pid of the session the record NAMES, not of whatever the env claims.
    cpid = core_pid(sock, observed_session)
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
        "socket": sock,
        # Same runtime-authored argument as `socket`, but the env cannot be
        # trusted here: the Claude launcher hardcodes the session, so ask tmux.
        "session": observed_session,
        # Self-reported locality (Track 10): {kind: local|cloud, host}. Additive
        # and informational — mtime remains the liveness signal — so readers
        # that don't know the field are unaffected.
        "locality": _locality(),
        # A client verified to speak this server and this (observed) session, plus the
        # server's own version: a compatible client for readers, not the creator.
        **_tmux_backend(sock=sock, sess=observed_session),
        "schema_version": 4,
    }
    tmp = target.with_suffix(".alive.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(target)


_STARTED_AT: float = time.time()
_SHUTDOWN_REQUESTED = False
_SIGNALLED = False  # set only by the signal handler; the loop flag alone is also set by harnesses


def _handle_signal(signum: int, frame) -> None:
    """Mark shutdown so the loop exits at the top of the next sleep; also
    unlink the .alive file so peers see this core leave immediately rather
    than wait for mtime staleness."""
    global _SHUTDOWN_REQUESTED, _SIGNALLED
    _SHUTDOWN_REQUESTED = True
    _SIGNALLED = True
    try:
        # Tombstone BEFORE the unlink: recover-core must not read a graceful
        # stop as death and relaunch a core someone stopped on purpose (#2160).
        mark_stopped()
    except Exception:  # pragma: no cover — best-effort
        pass
    try:
        _alive_path().unlink(missing_ok=True)
    except Exception:  # pragma: no cover — best-effort cleanup
        pass


# One `core_pid() is None` can mean "cannot tell", not "dead" — see run_forever().
ABSENT_BEATS_BEFORE_DEATH = 3
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
    # Before first sighting, publish NOTHING: write_beat() deliberately falls
    # back to the writer pid when resolution is unavailable, and refreshing
    # that fallback forever when a cold-started core never appears advertises
    # a failed boot as healthy. Keep waiting (so a late core can still arm the
    # gate), but remove any stale prior record until the real core is observed.
    saw_core = False
    _record_writer_pid()
    try:
        # A new run supersedes any prior graceful-stop tombstone.
        _alive_path().with_suffix(".stopped").unlink(missing_ok=True)
    except Exception:  # pragma: no cover — best-effort
        pass
    absent_streak = 0
    global _LAST_SESSION_PROBE
    while not _SHUTDOWN_REQUESTED:
        _LAST_SESSION_PROBE = False
        present = core_pid() is not None
        saw_core = saw_core or present
        # An unobserved probe (tmux missing/hung, or a refused client) is not an
        # absence: it neither resets nor advances the streak of observed misses.
        if present:
            absent_streak = 0
        elif _LAST_SESSION_PROBE is not None:
            absent_streak += 1
        if saw_core and absent_streak >= ABSENT_BEATS_BEFORE_DEATH:
            print("core_heartbeat: core pane is gone — stopping beat and "
                  "removing .alive so readers see it leave", file=sys.stderr, flush=True)
            try:
                _alive_path().unlink(missing_ok=True)
            except Exception:
                pass
            return 0
        if saw_core:
            try:
                write_beat(status=status)
                if not _PID_RECORDED:
                    _record_writer_pid()
            except Exception as e:
                # Don't die on transient FS hiccups — log + retry next tick.
                print(f"core_heartbeat: write failed: {e}", file=sys.stderr, flush=True)
        else:
            try:
                _alive_path().unlink(missing_ok=True)
            except Exception:
                pass
        # Sleep in small slices so SIGTERM is responsive (signal handler
        # sets the flag; we check it between slices instead of blocking
        # for the full `interval`).
        slept = 0.0
        slice_s = min(1.0, interval)
        while slept < interval and not _SHUTDOWN_REQUESTED:
            time.sleep(slice_s)
            slept += slice_s
    if _SIGNALLED:
        try:
            # A beat that was mid-write when the signal landed may have republished the file.
            _alive_path().unlink(missing_ok=True)
        except Exception:  # pragma: no cover — best-effort
            pass
    return 0


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return not (st.stdout or "").strip().startswith("Z")
    except Exception:
        return True


def _pidfile() -> Path:
    return _alive_path().with_suffix(".heartbeat.pid")


_PID_RECORDED = False


def _record_writer_pid() -> None:
    """The writer's own record of itself, beside the .alive. Never creates the directory: only a beat
    may bring state/cores into existence (a harness that stubs write_beat must leave no trace)."""
    global _PID_RECORDED
    try:
        if CORES_DIR.is_dir():
            _pidfile().write_text(f"{os.getpid()} {Path(__file__).resolve()}\n")
            _PID_RECORDED = True
    except Exception:  # pragma: no cover — best-effort
        pass


def _recorded_writer_pids() -> list[int]:
    """Pids this checkout's records name: the pidfile and .alive's heartbeat_pid. Never a sweep."""
    pids: list[int] = []
    try:
        head, _, path = _pidfile().read_text().strip().partition(" ")
        if head.isdigit() and path == str(Path(__file__).resolve()):
            pids.append(int(head))
    except Exception:
        pass
    try:
        hp = json.loads(_alive_path().read_text()).get("heartbeat_pid")
        if isinstance(hp, int) and hp not in pids:
            pids.append(hp)
    except Exception:
        pass
    return pids


def _is_writer_argv(args: str, script: str) -> bool:
    """`<python> <this script> [flags]` and nothing else: a `-c` program that mentions the path is not a writer."""
    i = args.find(script)
    if i < 0:
        return False
    prefix = args[:i].rstrip()
    after = args[i + len(script):]
    # Apple's framework interpreter reports itself as `.../Python.app/Contents/MacOS/Python`.
    return (bool(re.search(r"python[0-9.]*$", prefix, re.IGNORECASE)) and " -c" not in f" {prefix}"
            and (after == "" or after[0] == " "))


def stop_other_writers(timeout_s: float = 5.0) -> int:
    """SIGTERM the heartbeat writer(s) this checkout's own records name — after proving each pid is an
    interpreter running exactly this script — and wait for exit (SIGKILL past the timeout). Nothing
    is swept by argv, and an ambiguous pid is left alone: killing the wrong process is the worse error."""
    me, parent = os.getpid(), os.getppid()
    script = str(Path(__file__).resolve())
    pids = []
    for pid in _recorded_writer_pids():
        if pid in (me, parent) or pid <= 1:
            continue
        try:
            r = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        except Exception:
            continue
        if r.returncode == 0 and _is_writer_argv((r.stdout or "").strip(), script):
            pids.append(pid)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + timeout_s
    left = list(pids)
    while left and time.monotonic() < deadline:
        left = [p for p in left if _pid_running(p)]
        if left:
            time.sleep(0.1)
    for pid in left:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return len(pids)


def mark_stopped() -> None:
    """Publish the durable graceful-stop tombstone for THIS host.

    The one shared implementation behind both writers: the sidecar's own
    SIGTERM/SIGINT handler, and stop-core.sh's --mark-stopped call — the
    canonical stop path kills tmux sessions and never signals the sidecar,
    so without this the sidecar's core-gone exit reads as a crash and
    recover-core may relaunch a deliberately stopped core (#2160)."""
    # mkdir: a stop can precede the first beat, and the signal handler's
    # best-effort except would swallow the miss — tombstone silently absent.
    CORES_DIR.mkdir(parents=True, exist_ok=True)
    _alive_path().with_suffix(".stopped").write_text(str(time.time()))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--interval", type=float, default=30.0, help="seconds between beats (default: 30)")
    p.add_argument("--status", type=str, default="running", help="status string written into the .alive file")
    p.add_argument("--once", action="store_true", help="write a single beat and exit (for tests/debugging)")
    p.add_argument("--mark-stopped", action="store_true",
                   help="write the graceful-stop tombstone and exit (called by stop-core.sh)")
    p.add_argument("--stop", action="store_true",
                   help="stop every other heartbeat writer of this checkout and wait for it to exit (restart handoff)")
    return p.parse_args(argv)


def _emit_token_usage() -> None:  # pragma: no cover — network + optional-skill glue
    """Best-effort: read the quota-tracker's rate-limit snapshot and emit an
    anonymous ``token_usage`` telemetry event (bucketed utilization + status).

    Optional-skill safe: if the quota-tracker script isn't installed or the read
    fails, emit an ``unavailable`` categorical event with sentinel percentages
    so fleet views can distinguish "cannot report" from "no usage." Never raises
    to the caller.
    """
    try:
        import subprocess
        from telemetry import enabled, token_usage
        from util_paths import claude_home_path

        # Skip BEFORE read-quota.py: that reader writes quota-burn-history.json,
        # so running it would break the opt-out contract, not just the upload.
        if not enabled():
            return
        script = claude_home_path("skills", "quota-tracker", "scripts", "read-quota.py")
        if not script.exists():
            token_usage(status="unavailable")
            return
        try:
            out = subprocess.run(
                [sys.executable, str(script), "--json"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception:
            token_usage(status="unavailable")
            return
        if out.returncode != 0 or not out.stdout.strip():
            token_usage(status="unavailable")
            return
        try:
            data = json.loads(out.stdout)
        except (TypeError, ValueError, json.JSONDecodeError):
            token_usage(status="unavailable")
            return
        if not isinstance(data, dict):
            token_usage(status="unavailable")
            return
        rem5 = data.get("remaining_5h_pct")
        rem7 = data.get("remaining_7d_pct")
        util5 = (100 - rem5) if isinstance(rem5, (int, float)) else None
        util7 = (100 - rem7) if isinstance(rem7, (int, float)) else None
        token_usage(util5, util7, status=str(data.get("status", "unknown")))
    except Exception:
        pass  # telemetry must never affect the core


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.mark_stopped:
        mark_stopped()
        return 0
    if args.stop:
        print(f"core_heartbeat: stopped {stop_other_writers()} writer(s)", flush=True)
        return 0
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
    # Daemon thread: a quota read must never delay the beat loop.
    try:  # pragma: no cover — fire-and-forget glue
        import threading
        threading.Thread(target=_emit_token_usage, daemon=True).start()
    except Exception:  # pragma: no cover — telemetry must never break the core
        pass
    return run_forever(interval=args.interval, status=args.status)


if __name__ == "__main__":
    sys.exit(main())
