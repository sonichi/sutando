#!/usr/bin/env python3
"""core_heartbeat: `.alive` must report the CORE's pid, and stop beating when the core is gone.

Regression for 2026-08-01. `write_beat` wrote `os.getpid()` — the *writer's* pid — while the
module docstring already claimed the field was the core's. The writer is started detached by
startup.sh (PPID 1), is never killed by restart.sh, and is only started `if ! pgrep`, so it
outlives every core restart. Consequence: `.alive` kept a FRESH mtime carrying a pid that was
never the core's, and a DEAD core read as healthy. Confirmed on two hosts (Pro: writer ELAPSED
2-01:49 spanning several core restarts).

Why it is urgent rather than tidy: #2333's review asks to prove a restarted service "publishes a
healthy heartbeat". Against the pre-fix instrument that clause CANNOT FAIL, so restart evidence
could come back green for the wrong reason.

Hermetic by construction — `_alive_path` is redirected into a tmpdir, so nothing here can touch a
real workspace (`$SUTANDO_WORKSPACE` is no longer honored as of v0.8, so setting it would NOT
isolate; the resolved path is what matters).
"""
import importlib
import json
import os
import sys
import tempfile
import pathlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
ch = importlib.import_module("core_heartbeat")
_REAL_CORE_PID = ch.core_pid   # kept: the monkeypatches below would otherwise
                              # make the "real function" tests exercise a fake

_fails = []
def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        _fails.append(name)


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    alive = tmp / "testhost.alive"
    ch.CORES_DIR = tmp
    ch._alive_path = lambda: alive
    # Prove the redirect actually took, or every assertion below is vacuous.
    check("ISOLATION: resolved .alive path is inside the tmpdir",
          str(ch._alive_path()).startswith(str(tmp)))

    # --- payload: pid is the CORE's, writer's pid kept separately ------------
    ch.core_pid = lambda socket_path=None, session=None: 424242
    ch.write_beat()
    rec = json.loads(alive.read_text())
    check("pid is the CORE's pane pid (424242)", rec.get("pid") == 424242)
    check("pid is NOT the writer's pid (the pre-fix behaviour)", rec.get("pid") != os.getpid())
    check("heartbeat_pid records the writer", rec.get("heartbeat_pid") == os.getpid())
    check("pid and heartbeat_pid are different processes", rec["pid"] != rec["heartbeat_pid"])
    check("schema_version advertises the contract change", rec.get("schema_version", 0) >= 3)
    check("socket is still recorded (unchanged contract)", "socket" in rec)

    # --- fail-open: tmux unusable must not blank the field or stop reporting -
    ch.core_pid = lambda socket_path=None, session=None: None
    alive.unlink()
    ch.write_beat()
    rec2 = json.loads(alive.read_text())
    check("fail-open: a beat is still written when the core pid is unknown", alive.exists())
    check("fail-open: pid falls back to the writer's own", rec2.get("pid") == os.getpid())

    # --- the loop stops once an OBSERVED core disappears ---------------------
    seen = {"n": 0}
    def vanishing(socket_path=None):
        seen["n"] += 1
        return 999999 if seen["n"] <= 2 else None
    ch.core_pid = vanishing
    ch.write_beat = lambda status="running": None
    alive.write_text("{}")
    rc = ch.run_forever(interval=0.01)
    check("run_forever returns 0 when the core vanishes", rc == 0)
    check("it removed .alive so readers see the core leave", not alive.exists())

    # --- CONTROL: a core that stays PRESENT must NOT trigger the vanish path -
    # Without this, "stops beating when the core is gone" would also pass if the
    # loop simply stopped unconditionally — the assertion has to be able to fail
    # in the opposite direction too.
    calls = {"n": 0}
    def live(socket_path=None):
        calls["n"] += 1
        if calls["n"] > 3:          # let it beat a few times, then ask it to stop
            ch._SHUTDOWN_REQUESTED = True
        return 111111
    ch.core_pid = live
    ch._SHUTDOWN_REQUESTED = False
    alive.write_text("{}")
    rc2 = ch.run_forever(interval=0.01)
    check("CONTROL: a live core exits via the shutdown flag, not the vanish path", rc2 == 0)
    check("CONTROL: .alive SURVIVES while the core is present", alive.exists())

