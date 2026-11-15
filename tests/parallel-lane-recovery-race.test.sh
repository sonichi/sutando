#!/usr/bin/env bash
# Exercise recovery boundaries with real Git in disposable repositories.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 - "$here/scripts/parallel-suite-lane.sh" <<'PY'
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

lane = str(Path(sys.argv[1]).resolve())
real_git = shutil.which("git")
failed = 0


def git(repo, *args):
    return subprocess.check_output([real_git, "-C", str(repo), *args], text=True).strip()


def running(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path("/proc", str(pid), "stat")
    try:
        return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return not Path("/proc").exists()


def wait_for(predicate, scheduler, message, seconds=30):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert scheduler.poll() is None, message + ": scheduler exited"
        assert time.monotonic() < deadline, message + ": timed out"
        time.sleep(0.01)


wrapper = '''#!/usr/bin/env python3
import os
from pathlib import Path
import sys
import time
rec = Path(os.environ["LANE_RACE_RECORD"])
command = os.environ["LANE_RACE_COMMAND"]
if command in sys.argv[1:] and not (rec / "gate").exists():
    (rec / "gate").touch()
    if command == "checkout" and (rec / "children").exists():
        for pid in map(int, (rec / "children").read_text().split()):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            stat = Path("/proc", str(pid), "stat")
            if stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                continue
            (rec / "recovery-live").touch()
    deadline = time.monotonic() + 40
    while not (rec / "release").exists():
        if time.monotonic() > deadline:
            sys.exit("fixture gate timed out")
        time.sleep(0.01)
os.execv(os.environ["LANE_REAL_GIT"], [os.environ["LANE_REAL_GIT"], *sys.argv[1:]])
'''

descendant = '''import os
from pathlib import Path
import signal
import subprocess
import sys
import time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
rec, worker = map(Path, sys.argv[1:])
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
(rec / "children").write_text(f"{os.getpid()} {child.pid}")
for _ in range(1000):
    if os.environ.get("LANE_CHILD_SWAP") and (rec / "gate").exists():
        worker.rename(str(worker) + ".saved")
        worker.symlink_to(os.environ["LANE_CALLER"], target_is_directory=True)
        (rec / "swapped").touch()
        break
    if not worker.exists():
        (rec / "cleanup-live").touch()
        break
    time.sleep(0.01)
'''

with tempfile.TemporaryDirectory(prefix="lane-race-", dir=os.environ.get("TMPDIR")) as tmp:
    root = Path(tmp).resolve()
    creation_modes = tuple(f"create-{boundary}-{sig}" for boundary in ("added", "partial")
                           for sig in ("INT", "TERM", "HUP"))
    for mode in ("checkout", "clean", "child-checkout", "child-clean", "descendants", "spawn-interrupt", "cancel-failure") + creation_modes:
        rec = root / mode
        rec.mkdir()
        repo = rec / "repo"
        repo.mkdir()
        lane_tmp = rec / "tmp"
        lane_tmp.mkdir()
        env = dict(os.environ, TMPDIR=str(lane_tmp), GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_CONFIG_NOSYSTEM="1", LANE_REAL_GIT=real_git,
                   LANE_RACE_RECORD=str(rec), LANE_SCHEDULER=lane,
                   LANE_CALLER=str(repo),
                   LANE_RACE_COMMAND="checkout" if mode == "descendants" else mode.removeprefix("child-"))
        for key in subprocess.check_output([real_git, "rev-parse", "--local-env-vars"],
                                           text=True).splitlines():
            env.pop(key, None)
        for args in (("init", "-q"), ("config", "user.name", "Fixture"),
                     ("config", "user.email", "fixture@example.com"),
                     ("config", "core.hooksPath", os.devnull),
                     ("config", "commit.gpgSign", "false")):
            git(repo, *args)
        (repo / "tracked").write_text("baseline\n")
        git(repo, "add", "tracked")
        git(repo, "commit", "-qm", "baseline")
        head = git(repo, "rev-parse", "HEAD")
        symbolic = git(repo, "symbolic-ref", "HEAD")
        refs = git(repo, "show-ref")
        (repo / "tracked").write_text("caller-staged\n")
        git(repo, "add", "tracked")
        index = (repo / ".git/index").read_bytes()
        (repo / "tracked").write_text("caller-unstaged\n")
        (repo / "untracked").write_text("keep\n")
        bin_dir = rec / "bin"
        bin_dir.mkdir()
        (bin_dir / "git").write_text(wrapper)
        (bin_dir / "git").chmod(0o755)
        env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
        suite = rec / "suite.py"
        suite.write_text('''import os
from pathlib import Path
import subprocess
import sys
import time
rec = Path(os.environ["LANE_RACE_RECORD"])
(rec / "worker").write_text(str(Path.cwd()))
Path("tracked").write_text("suite-mutated\\n")
if (rec / "descendant.py").exists():
    subprocess.Popen([sys.executable, str(rec / "descendant.py"), str(rec), str(Path.cwd())],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 10
    while not (rec / "children").exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
if os.environ.get("LANE_HOLD_SUITE"):
    time.sleep(30)
print("suite-returned")
''')
        observe = rec / "observe.py"
        observe.write_text('from pathlib import Path\nassert Path("tracked").read_text() == "baseline\\n"\n')
        (rec / "files").write_text(str(suite) + "\n" + str(observe) + "\n")
        if mode in ("descendants", "spawn-interrupt", "child-checkout", "child-clean", "cancel-failure"):
            (rec / "descendant.py").write_text(descendant)
        if mode.startswith("child-"):
            env["LANE_CHILD_SWAP"] = "1"
        if mode == "descendants":
            (rec / "release").touch()
        if mode == "cancel-failure":
            bash_env = rec / "bash-env"
            bash_env.write_text('kill() { [ "$1" != -KILL ] || return 1; builtin kill "$@"; }\n')
            env["BASH_ENV"] = str(bash_env)
        if mode == "spawn-interrupt":
            env["LANE_HOLD_SUITE"] = "1"
            bash_env = rec / "bash-env"
            bash_env.write_text('''trap 'case "$BASH_COMMAND" in pids+=*)
        if [ "$0" = "$LANE_SCHEDULER" ] && [ ! -e "$LANE_RACE_RECORD/signalled" ]; then
            printf "%s\\n" "$!" > "$LANE_RACE_RECORD/spawned"
            for _probe in {1..100}; do
                [ -e "$LANE_RACE_RECORD/children" ] && break
                sleep 0.01
            done
            : > "$LANE_RACE_RECORD/signalled"
            kill -INT "$$"
        fi ;;
    esac' DEBUG
''')
            env["BASH_ENV"] = str(bash_env)
        if mode in creation_modes:
            _, boundary, sig = mode.split("-")
            env["LANE_CREATION_SIGNAL"] = sig
            env["LANE_CREATION_BOUNDARY"] = 'CREATED="$_w"' if boundary == "added" else 'worker_backlinks[$_w]='
            bash_env = rec / "bash-env"
            bash_env.write_text('''trap 'if [ "$0" = "$LANE_SCHEDULER" ] && [ "${_w:-0}" = 2 ] &&
        [[ "$BASH_COMMAND" == "$LANE_CREATION_BOUNDARY"* ]] && [ ! -e "$LANE_RACE_RECORD/signalled" ]; then
            [ -f "$WTDIR/$_w/.git" ] || exit 99
            printf "%s\\n" "$WTDIR/$_w" > "$LANE_RACE_RECORD/signalled"
            kill -"$LANE_CREATION_SIGNAL" "$$"
        fi' DEBUG
''')
            env["BASH_ENV"] = str(bash_env)
        scheduler = None
        try:
            with (rec / "log").open("w") as log:
                scheduler = subprocess.Popen(["bash", lane, "2" if mode in creation_modes else "1", str(rec / "files"), str(rec),
                                              sys.executable], cwd=repo, env=env,
                                             stdout=log, stderr=log, start_new_session=True)
                if mode in ("checkout", "clean", "child-checkout", "child-clean"):
                    wait_for(lambda: (rec / "gate").exists(), scheduler, "destructive gate")
                    worker = Path((rec / "worker").read_text())
                    if mode.startswith("child-"):
                        children = list(map(int, (rec / "children").read_text().split()))
                        wait_for(lambda: (rec / "swapped").exists() or not running(children[0]),
                                 scheduler, "owned child swap or termination")
                    else:
                        worker.rename(str(worker) + ".saved")
                        worker.symlink_to(repo, target_is_directory=True)
                    (rec / "release").touch()
                status = scheduler.wait(timeout=60)
            assert (repo / ".git/index").read_bytes() == index, "caller index bytes changed"
            assert git(repo, "show", ":tracked") == "caller-staged", "caller staged content changed"
            assert (repo / "tracked").read_text() == "caller-unstaged\n", "caller working file changed"
            assert (repo / "untracked").read_text() == "keep\n", "caller untracked file changed"
            assert git(repo, "rev-parse", "HEAD") == head, "caller HEAD changed"
            assert git(repo, "symbolic-ref", "HEAD") == symbolic, "caller symbolic HEAD changed"
            assert git(repo, "show-ref") == refs, "shared branch refs changed"
            if mode in creation_modes:
                assert (rec / "signalled").exists(), "creation interrupt was not injected"
                assert status == {"INT": 130, "TERM": 143, "HUP": 129}[sig], f"{sig} returned {status}"
                assert not (rec / "worker").exists(), "suite started after creation interrupt"
                assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1, "creation interrupt leaked a registration"
                assert not list(lane_tmp.iterdir()), "creation interrupt leaked lane files"
            elif mode in ("checkout", "clean"):
                assert status != 0, "swapped worker accepted"
                assert worker.is_symlink(), "suspect worker removed"
                assert Path(str(worker) + ".saved").is_dir(), "original worker removed"
                assert not (rec / "2.rc").exists(), "another suite reached replaced worker"
                assert "retaining worker files" in (rec / "log").read_text()
            elif mode == "cancel-failure":
                assert status != 0, "unconfirmed quiescence returned success"
                worker = Path((rec / "worker").read_text())
                assert worker.is_dir(), "cleanup removed a worker with live descendants"
                assert not (rec / "2.rc").exists(), "another suite reached an unquiesced worker"
                assert not (rec / "gate").exists(), "recovery reached an unquiesced worker"
                assert not (rec / "cleanup-live").exists(), "cleanup reached a running descendant"
                children = list(map(int, (rec / "children").read_text().split()))
                assert any(running(pid) for pid in children), "failed-cancellation control never stayed live"
                assert "retaining" in (rec / "log").read_text(), "missing retention diagnostic"
                assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 2
            else:
                if mode == "spawn-interrupt":
                    assert (rec / "signalled").exists(), "registration interrupt was not injected"
                    assert status == 130, f"SIGINT returned {status}"
                else:
                    assert status == 0, f"normal suite returned {status}"
                    assert (rec / "1.rc").read_text().strip() == "0"
                    assert (rec / "2.rc").read_text().strip() == "0"
                    assert not (rec / "swapped").exists(), "suite child survived until recovery"
                if (rec / "children").exists():
                    children = list(map(int, (rec / "children").read_text().split()))
                    deadline = time.monotonic() + 1
                    while any(running(pid) for pid in children) and time.monotonic() < deadline:
                        time.sleep(0.01)
                    assert not any(running(pid) for pid in children), "owned descendants survived exit"
                assert not (rec / "recovery-live").exists(), "recovery reached a running descendant"
                assert not (rec / "cleanup-live").exists(), "cleanup reached a running descendant"
                assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
            print(f"PASS: {mode} preserves caller work and enforces recovery ownership", flush=True)
        except (AssertionError, OSError, subprocess.SubprocessError) as error:
            failed += 1
            print(f"FAIL: {mode}: {error}", flush=True)
            print((rec / "log").read_text(), flush=True)
        finally:
            if scheduler is not None and scheduler.poll() is None:
                os.killpg(scheduler.pid, signal.SIGKILL)
                scheduler.wait()
            if (rec / "spawned").exists():
                try:
                    os.killpg(int((rec / "spawned").read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if (rec / "children").exists():
                for pid in map(int, (rec / "children").read_text().split()):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
sys.exit(bool(failed))
PY
