#!/usr/bin/env python3
"""Two liveness P1s from the #2488 review (qingyun-wu, head 01373d8a).

1. `pgrep -a` is NOT portable. On Linux it prints "PID argv"; on macOS/BSD `-a`
   means "include ANCESTORS" and the output is bare PIDs. `core_pid()` parsed
   argv out of that output, so on macOS the `--name <session>` match could never
   fire and the Claude-runtime identity branch silently fell through to the pane
   path. Reproduced on the dev host: `pgrep -ax claude` printed two bare PIDs,
   one of them an ancestor of pgrep itself.

2. `run_forever()` acted on a SINGLE `core_pid() is None`. That value means both
   "the core is gone" and "I could not tell" (tmux timeout, exec failure) — they
   are indistinguishable — so one flaky read removed `.alive` and reported a
   false death to every peer.

Both are driven through the REAL functions with fake `pgrep`/`ps`/`tmux` on PATH
rather than monkeypatched resolvers: a stub cannot cover the branch it replaces.

Run: python3 tests/core-heartbeat-pgrep-portability.test.py   (exit 0 / 1)
"""
from __future__ import annotations

import importlib.util
import os
import stat
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("ch", REPO / "src" / "core_heartbeat.py")
ch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ch)

# Captured BEFORE any monkeypatch below. The run_forever block replaces
# ch.core_pid with a stub and does not restore it, so later sections MUST use
# this reference or they silently exercise the stub instead of the resolver.
_REAL_CORE_PID = ch.core_pid

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def _bin(d: Path, name: str, body: str) -> None:
    p = d / name
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


SESSION = "sutando-core"

# ── 1. BSD-shaped pgrep: `-a` yields bare PIDs (ancestors), argv only via ps ──
box = Path(tempfile.mkdtemp(prefix="ch-bsd-"))
# tmux: has-session succeeds; list-panes returns nothing, so ONLY the pgrep
# branch can produce a pid. If that branch is broken, core_pid() returns None.
_bin(box, "tmux", 'case "$*" in *has-session*) exit 0;; *list-panes*) exit 1;; esac; exit 0')
# BSD semantics: -a prints ancestors as bare pids; -x prints matching pids.
_bin(box, "pgrep", 'if [ "$1" = "-ax" ]; then echo 4242; echo 99999; exit 0; fi\n'
                   'if [ "$1" = "-x" ]; then echo 4242; exit 0; fi\n'
                   'exit 1')
_bin(box, "ps", f'echo "claude --name {SESSION} --resume"')

_orig_path = os.environ["PATH"]
os.environ["PATH"] = f"{box}:{_orig_path}"
try:
    got = _REAL_CORE_PID("/tmp/does-not-matter.sock")
finally:
    os.environ["PATH"] = _orig_path

check("CONTROL: fake toolchain is on PATH and tmux has-session succeeds", box.joinpath("pgrep").exists())
check("BSD pgrep (-a = ancestors, bare pids): core_pid still resolves 4242 via ps",
      got == 4242, f"got {got!r} — the identity branch fell through")

# ── 2. Absence must be CONSECUTIVE before .alive is removed ──────────────────
ws = Path(tempfile.mkdtemp(prefix="ch-ws-"))
(ws / "state" / "cores").mkdir(parents=True, exist_ok=True)

check("floor constant exists and is >1 beat", getattr(ch, "ABSENT_BEATS_BEFORE_DEATH", 1) > 1)

alive = ws / "state" / "cores" / "testhost.alive"
ch._alive_path = lambda: alive          # noqa: E731  — path only, not the resolver
ch.write_beat = lambda status=None: alive.write_text("{}")

seq = [4242, None, 4242, None, None, None]   # one blip, recovery, then real death
calls = {"n": 0}


def flaky(socket_path=None):
    i = calls["n"]
    calls["n"] += 1
    return seq[i] if i < len(seq) else None


ch.core_pid = flaky
rc = ch.run_forever(interval=0.01, status="test")   # existing suite's pattern

check("a SINGLE absent read did not end the loop (blip at beat 2 survived)", calls["n"] > 3,
      f"stopped after {calls['n']} reads — one None was treated as death")
check("consecutive absence DOES stop it", rc == 0)
check(".alive removed once death is confirmed", not alive.exists())


# ── 3. A CLAUDE session with no claude process is DEAD, not "any pane" ───────
# john-the-dev, #2488: the pane fallback exists for NON-Claude runtimes. Applied
# to a Claude session it returns a sibling/shell pane pid, .alive keeps beating,
# and peers treat a dead core as live indefinitely. Only an AFFIRMATIVE "claude"
# identification skips the fallback — unknown must keep the old behaviour, so a
# healthy non-Claude host can never be turned into a permanent false death.

