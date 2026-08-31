#!/usr/bin/env python3
"""Tests for scripts/voice-lock.py — the guarded PID-lock helper (impl plan
WS1 Step 3; amendments R3/S4/U1/Z1).

Covers: fresh acquire; acquire vs. live holder (exit 7); stale structured
lock replaced; stale legacy bare-pid replaced; PID-reuse handling (steal
treats a start-time mismatch as stale; acquire blocks on a live legacy pid
with no start-time evidence); malformed/partial JSON = "unknown" and acquire
refuses to clobber while live evidence exists; two concurrent acquires →
exactly one winner; steal against a live owner refuses; guard-hold releases
on holder death; takeover: graceful TERM (no KILL), TERM-ignored escalation,
concurrent replacement, listener/entry/PGID mismatch → blocked with no signal
(U1), owned mode kills a non-listening process group (Z1).

Run: python3 tests/voice-lock.test.py
Exit: 0 = all pass, 1 = failure
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HELPER = REPO / "scripts" / "voice-lock.py"
PY = sys.executable or "python3"

failures = []


def check(name, cond, detail=""):
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {name}" + ("" if cond else f" — {detail}"))
    if not cond:
        failures.append(name)


def helper_command(args):
    command = [PY]
    if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
        command += ["-m", "coverage", "run", f"--rcfile={REPO / '.coveragerc'}"]
    return command + [str(HELPER)] + [str(a) for a in args]


def run_helper(args, timeout=30, start_new_session=False):
    return subprocess.run(
        helper_command(args),
        capture_output=True,
        text=True,
        timeout=timeout,
        start_new_session=start_new_session,
    )


def out_json(proc):
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}


def my_start_time_ms(pid):
    out = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True
    ).stdout.strip()
    return int(time.mktime(time.strptime(out)) * 1000)


def spawn_sleeper(trap_term=False):
    """A live process to act as a lock owner. With trap_term=True it ignores
    SIGTERM (the escalation case)."""
    code = (
        "import signal, time\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if trap_term else "")
        + "time.sleep(120)\n"
    )
    return subprocess.Popen([PY, "-c", code], start_new_session=True)


def spawn_listener_group(port, entry_path):
    """A process group whose leader argv names `entry_path` and which LISTENs
    on `port` — a stand-in voice-agent for adopted-mode takeover tests.
    Returns the Popen. argv[1] is the entry path (visible in ps args)."""
    code = (
        "import socket, sys, time\n"
        "s = socket.socket()\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "s.bind(('127.0.0.1', int(sys.argv[2])))\n"
        "s.listen(1)\n"
        "time.sleep(120)\n"
    )
    p = subprocess.Popen(
        [PY, "-c", code, str(entry_path), str(port)], start_new_session=True
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            c = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            c.close()
            return p
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("stand-in listener never bound")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def structured_lock(pidfile, pid, start_ms, entry, workspace, lock_id=None):
    rec = {
        "v": 1,
        "lockId": lock_id or f"vl1-{uuid.uuid4()}",
        "pid": pid,
        "startTimeMs": start_ms,
        "entry": str(entry),
        "workspace": str(workspace),
    }
    pidfile.write_text(json.dumps(rec) + "\n")
    return rec


def main():
    tmp = Path(tempfile.mkdtemp(prefix="voice-lock-test-"))
    ws = tmp / "workspace"
    ws.mkdir()
    pidfile = ws / "state" / "locks" / "voice-agent.pid"
    pidfile.parent.mkdir(parents=True)
    guard = ws / ".voice-agent.lock.guard"
    entry = tmp / "src" / "voice-agent.ts"
    entry.parent.mkdir()
    entry.write_text("// stand-in entry\n")

    def base(cmd):
        return [cmd, "--pidfile", pidfile, "--guard", guard]

    try:
        # --- acquire: fresh ---
        print("acquire:")
        p = run_helper(base("acquire") + ["--pid", os.getpid(), "--entry", entry, "--workspace", ws])
        check("fresh acquire exits 0", p.returncode == 0, p.stdout + p.stderr)
        lock = json.loads(pidfile.read_text())
        check("lock is structured v1", lock.get("v") == 1 and lock.get("pid") == os.getpid())
        check("lock carries a random lockId", str(lock.get("lockId", "")).startswith("vl1-"))
        check(
            "startTimeMs from ps lstart (±2s)",
            abs(lock["startTimeMs"] - my_start_time_ms(os.getpid())) <= 2000,
        )

        # --- acquire vs live holder → exit 7 ---
        p = run_helper(base("acquire") + ["--pid", 99999, "--entry", entry, "--workspace", ws])
        check("acquire vs live holder exits 7", p.returncode == 7, str(p.returncode))
        check("held payload names the holder", out_json(p).get("code") == "held")
        check("live lock untouched", json.loads(pidfile.read_text())["lockId"] == lock["lockId"])

        # --- stale structured lock (dead pid) replaced ---
        dead = spawn_sleeper()
        dead_pid = dead.pid
        dead_start = my_start_time_ms(dead_pid)
        dead.kill()
        dead.wait()
        structured_lock(pidfile, dead_pid, dead_start, entry, ws)
        p = run_helper(base("acquire") + ["--pid", os.getpid(), "--entry", entry, "--workspace", ws])
        check("stale structured lock replaced", p.returncode == 0, p.stdout + p.stderr)
        check("new lock names acquirer", json.loads(pidfile.read_text())["pid"] == os.getpid())

        # --- stale legacy bare-pid replaced ---
        pidfile.write_text(f"{dead_pid}\n")
        p = run_helper(base("acquire") + ["--pid", os.getpid(), "--entry", entry, "--workspace", ws])
        check("stale legacy bare-pid replaced", p.returncode == 0, p.stdout + p.stderr)

        # --- live legacy pid blocks acquire (no start-time evidence) ---
        sleeper = spawn_sleeper()
        pidfile.write_text(f"{sleeper.pid}\n")
        p = run_helper(base("acquire") + ["--pid", os.getpid(), "--entry", entry, "--workspace", ws])
        check("acquire blocks on live legacy pid", p.returncode == 7, str(p.returncode))

        # --- #2722 transition: a LIVE pre-move owner at the legacy path must
        # hold canonical acquisition inside the same guarded transaction.
        print("legacy-path transition (#2722):")
        pidfile.unlink()
        legacy_pidfile = ws / ".voice-agent.pid"
        oldowner = spawn_sleeper()
        structured_lock(legacy_pidfile, oldowner.pid, my_start_time_ms(oldowner.pid), entry, ws)
        p = run_helper(base("acquire") + ["--pid", os.getpid(), "--entry", entry,
                                          "--workspace", ws, "--legacy-pidfile", legacy_pidfile])
        check("live legacy owner holds canonical acquire (7)", p.returncode == 7, p.stdout + p.stderr)
        check("held payload marks the legacy location", out_json(p).get("at") == "legacy", p.stdout)
        check("live legacy record left intact",
              json.loads(legacy_pidfile.read_text())["pid"] == oldowner.pid)
        check("no canonical record created", not pidfile.exists())
        check("pre-move owner never signaled", oldowner.poll() is None)

        # Stale pre-move record → retired in-transaction, canonical created.
        oldowner.kill()
        oldowner.wait()
        p = run_helper(base("acquire") + ["--pid", os.getpid(), "--entry", entry,
                                          "--workspace", ws, "--legacy-pidfile", legacy_pidfile])
        check("stale legacy retired and canonical acquired", p.returncode == 0, p.stdout + p.stderr)
        check("legacy record gone", not legacy_pidfile.exists())
        check("canonical names acquirer", json.loads(pidfile.read_text())["pid"] == os.getpid())

        # Control: without --legacy-pidfile a legacy record is invisible —
        # proves the flag, not coincidence, gates the new branch.
        pidfile.unlink()
        legacy_pidfile.write_text("{junk")
        p = run_helper(base("acquire") + ["--pid", os.getpid(), "--entry", entry, "--workspace", ws])
        check("without the flag, legacy path is not consulted",
              p.returncode == 0 and legacy_pidfile.exists(), p.stdout + p.stderr)
        legacy_pidfile.unlink()
        pidfile.unlink()
        # Restore the state the later sections expect (sleeper bare-pid lock).
        pidfile.write_text(f"{sleeper.pid}\n")

        # --- read normalization ---
        print("read:")
        p = run_helper(["read", "--pidfile", pidfile])
        check("legacy read kind", out_json(p).get("kind") == "legacy")
        structured_lock(pidfile, sleeper.pid, my_start_time_ms(sleeper.pid), entry, ws)
        p = run_helper(["read", "--pidfile", pidfile])
        check("structured read kind", out_json(p).get("kind") == "structured")
        pidfile.write_text("{not json at all")
        p = run_helper(["read", "--pidfile", pidfile])
        check("malformed read kind=unknown", out_json(p).get("kind") == "unknown")
        pidfile.unlink()
        p = run_helper(["read", "--pidfile", pidfile])
        check("absent read kind=absent", out_json(p).get("kind") == "absent")

        # --- malformed/partial JSON: acquire refuses while live evidence exists ---
        pidfile.write_text(json.dumps({"pid": sleeper.pid, "halfway": True}))
        p = run_helper(base("acquire") + ["--pid", os.getpid(), "--entry", entry, "--workspace", ws])
        check("partial JSON + live pid blocks acquire", p.returncode == 7, str(p.returncode))
        pidfile.write_text("{not json at all")
        p = run_helper(base("acquire") + ["--pid", os.getpid(), "--entry", entry, "--workspace", ws])
        check("unparseable with no live evidence is replaced", p.returncode == 0, p.stdout + p.stderr)

        # --- steal ---
        print("steal:")
        structured_lock(pidfile, sleeper.pid, my_start_time_ms(sleeper.pid), entry, ws)
        p = run_helper(base("steal") + ["--expect-pid", sleeper.pid])
        check("steal against live owner refuses (exit 3)", p.returncode == 3, str(p.returncode))
        check("owner-alive code", out_json(p).get("code") == "owner-alive")
        check("live lock never removed", pidfile.exists())
        # PID reuse: live pid, wrong startTimeMs → stale for steal
        structured_lock(pidfile, sleeper.pid, my_start_time_ms(sleeper.pid) - 60_000, entry, ws)
        p = run_helper(base("steal") + ["--expect-pid", sleeper.pid])
        check("PID-reuse (start-time mismatch) stolen", p.returncode == 0 and out_json(p).get("stolen") is True, p.stdout)

        # --- release ---
        print("release:")
        structured_lock(pidfile, 4194000, 12345, entry, ws)  # dead arbitrary pid
        p = run_helper(base("release") + ["--pid", 4194000])
        check("release matching dead pid unlinks", p.returncode == 0 and not pidfile.exists(), p.stdout)
        structured_lock(pidfile, sleeper.pid, my_start_time_ms(sleeper.pid), entry, ws)
        p = run_helper(base("release") + ["--pid", 4194001])
        check("release with wrong pid is a no-op", p.returncode == 0 and pidfile.exists())
        pidfile.unlink()

        # --- two concurrent acquires → exactly one winner ---
        print("concurrency:")
        s1 = spawn_sleeper()
        s2 = spawn_sleeper()
        procs = [
            subprocess.Popen(
                helper_command(
                    ["acquire", "--pidfile", pidfile, "--guard", guard,
                     "--pid", s.pid, "--entry", entry, "--workspace", ws]
                ),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for s in (s1, s2)
        ]
        codes = sorted(p.wait(timeout=30) for p in procs)
        check("exactly one winner (0) and one held (7)", codes == [0, 7], str(codes))
        winner_pid = json.loads(pidfile.read_text())["pid"]
        check("lock names one of the contenders", winner_pid in (s1.pid, s2.pid))
        for s in (s1, s2):
            s.kill()
            s.wait()
        pidfile.unlink()

        # --- guard-hold: flock released on holder death ---
        print("guard-hold:")
        holder = subprocess.Popen(
            helper_command(["guard-hold", "--guard", guard]),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        line = holder.stdout.readline()
        check("guard-hold reports ok", json.loads(line).get("ok") is True, line)
        second = run_helper(["guard-hold", "--guard", guard], timeout=10)
        check("second guard-hold refused while held", second.returncode == 3, str(second.returncode))
        holder.kill()
        holder.wait()
        fd = os.open(str(guard), os.O_RDWR)
        acquired = True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            acquired = False
        finally:
            os.close(fd)
        check("flock released on holder death", acquired)

        # --- takeover (adopted, S4/U1) ---
        print("takeover (adopted):")
        port = free_port()

        # Graceful TERM: python sleeper dies on TERM → no KILL.
        agent = spawn_listener_group(port, entry)
        structured_lock(pidfile, agent.pid, my_start_time_ms(agent.pid), entry, ws)
        p = run_helper(
            base("takeover")
            + ["--workspace", ws, "--mode", "adopted", "--port", port, "--entry", entry]
        )
        res = out_json(p)
        check("graceful takeover replaced", p.returncode == 0 and res.get("code") == "replaced", p.stdout + p.stderr)
        check("graceful takeover used no KILL", res.get("usedKill") is False, str(res))
        check("owner terminated", agent.wait(timeout=5) is not None)
        check("lock unlinked after revalidate", not pidfile.exists())

        # TERM-ignored escalation → KILL.
        code_ignore = (
            "import signal, socket, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "s = socket.socket()\n"
            "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
            "s.bind(('127.0.0.1', int(sys.argv[2])))\n"
            "s.listen(1)\n"
            "time.sleep(120)\n"
        )
        stubborn = subprocess.Popen(
            [PY, "-c", code_ignore, str(entry), str(port)], start_new_session=True
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                c = socket.create_connection(("127.0.0.1", port), timeout=0.5)
                c.close()
                break
            except OSError:
                time.sleep(0.05)
        structured_lock(pidfile, stubborn.pid, my_start_time_ms(stubborn.pid), entry, ws)
        p = run_helper(
            base("takeover")
            + ["--workspace", ws, "--mode", "adopted", "--port", port, "--entry", entry,
               "--term-wait-ms", 500]
        )
        res = out_json(p)
        check("TERM-ignored escalates to KILL", p.returncode == 0 and res.get("usedKill") is True, p.stdout + p.stderr)
        stubborn.wait(timeout=5)
        check("stubborn owner gone", not pidfile.exists())

        # U1: lock naming a NON-listener must block, no signal sent.
        bystander = spawn_sleeper()
        listener = spawn_listener_group(port, entry)
        structured_lock(pidfile, bystander.pid, my_start_time_ms(bystander.pid), entry, ws)
        p = run_helper(
            base("takeover")
            + ["--workspace", ws, "--mode", "adopted", "--port", port, "--entry", entry]
        )
        check("lock/listener pid mismatch → blocked (exit 3)", p.returncode == 3, p.stdout + p.stderr)
        check("blocked code", out_json(p).get("code") == "takeover-blocked")
        time.sleep(0.2)
        check("no signal sent to the lock's pid", bystander.poll() is None)
        check("blocked leaves the lock in place", pidfile.exists())

        # U1: entry mismatch → blocked, no signal.
        wrong_entry = tmp / "src" / "not-voice-agent.ts"
        wrong_entry.write_text("// impostor\n")
        structured_lock(pidfile, listener.pid, my_start_time_ms(listener.pid), wrong_entry, ws)
        p = run_helper(
            base("takeover")
            + ["--workspace", ws, "--mode", "adopted", "--port", port, "--entry", entry]
        )
        check("entry mismatch → blocked (exit 3)", p.returncode == 3, p.stdout + p.stderr)
        time.sleep(0.2)
        check("listener not signaled on entry mismatch", listener.poll() is None)

        # Concurrent replacement: two takeovers, guard-serialized — exactly one
        # replaces; the other observes no lock; the process dies exactly once.
        structured_lock(pidfile, listener.pid, my_start_time_ms(listener.pid), entry, ws)
        t_args = helper_command(
            ["takeover", "--pidfile", pidfile, "--guard", guard,
             "--workspace", ws, "--mode", "adopted", "--port", port, "--entry", entry]
        )
        tp = [subprocess.Popen(t_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        results = []
        for proc in tp:
            proc.wait(timeout=30)
            stdout, _ = proc.communicate()
            try:
                results.append(json.loads(stdout.strip().splitlines()[-1]))
            except (ValueError, IndexError):
                results.append({})
        codes = sorted(r.get("code", "?") for r in results)
        check("concurrent replacement: one replaced, one no-lock", codes == ["no-lock", "replaced"], str(codes))
        check("lock gone after concurrent takeover", not pidfile.exists())
        bystander.kill()
        bystander.wait()

        # U1 (review): entry paths containing spaces (e.g. under
        # 'Application Support') must validate — argv matching runs against
        # the FULL `ps -o args=` string, never whitespace tokens, which would
        # split the path into fragments that can never match.
        spaced_entry = tmp / "Application Support" / "sutando" / "dist" / "voice-agent.js"
        spaced_entry.parent.mkdir(parents=True)
        spaced_entry.write_text("// packaged stand-in entry\n")
        spaced = spawn_listener_group(port, spaced_entry)
        structured_lock(pidfile, spaced.pid, my_start_time_ms(spaced.pid), spaced_entry, ws)
        p = run_helper(
            base("takeover")
            + ["--workspace", ws, "--mode", "adopted", "--port", port, "--entry", spaced_entry]
        )
        res = out_json(p)
        check(
            "spaced-path entry takeover replaced",
            p.returncode == 0 and res.get("code") == "replaced",
            p.stdout + p.stderr,
        )
        check("spaced-path owner terminated", spaced.wait(timeout=5) is not None)
        check("spaced-path lock unlinked", not pidfile.exists())

        # --- takeover (owned, Z1): live NON-listening owned child + group ---
        print("takeover (owned):")
        # Parent (no listener — like a dev tsx parent) spawning a child in the
        # same process group; the child writes the lock (descendant holder).
        parent_code = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(120)\n"
        )
        parent = subprocess.Popen(
            [PY, "-c", parent_code, str(entry)],
            start_new_session=True, stdout=subprocess.PIPE, text=True,
        )
        child_pid = int(parent.stdout.readline())
        structured_lock(pidfile, child_pid, my_start_time_ms(child_pid), entry, ws)
        # Entry validation happens against the OWNED ROOT argv (parent carries
        # the entry token), no listener requirement.
        p = run_helper(
            base("takeover")
            + ["--workspace", ws, "--mode", "owned",
               "--pid", parent.pid, "--start-time-ms", my_start_time_ms(parent.pid),
               "--entry", entry]
        )
        res = out_json(p)
        check("owned takeover replaced (no listener required)", p.returncode == 0 and res.get("code") == "replaced", p.stdout + p.stderr)
        parent.wait(timeout=5)
        time.sleep(0.3)
        alive_child = True
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            alive_child = False
        check("whole process group terminated (worker too)", not alive_child)
        check("owned takeover unlinked the lock", not pidfile.exists())

        # A caller-provided zero PGID must never target the helper's own group.
        pgid_zero_target = subprocess.Popen(
            [PY, "-c", "import time; time.sleep(120)", str(entry)],
            start_new_session=True,
        )
        p = run_helper(
            base("takeover")
            + ["--workspace", ws, "--mode", "owned",
               "--pid", pgid_zero_target.pid,
               "--start-time-ms", my_start_time_ms(pgid_zero_target.pid),
               "--pgid", 0, "--entry", entry],
            start_new_session=True,
        )
        check("owned zero pgid → blocked", p.returncode == 3, p.stdout + p.stderr)
        check("owned zero pgid sends no signal", pgid_zero_target.poll() is None)
        pgid_zero_target.kill()
        pgid_zero_target.wait()

        # Owned-mode start-time mismatch → blocked, nothing killed.
        loner = spawn_sleeper()
        p = run_helper(
            base("takeover")
            + ["--workspace", ws, "--mode", "owned",
               "--pid", loner.pid, "--start-time-ms", my_start_time_ms(loner.pid) - 60_000,
               "--entry", entry]
        )
        check("owned start-time mismatch → blocked", p.returncode == 3, p.stdout + p.stderr)
        time.sleep(0.2)
        check("owned mismatch sends no signal", loner.poll() is None)
        loner.kill()
        loner.wait()

        sleeper.kill()
        sleeper.wait()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f"\n{len(failures)} failure(s): {failures}")
        return 1
    print("\nAll voice-lock tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
