#!/usr/bin/env python3
"""A handler that exits 0 without writing a result is a finished run, not a dead one.

The drain reaps a running marker whose worker pid is gone. It used to read
"no deliverable result" as "died before producing one" and hand the task back
with a synthetic exit 1 — but a routing handler finishes by writing a sentinel
elsewhere and never a result, so every fast exit that the drain observed before
the fifo's HANDLER_DONE was consumed became a false failure (five owner-facing
replies on 2026-09-10). The runner now leaves an rc receipt the drain reads first.

Run:  python3 tests/watch-tasks-stream-exited-runner-is-not-dead.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "reap", REPO / "tests" / "watch-tasks-stream-dead-worker-reap.test.py")
reap = importlib.util.module_from_spec(_spec)
sys.argv = [sys.argv[0], "--import-only"]
_spec.loader.exec_module(reap)

failures: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f" — {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(name)


def drop_redelivery(h, *names):
    """The harness feeds a task twice — the startup sweep AND the fswatch line —
    so a late duplicate re-claims a task the reap has already released. The
    watcher's own `[ -f "$path" ]` filter drops it once the file is gone."""
    for name in names:
        (h.ws / "tasks" / name).unlink(missing_ok=True)


def scenario_receipt_outranks_missing_result():
    """The state the race leaves behind, built by hand so timing cannot hide it:
    a running marker, a worker pid that is gone, NO result — and the rc receipt
    the runner writes before it signals. The drain must read the receipt."""
    h = reap.Harness()  # default handler sleeps: a runner we can end ourselves
    h.start()
    try:
        h.deliver("task-exited.txt")
        if not reap.wait_for(lambda: h.dispatch() and "task-exited.txt" in reap.names(h.dispatch() / "running")):
            check("receipt scenario: task dispatched", False); return
        d = h.dispatch()
        if not reap.wait_for(lambda: (d / "workers" / "task-exited.txt").read_text().strip().isdigit()):
            check("receipt scenario: worker pid recorded", False); return
        pid = int((d / "workers" / "task-exited.txt").read_text().strip())
        (d / "settled" / "task-exited.txt.rc").write_text("0\n")  # what an exited runner leaves
        os.kill(pid, signal.SIGKILL)                                   # gone before HANDLER_DONE
        reap.wait_for(lambda: not reap.alive(pid), timeout=10)
        h.deliver("task-nudge1.txt")                                   # arrival drains
        gone = reap.wait_for(lambda: "task-exited.txt" not in reap.names(h.dispatch() / "running"), timeout=30)
        check("receipt scenario: the marker is retired", gone)
        time.sleep(1.0)
        published = (h.ws / "results" / "task-exited.txt").exists()
        check("receipt scenario: rc=0 receipt + no result -> NOT published as a failure", not published)
        check("receipt scenario: the receipt is cleaned up",
              not (d / "settled" / "task-exited.txt.rc").exists())
    finally:
        h.stop()


def scenario_the_runner_writes_the_receipt():
    """The producer half, driven through the real --handler-runner call: a handler
    that exits 7 must leave `7` in the settled receipt, written before the fifo
    signal. Fabricating the receipt would pin the drain and not the runner."""
    with tempfile.TemporaryDirectory(prefix="runner-receipt-") as tmp:
        root = Path(tmp)
        ws = root / "ws"
        (ws / "logs").mkdir(parents=True)
        (ws / "results").mkdir()
        dispatch = root / "dispatch"
        for sub in ("pending", "running", "settled", "workers"):
            (dispatch / sub).mkdir(parents=True)
        handler = root / "handler.sh"
        handler.write_text("#!/bin/sh\nexit 7\n")
        handler.chmod(0o755)
        task = ws / "task-demo.txt"
        task.write_text("task: demo\n")
        fifo = root / "events"
        os.mkfifo(fifo)
        # The runner's HANDLER_DONE write blocks until a reader opens the fifo.
        rfd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        env = dict(os.environ)
        env.update(SUTANDO_TASKS_DIR=str(ws / "tasks"), SUTANDO_WORKSPACE_DIR=str(ws),
                   SUTANDO_WORKSPACE=str(ws))

        def run(*extra):
            return subprocess.run(
                ["/bin/bash", str(REPO / "src" / "watch-tasks-stream.sh"), "--handler-runner",
                 str(handler), "claude", str(ws), str(task), str(ws / "results"), str(REPO),
                 str(fifo), "task-demo.txt", *extra],
                env=env, timeout=60, capture_output=True, text=True)

        proc = run(str(dispatch))
        check("runner: the --handler-runner call exits 0", proc.returncode == 0,
              f"rc={proc.returncode} stderr={proc.stderr[-200:]!r}")
        receipt = dispatch / "settled" / "task-demo.txt.rc"
        check("runner: the real runner writes the rc receipt", receipt.is_file())
        check("runner: the receipt carries the handler's exit code",
              receipt.is_file() and receipt.read_text().strip() == "7",
              receipt.read_text() if receipt.is_file() else "<absent>")
        signalled = b""
        deadline = time.time() + 10
        while b"\n" not in signalled and time.time() < deadline:
            try:
                signalled += os.read(rfd, 4096)
            except BlockingIOError:
                time.sleep(0.05)
        check("runner: the fifo still carries HANDLER_DONE with the same rc",
              signalled.decode().strip() == "HANDLER_DONE: 7 task-demo.txt", repr(signalled))

        receipt.unlink(missing_ok=True)
        compat = run()  # back-compat control: the old nine-argument call shape
        check("back-compat control: the receipt-less call shape still exits 0",
              compat.returncode == 0, f"rc={compat.returncode} stderr={compat.stderr[-200:]!r}")
        check("back-compat control: it writes no receipt", not receipt.exists())
        os.close(rfd)