# --- core_pid() itself: real subprocess, fake tmux on PATH ------------------
ch.core_pid = _REAL_CORE_PID   # undo the monkeypatches; without this these two
                               # assertions would call the stub and pass vacuously
with tempfile.TemporaryDirectory() as td2:
    b = Path(td2) / "bin"; b.mkdir()
    # NON-Claude runtime, so the scoped-pane path is legitimately reachable.
    # Under the repaired contract a session identified as Claude with no
    # matching `claude --name <sess>` process resolves ABSENT and never reaches
    # list-panes — that arm is asserted separately below.
    (b / "tmux").write_text(
        "#!/bin/sh\n"
        "for a in \"$@\"; do\n"
        "  case \"$a\" in show-environment) echo 'SUTANDO_CORE_RUNTIME=codex'; exit 0 ;; esac\n"
        "done\n"
        "echo 31337\n")
    (b / "tmux").chmod(0o755)
    # Stub pgrep too: without it this case calls the REAL pgrep, and on a host
    # that is actually running a core (`claude --name sutando-core`) it would
    # return that live pid instead of 31337. Host-dependent, green in CI only.
    (b / "pgrep").write_text("#!/bin/sh\nexit 1\n"); (b / "pgrep").chmod(0o755)
    os.environ["PATH"] = f"{b}:{os.environ['PATH']}"
    check("core_pid() parses the pane pid from tmux (non-Claude runtime)",
          _REAL_CORE_PID("/tmp/x.sock") == 31337)
    (b / "tmux").write_text("#!/bin/sh\nexit 1\n")
    check("core_pid() returns None when tmux fails (no server / socket gone)",
          _REAL_CORE_PID("/tmp/x.sock") is None)

# --- REGRESSION 1: heartbeat starts BEFORE the core (cold boot) --------------
# startup.sh:632 launches this process before the core launcher, so `saw_core`
# must ARM on first sighting, not be decided once up front. Against the reviewed
# head this loop wrote three beats and left .alive present.
with tempfile.TemporaryDirectory() as td3:
    tmp3 = pathlib.Path(td3); a3 = tmp3 / "h.alive"
    ch.CORES_DIR = tmp3; ch._alive_path = lambda: a3
    seq = {"n": 0}
    def late_then_gone(socket_path=None, session=None):
        seq["n"] += 1
        if seq["n"] <= 2:
            return None        # cold boot: no core yet
        if seq["n"] <= 5:
            return 777         # core comes up
        return None            # ...and dies
    ch.core_pid = late_then_gone
    # The bound lives in write_beat, NOT in the core_pid stub: on the pre-fix
    # module `saw_core` is decided once and stays False, so core_pid() is never
    # called again and a counter there would never advance — the loop would hang
    # instead of failing. write_beat is called every iteration on BOTH versions,
    # so this terminates either way and lets the .alive assertion be the verdict.
    beats = {"n": 0}
    def bounded_beat(status="running"):
        beats["n"] += 1
        a3.write_text("{}")
        if beats["n"] > 12:
            ch._SHUTDOWN_REQUESTED = True
    ch.write_beat = bounded_beat
    ch._SHUTDOWN_REQUESTED = False
    rc3 = ch.run_forever(interval=0.01)
    check("cold boot: gate arms on a LATER core and still fires when it dies", rc3 == 0)
    check("cold boot: .alive removed once the late core vanished", not a3.exists())
    check("cold boot: it did not stop before the core ever appeared", seq["n"] > 3)

