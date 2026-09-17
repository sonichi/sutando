#!/usr/bin/env python3
"""A core-scope stop gates only the core's own intake; a peer worker keeps
draining. `--scope all` gates every watcher. A gated event is HELD, not dropped.

Two REAL watchers share one workspace: the core (no instance id) and a worker
(`SUTANDO_INSTANCE_ID=worker-1`). The gates are marked and cleared through the
production `src/shutdown.py` CLI with exactly the arguments `restart.sh` passes
(`--gate instance` for the default core scope, `--gate workspace` for `all`),
so this suite and `tests/restart-scope-isolation.test.sh` (which pins WHICH gate
restart.sh marks) meet on the same file.

fswatch does not replay. Before this fix every watcher consulted one shared
`state/shutdown.sentinel` and `continue`d past a gated event, so a task landing
during a peer core's restart window was silently lost by the worker, and a
watcher that survived its own restart (ownership unconfirmed) lost it too.

HARNESS RULES inherited from watch-tasks-stream-sentinel-record.test.py: every
watcher runs in its OWN session (cleanup() ends in `kill 0`); the workspace is a
private temp tree whose resolution is ASSERTED before any child is spawned;
`fswatch` is a polling stub on the child's PATH. No `pkill -f` anywhere.

Run: python3 tests/watch-tasks-stream-instance-intake-gate.test.py   (exit 0/1)
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHUTDOWN_PY = REPO / "src" / "shutdown.py"
UTIL_PATHS = REPO / "src" / "util_paths.py"

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def make_stub_fswatch(bin_dir: Path) -> None:
    """Prints every new .txt in the watched dir as fswatch does: absolute
    physical path, one per line, polled — so a held event is observable."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "fswatch"
    stub.write_text(
        "#!/bin/bash\n"
        "for a in \"$@\"; do d=\"$a\"; done\n"
        "d=\"$(cd \"$d\" 2>/dev/null && pwd -P)\" || exit 1\n"
        "seen=\"\"\n"
        "while :; do\n"
        "  for f in \"$d\"/*.txt; do\n"
        "    [ -f \"$f\" ] || continue\n"
        "    case \"$seen\" in *\"|$f|\"*) continue ;; esac\n"
        "    seen=\"$seen|$f|\"\n"
        "    printf '%s\\n' \"$f\"\n"
        "  done\n"
        "  sleep 0.2\n"
        "done\n"
    )
    stub.chmod(0o755)