def _farm(runtime_line: str, claude_alive: bool, kill_config: bool = False,
          pane_pid: int = 7777):
    """Fake toolchain: tmux answers has-session / show-environment / list-panes.

    `ps` is deliberately PID-AWARE. It used to echo the core's argv for every
    pid, which made the fixture unable to express the difference between "this
    pane IS the core" and "this pane is a leftover shell in the core's session"
    — the two cases the `--name <sess>` argv test exists to separate. With a
    blanket stub, any pane-scoped lookup trivially "finds" a core, so a change
    that resurrects a corpse and a change that correctly identifies a live core
    are indistinguishable. Only 4242 is the core here; every other pid is a
    shell, which is what a surviving sibling pane actually looks like.
    """
    d = Path(tempfile.mkdtemp(prefix="ch-rt-"))
    tmux = "\n".join([
        'case "$*" in',
        '  *has-session*)      exit 0 ;;',
        f'  *show-environment*) {runtime_line}; exit 0 ;;',
        f'  *list-panes*)       echo {pane_pid}; exit 0 ;;',
        'esac',
        'exit 0',
    ])
    _bin(d, "tmux", tmux)
    _bin(d, "pgrep", "echo 4242\nexit 0" if claude_alive else "exit 1")
    _bin(d, "ps", "\n".join([
        'last=""',
        'for a in "$@"; do last="$a"; done',
        'case "$last" in',
        f'  4242) echo "claude --name {SESSION} --resume" ;;',
        '  *)    echo "-zsh" ;;',
        'esac',
        'exit 0',
    ]))
    if kill_config:
        # Truly-undeterminable runtime: the session env is unset AND the config
        # fallback cannot answer. Without this the config in THIS repo says
        # "claude", so the unknown branch is unreachable and the case would be
        # testing the claude branch under a misleading name.
        # NOTE: stubbing `bash` alone stopped being sufficient once the resolver
        # switched from `bash scripts/sutando-config.sh core-runtime` to a direct
        # `from sutando_config import resolve_core_runtime` (the shell-out needed
        # a repo-root walk that scripts/lint-workspace-resolution.sh rejects).
        # A PATH stub cannot blind an in-process import, so `_blind_config()`
        # below does that half. Both are kept: the `bash` stub still covers the
        # subprocess path if it ever returns.
        _bin(d, "bash", "exit 1")
    return d


def _blind_config():
    """Make the in-process config fallback unanswerable; returns a restore token.

    Mirrors the `bash` stub for the import-based lookup. Without this the
    UNKNOWN-runtime case silently becomes a second CLAUDE case — it would still
    pass by asserting None while claiming to prove the fallback is preserved.
    """
    saved = (True, sys.modules.get("sutando_config"))
    stub = types.ModuleType("sutando_config")

    def _unavailable(*a, **k):
        raise RuntimeError("config unavailable (test)")

    stub.resolve_core_runtime = _unavailable
    sys.modules["sutando_config"] = stub
    return saved


def _restore_config(token):
    if token is None:
        return
    _, saved = token
    if saved is None:
        sys.modules.pop("sutando_config", None)
    else:
        sys.modules["sutando_config"] = saved


# The `alive` column is "does `pgrep -x claude` match?", NOT "is the core
# running?". Conflating the two is what hid the versioned-binary case below:
# every pre-existing row set pgrep to answer, so no row could express a host
# where the core is healthy but its executable is not NAMED `claude`.
CASES = [
    ("claude session + claude process   -> the claude pid",
     'echo SUTANDO_CORE_RUNTIME=claude', True,  4242),
    ("CLAUDE session + NO claude proc   -> None (NOT pane 7777)",
     'echo SUTANDO_CORE_RUNTIME=claude', False, None),
    ("codex session + no claude proc    -> pane fallback still works",
     'echo SUTANDO_CORE_RUNTIME=codex',  False, 7777),
    ("UNKNOWN runtime + no claude proc  -> pane fallback preserved",
     'echo "-SUTANDO_CORE_RUNTIME"',     False, 7777, True),
    # Versioned install: Claude Code runs from `~/.local/share/claude/versions/
    # <ver>`, so the kernel accounting name that `pgrep -x` matches is `<ver>`,
    # not `claude` — pgrep matches NOTHING for a perfectly healthy core. The
    # core is the session's own pane, and its argv still names the session, so
    # the pane-scoped argv check must resolve it. Before the fix this returned
    # None, `.alive` was never written, and a live core read dead to every
    # reader — the inverse of #2488 and the more dangerous direction, since a
    # consumer that relaunches a dead core would relaunch a live one in a loop.
    ("versioned binary (pgrep -x misses) -> pane argv still identifies the core",
     'echo SUTANDO_CORE_RUNTIME=claude', False, 4242, False, 4242),
]
for case in CASES:
    label, rt, alive, want = case[:4]
    _kill = bool(len(case) > 4 and case[4])
    _pane = case[5] if len(case) > 5 else 7777
    box = _farm(rt, alive, kill_config=_kill, pane_pid=_pane)
    _op = os.environ["PATH"]
    os.environ["PATH"] = f"{box}:{_op}"
    _tok = _blind_config() if _kill else None
    try:
        got = _REAL_CORE_PID("/tmp/s.sock")
    finally:
        os.environ["PATH"] = _op
        _restore_config(_tok)
    check(label, got == want, f"got {got!r}, want {want!r}")

print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("all checks passed — BSD pgrep resolves, and one flaky read is not death")