# --- NEGATIVE CONTROL: a core that NEVER starts must never read healthy ------
# Before this guard, every iteration called write_beat(), whose documented
# resolver-failure fallback records the heartbeat writer pid. A failed cold
# boot therefore refreshed .alive forever despite no core ever being observed.
with tempfile.TemporaryDirectory() as td_never:
    tmp_never = pathlib.Path(td_never); a_never = tmp_never / "h.alive"
    ch.CORES_DIR = tmp_never; ch._alive_path = lambda: a_never
    a_never.write_text("{}")       # stale record from an earlier process
    probes = {"n": 0}
    def never_appears(socket_path=None, session=None):
        probes["n"] += 1
        if probes["n"] >= 5:
            ch._SHUTDOWN_REQUESTED = True
        return None
    writes = {"n": 0}
    def count_beat(status="running"):
        writes["n"] += 1
        a_never.write_text("{}")
    ch.core_pid = never_appears
    ch.write_beat = count_beat
    ch._SHUTDOWN_REQUESTED = False
    rc_never = ch.run_forever(interval=0.01)
    check("never-started core: loop remains responsive to shutdown", rc_never == 0)
    check("never-started core: no heartbeat is published", writes["n"] == 0)
    check("never-started core: stale .alive is removed", not a_never.exists())

# --- REGRESSION 2: sibling pane / watcher session must not read as the core ---
# Codex runs `${SESSION}-watcher` on the SAME socket; Claude preserves sibling
# WINDOWS in the core session. A first-pane-wins lookup returns those.
with tempfile.TemporaryDirectory() as td4:
    b4 = pathlib.Path(td4) / "bin"; b4.mkdir(parents=True)
    # tmux stub: the core session does NOT exist; only the watcher does.
    (b4 / "tmux").write_text(
        "#!/bin/sh\n"
        "for a in \"$@\"; do\n"
        "  case \"$a\" in =sutando-core) exit 1 ;; esac\n"   # exact session absent
        "done\n"
        "echo 99999\n")                                         # any pane query would yield this
    (b4 / "tmux").chmod(0o755)
    # no `claude --name sutando-core` process either
    (b4 / "pgrep").write_text("#!/bin/sh\nexit 1\n"); (b4 / "pgrep").chmod(0o755)
    os.environ["PATH"] = f"{b4}:{os.environ['PATH']}"
    got = _REAL_CORE_PID("/tmp/x.sock")
    check("dead core + live watcher/sibling on the socket -> core_pid() is None (not 99999)",
          got is None)

# --- REAL core_pid() through fake tmux+pgrep: every branch, not just the stub ---
# The regressions above monkeypatch core_pid, which is right for the LOOP but leaves
# the resolver's own branches unexecuted (diff-cover flagged 14 such lines).
def _stub(d: pathlib.Path, name: str, body: str):
    d.mkdir(parents=True, exist_ok=True)
    f = d / name; f.write_text(body); f.chmod(0o755); return f

