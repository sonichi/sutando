#!/usr/bin/env python3
"""A failed launch must RESTORE the shutdown sentinel — proved on the real launcher (#2165).

The sibling suites cannot see this failure. `shutdown-sentinel.test.py` drives the
helper directly, and `shutdown-sentinel-survives-failed-launch.test.py` supplies its
own restore call, so both pass while production exits first: the launchers run under
`set -e`, and a failed `exec` returns non-zero as a simple command, which errexit
treats as fatal before the restore line is ever reached. The source-order check only
proves a call textually follows `exec`.

So this test runs the ACTUAL launcher, with its own `set -e` and `shopt -s execfail`
intact, in a sandbox repo, and forces a genuine failed exec. If the sentinel is lost
the next core silently skips every task all session — a healthy-looking core that
stops answering.

Run: python3 tests/shutdown-sentinel-restored-by-real-launcher.test.py  (exit 0/1)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REAL_REPO = Path(__file__).resolve().parent.parent
# Only what the launcher sources or shells out to before its bare-exec branch.
NEEDED = (
    "src/agent/claude/cli/start-cli.sh",
    # everything the launcher sources before its bare-exec branch; a missing one
    # kills it at line 23 and the sentinel then "survives" only by never being cleared.
    "src/agent/restart-guard.sh",
    "src/claude_config_dir.sh",
    "src/shutdown.py",
    "src/workspace_default.py",
    "src/sutando_config.py",
    "src/util_paths.py",
    "scripts/python-binary.sh",
    "scripts/sutando-config.sh",
)
failures: list[str] = []


def _exe(path: Path, body: str, mode: int = 0o755) -> None:
    path.write_text(body)
    path.chmod(mode)


with tempfile.TemporaryDirectory() as tmp:
    root, binp = Path(tmp) / "repo", Path(tmp) / "bin"
    for rel in NEEDED:
        src = REAL_REPO / rel
        if not src.exists():
            failures.append(f"{rel}: missing from the repo — launcher cannot be exercised")
            continue
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    workspace = root / "workspace"
    (workspace / "state").mkdir(parents=True)
    (root / "sutando.config.json").write_text(
        json.dumps({"core": {"runtime": "claude"}, "workspace": {"path": str(workspace)}})
    )

    binp.mkdir()
    # `claude` resolves on PATH but cannot be executed -> a REAL failed exec (126),
    # which is the only way to reach the restore path under errexit.
    _exe(binp / "claude", "#!/bin/sh\nexit 0\n", 0o000)
    # No core is running in the sandbox; without this the launcher finds the host's
    # real core and exits early, never reaching the branch under test.
    _exe(binp / "pgrep", "#!/bin/sh\nexit 1\n")
    _exe(binp / "pkill", "#!/bin/sh\nexit 0\n")

    sentinel = workspace / "state" / "shutdown.sentinel"
    sentinel.write_text("stopped-on-purpose\n")

    # Hiding tmux by PATH ORDER fails where it lives in a dir the launcher needs.
    # So: build the PATH from scratch, with no tmux and no node (see below).
    for tool in ("bash", "sh", "env", "python3", "python", "uname", "mkdir", "rm",
                 "cat", "grep", "sed", "awk", "dirname", "basename", "mktemp",
                 "cp", "mv", "date", "tr", "id", "stat", "sleep", "head", "tail",
                 "wc", "cut", "sort", "touch", "chmod", "ln", "readlink"):
        # node is deliberately ABSENT: the launcher builds its --settings JSON with
        # it, and the sandbox has no .mjs, so present-but-broken errexits before the branch.
        src = shutil.which(tool)
        if src and not (binp / tool).exists():
            (binp / tool).symlink_to(src)
    assert shutil.which("tmux", path=str(binp)) is None, "sandbox PATH must not expose tmux"

    proc = subprocess.run(
        ["bash", str(root / "src/agent/claude/cli/start-cli.sh")],
        env={"HOME": os.environ.get("HOME", tmp), "PATH": str(binp), "TERM": "dumb"},
        capture_output=True, text=True, timeout=180,
    )

    if not sentinel.exists():
        failures.append(
            "the shutdown sentinel was LOST after a failed launch — errexit exits on the "
            "failed exec before restore_shutdown_sentinel runs, so the next core boot "
            "clears nothing and the intake gate holds every task all session")
    elif sentinel.read_text() != "stopped-on-purpose\n":
        failures.append(f"sentinel restored with wrong content: {sentinel.read_text()!r}")

    if proc.returncode in (0, 1):
        failures.append(
            f"exit status {proc.returncode} does not carry the failed exec's own status "
            f"(expected 126 for a non-executable target); a hardcoded `exit 1` hides which "
            f"launch failure occurred")

    if "failed to exec" not in proc.stderr:
        failures.append(
            "the failed-launch notice never printed — the restore branch was not reached")

if failures:
    # Without this a CI failure says only "an assertion failed" and costs a whole
    # round trip to learn which branch the launcher actually took.
    print("\n".join(f"  FAIL {f}" for f in failures))
    print(f"  --- launcher rc={proc.returncode} ---")
    for stream, name in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
        tail = [ln for ln in (stream or "").splitlines() if ln.strip()][-12:]
        print(f"  --- {name} (last {len(tail)}) ---")
        for ln in tail:
            print(f"      {ln[:160]}")
else:
    print("  ok  failed launch restores the shutdown sentinel, on the real launcher")
sys.exit(1 if failures else 0)
