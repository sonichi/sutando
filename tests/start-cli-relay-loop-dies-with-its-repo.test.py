#!/usr/bin/env python3
"""The relay loop that start-cli.sh backgrounds must stop when its inputs vanish.

The launcher runs `bash -c '<loop>' relay-loop "$PY" "$REPO/src/core-supervisor-relay.py" …`
detached, with `$PY` an absolute path and `sleep` resolved from PATH. A test that
runs the real launcher from a temporary repo copy (shutdown-sentinel-immediate-exit-
controls) deletes that copy afterwards; the loop then has no interpreter and no
`sleep`, so `while true` spins at full CPU forever, appending two error lines per
iteration to /tmp/core-supervisor-relay.log (107 GB on one machine before it was
noticed). The same shape bites whenever the engine directory is removed from under
a running loop.

This test extracts the loop body from start-cli.sh and drives it directly:
  a) interpreter, script and `sleep` all gone -> exits promptly, no spin
  b) the relay is invoked with the launcher's argv and the loop ends once the
     script disappears
  c) a failing `sleep` ends the loop instead of degrading into a busy loop
  d) tied to a real tmux session: keeps running while it exists, exits 0 once
     it has been gone for three checks
  e) no socket given: the old inputs-only contract still governs the loop
  f) an ambiguous tmux failure (refused, unrecognised, empty stderr) is
     UNKNOWN, never a confirmed absence, so it cannot end the loop on its own
  g) structural: the loop delegates to tmux-probe-cli.py, no private copy of
     tmux's absence-message strings

Run: python3 tests/start-cli-relay-loop-dies-with-its-repo.test.py  (exit 0/1)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"
failures: list[str] = []


def _loop_body() -> str:
    src = LAUNCHER.read_text(encoding="utf-8")
    m = re.search(r"bash -c '([^']*)' \\\n\s*relay-loop ", src)
    assert m, "relay loop launch not found in start-cli.sh — did its shape change?"
    return m.group(1)


def _run(body: str, argv: list[str], path: str, timeout: float = 5.0):
    """Returns (rc, stderr) or (None, stderr) when the loop outlived the timeout."""
    p = subprocess.Popen(["/bin/bash", "-c", body, "relay-loop", *argv],
                         env={"PATH": path}, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        _, err = p.communicate(timeout=timeout)
        return p.returncode, err
    except subprocess.TimeoutExpired:
        p.kill()
        _, err = p.communicate()
        return None, err


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _dual_python3_stub(calls: Path, srcdir: Path, probes: Path) -> str:
    """A fake python3 that counts relay calls AND records each classification
    probe's exit code (0 PRESENT / 1 ABSENT / 2 UNKNOWN) to `probes`, real
    tmux-probe-cli.py (real tmux_probe.py copied beside it) doing the actual
    classification -- the loop's own delegation, exercised. Recording the
    exit code (not just a relay-call count) is what lets the caller assert on
    the loop's actual miss-streak invariant instead of a wall-clock-sensitive
    call-count delta -- the two can diverge whenever a probe that was already
    in flight (started before the session died, observed while it was still
    alive) lands after the caller's own "before" snapshot."""
    (srcdir / "tmux_probe.py").write_text((REPO / "src" / "tmux_probe.py").read_text())
    (srcdir / "tmux-probe-cli.py").write_text((REPO / "src" / "tmux-probe-cli.py").read_text())
    return (f'#!/bin/sh\ncase "$1" in\n'
            f'  */tmux-probe-cli.py) {sys.executable} "$@"; rc=$?; echo "$rc" >> "{probes}"; exit $rc ;;\n'
            f'  *) echo run >> "{calls}" ;;\nesac\n')


def check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        failures.append(f"{label}: {detail}")


body = _loop_body()

# a) the fixture is gone: no interpreter, no script, no `sleep` on PATH.
with tempfile.TemporaryDirectory() as td:
    gone = Path(td) / "gone"
rc, err = _run(body, [f"{gone}/bin/python3", f"{gone}/repo/src/core-supervisor-relay.py",
                      f"{gone}/ws/state/core-supervisor.json",
                      f"{gone}/ws/state/core-supervisor-relay.state",
                      f"{gone}/ws/state/last-owner-activity.json"],
               path=f"{gone}/bin")
check("a) loop exits when its repo and PATH are gone", rc is not None,
      f"still running after 5s; wrote {err.count(chr(10))} stderr lines — the busy loop")
check("a) no spin before exit", err.count("\n") <= 4,
      f"{err.count(chr(10))} stderr lines before exiting")