_ORIG_PATH = os.environ["PATH"]
with tempfile.TemporaryDirectory() as td5:
    b = pathlib.Path(td5) / "bin"
    # session EXISTS (exit 0 for =sutando-core), pgrep finds the core claude
    _stub(b, "tmux", "#!/bin/sh\nexit 0\n")
    # Portable toolchain (2026-08-02, #2488 review): `pgrep -x` yields bare pids
    # on BOTH Linux and macOS; argv comes from `ps`. The old fake emitted the
    # Linux-only `pgrep -a` shape ("PID argv"), which is exactly the assumption
    # that made this branch dead on macOS — the fixture encoded the bug.
    _stub(b, "pgrep", "#!/bin/sh\necho 4242\n")
    _stub(b, "ps", "#!/bin/sh\necho 'claude --name sutando-core --foo'\n")
    os.environ["PATH"] = f"{b}:{_ORIG_PATH}"
    check("REAL: pgrep match on `--name <session>` returns the core pid",
          _REAL_CORE_PID("/tmp/s.sock") == 4242)

    # same, the `--name=<session>` spelling
    _stub(b, "pgrep", "#!/bin/sh\necho 4243\n")
    _stub(b, "ps", "#!/bin/sh\necho 'claude --name=sutando-core'\n")
    check("REAL: the `--name=<session>` spelling also matches",
          _REAL_CORE_PID("/tmp/s.sock") == 4243)

    def _tmux_runtime(rt: str) -> str:
        return ("#!/bin/sh\n"
                "for a in \"$@\"; do\n"
                "  case \"$a\" in\n"
                f"    show-environment) echo 'SUTANDO_CORE_RUNTIME={rt}'; exit 0 ;;\n"
                "    list-panes) echo 777; exit 0 ;;\n"
                "  esac\n"
                "done\n"
                "exit 0\n")

    # --- NEGATIVE CONTROLS: a PREFIXED session name must NOT match -------------
    # qingyun-wu, #2488 @8de9021e: the match was `f"--name {sess}" in args`, a
    # substring test, so target `sutando-core` was also satisfied by
    # `claude --name sutando-core-watcher`. They reproduced the resolver
    # returning that pid with no exactly-named core alive — a prefixed sibling
    # keeping .alive fresh over a dead core, i.e. the bug this PR removes.
    # Each case below returns the WRONG pid on the pre-fix module.
    for spelling, argv in (
        ("--name <prefixed>",  "claude --name sutando-core-watcher --resume"),
        ("--name=<prefixed>",  "claude --name=sutando-core-secondary"),
        ("--name <suffixed>",  "claude --name x-sutando-core"),
        ("--name absent",      "claude --resume sutando-core"),
    ):
        _stub(b, "pgrep", "#!/bin/sh\necho 4242\n")
        _stub(b, "ps", f"#!/bin/sh\necho '{argv}'\n")
        _stub(b, "tmux", _tmux_runtime("claude"))
        check(f"NEGATIVE: {spelling} must NOT be taken for the core",
              _REAL_CORE_PID("/tmp/s.sock") is None)

    # POSITIVE control in the same block: the exact name still resolves, so the
    # negatives above cannot be passing merely because everything returns None.
    # PID-AWARE on purpose. A blanket `ps` that answers with the core's argv for
    # EVERY pid cannot distinguish "this pane is the core" from "this pane is a
    # leftover shell", so it silently decides which resolver branch wins. Here
    # only 4242 (pgrep's hit) is the core; the session's pane 777 is a shell, so
    # the session-scoped lookup correctly declines and this stays a test of the
    # process-name branch — which is what the assertion below is about.
    _stub(b, "ps", "#!/bin/sh\nlast=\"\"\nfor a in \"$@\"; do last=\"$a\"; done\n"
                   "case \"$last\" in 4242) echo 'claude --name sutando-core --resume' ;; "
                   "*) echo '-zsh' ;; esac\n")
    check("CONTROL: the EXACT session name still resolves (negatives aren't vacuous)",
          _REAL_CORE_PID("/tmp/s.sock") == 4242)


    # --- the repaired contract: the SESSION RUNTIME decides what "no matching
    # claude process" means. Claude -> ABSENT (a preserved sibling window must
    # never resurrect a dead core, john-the-dev on #2488). Non-Claude -> the
    # scoped-pane fallback still applies. Each Claude arm below is paired with
    # the identical state on a non-Claude session, so the assertion proves the
    # DISCRIMINATION and not merely that something returned None.
    # a NON-core claude must not match (someone else's claude on the box)
    _stub(b, "pgrep", "#!/bin/sh\necho 999\n")
    _stub(b, "ps", "#!/bin/sh\necho 'claude --name something-else'\n")
    _stub(b, "tmux", _tmux_runtime("claude"))
    check("REAL: a claude for a DIFFERENT session resolves ABSENT on a Claude session",
          _REAL_CORE_PID("/tmp/s.sock") is None)
    _stub(b, "tmux", _tmux_runtime("codex"))
    check("CONTROL: identical state on a non-Claude session still uses the scoped pane",
          _REAL_CORE_PID("/tmp/s.sock") == 777)

    # pgrep finds nothing at all -> same discrimination
    _stub(b, "pgrep", "#!/bin/sh\nexit 1\n")
    _stub(b, "tmux", _tmux_runtime("claude"))
    check("REAL: no core claude on a Claude session -> ABSENT (no pane resurrection)",
          _REAL_CORE_PID("/tmp/s.sock") is None)
    _stub(b, "tmux", _tmux_runtime("codex"))
    check("CONTROL: no core claude on a non-Claude session -> scoped pane pid",
          _REAL_CORE_PID("/tmp/s.sock") == 777)

    # pane list empty -> None (not a stale value). Pinned to a non-Claude
    # runtime deliberately: on a Claude session the resolver short-circuits
    # ABOVE list-panes, so this would pass without ever exercising the branch
    # it names — a test that cannot fail.
    _stub(b, "tmux", "#!/bin/sh\nfor a in \"$@\"; do case $a in show-environment) echo 'SUTANDO_CORE_RUNTIME=codex'; exit 0;; list-panes) exit 0;; esac; done\nexit 0\n")
    check("REAL: session exists but NO panes -> None", _REAL_CORE_PID("/tmp/s.sock") is None)

    # list-panes itself fails -> None
    _stub(b, "tmux", "#!/bin/sh\nfor a in \"$@\"; do case $a in show-environment) echo 'SUTANDO_CORE_RUNTIME=codex'; exit 0;; list-panes) exit 1;; esac; done\nexit 0\n")
    check("REAL: list-panes failure -> None", _REAL_CORE_PID("/tmp/s.sock") is None)
