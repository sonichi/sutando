#!/usr/bin/env python3
"""Pins that the Codex launcher publishes core-runtime.json only AFTER the tmux
session exists and is verifiably ours (#2406 review): a failed launch must not
replace a truthful marker for a runtime that never became live.

Drives the real launcher with a stub tmux whose has-session/show-environment
answers are set per-case, so both outcomes are exercised hermetically."""
from __future__ import annotations
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "agent" / "codex" / "cli" / "start-cli.sh"

_TOOLS = [
    "bash", "sh", "env", "python3", "dirname", "hostname", "date", "sed",
    "mkdir", "mktemp", "rm", "cat", "sleep", "uname", "cut", "grep", "head",
    "tail", "chmod", "ls", "tr", "wc", "find", "stat", "touch", "cp", "mv",
    "printf", "expr", "id", "whoami", "basename", "awk", "sort", "cksum",
    "xargs", "true", "false", "test", "readlink", "od", "seq", "kill", "ps",
]

TMUX_STUB = """#!/bin/bash
# Stateful stub tmux: no session exists until new-session creates one, so the
# launcher's already-running guard does not short-circuit the launch path.
# LAUNCH_OK=0 models a launch that does not take (session never appears).
STATE="$HOME/.tmux-stub-sessions"
while [ "$1" = "-S" ]; do shift 2; done
sub="$1"; shift
target=""
while [ $# -gt 0 ]; do
  case "$1" in
    -t) target="${2#=}"; shift 2 ;;
    -s) target="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$sub" in
  new-session)
    [ "${LAUNCH_OK:-0}" = "1" ] && echo "$target" >> "$STATE"
    exit 0 ;;
  has-session)
    grep -qxF "$target" "$STATE" 2>/dev/null && exit 0 || exit 1 ;;
  show-environment)
    grep -qxF "$target" "$STATE" 2>/dev/null && echo "SUTANDO_CORE_RUNTIME=codex"
    exit 0 ;;
  *) exit 0 ;;
esac
"""


def _run(launch_ok: bool) -> Path:
    td = Path(tempfile.mkdtemp())
    bind = td / "bin"; bind.mkdir()
    ws = td / "ws"
    for tool in _TOOLS:
        real = shutil.which(tool)
        if real and not (bind / tool).exists():
            (bind / tool).symlink_to(real)
    for name, body in (
        ("codex", "#!/bin/bash\n[ \"$1\" = login ] && exit 0\nexit 0\n"),
        ("fswatch", "#!/bin/bash\nexit 0\n"),
        ("pgrep", "#!/bin/bash\nexit 1\n"),
        ("tmux", TMUX_STUB),
    ):
        p = bind / name; p.write_text(body); p.chmod(0o755)
    env = {
        "PATH": str(bind), "HOME": str(td),
        "SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(ws),
        "LAUNCH_OK": "1" if launch_ok else "0",
    }
    subprocess.run(["/bin/bash", str(SCRIPT)], env=env,
                   capture_output=True, text=True, timeout=60)
    return ws


def _marker(ws: Path) -> Path:
    return ws / "state" / "core-runtime.json"


failures = []

# NEGATIVE: the launch did not produce a session we own -> no marker.
ws = _run(launch_ok=False)
if _marker(ws).exists():
    failures.append("a failed launch WROTE core-runtime.json (publish is not gated)")
else:
    print("  ok  failed launch leaves no core-runtime.json")

# POSITIVE: the probe can produce a marker at all — otherwise the negative above
# passes by construction and certifies nothing.
ws = _run(launch_ok=True)
if _marker(ws).exists():
    print("  ok  successful launch DOES write core-runtime.json")
else:
    failures.append("successful launch wrote no marker — the negative case proves nothing")

if failures:
    for f in failures:
        print(f"  FAIL  {f}")
    sys.exit(1)
print("all checks passed — codex marker published only after verified session")
