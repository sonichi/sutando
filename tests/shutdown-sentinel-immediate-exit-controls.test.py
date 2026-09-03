#!/usr/bin/env python3
"""A tmux branch that accepts the command but whose child dies must NOT clear the sentinel.

The sibling suite `shutdown-sentinel-survives-failed-launch.test.py` cannot see this
failure: it accepts nearby token presence (`healed_idx`, `new-session`) as proof of a
liveness check, so all 22 of its checks passed at 7cc1414c, when all three branches
still cleared immediately after command acceptance. A control that passes on the broken
code is not a control.

So this drives the REAL launcher into each branch with tmux ACCEPTING every command
while no core process exists — `new-window`/`new-session` return success, the liveness
probe stays false. That is the immediate-exit case: tmux took the command, the child
was gone before anyone looked. The sentinel must survive it, because clearing it opens
task intake when no core is serving.

Run: python3 tests/shutdown-sentinel-immediate-exit-controls.test.py  (exit 0/1)
"""
from __future__ import annotations

import os
import pty
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from pathlib import Path

REAL_REPO = Path(__file__).resolve().parent.parent
NEEDED = (
    "src/agent/claude/cli/start-cli.sh",
    "src/agent/codex/cli/start-cli.sh",
    "src/agent/restart-guard.sh",
    "src/claude_config_dir.sh",
    "src/shutdown.py",
    "src/workspace_default.py",
    "src/sutando_config.py",
    "src/util_paths.py",
    "src/agent/codex/cli/task-notifier.sh",
    "src/agent/codex/cli/task-notifier-supervisor.sh",
    "src/watch-tasks-stream.sh",
    "scripts/python-binary.sh",
    "scripts/sutando-config.sh",
    # without these the launcher aborts before tmux and the assert is vacuous
    "scripts/install-personal-claude-hook.sh",
)
failures: list[str] = []


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


# tmux accepts every command and reports the session present; liveness is
# answered separately by pgrep, which finds nothing.
TMUX_STUB = """#!/bin/bash
args="$*"
case "$args" in
  *new-window*|*new-session*) echo 0; exit 0 ;;
  *has-session*)              exit ${STUB_HAS_SESSION_RC:-0} ;;
  *list-windows*)             echo "0: core"; exit 0 ;;
  *)                          exit 0 ;;
esac
"""
# No process ever matches, so core_claude_running() is false before AND after
# the window is created — the child that exited immediately.
PGREP_STUB = "#!/bin/bash\nexit 1\n"
CODEX_STUB = "#!/bin/bash\nexit 0\n"


def _launch(cmd, env, cwd, tty: bool):
    """Run the launcher, optionally under a pty so `[ -t 1 ]` is true."""
    if not tty:
        return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd, timeout=180)
    # capture_output makes stdout a pipe, so `[ -t 1 ]` is false and the TTY
    # branch is unreachable however the case is labelled. A pty is the only way in.
    mfd, sfd = pty.openpty()
    chunks: list[bytes] = []

    def _drain():
        while True:
            try:
                b = os.read(mfd, 4096)
            except OSError:
                break
            if not b:
                break
            chunks.append(b)

    th = threading.Thread(target=_drain, daemon=True)
    th.start()
    proc = subprocess.Popen(cmd, stdin=sfd, stdout=sfd, stderr=subprocess.PIPE,
                            text=True, env=env, cwd=cwd)
    err = proc.communicate(timeout=180)[1]
    os.close(sfd)
    th.join(timeout=2)
    try:
        os.close(mfd)
    except OSError:
        pass
    return SimpleNamespace(returncode=proc.returncode,
                           stdout=b"".join(chunks).decode("utf-8", "replace"),
                           stderr=err or "")