os.environ["PATH"] = _ORIG_PATH

# tmux BINARY ABSENT entirely -> _tmux swallows the OSError and core_pid is None.
# PATH is emptied rather than stubbed, so this exercises the except branch for real.
with tempfile.TemporaryDirectory() as td6:
    os.environ["PATH"] = td6
    check("REAL: tmux binary missing -> _tmux returns None, core_pid None (no crash)",
          _REAL_CORE_PID("/tmp/s.sock") is None)
os.environ["PATH"] = _ORIG_PATH

# --- the three defensive branches, which only a hostile environment reaches ----
with tempfile.TemporaryDirectory() as td7:
    b7 = pathlib.Path(td7) / "bin"; b7.mkdir(parents=True)
    _stub(b7, "tmux", "#!/bin/sh\nfor a in \"$@\"; do case $a in list-panes) echo 555; exit 0;; esac; done\nexit 0\n")
    # pgrep prints a NON-NUMERIC first token before the real row. Without the
    # `continue` this would crash on int(); with it, the real row still matches.
    # `pgrep -x` should print only pids, but a hostile/odd binary could emit a
    # header row; the `isdigit()` guard must skip it and still match the real pid.
    _stub(b7, "pgrep", "#!/bin/sh\necho 'PID'\necho 4321\n")
    # PID-aware for the same reason as the control above: pane 555 must read as a
    # shell so the session-scoped lookup declines and execution actually reaches
    # the pgrep branch whose `isdigit()` guard this case exists to exercise.
    _stub(b7, "ps", "#!/bin/sh\nlast=\"\"\nfor a in \"$@\"; do last=\"$a\"; done\n"
                    "case \"$last\" in 4321) echo 'claude --name sutando-core' ;; "
                    "*) echo '-zsh' ;; esac\n")
    os.environ["PATH"] = f"{b7}:{_ORIG_PATH}"
    check("a non-numeric pgrep line is skipped, the real row still matches",
          _REAL_CORE_PID("/tmp/s.sock") == 4321)
os.environ["PATH"] = _ORIG_PATH

