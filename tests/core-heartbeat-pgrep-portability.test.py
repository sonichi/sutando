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
          pane_pid: "int | str" = 7777, pgrep_pid: int = 4242,
          core_argv_pids: "tuple" = (4242,), other_window_pid: "int | None" = None):
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
        # Two cases, because tmux distinguishes them and the resolver must too:
        #   `list-panes -s -t =sess` -> every pane in the SESSION (all windows)
        #   `list-panes    -t =sess` -> only the CURRENT WINDOW's panes
        # `other_window_pid` is a pane that exists in the session but NOT in the
        # selected window, so it is reachable only via `-s`. Modelling it is the
        # difference between "several tokens on one result" (which the old
        # fixture could express) and "several WINDOWS" (which it could not) —
        # the gap that let a core in a sibling window read as absent.
        f"  *list-panes*-s*|*-s*list-panes*)  printf '%s\\n' {other_window_pid} {pane_pid}; exit 0 ;;"
        if other_window_pid else "",
        f"  *list-panes*)       printf '%s\\n' {pane_pid}; exit 0 ;;",
        'esac',
        'exit 0',
    ])
    _bin(d, "tmux", tmux)
    _bin(d, "pgrep", f"echo {pgrep_pid}\nexit 0" if claude_alive else "exit 1")
    # `core_argv_pids` are the pids whose argv NAMES the session. Everything else
    # is a shell. Keeping this a set rather than a single pid is what lets a case
    # put a convincing impostor (right argv, wrong session) in pgrep's output.
    _bin(d, "ps", "\n".join([
        'last=""',
        'for a in "$@"; do last="$a"; done',
        'case "$last" in',
        f'  {"|".join(str(p) for p in core_argv_pids)}) echo "claude --name {SESSION} --resume" ;;',
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
    # Same host shape, but `list-panes` emits a NON-PID token before the real
    # pane. Real tmux can put a blank line or a warning on stdout, and a
    # non-numeric token must be skipped rather than crash the resolver or abort
    # the scan — the core is still found on the next token. Without this row the
    # `if not pid_s.isdigit(): continue` guard is never executed by any test.
    ("noisy list-panes (non-pid token first) -> skipped, core still found",
     'echo SUTANDO_CORE_RUNTIME=claude', False, 4242, False, "- 4242"),
    # ORDERING. `pgrep -x claude` sweeps the WHOLE MACHINE — it knows nothing of
    # the socket or the session. Verified on a peer host (Sutando-Pro, #2580):
    # it returned a 16-day-old `claude --resume` under Terminal, not on the tmux
    # socket at all. There the argv test rejected it, but that only holds while
    # no foreign process happens to name this session — a second core on a
    # DIFFERENT socket, or a leftover, would be accepted and written into
    # `.alive` as this host's core.
    #
    # Here pgrep offers 9999 with the core's exact argv (a convincing impostor)
    # while the session's own pane is 4242. ONLY the branch order decides: pane
    # first -> 4242 (right), pgrep first -> 9999 (a pid from another session).
    ("machine-wide pgrep hit with matching argv loses to the session's own pane",
     'echo SUTANDO_CORE_RUNTIME=claude', True, 4242, False, 4242, 9999, (4242, 9999)),
    # TWO WINDOWS. `list-panes -t =sess` reports only the CURRENT window, so a
    # core in a non-selected sibling window is invisible to it; the pgrep
    # fallback cannot see a version-named executable either, and the resolver
    # returns None for a live core. Sibling windows are preserved deliberately by
    # the launcher (start-cli.sh G10 heal), so which window happens to be
    # selected decided whether the core could be found.
    #
    # Here the selected window holds only pane 7777 (a shell) and the core 4242
    # lives in another window of the same session — reachable only via `-s`.
    # Review-caught, qingyun-wu on #2581, reproduced live:
    #     list-panes    -t "=sutando-core" -> sibling
    #     list-panes -s -t "=sutando-core" -> core, sibling
    ("core in a NON-SELECTED window of the same session is still found (-s)",
     'echo SUTANDO_CORE_RUNTIME=claude', False, 4242, False, 7777, 4242, (4242,), 4242),
]
for case in CASES:
    label, rt, alive, want = case[:4]
    _kill = bool(len(case) > 4 and case[4])
    _pane = case[5] if len(case) > 5 else 7777
    _pgrep = case[6] if len(case) > 6 else 4242
    _argvp = case[7] if len(case) > 7 else (4242,)
    _otherw = case[8] if len(case) > 8 else None
    box = _farm(rt, alive, kill_config=_kill, pane_pid=_pane,
                pgrep_pid=_pgrep, core_argv_pids=_argvp, other_window_pid=_otherw)
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