def scenario_a_dead_runner_is_still_reaped():
    h = reap.Harness()  # default handler sleeps forever: a runner that can be killed
    h.start()
    try:
        h.deliver("task-dead1.txt")
        if not reap.wait_for(lambda: h.dispatch() and "task-dead1.txt" in reap.names(h.dispatch() / "running")):
            check("dead-runner control: task dispatched", False); return
        drop_redelivery(h, "task-dead1.txt")
        pids = h.kill_workers(expect=1)
        check("dead-runner control: one runner killed", len(pids) == 1, f"pids={pids}")
        h.deliver("task-nudge.txt")  # arrival drains
        got = reap.wait_for(lambda: (h.ws / "results" / "task-dead1.txt").exists(), timeout=30)
        check("dead-runner control: a runner killed before any receipt is still published as failed", got)
        check("dead-runner control: and its claim is released",
              reap.wait_for(lambda: not (h.ws / "state" / "task-event-handler-claims"
                                         / "task-dead1.txt").exists(), timeout=15))
    finally:
        h.stop()


def scenario_an_interrupted_receipt_write_leaves_no_receipt():
    """The producer half of the strand: a kill landing between the redirection
    opening the receipt and printf writing it used to publish a zero-byte file
    that the drain read as a complete exit code. The write goes to a same-dir
    temp now, so the path the drain reads never exists half-written."""
    with tempfile.TemporaryDirectory(prefix="runner-pause-") as tmp:
        root = Path(tmp)
        ws = root / "ws"
        (ws / "logs").mkdir(parents=True)
        (ws / "results").mkdir()
        dispatch = root / "dispatch"
        for sub in ("pending", "running", "settled", "workers"):
            (dispatch / sub).mkdir(parents=True)
        handler = root / "handler.sh"
        handler.write_text("#!/bin/sh\nexit 7\n")
        handler.chmod(0o755)
        task = ws / "task-pause.txt"
        task.write_text("task: demo\n")
        fifo = root / "events"
        os.mkfifo(fifo)
        rfd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        paused = root / "paused"
        # The redirection is applied to the CALL, so bash opens the target before
        # this body runs: entering it IS the open-but-unwritten boundary.
        bash_env = root / "pause.sh"
        bash_env.write_text(
            'printf() {\n'
            '  if [ "$1" = \'%s\\n\' ]; then\n'
            f'    : > "{paused}"\n'
            '    sleep 120\n'
            '  fi\n'
            '  builtin printf "$@"\n'
            '}\n')
        env = dict(os.environ)
        env.update(SUTANDO_TASKS_DIR=str(ws / "tasks"), SUTANDO_WORKSPACE_DIR=str(ws),
                   SUTANDO_WORKSPACE=str(ws), BASH_ENV=str(bash_env))
        proc = subprocess.Popen(
            ["/bin/bash", str(REPO / "src" / "watch-tasks-stream.sh"), "--handler-runner",
             str(handler), "claude", str(ws), str(task), str(ws / "results"), str(REPO),
             str(fifo), "task-pause.txt", str(dispatch)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        receipt = dispatch / "settled" / "task-pause.txt.rc"
        try:
            reached = reap.wait_for(paused.exists, timeout=30, step=0.1)
            check("interrupted write: the producer reached the open-before-write boundary", reached)
            temps = [q for q in (dispatch / "settled").iterdir() if q.name.startswith(".")]
            check("interrupted write: the opened path is a same-dir temp, not the receipt",
                  len(temps) == 1 and temps[0].stat().st_size == 0,
                  f"settled={[q.name for q in (dispatch / 'settled').iterdir()]}")
            check("interrupted write: no receipt exists while the write is unfinished",
                  not receipt.exists())
        finally:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait(timeout=10)
        check("interrupted write: the killed producer left no receipt behind", not receipt.exists(),
              repr(receipt.read_text()) if receipt.exists() else "")
        os.close(rfd)


def scenario_an_incomplete_receipt_is_not_an_exit_code():
    """The consumer half: the bytes that interruption used to leave must take the
    missing-receipt recovery — a terminal failure and a RELEASED claim — instead
    of passing rc="" through both numeric branches and stranding the task."""
    h = reap.Harness()
    h.start()
    try:
        for name in ("task-empty.txt", "task-garbage.txt"):
            h.deliver(name)
        if not reap.wait_for(lambda: len(reap.names(h.dispatch() and h.dispatch() / "running")) >= 2):
            check("incomplete receipt: both tasks dispatched", False); return
        d = h.dispatch()
        drop_redelivery(h, "task-empty.txt", "task-garbage.txt")
        (d / "settled" / "task-empty.txt.rc").write_text("")        # opened, never written
        (d / "settled" / "task-garbage.txt.rc").write_text("x\n")   # present, not an exit code
        claims = h.ws / "state" / "task-event-handler-claims"
        held = {n: (claims / n).is_file() for n in ("task-empty.txt", "task-garbage.txt")}
        h.kill_workers(expect=2)
        h.deliver("task-nudge-inc.txt")
        for name, label in (("task-empty.txt", "a zero-byte"), ("task-garbage.txt", "a non-numeric")):
            res = h.ws / "results" / name
            got = reap.wait_for(
                lambda res=res: res.is_file() and reap.FAILURE_TEXT in res.read_text(),
                timeout=30, nudge=lambda i, n=name: h.deliver(f"task-nudge-{n[5:-4]}-{i}.txt"))
            check(f"{label} receipt publishes the terminal failure", got,
                  f"exists={res.exists()}")
            # held_before is the discriminator: a claim that never existed would
            # make "released" trivially true and assert nothing about the fix.
            released = reap.wait_for(lambda name=name: not (claims / name).exists(), timeout=15)
            check(f"{label} receipt releases the claim rather than stranding the task",
                  held[name] and released,
                  f"held_before={held[name]} still_held={(claims / name).exists()}")
    finally:
        h.stop()


def scenario_shutdown_settles_a_completed_receipt():
    """A runner that returned 0 and was killed before its fifo notification has
    already succeeded. Graceful shutdown used to publish an owner-facing
    "interrupted" failure over it and leave the receipt in the dispatch dir."""
    h = reap.Harness()
    h.start()
    try:
        h.deliver("task-cleanup.txt")
        if not reap.wait_for(lambda: h.dispatch() and "task-cleanup.txt" in reap.names(h.dispatch() / "running")):
            check("shutdown receipt: task dispatched", False); return
        d = h.dispatch()
        drop_redelivery(h, "task-cleanup.txt")
        claims = h.ws / "state" / "task-event-handler-claims"
        check("shutdown receipt: the claim is held before shutdown",
              (claims / "task-cleanup.txt").is_file())
        (d / "settled" / "task-cleanup.txt.rc").write_text("0\n")  # written before the fifo write
        h.stop(graceful=True)
        check("shutdown receipt: an exit-0 receipt is NOT published as interrupted",
              not (h.ws / "results" / "task-cleanup.txt").exists(),
              (h.ws / "results" / "task-cleanup.txt").read_text()[:60]
              if (h.ws / "results" / "task-cleanup.txt").exists() else "")
        check("shutdown receipt: the claim is released",
              not (claims / "task-cleanup.txt").exists())
        check("shutdown receipt: the settled receipt does not leak",
              not (d / "settled" / "task-cleanup.txt.rc").exists())
    finally:
        h.stop()


def scenario_shutdown_control_a_receiptless_marker_is_interrupted():
    """The control that keeps the fix from being "shutdown publishes nothing":
    the same shutdown with no receipt must still report the interruption."""
    h = reap.Harness()
    h.start()
    try:
        h.deliver("task-interrupted.txt")
        if not reap.wait_for(lambda: h.dispatch() and "task-interrupted.txt" in reap.names(h.dispatch() / "running")):
            check("shutdown control: task dispatched", False); return
        drop_redelivery(h, "task-interrupted.txt")
        h.stop(graceful=True)
        res = h.ws / "results" / "task-interrupted.txt"
        check("shutdown control: a receipt-less marker is still published as interrupted",
              res.is_file() and reap.FAILURE_TEXT in res.read_text(),
              f"exists={res.exists()}")
    finally:
        h.stop()


if __name__ == "__main__":
    scenario_receipt_outranks_missing_result()
    scenario_the_runner_writes_the_receipt()
    scenario_an_interrupted_receipt_write_leaves_no_receipt()
    scenario_an_incomplete_receipt_is_not_an_exit_code()
    scenario_shutdown_settles_a_completed_receipt()
    scenario_shutdown_control_a_receiptless_marker_is_interrupted()
    scenario_a_dead_runner_is_still_reaped()
    print(f"{len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
