#!/usr/bin/env python3
"""A worker's Retry now survives the take-down of the session that asked for it.

The seat's input watcher runs `pool_manage.py retry`; the take-down closes the watcher's own
tmux session, which hangs up every process in it. Reproduced with real processes and a real
SIGHUP: a "monitor" process group stands for the input watcher's session, the retry is started
inside it, and closing the `-input` session signals that whole group. The restart must still
happen, and the seat must never be left paused.

Run: python3 tests/pool-retry-detached.test.py
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
WID = "b35461a4a76a408a89dfdb263ff748fe"

# The pool's own code runs unchanged; only its tmux, records and seat start are stand-ins.
WRAPPER = textwrap.dedent('''
    import contextlib, os, signal, subprocess, sys, time
    from pathlib import Path
    sys.path[:0] = [{scripts!r}, {src!r}]
    import pool_manage as pm
    ws = Path(sys.argv[sys.argv.index("--workspace") + 1])
    flag, started, killed = ws / "paused", ws / "restarted", set()
    pm.RETRY_ENTRY = Path(__file__)
    pm._worker = lambda w, t: ({wid!r}, {{"state": "active", "name": "Mira"}})
    pm.sup.is_paused = lambda w, wid: flag.exists() and flag.read_text() == "1"
    pm.sup.set_paused = lambda w, wid, v: flag.write_text("1" if v else "0")
    pm.sup.run_socket = lambda w, wid: "sock"
    pm.sw.session_state = lambda name, *a, **k: "absent" if name in killed else "exists"
    pm.rem.pool_lock = lambda *a, **k: contextlib.nullcontext()
    pm.rem.close_dead_runs = lambda *a, **k: []
    pm.rem.restart = lambda *a, **k: (started.write_text("yes"), {{"result": "restarted", "detail": {{}}}})[1]

    def runner(argv, **kw):
        if "kill-session" in argv:
            name = argv[-1].lstrip("=")
            killed.add(name)
            if name.endswith("-input"):  # the input watcher's session: every process in it is hung up
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(int(os.environ["MONITOR_PGID"]), signal.SIGHUP)
                time.sleep(0.5)
        return subprocess.CompletedProcess(argv, 0, "", "")

    # The asking process and its session-mates (the input watcher) form one process group.
    os.environ.setdefault("MONITOR_PGID", str(os.getpgrp()))
    if "--in-place" not in sys.argv:  # the session that asked: the watcher shares its group
        watcher = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        (ws / "watcher.pid").write_text(str(watcher.pid))
    sys.exit(pm.main(sys.argv[1:], runner=runner))
''')


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()[:1] not in ("", "Z")


class RetryOutlivesItsSession(unittest.TestCase):
    def test_the_restart_happens_and_the_pause_is_lifted_although_the_asking_session_is_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "logs").mkdir()
            wrapper = ws / "pool_manage_wrapper.py"
            wrapper.write_text(WRAPPER.format(scripts=str(SCRIPTS), src=str(REPO / "src"), wid=WID))
            env = dict(os.environ, SUTANDO_WORKSPACE=str(ws), SUTANDO_TEST_MODE="1")
            env.pop("MONITOR_PGID", None)
            asked = subprocess.Popen([sys.executable, str(wrapper), "--workspace", str(ws), "--repo", str(REPO),
                                      "retry", WID], env=env, start_new_session=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            asked.wait(timeout=30)
            deadline = time.time() + 20
            while time.time() < deadline and not (ws / "restarted").exists():
                time.sleep(0.2)
            log = (ws / "logs" / f"pool-retry-{WID}.log")
            detail = log.read_text() if log.exists() else asked.stderr.read().decode()
            self.assertTrue((ws / "restarted").exists(), "the seat was never restarted: " + detail)
            deadline = time.time() + 5
            while time.time() < deadline and (ws / "paused").read_text() != "0":
                time.sleep(0.1)
            self.assertEqual((ws / "paused").read_text(), "0", "the seat was left paused")
            pid = int((ws / "watcher.pid").read_text())
            deadline = time.time() + 5
            while time.time() < deadline and _alive(pid):
                time.sleep(0.1)
            self.assertFalse(_alive(pid), "the asking session was closed, as live")

if __name__ == "__main__":
    unittest.main()
