#!/usr/bin/env python3
"""Tests for scripts/restart-voice-agent.sh's guarded lock migration (impl
plan WS1 Step 5, amendments S4/U1/R3).

The stale-pid block must delegate the WHOLE kill-and-replace transaction to
`voice-lock.py takeover` (validate → TERM → wait → KILL → revalidate → unlink
under the held fcntl guard) — no shell-side `kill`, never `rm -f`.

Cases: legacy bare-pid lock with a dead owner → cleared; structured dev-shape
and packaged-shape locks with live validated owners → killed + stolen;
malformed lock → left in place with a WARN; live structured lock whose owner
is a real running (non-listening / unvalidated) dummy process → never
removed, never signaled; launchd job not loaded → abort BEFORE the takeover
(nothing would respawn a killed agent on such a host — amendment T4 gate).

`launchctl` and `lsof` are PATH shims: launchctl records its invocation and
flips a marker; lsof reports a configured "listener" pid before the marker and
a fresh dummy pid after it (so the script's etime verification passes fast).

Run: python3 tests/restart-voice-agent-lock.test.py
Exit: 0 = all pass, 1 = failure
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "restart-voice-agent.sh"
PY = sys.executable or "python3"

failures = []


def check(name, cond, detail=""):
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {name}" + ("" if cond else f" — {detail}"))
    if not cond:
        failures.append(name)


def my_start_time_ms(pid):
    out = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True
    ).stdout.strip()
    return int(time.mktime(time.strptime(out)) * 1000)


def structured_lock(pidfile, pid, start_ms, entry, workspace):
    pidfile.write_text(
        json.dumps(
            {
                "v": 1,
                "lockId": f"vl1-{uuid.uuid4()}",
                "pid": pid,
                "startTimeMs": start_ms,
                "entry": str(entry),
                "workspace": str(workspace),
            }
        )
        + "\n"
    )


def spawn_dummy(entry=None):
    """A live process; with `entry`, its argv carries that path token (the
    stand-in for a voice-agent whose args name the entry point)."""
    args = [PY, "-c", "import time; time.sleep(120)"]
    if entry is not None:
        args.append(str(entry))
    return subprocess.Popen(args, start_new_session=True)


class Lab:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="restart-va-lock-"))
        self.ws = self.tmp / "workspace"
        self.ws.mkdir()
        self.state = self.tmp / "shim-state"
        self.state.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "launchctl").write_text(
            "#!/bin/bash\n"
            'echo "launchctl-stub: $*" >> "$SHIM_STATE_DIR/launchctl.log"\n'
            '# SHIM_NO_JOB=1 emulates a host where the launchd job is not loaded.\n'
            'if [ "$1" = "print" ] && [ -n "${SHIM_NO_JOB:-}" ]; then exit 1; fi\n'
            'if [ "$1" = "kickstart" ]; then touch "$SHIM_STATE_DIR/kickstarted"; fi\n'
            "exit 0\n"
        )
        (self.bin / "launchctl").chmod(0o755)
        (self.bin / "lsof").write_text(
            "#!/bin/bash\n"
            "# emulates: lsof -nP -tiTCP:9900 -sTCP:LISTEN\n"
            'if [ -f "$SHIM_STATE_DIR/kickstarted" ]; then\n'
            '  [ -n "${SHIM_FRESH_PID:-}" ] && echo "$SHIM_FRESH_PID"\n'
            "else\n"
            '  [ -n "${SHIM_OLD_PID:-}" ] && echo "$SHIM_OLD_PID"\n'
            "fi\n"
            "exit 0\n"
        )
        (self.bin / "lsof").chmod(0o755)
        locks = self.ws / "state" / "locks"
        locks.mkdir(parents=True)
        self.pidfile = locks / "voice-agent.pid"
        self.legacy_pidfile = self.ws / ".voice-agent.pid"
        self.procs = []

    def run_script(self, old_pid="", fresh=True, no_job=False):
        fresh_proc = None
        env = dict(os.environ)
        env.update(
            {
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "SHIM_STATE_DIR": str(self.state),
                "SHIM_OLD_PID": str(old_pid),
                "SUTANDO_WORKSPACE": str(self.ws),
                "SUTANDO_TEST_MODE": "1",
            }
        )
        if no_job:
            env["SHIM_NO_JOB"] = "1"
        if fresh:
            fresh_proc = spawn_dummy()
            self.procs.append(fresh_proc)
            env["SHIM_FRESH_PID"] = str(fresh_proc.pid)
        # Reset the per-run marker.
        marker = self.state / "kickstarted"
        if marker.exists():
            marker.unlink()
        return subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=90
        )

    def cleanup(self):
        for p in self.procs:
            try:
                p.kill()
                p.wait(timeout=5)
            except Exception:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)


def main():
    lab = Lab()
    entry_dev = REPO / "src" / "voice-agent.ts"
    entry_pkg = REPO / "dist" / "voice-agent.js"
    try:
        # --- legacy bare-pid lock, dead owner → cleared ---
        print("legacy dead owner:")
        dead = spawn_dummy()
        dead_pid = dead.pid
        dead.kill()
        dead.wait()
        lab.pidfile.write_text(f"{dead_pid}\n")
        p = lab.run_script()
        check("script succeeds", p.returncode == 0, p.stdout + p.stderr)
        check("legacy dead lock cleared", not lab.pidfile.exists())
        check("guarded takeover reported", "guarded lock takeover" in p.stdout, p.stdout)

        # --- structured dev-shape lock, live validated owner → killed + stolen ---
        print("structured dev shape:")
        agent = spawn_dummy(entry_dev)
        lab.procs.append(agent)
        structured_lock(lab.pidfile, agent.pid, my_start_time_ms(agent.pid), entry_dev, lab.ws)
        p = lab.run_script(old_pid=agent.pid)
        check("script succeeds", p.returncode == 0, p.stdout + p.stderr)
        check("dev-shape owner killed", agent.poll() is not None)
        check("dev-shape lock stolen", not lab.pidfile.exists())
        check('takeover result says "replaced"', '"replaced"' in p.stdout, p.stdout)

        # --- structured packaged-shape lock → killed + stolen ---
        print("structured packaged shape:")
        agent = spawn_dummy(entry_pkg)
        lab.procs.append(agent)
        structured_lock(lab.pidfile, agent.pid, my_start_time_ms(agent.pid), entry_pkg, lab.ws)
        p = lab.run_script(old_pid=agent.pid)
        check("script succeeds", p.returncode == 0, p.stdout + p.stderr)
        check("packaged-shape owner killed", agent.poll() is not None)
        check("packaged-shape lock stolen", not lab.pidfile.exists())

        # --- transition fallback (#2722): a pre-move agent's root lock must
        # stay reachable, or the restart path silently cannot stop it.
        print("legacy-root fallback (pre-move agent):")
        agent = spawn_dummy(entry_dev)
        lab.procs.append(agent)
        structured_lock(lab.legacy_pidfile, agent.pid, my_start_time_ms(agent.pid), entry_dev, lab.ws)
        p = lab.run_script(old_pid=agent.pid)
        check("script succeeds", p.returncode == 0, p.stdout + p.stderr)
        check("pre-move owner killed via fallback", agent.poll() is not None)
        check("legacy lock stolen", not lab.legacy_pidfile.exists())
        check("fallback announced on stderr", "transition window" in p.stderr, p.stderr)
        check("canonical path untouched", not lab.pidfile.exists())

        # --- canonical wins when BOTH exist: without this control the rows
        # above are consistent with a resolver that always answers legacy.
        print("canonical-first when both exist:")
        agent = spawn_dummy(entry_dev)
        lab.procs.append(agent)
        structured_lock(lab.pidfile, agent.pid, my_start_time_ms(agent.pid), entry_dev, lab.ws)
        lab.legacy_pidfile.write_text("{stale legacy junk")
        p = lab.run_script(old_pid=agent.pid)
        check("script succeeds", p.returncode == 0, p.stdout + p.stderr)
        check("canonical owner killed", agent.poll() is not None)
        check("canonical lock stolen", not lab.pidfile.exists())
        check("no fallback line when canonical exists", "transition window" not in p.stderr, p.stderr)
        lab.legacy_pidfile.unlink()

        # --- malformed lock → left in place, WARN, no rm -f ---
        print("malformed lock:")
        lab.pidfile.write_text("{definitely-not json")
        p = lab.run_script()
        check("script still proceeds to kickstart", "kickstart" in p.stdout, p.stdout)
        check("malformed lock left in place", lab.pidfile.exists())
        check("script warns takeover-blocked", "takeover-blocked" in p.stdout, p.stdout)

        # --- live structured lock, unvalidated owner → NEVER removed ---
        # The dummy is a real running process but NOT the :9900 listener (the
        # lsof shim reports no listener), so U1's cross-check blocks: no
        # signal, lock untouched.
        print("live unvalidated owner:")
        bystander = spawn_dummy(entry_dev)
        lab.procs.append(bystander)
        structured_lock(lab.pidfile, bystander.pid, my_start_time_ms(bystander.pid), entry_dev, lab.ws)
        p = lab.run_script(old_pid="")  # no listener reported pre-kickstart
        check("live lock never removed", lab.pidfile.exists())
        check("live owner never signaled", bystander.poll() is None)
        check("blocked is a WARN, not a failure of the lock phase", "takeover-blocked" in p.stdout, p.stdout)
        bystander.kill()
        bystander.wait()

        # --- launchd job not loaded → abort BEFORE the takeover, kill nothing ---
        # With voice-config-switch and health-check --fix routed through this
        # wrapper (amendment T4), a plain dev checkout (startup.sh-launched
        # agent, no launchd job) must not have its working agent killed with
        # nothing left to respawn it.
        print("no launchd job:")
        agent = spawn_dummy(entry_dev)
        lab.procs.append(agent)
        structured_lock(lab.pidfile, agent.pid, my_start_time_ms(agent.pid), entry_dev, lab.ws)
        p = lab.run_script(old_pid=agent.pid, no_job=True)
        check("aborts non-zero when the job is not loaded", p.returncode == 5, str(p.returncode))
        check("abort names the remedy", "bash src/restart.sh" in p.stdout, p.stdout)
        time.sleep(0.2)
        check("no-job abort signals nothing", agent.poll() is None)
        check("no-job abort leaves the lock", lab.pidfile.exists())
        check("no kickstart attempted", not (lab.state / "kickstarted").exists())
        agent.kill()
        agent.wait()
        lab.pidfile.unlink()
    finally:
        lab.cleanup()

    if failures:
        print(f"\n{len(failures)} failure(s): {failures}")
        return 1
    print("\nAll restart-voice-agent lock-migration tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