# b) the relay runs with the launcher's argv, and the loop ends once the script vanishes.
with tempfile.TemporaryDirectory() as td:
    binp, repo = Path(td) / "bin", Path(td) / "repo"
    binp.mkdir()
    (repo / "src").mkdir(parents=True)
    calls = Path(td) / "calls.log"
    script = repo / "src" / "core-supervisor-relay.py"
    script.write_text("# stand-in for the relay\n")
    # Records its argv, then removes the script: the next loop check must stop.
    _exe(binp / "python3", f'#!/bin/sh\necho "$@" >> "{calls}"\n/bin/rm -f "$1"\n')
    _exe(binp / "sleep", "#!/bin/sh\nexit 0\n")
    rc, err = _run(body, [str(binp / "python3"), str(script), "SIG", "STATE", "ACTIVE"],
                   path=str(binp))
    recorded = calls.read_text().splitlines() if calls.exists() else []
    check("b) loop exits once the relay script is gone", rc is not None,
          f"still running after 5s; relay invoked {len(recorded)} times; stderr: {err[-300:]}")
    check("b) relay invoked exactly once with the launcher's argv",
          recorded == [f"{script} --signal SIG --state-file STATE --active-from ACTIVE"],
          f"recorded {len(recorded)} calls; first: {recorded[:1]!r}")

# c) `sleep` failing must end the loop, not turn it into a busy loop.
with tempfile.TemporaryDirectory() as td:
    binp, repo = Path(td) / "bin", Path(td) / "repo"
    binp.mkdir()
    (repo / "src").mkdir(parents=True)
    calls = Path(td) / "calls.log"
    script = repo / "src" / "core-supervisor-relay.py"
    script.write_text("# stand-in for the relay\n")
    _exe(binp / "python3", f'#!/bin/sh\necho run >> "{calls}"\n')
    _exe(binp / "sleep", "#!/bin/sh\nexit 1\n")
    rc, err = _run(body, [str(binp / "python3"), str(script), "SIG", "STATE", "ACTIVE"],
                   path=str(binp))
    n = len(calls.read_text().splitlines()) if calls.exists() else 0
    check("c) a failing sleep ends the loop", rc is not None and rc != 0,
          f"rc={rc}; relay invoked {n} times")
    check("c) the relay ran once before the loop gave up", n == 1, f"relay invoked {n} times")

# d) tied to its core session: runs while the session exists, exits 0 once it has been
#    gone for three checks, so a scratch launch never leaves a loop behind.
import shutil
tmux = shutil.which("tmux")
if tmux is None:
    print("  skip d) tmux not installed — session-lifetime case not run")
