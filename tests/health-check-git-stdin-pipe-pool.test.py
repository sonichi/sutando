#!/usr/bin/env python3
"""The carrier check-ignore probe must finish while macOS's pipe-buffer pool is spent.

When the pool is spent, a new pipe holds 512 bytes and cannot grow, yet poll()
still reports it writable. `subprocess.run(input=)` then blocks in os.write() on
git's stdin while git blocks writing its stdout, and `timeout=` never fires.

The probe runs in a child with a wall-clock bound; needing the kill is the
failure. Spending the pool limits every new pipe on the HOST to 512 bytes for
the run (~1 s when fixed, BOUND_S when broken), so it is opt-in:

    SUTANDO_TEST_PIPE_POOL=1 python3 tests/health-check-git-stdin-pipe-pool.test.py

Skipped off macOS, without the opt-in, and when the pool cannot be spent.
"""

from _helpers.hermetic_env import isolate_os_environ, isolated_child_env

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import atexit
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "health-check.py"
BOUND_S = 20

GENV = isolated_child_env(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
            GIT_CONFIG_NOSYSTEM="1")
GIT = ["git", "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "core.fsmonitor=false"]

# Fills pipes until a fresh one cannot grow past 512 bytes and keeps it so; exits
# (freeing the pool) on stdin EOF, i.e. when this test dies, or after argv[1] s.
HOLDER = textwrap.dedent("""
    import os, resource, select, sys, time
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(8000, hard), hard))
    hold = []
    def top_up():
        while len(hold) < 4000:
            r, w = os.pipe(); os.set_blocking(w, False); n = 0
            while True:
                try: n += os.write(w, b"x" * 512)
                except BlockingIOError: break
            hold.append((r, w))
            if n <= 512: return True
        return False
    print("spent" if top_up() else "unspendable", flush=True)
    end = time.monotonic() + float(sys.argv[1])
    while time.monotonic() < end and not select.select([sys.stdin], [], [], 0.05)[0]:
        top_up()
""")

PROBE = textwrap.dedent("""
    import importlib.util, sys
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("hc", sys.argv[1])
    m = importlib.util.module_from_spec(spec)
    try: spec.loader.exec_module(m)
    except SystemExit: pass
    target = Path(sys.argv[2])
    print(m._carrier_target_verdict(target, target / "notes"))
""")

fails = []
children = []


def _reap():
    for p in children:
        if p.poll() is None:
            p.kill()
            p.wait()


atexit.register(_reap)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"       {detail}")
        fails.append(name)


def carrier_workspace(tmp: Path, nfiles: int) -> Path:
    ws = tmp / "workspace"
    ws.mkdir()
    subprocess.run(GIT + ["init", "-q", str(ws)], env=GENV, check=True)
    for i in range(nfiles):
        d = ws / "notes" / f"section-{i % 50:02d}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"a-reasonably-long-note-name-{i:05d}.md").write_text("x\n")
    (ws / ".git" / "info" / "exclude").write_text("*\n!notes/\n!notes/**\n")
    return ws


def run_bounded(target: Path) -> "tuple[bool, str, float]":
    """(finished, stdout, seconds) for one probe run with the pool spent."""
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(BOUND_S + 5)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    children.append(holder)
    try:
        if holder.stdout.readline().strip() != "spent":
            return True, "SKIP", 0.0
        t0 = time.monotonic()
        probe = subprocess.Popen([sys.executable, "-c", PROBE, str(SRC), str(target)],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, text=True)
        children.append(probe)
        try:
            out, _ = probe.communicate(timeout=BOUND_S)
            return True, out.strip(), time.monotonic() - t0
        except subprocess.TimeoutExpired:
            probe.kill()
            probe.communicate()
            return False, "", time.monotonic() - t0
    finally:
        _reap()


if sys.platform != "darwin":
    print("skip: the spent-pool poll() mismatch is a macOS kernel behaviour")
    raise SystemExit(0)
if os.environ.get("SUTANDO_TEST_PIPE_POOL") != "1":
    print("skip: spends the host-wide pipe pool; set SUTANDO_TEST_PIPE_POOL=1 to run")
    raise SystemExit(0)

with tempfile.TemporaryDirectory() as td:
    ws = carrier_workspace(Path(td), nfiles=2000)
    done, out, took = run_bounded(ws)
    if out == "SKIP":
        print("skip: could not spend the pipe-buffer pool on this host")
        raise SystemExit(0)
    check("check-ignore probe finishes with the pipe pool spent", done,
          f"killed after {took:.0f}s: blocked writing git's stdin (a fixed probe takes ~1s)")
    check("...and reads carried", out.splitlines()[-1:] == ["carried"], f"got {out!r}")
    print(f"  probe took {took:.2f}s")

if fails:
    print(f"\n{len(fails)} FAILED: {fails}")
    raise SystemExit(1)
print("\nall ok")
