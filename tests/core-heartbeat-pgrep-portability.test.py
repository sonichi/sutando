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
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("ch", REPO / "src" / "core_heartbeat.py")
ch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ch)

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
    got = ch.core_pid("/tmp/does-not-matter.sock")
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

print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("all checks passed — BSD pgrep resolves, and one flaky read is not death")
