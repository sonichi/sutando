#!/usr/bin/env python3
"""Sutando.app's watcher check asks the shared ownership policy about THIS
install's core watcher — never a host-wide process listing.

A host-wide probe (`pgrep -f watch-tasks`, then an anchored `/bin/ps` scan)
returns alive on ANY watcher on the machine. On a pool host a surviving worker
watcher therefore masked a missing core watcher: checkWatcher() returned before
its tmux `watcher` re-arm, and the core's tasks stopped draining silently. The
app now runs `src/watcher_identity.sh core-alive`, which resolves the default
core's sentinel and confirms THAT record (instance, workspace, code_path,
incarnation, executed argv, age) through src/watcher_identity.py — the same
policy restart.sh and the startup reaper use — and maps rc 0/1/other to
true/false/nil (nil = unknown, never an alert).

Source pins keep the app on that path; the behavioral half drives the helper
itself against a sandbox checkout with real processes:
  core present  -> rc 0
  core ABSENT, a live worker watcher present -> rc 1 (the masking case)
  core record naming a dead pid -> rc 1

Run: python3 tests/app-checkwatcher-core-alive.test.py
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import watcher_identity as wid  # noqa: E402

SRC = REPO / "src" / "Sutando" / "main.swift"
text = SRC.read_text()
fn = re.search(r"func watcherProcessSeen\(\) -> Bool\? \{(.*?)\n    \}\n", text, re.S)
body = fn.group(1) if fn else ""

checks = {
    "watcherProcessSeen exists and returns an optional (unknown is a third state)": fn is not None,
    "the probe runs the shared policy's core-alive entry": "watcher_identity.sh" in body and '"core-alive"' in body,
    "it is executed from THIS checkout (repoRoot), not a guessed path": "repoRoot" in body,
    "no host-wide process listing remains (no /bin/ps)": '"/bin/ps"' not in body and "pid,command" not in body,
    "no pgrep-based probe remains": "pgrep" not in body,
    "the loose 'watch-tasks' pattern is gone": '"watch-tasks"' not in body and '"-f", "watch-tasks"' not in body,
    "rc 0 is alive": re.search(r"case 0:\s*return true", body) is not None,
    "rc 1 is definitely not alive (the only value that may alert)": re.search(r"case 1:.*?return false", body, re.S) is not None,
    "any other rc is unknown, not dead": re.search(r"default:.*?return nil", body, re.S) is not None,
    "a helper that cannot be launched is unknown": re.search(r"guard let .*runShellStatus.* else \{\s*return nil", body, re.S) is not None,
    "checkWatcher treats nil as unknown and does not alert": "case .none:" in text and "not alerting on an unknown" in text,
    "checkWatcher alerts only on an explicit false": "case .some(false): break" in text,
    # The Swift copy of the argv predicate was a second implementation of
    # src/watcher_identity.py; a re-added one would drift from the policy.
    "no private argv predicate survives in the app": "watcherLineMatches" not in text
        and "matchesWatcherScriptAtBoundary" not in text and '"watch-tasks-stream.sh"' not in text,
}

# ── behavioral: the helper against real processes in a sandbox checkout ──────
# Every src/ entry is linked except watch-tasks-stream.sh, a sleeper: "this checkout's watcher".
IDENTITY_SH = None
SPAWNED = []


def _sandbox(box: pathlib.Path) -> pathlib.Path:
    sb = box / "checkout"
    (sb / "src").mkdir(parents=True)
    for e in (REPO / "src").iterdir():
        if e.name != "watch-tasks-stream.sh":
            (sb / "src" / e.name).symlink_to(e)
    (sb / "scripts").symlink_to(REPO / "scripts")
    sleeper = sb / "src" / "watch-tasks-stream.sh"
    sleeper.write_text("#!/usr/bin/env bash\nwhile :; do /bin/sleep 0.2; done\n")
    sleeper.chmod(0o755)
    return sb


def _spawn(script: pathlib.Path) -> int:
    p = subprocess.Popen(["bash", str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    SPAWNED.append(p)
    for _ in range(50):
        if wid.proc_argv_vector(p.pid) is not None:
            break
        time.sleep(0.02)
    return p.pid


def _record(sb: pathlib.Path, pf: pathlib.Path, pid: int, instance: str, ws: pathlib.Path) -> None:
    """The record the watcher publishes, through the production writer."""
    subprocess.run(["bash", "-c",
                    '. "$1"; sentinel_lock_acquire "$2" || exit 4; '
                    'sentinel_write_record "$2" "$3" "$4" "$5" "$6" v "$7"; rc=$?; '
                    'sentinel_lock_release "$2"; exit $rc',
                    "_", str(sb / "src" / "watcher_sentinel.sh"), str(pf), str(pid), instance,
                    f"{int(time.time())}-{pid}-1", str(sb / "src" / "watch-tasks-stream.sh"), str(ws)],
                   check=True, capture_output=True, text=True, timeout=30)


def _core_alive(sb: pathlib.Path, env: dict) -> "tuple[int, str]":
    r = subprocess.run(["bash", str(sb / "src" / "watcher_identity.sh"), "core-alive"],
                       env=env, capture_output=True, text=True, timeout=60)
    return r.returncode, (r.stdout + r.stderr).strip()


def _sentinel_for(sb: pathlib.Path, env: dict, state: pathlib.Path, instance: "str | None") -> pathlib.Path:
    cmd = [sys.executable, str(sb / "src" / "util_paths.py"), "watcher-sentinel", str(state)]
    if instance:
        cmd.append(instance)
    return pathlib.Path(subprocess.run(cmd, env=env, capture_output=True, text=True, check=True).stdout.strip())


def _instance_key(sb: pathlib.Path, pf: pathlib.Path) -> str:
    return subprocess.run(["bash", "-c", '. "$1"; sentinel_instance_from_path "$2"', "_",
                           str(sb / "src" / "watcher_sentinel.sh"), str(pf)],
                          capture_output=True, text=True, check=True).stdout.strip()


box = pathlib.Path(tempfile.mkdtemp(prefix="core-alive-"))
try:
    sb = _sandbox(box)
    ws = box / "ws"
    state = ws / "state"
    (ws / "tasks").mkdir(parents=True)
    state.mkdir()
    env = dict(os.environ, SUTANDO_WORKSPACE=str(ws), SUTANDO_TEST_MODE="1")
    env.pop("SUTANDO_INSTANCE_ID", None)
    resolved = subprocess.run(["bash", str(sb / "scripts" / "sutando-config.sh"), "workspace"],
                              env=env, capture_output=True, text=True).stdout.strip()
    checks["harness: the sandbox resolves the declared workspace"] = (
        bool(resolved) and pathlib.Path(resolved).resolve() == ws.resolve())
    ws_phys = pathlib.Path(resolved) if resolved else ws
    core_pf = _sentinel_for(sb, env, state, None)
    worker_pf = _sentinel_for(sb, env, state, "worker-1")
    checks["harness: the core and worker resolve distinct sentinels"] = core_pf != worker_pf

    # (1) nothing recorded, nothing running -> not alive
    rc, why = _core_alive(sb, env)
    checks["no core record -> rc 1 (definitely not alive)"] = rc == 1
    if rc != 1:
        print(f"      got rc={rc}: {why}", file=sys.stderr)

    # (2) the masking case: a LIVE worker watcher, no core record
    worker_pid = _spawn(sb / "src" / "watch-tasks-stream.sh")
    _record(sb, worker_pf, worker_pid, _instance_key(sb, worker_pf), ws_phys)
    seen = wid.inspect_pid(worker_pid)
    checks["CONTROL: the worker IS a real watcher by the shared predicate (a host-wide probe would see it)"] = (
        seen.observed and seen.watcher is True)
    rc, why = _core_alive(sb, env)
    checks["core ABSENT + worker watcher LIVE -> rc 1: the worker does not stand in for the core"] = rc == 1
    if rc != 1:
        print(f"      got rc={rc}: {why}", file=sys.stderr)
    checks["...and the reason names the missing core record"] = "no core watcher record" in why

    # (3) positive control: a live, recorded core watcher of this checkout
    core_pid = _spawn(sb / "src" / "watch-tasks-stream.sh")
    _record(sb, core_pf, core_pid, _instance_key(sb, core_pf), ws_phys)
    rc, why = _core_alive(sb, env)
    checks["core record + live core watcher -> rc 0 (alive)"] = rc == 0
    if rc != 0:
        print(f"      got rc={rc}: {why}", file=sys.stderr)
    checks["...naming the core's pid, not the worker's"] = why.strip() == f"pid {core_pid}"

    # (4) the core died and left its record: dead pid -> not alive
    p = SPAWNED[-1]
    p.kill()
    p.wait()
    rc, why = _core_alive(sb, env)
    checks["core record naming a DEAD pid -> rc 1, while the worker is still live"] = rc == 1
    if rc != 1:
        print(f"      got rc={rc}: {why}", file=sys.stderr)
finally:
    for p in SPAWNED:
        try:
            p.kill()
            p.wait()
        except OSError:
            pass
    shutil.rmtree(box, ignore_errors=True)

fails = [k for k, ok in checks.items() if not ok]
for k, ok in checks.items():
    print(("ok   " if ok else "FAIL ") + k)
print("all checks pass" if not fails else f"FAILED ({len(fails)})")
sys.exit(1 if fails else 0)