def run_branch(label: str, launcher_rel: str, env_extra: dict, *, tty: bool = False) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root, binp = Path(tmp) / "repo", Path(tmp) / "bin"
        binp.mkdir(parents=True)
        missing = False
        for rel in NEEDED:
            src = REAL_REPO / rel
            if not src.exists():
                failures.append(f"{label}: {rel} missing from repo")
                missing = True
                continue
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        if missing:
            return

        _exe(binp / "tmux", TMUX_STUB)
        _exe(binp / "pgrep", PGREP_STUB)
        _exe(binp / "codex", CODEX_STUB)
        _exe(binp / "fswatch", "#!/bin/bash\nexit 0\n")
        # mktemp is load-bearing: without it claude_config_dir.sh fails and the
        # launcher exits before reaching tmux, making the assert vacuous.
        for real in ("bash", "python3", "sed", "awk", "grep", "ps", "seq", "sleep",
                     "cat", "mkdir", "rm", "date", "uname", "dirname", "basename",
                     "tr", "head", "tail", "cut", "wc", "sort", "id", "hostname",
                     "cksum", "mktemp", "touch", "chmod", "ln", "cp", "mv", "pwd", "expr",
                     "printf", "sh", "find", "xargs", "stat", "realpath", "env", "which"):
            found = shutil.which(real)
            if found:
                try:
                    (binp / real).symlink_to(found)
                except FileExistsError:
                    pass

        ws = root / "workspace"
        (ws / "state").mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.update({
            "PATH": str(binp),
            "HOME": str(root),
            "SUTANDO_TMUX_SOCKET": str(root / "sock"),
            "SUTANDO_TMUX_SESSION": "sutando-core",
        })
        env.update(env_extra)
        # `[ -t 1 ] && [ -z "$TMUX" ]` — a pty alone is not enough if TMUX is inherited.
        if tty:
            env.pop("TMUX", None)

        # Read it back after marking: otherwise "still set" is vacuous, since a
        # probe that never sees a set sentinel cannot fail.
        mark = subprocess.run([sys.executable, str(root / "src/shutdown.py"), "mark", label],
                              capture_output=True, text=True, env=env, cwd=str(root))
        path_p = subprocess.run([sys.executable, str(root / "src/shutdown.py"), "path"],
                                capture_output=True, text=True, env=env, cwd=str(root))
        sentinel = Path(path_p.stdout.strip()) if path_p.stdout.strip() else None
        if sentinel is None or not sentinel.exists():
            failures.append(f"{label}: setup failed — sentinel not set after mark "
                            f"(mark rc={mark.returncode}, stderr={mark.stderr.strip()[:200]})")
            return

        proc = _launch(["bash", str(root / launcher_rel)], env, str(root), tty)

        # Both branches print "did not come up", so that alone cannot prove WHICH ran.
        # The detached tail is unique to the else branch; a tty case emitting it collapsed.
        if tty and "Started sutando-core detached" in (proc.stdout + proc.stderr):
            failures.append(
                f"{label}: took the DETACHED branch despite tty=True — this control is a "
                f"duplicate of the detached one, not coverage of the TTY path")
            return

        # A branch that never ran also leaves the sentinel in place, so "SURVIVED"
        # cannot tell a working gate from an unreached one without this line.
        if "did not come up" not in proc.stderr:
            failures.append(
                f"{label}: launcher never reached the liveness gate (rc={proc.returncode}) — "
                f"this check would pass against ANY implementation. stderr tail: "
                f"{proc.stderr.strip()[-300:]}")
            return
        if sentinel.exists():
            print(f"OK: {label} — tmux accepted the command, no core lived, sentinel SURVIVED")
        else:
            failures.append(
                f"{label}: sentinel was CLEARED after tmux merely accepted the command "
                f"while no core process existed — task intake would open with nothing serving. "
                f"launcher rc={proc.returncode}\n"
                f"  stdout tail: {proc.stdout.strip()[-400:]}\n"
                f"  stderr tail: {proc.stderr.strip()[-400:]}")


run_branch("claude-heal", "src/agent/claude/cli/start-cli.sh", {})
run_branch("codex-detached", "src/agent/codex/cli/start-cli.sh",
           {"TMUX": "forced", "STUB_HAS_SESSION_RC": "1"})
run_branch("codex-tty", "src/agent/codex/cli/start-cli.sh",
           {"STUB_HAS_SESSION_RC": "1"}, tty=True)



if failures:
    print("\nFAILURES:")
    for f in failures:
        print(" -", f)
    raise SystemExit(1)
print("\nAll immediate-exit controls passed.")