class Watcher:
    """One watcher in its OWN session, so its `kill 0` cannot reach us."""

    def __init__(self, name: str, workspace: Path, bin_dir: Path, out: Path,
                 instance: str | None):
        self.name = name
        self.env = dict(os.environ, SUTANDO_WORKSPACE=str(workspace), SUTANDO_TEST_MODE="1",
                        PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        self.env.pop("SUTANDO_INSTANCE_ID", None)
        if instance:
            self.env["SUTANDO_INSTANCE_ID"] = instance
        got = subprocess.run(["bash", "scripts/sutando-config.sh", "workspace"],
                             cwd=str(REPO), env=self.env,
                             capture_output=True, text=True).stdout.strip()
        if not got or Path(got).resolve() != workspace.resolve():
            raise SystemExit(f"REFUSING TO SPAWN: {name} would resolve {got!r}, "
                             f"not the declared {workspace}")
        self.out = out
        self.fh = open(out, "ab")
        self.err = open(str(out) + ".err", "ab")
        self.proc = subprocess.Popen(
            ["bash", "src/watch-tasks-stream.sh"], cwd=str(REPO), env=self.env,
            stdout=self.fh, stderr=self.err, start_new_session=True,
        )

    def alive(self) -> bool:
        return self.proc.poll() is None

    def emitted(self, filename: str) -> bool:
        try:
            return f"TASK_FILE: {filename}" in self.out.read_text(errors="replace")
        except OSError:
            return False

    def gate_path(self, gate: str) -> Path:
        """The gate THIS watcher's identity resolves — through the one owner."""
        state = Path(self.env["SUTANDO_WORKSPACE"]) / "state"
        sub = "shutdown-gate" if gate == "workspace" else "instance-shutdown-gate"
        r = subprocess.run([sys.executable, str(UTIL_PATHS), sub, str(state)],
                           env=self.env, capture_output=True, text=True)
        return Path(r.stdout.strip())

    def shutdown(self, *args: str) -> subprocess.CompletedProcess:
        """`shutdown.py` as THIS identity — what restart.sh runs in its env."""
        state = Path(self.env["SUTANDO_WORKSPACE"]) / "state"
        return subprocess.run([sys.executable, str(SHUTDOWN_PY), *args, "--state-dir", str(state)],
                              env=self.env, capture_output=True, text=True)

    def term(self) -> None:
        """What a confirmed stop delivers: TERM to the watcher itself."""
        try:
            os.kill(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def hard_stop(self) -> None:
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        for fh in (self.fh, self.err):
            try:
                fh.close()
            except OSError:
                pass


def wait_for(pred, timeout: float = 10.0, step: float = 0.2) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except OSError:
            pass
        time.sleep(step)
    return False


def drop_task(tasks: Path, name: str) -> str:
    (tasks / name).write_text(f"id: {name[:-4]}\nsource: test\ntask: hold me\n")
    return name


def main() -> int:
    box = Path(tempfile.mkdtemp(prefix="intake-gate-"))
    ws = box / "ws"
    (ws / "tasks").mkdir(parents=True)
    (ws / "state").mkdir()
    bin_dir = box / "bin"
    make_stub_fswatch(bin_dir)
    tasks = ws / "tasks"
    state = ws / "state"
    core = Watcher("core", ws, bin_dir, box / "core.out", None)
    worker = Watcher("worker-1", ws, bin_dir, box / "worker.out", "worker-1")
    procs = [core, worker]
    try:
        ok = wait_for(lambda: len(list(state.glob("watch-tasks-stream*.pid"))) == 2)
        sentinels = sorted(p.name for p in state.glob("watch-tasks-stream*.pid"))
        check("both watchers publish their own record", ok and core.alive() and worker.alive(),
              f"sentinels={sentinels} core={core.alive()} worker={worker.alive()}")
        if not ok:
            return 1

        core_gate, worker_gate = core.gate_path("instance"), worker.gate_path("instance")
        ws_gate = core.gate_path("workspace")
        check("the two instance gates are distinct files beside their records",
              core_gate != worker_gate and core_gate.parent == state and worker_gate.parent == state,
              f"core={core_gate.name} worker={worker_gate.name}")
        check("the worker's identity resolves the SAME workspace-wide gate",
              worker.gate_path("workspace") == ws_gate)
        check("the workspace-wide gate is state/shutdown.sentinel (the pre-fix file)",
              ws_gate == state / "shutdown.sentinel", str(ws_gate))

        # --- control: with no gate, both drain the same task ------------------
        t1 = drop_task(tasks, "task-t1.txt")
        check("CONTROL: both watchers emit a task with no gate set",
              wait_for(lambda: core.emitted(t1) and worker.emitted(t1)),
              f"core={core.emitted(t1)} worker={worker.emitted(t1)}")

        # --- core scope: restart.sh marks `--gate instance` in the core's env --
        r = core.shutdown("mark", "restart.sh --stop-only", "--gate", "instance")
        check("core-scope mark exits 0", r.returncode == 0, r.stderr)
        check("core-scope mark writes the core's instance gate ONLY",
              core_gate.exists() and not ws_gate.exists() and not worker_gate.exists(),
              f"present={[p.name for p in state.glob('*shutdown*')]}")
        t2 = drop_task(tasks, "task-t2.txt")
        check("a task arriving during the core's window IS emitted by the worker",
              wait_for(lambda: worker.emitted(t2)), "worker never emitted it")
        time.sleep(1.5)
        check("...and is HELD by the core (not emitted while its gate is set)",
              not core.emitted(t2))
        check("the core is still alive while holding (a hold is not a crash)", core.alive())
        r = core.shutdown("clear", "--gate", "instance")
        check("core-scope clear exits 0 and removes the gate", r.returncode == 0 and not core_gate.exists())
        check("the held task is emitted by the core once its gate lifts — no new event needed",
              wait_for(lambda: core.emitted(t2)), "the event was dropped")

        # --- all scope: `--gate workspace` gates every watcher ---------------
        r = core.shutdown("mark", "restart.sh --scope all", "--gate", "workspace")
        check("all-scope mark writes the workspace-wide gate ONLY",
              r.returncode == 0 and ws_gate.exists() and not core_gate.exists() and not worker_gate.exists())
        t3 = drop_task(tasks, "task-t3.txt")
        time.sleep(2.0)
        check("under --scope all BOTH defer the task",
              not core.emitted(t3) and not worker.emitted(t3),
              f"core={core.emitted(t3)} worker={worker.emitted(t3)}")
        check("...and both are alive (holding, not exited)", core.alive() and worker.alive())
        # The worker's own `clear` must lift the shared gate too: it is the file
        # a launcher's bare `clear` removes, whichever identity boots first.
        r = worker.shutdown("clear")
        check("a bare clear (launcher boot) removes the workspace-wide gate",
              r.returncode == 0 and not ws_gate.exists())
        check("both then emit the deferred task without a new event",
              wait_for(lambda: core.emitted(t3) and worker.emitted(t3)),
              f"core={core.emitted(t3)} worker={worker.emitted(t3)}")

        # --- --stop-only under core scope: the gate stays, the core is stopped,
        #     the worker is untouched, and nothing is lost -------------------
        core.shutdown("mark", "restart.sh --stop-only", "--gate", "instance")
        t4 = drop_task(tasks, "task-t4.txt")
        check("--stop-only (core): the worker still emits", wait_for(lambda: worker.emitted(t4)))
        time.sleep(1.0)
        check("--stop-only (core): the core holds", not core.emitted(t4))
        core.term()
        check("the TERMed core exits while holding (the hold does not wedge the trap)",
              wait_for(lambda: not core.alive()))
        check("its gate is LEFT SET by --stop-only (the clean-exit signal)", core_gate.exists())
        check("the worker's gate never appeared and the worker is alive",
              not worker_gate.exists() and worker.alive())
        check("the held task is still on disk for the next sweep", (tasks / t4).exists())
        # A launcher's bare `clear` in the core's env lifts the gate --stop-only
        # left; the re-armed core's startup sweep then emits what was held.
        r = core.shutdown("clear")
        check("a bare clear in the core's env lifts its instance gate", r.returncode == 0 and not core_gate.exists())
        core2 = Watcher("core-2", ws, bin_dir, box / "core2.out", None)
        procs.append(core2)
        check("a re-armed core sweeps the held task on startup",
              wait_for(lambda: core2.emitted(t4)), "no TASK_FILE for the held task")
        check("the worker was never gated across the whole run",
              worker.emitted(t1) and worker.emitted(t2) and worker.emitted(t3) and worker.emitted(t4)
              and worker.alive())
    finally:
        for p in procs:
            p.hard_stop()
    print()
    if FAILURES:
        print(f"FAIL — {len(FAILURES)}: {FAILURES}")
        return 1
    print("PASS — instance-scoped intake gate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