with tempfile.TemporaryDirectory() as td_ps_fail:
    b_ps_fail = pathlib.Path(td_ps_fail) / "bin"; b_ps_fail.mkdir(parents=True)
    _stub(b_ps_fail, "tmux", "#!/bin/sh\nexit 0\n")
    _stub(b_ps_fail, "pgrep", "#!/bin/sh\necho 111\necho 222\n")
    _stub(b_ps_fail, "ps", "#!/bin/sh\n[ \"$4\" = 111 ] && exit 1\necho 'claude --name sutando-core'\n")
    os.environ["PATH"] = f"{b_ps_fail}:{_ORIG_PATH}"
    check("a failed ps lookup is skipped and the next matching pid resolves",
          _REAL_CORE_PID("/tmp/s.sock") == 222)
os.environ["PATH"] = _ORIG_PATH

with tempfile.TemporaryDirectory() as td8:
    b8 = pathlib.Path(td8) / "bin"; b8.mkdir(parents=True)
    # tmux PRESENT but pgrep ABSENT -> the subprocess call raises, the except
    # swallows it, and resolution falls through to the pane path rather than
    # propagating an exception out of a heartbeat.
    # Non-Claude runtime, so the pane path is the correct destination: on a
    # Claude session the resolver returns ABSENT before list-panes, and this
    # case would assert None while claiming to prove the exception was swallowed.
    _stub(b8, "tmux", "#!/bin/sh\nfor a in \"$@\"; do case $a in show-environment) echo 'SUTANDO_CORE_RUNTIME=codex'; exit 0;; list-panes) echo 556; exit 0;; esac; done\nexit 0\n")
    os.environ["PATH"] = str(b8)
    check("pgrep missing entirely -> swallowed, falls through to the pane path",
          _REAL_CORE_PID("/tmp/s.sock") == 556)
os.environ["PATH"] = _ORIG_PATH

# When tmux cannot report a session-owned runtime, the repo config remains the
# bounded fallback. A multiline value is defensive input: only its first line
# may become the runtime identifier.
_REAL_TMUX = ch._tmux
sc = importlib.import_module("sutando_config")
_REAL_RESOLVE_CORE_RUNTIME = sc.resolve_core_runtime
ch._tmux = lambda *args, **kwargs: None
sc.resolve_core_runtime = lambda: "codex\nignored"
check("session runtime falls back to the first non-empty config line",
      ch._session_runtime("/tmp/s.sock", "sutando-core") == "codex")
sc.resolve_core_runtime = _REAL_RESOLVE_CORE_RUNTIME
ch._tmux = _REAL_TMUX

# A transient heartbeat write failure must be logged and retried rather than
# killing the daemon. Bound the loop through the normal shutdown flag.
_write_fail_calls = {"n": 0}
def _present_then_stop(socket_path=None, session=None):
    _write_fail_calls["n"] += 1
    if _write_fail_calls["n"] >= 2:
        ch._SHUTDOWN_REQUESTED = True
    return 4242
def _write_fails(status="running"):
    raise OSError("transient write failure")
ch.core_pid = _present_then_stop
ch.write_beat = _write_fails
ch._SHUTDOWN_REQUESTED = False
check("transient write failure does not terminate the heartbeat loop",
      ch.run_forever(interval=0.01) == 0)

# .alive unlink raising must not crash the vanish path — the beat is stopping
# either way, and an unremovable file is not worth a traceback in a daemon.
class _Boom:
    def unlink(self, missing_ok=False):
        raise OSError("read-only filesystem")
ch.core_pid = lambda socket_path=None, session=None: None
ch._alive_path = lambda: _Boom()
ch.write_beat = lambda status="running": None
ch._SHUTDOWN_REQUESTED = False
_seen = {"n": 0}
def _once(socket_path=None, session=None):
    _seen["n"] += 1
    return 4242 if _seen["n"] == 1 else None
ch.core_pid = _once
check("vanish path survives an unlink that raises (returns 0, no traceback)",
      ch.run_forever(interval=0.01) == 0)

print()
if _fails:
    print(f"{len(_fails)} test(s) FAILED: {_fails}")
    sys.exit(1)
print("all tests passed — .alive reports the core's pid and stops when the core is gone")
