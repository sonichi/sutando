#!/usr/bin/env python3
"""Pins that a FAILED Claude launch leaves no false core-runtime.json claim.
Drives the real no-tmux path with a claude stub whose exec fails (bad interpreter)."""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"

_TOOLS = [
    "bash", "sh", "env", "python3", "dirname", "hostname", "date", "sed",
    "mkdir", "mktemp", "rm", "cat", "sleep", "uname", "cut", "grep", "head",
    "tail", "chmod", "ls", "tr", "wc", "find", "stat", "touch", "cp", "mv",
    "printf", "expr", "id", "whoami",
]


def _run_launcher(exec_ok: bool, prior: dict | None) -> tuple[Path, subprocess.CompletedProcess]:
    """Run start-cli.sh through the no-tmux path. exec_ok=False makes exec claude fail."""
    td = Path(tempfile.mkdtemp())
    bind = td / "bin"
    bind.mkdir()
    ws = td / "ws"
    if prior is not None:
        (ws / "state").mkdir(parents=True)
        (ws / "state" / "core-runtime.json").write_text(json.dumps(prior) + "\n")

    for tool in _TOOLS:
        real = shutil.which(tool)
        if real:
            link = bind / tool
            if not link.exists():
                link.symlink_to(real)
    # exec_ok=False: a real, executable file whose INTERPRETER is missing, so
    # `command -v claude` still succeeds and only the exec itself fails.
    body = "#!/bin/bash\nexit 0\n" if exec_ok else "#!/no/such/interpreter\n"
    (bind / "claude").write_text(body)
    (bind / "claude").chmod(0o755)
    (bind / "pgrep").write_text("#!/bin/bash\nexit 1\n")
    (bind / "pgrep").chmod(0o755)

    env = {
        "PATH": str(bind),
        "HOME": str(td),
        "SUTANDO_TEST_MODE": "1",
        "SUTANDO_WORKSPACE": str(ws),
    }
    proc = subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    return ws, proc


def case_failed_launch_writes_no_marker() -> list[str]:
    """No prior marker + failed exec => truthful absence, not a claude claim."""
    ws, proc = _run_launcher(exec_ok=False, prior=None)
    marker = ws / "state" / "core-runtime.json"
    if marker.exists():
        return [
            "a FAILED launch left core-runtime.json claiming "
            f"{json.loads(marker.read_text()).get('runtime')!r}; expected no file at all"
        ]
    return []


def case_failed_launch_preserves_prior_marker() -> list[str]:
    """A prior codex marker must survive a failed claude launch unchanged."""
    prior = {"runtime": "codex", "session": "sutando-core", "started_at": 1}
    ws, proc = _run_launcher(exec_ok=False, prior=prior)
    marker = ws / "state" / "core-runtime.json"
    if not marker.exists():
        return ["a failed claude launch DELETED a pre-existing marker; expected it preserved"]
    got = json.loads(marker.read_text())
    if got != prior:
        return [f"failed launch overwrote the prior marker: {got!r} != {prior!r}"]
    return []


def case_successful_launch_still_writes_marker() -> list[str]:
    """The positive control: the rollback must not suppress a GOOD launch's marker."""
    ws, proc = _run_launcher(exec_ok=True, prior=None)
    marker = ws / "state" / "core-runtime.json"
    if not marker.exists():
        return ["a SUCCESSFUL launch wrote no core-runtime.json — rollback is over-eager"]
    got = json.loads(marker.read_text())
    if got.get("runtime") != "claude":
        return [f'successful launch should claim "claude", got {got.get("runtime")!r}']
    return []


def main() -> int:
    cases = [
        case_failed_launch_writes_no_marker,
        case_failed_launch_preserves_prior_marker,
        case_successful_launch_still_writes_marker,
    ]
    failures = []
    for c in cases:
        got = c()
        print(f"  {'FAIL' if got else 'ok  '}  {c.__name__}")
        for g in got:
            print(f"        {g}")
        failures += got
    print(f"\n{len(cases) - sum(1 for c in cases if False)} case(s); {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