else:
    with tempfile.TemporaryDirectory() as td:
        binp, repo = Path(td) / "bin", Path(td) / "repo"
        binp.mkdir()
        (repo / "src").mkdir(parents=True)
        calls = Path(td) / "calls.log"
        probes = Path(td) / "probes.log"
        script = repo / "src" / "core-supervisor-relay.py"
        script.write_text("# stand-in for the relay\n")
        _exe(binp / "python3", _dual_python3_stub(calls, repo / "src", probes))
        _exe(binp / "sleep", "#!/bin/sh\nexit 0\n")
        os.symlink(tmux, binp / "tmux")
        sock = Path(td) / "tmux.sock"
        subprocess.run([tmux, "-S", str(sock), "new-session", "-d", "-s", "scratch-core", "sleep 300"], check=True)
        try:
            p = subprocess.Popen(["/bin/bash", "-c", body, "relay-loop", str(binp / "python3"), str(script),
                                  "SIG", "STATE", "ACTIVE", str(sock), "scratch-core"],
                                 env={"PATH": str(binp)}, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            # Poll, not a flat sleep: exits the instant the relay call registers,
            # while a loaded runner still gets real margin for the probe chain.
            deadline = time.time() + 10.0
            n_before, alive_while_session = 0, True
            while time.time() < deadline:
                if p.poll() is not None:
                    alive_while_session = False
                    break
                n_before = len(calls.read_text().splitlines()) if calls.exists() else 0
                if n_before >= 1:
                    break
                time.sleep(0.05)
            check("d) the loop keeps running while its session exists", alive_while_session,
                  f"exited rc={p.returncode} with the session present; relay ran {n_before}x")
            check("d) the relay is invoked while the session exists", n_before >= 1, f"relay ran {n_before}x")
            subprocess.run([tmux, "-S", str(sock), "kill-server"], check=False)
            try:
                _, err = p.communicate(timeout=5)
                rc = p.returncode
            except subprocess.TimeoutExpired:
                p.kill()
                _, err = p.communicate()
                rc = None
            # Probe outcomes, not a relay-call delta: an in-flight probe at
            # kill-server can land an unrelated relay call after n_before.
            probe_hist = probes.read_text().splitlines() if probes.exists() else []
            check("d) the loop exits 0 once its session is gone", rc == 0,
                  f"rc={rc}; probes={probe_hist}; stderr: {err[-200:]}")
            tail_absent = 0
            for outcome in reversed(probe_hist):
                if outcome == "1":
                    tail_absent += 1
                else:
                    break
            check("d) exactly three consecutive ABSENT probes precede the exit",
                  tail_absent == 3, f"probe history: {probe_hist}")
        finally:
            subprocess.run([tmux, "-S", str(sock), "kill-server"], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# e) a launch that gives no socket keeps the old contract: only the inputs govern the loop.
with tempfile.TemporaryDirectory() as td:
    binp, repo = Path(td) / "bin", Path(td) / "repo"
    binp.mkdir()
    (repo / "src").mkdir(parents=True)
    calls = Path(td) / "calls.log"
    script = repo / "src" / "core-supervisor-relay.py"
    script.write_text("# stand-in for the relay\n")
    # PATH holds only the stubs, so the counter must name its tools absolutely.
    _exe(binp / "python3", f'#!/bin/sh\necho run >> "{calls}"\n[ "$(/usr/bin/wc -l < "{calls}")" -lt 3 ] || /bin/rm -f "$1"\n')
    _exe(binp / "sleep", "#!/bin/sh\nexit 0\n")
    rc, err = _run(body, [str(binp / "python3"), str(script), "SIG", "STATE", "ACTIVE"], path=str(binp))
    n = len(calls.read_text().splitlines()) if calls.exists() else 0
    check("e) without a socket the loop runs until its script is gone", rc is not None and n == 3,
          f"rc={rc}; relay ran {n}x")

# f) an ambiguous tmux failure (refused, unrecognised, empty stderr) is UNKNOWN,
#    never a confirmed absence, and must not advance the miss count.
with tempfile.TemporaryDirectory() as td:
    binp, repo = Path(td) / "bin", Path(td) / "repo"
    binp.mkdir()
    (repo / "src").mkdir(parents=True)
    calls = Path(td) / "calls.log"
    script = repo / "src" / "core-supervisor-relay.py"
    script.write_text("# stand-in for the relay\n")
    # Real classification delegation, plus a relay-call cap (6 -- a buggy
    # 3-strike loop would stop at 3) that removes the script to end the run.
    _exe(binp / "python3", f'#!/bin/sh\ncase "$1" in\n'
         f'  */tmux-probe-cli.py) exec {sys.executable} "$@" ;;\n'
         f'  *) echo run >> "{calls}"\n'
         f'     [ "$(/usr/bin/wc -l < "{calls}")" -lt 6 ] || /bin/rm -f "$1" ;;\n'
         f'esac\n')
    (repo / "src" / "tmux_probe.py").write_text((REPO / "src" / "tmux_probe.py").read_text())
    (repo / "src" / "tmux-probe-cli.py").write_text((REPO / "src" / "tmux-probe-cli.py").read_text())
    _exe(binp / "sleep", "#!/bin/sh\nexit 0\n")
    # Always fails with a message that matches none of the absence signatures.
    _exe(binp / "tmux", '#!/bin/sh\necho "unrecognised: permission or version refusal" >&2\nexit 1\n')
    rc, err = _run(body, [str(binp / "python3"), str(script), "SIG", "STATE", "ACTIVE",
                          "/tmp/does-not-matter.sock", "some-session"], path=str(binp), timeout=10.0)
    n = len(calls.read_text().splitlines()) if calls.exists() else 0
    check("f) an unrecognised tmux failure never ends the loop on its own",
          rc is not None and n == 6, f"rc={rc}; relay ran {n}x (an old 3-strike bug would stop at 3)")

# g) structural: the loop delegates to tmux-probe-cli.py and carries no
#    private copy of tmux's absence-message strings.
check("g) the loop body references the shared classification CLI",
      "tmux-probe-cli.py" in body, "no reference to tmux-probe-cli.py in the loop body")
check("g) the loop body carries none of tmux's absence-message text",
      not any(s in body for s in ("find session", "no server running", "No such file")),
      "the loop body still duplicates a tmux error string instead of delegating")

if failures:
    print("\n".join(f"  FAIL {f}" for f in failures))
else:
    print("  ok  the relay loop stops when its interpreter, script, sleep or core session are gone")
sys.exit(1 if failures else 0)
