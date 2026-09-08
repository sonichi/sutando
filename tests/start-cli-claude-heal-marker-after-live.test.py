#!/usr/bin/env python3
"""Pins that the Claude HEAL path publishes core-runtime.json only AFTER the
healed core is verifiably live (#2406 review): tmux accepting `new-window` is
not evidence the child survived, so a heal that fails must leave no marker.

Sibling of start-cli-codex-marker-after-session.test.py, which pins the same
invariant for the Codex launcher's fresh-start path. Drives the real script
with stub tmux/pgrep/ps: the session always exists and no core runs, which is
exactly the heal precondition; LAUNCH_OK decides whether the healed window
comes up."""
from __future__ import annotations
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"
SESSION = "sutando-core"

_TOOLS = [
    "bash", "sh", "env", "python3", "dirname", "hostname", "date", "sed",
    "mkdir", "mktemp", "rm", "cat", "sleep", "uname", "cut", "grep", "head",
    "tail", "chmod", "ls", "tr", "wc", "find", "stat", "touch", "cp", "mv",
    "printf", "expr", "id", "whoami", "basename", "awk", "sort", "cksum",
    "xargs", "true", "false", "test", "readlink", "od", "seq", "kill",
]

# has-session always succeeds (the surviving session) and new-window reports an
# index; only LAUNCH_OK decides whether a core process then exists.
TMUX_STUB = """#!/bin/bash
while [ "$1" = "-S" ]; do shift 2; done
case "$1" in
  has-session) exit 0 ;;
  new-window)  [ "$LAUNCH_OK" = 1 ] && : > "$HOME/.core-alive"; echo 0; exit 0 ;;
  *) exit 0 ;;
esac
"""

# Core probe is `pgrep -ax claude` + `ps -p <pid> -o args=`; a `-f` query instead
# asks whether a supervisor runs, and yes there keeps real daemons out of the test.
PGREP_STUB = """#!/bin/bash
[ "$1" = "-f" ] && exit 0
[ -f "$HOME/.core-alive" ] || exit 1
echo "4242 claude"
"""
PS_STUB = """#!/bin/bash
if [ -f "$HOME/.core-alive" ]; then echo "claude --name %s -- /startup"; fi
exit 0
""" % SESSION


def _run(launch_ok: bool) -> Path:
    td = Path(tempfile.mkdtemp())
    ws = td / "workspace"; (ws / "state").mkdir(parents=True)
    bind = td / "bin"; bind.mkdir()
    for tool in _TOOLS:
        real = shutil.which(tool)
        if real and not (bind / tool).exists():
            (bind / tool).symlink_to(real)
    for name, body in (
        ("claude", "#!/bin/bash\nexit 0\n"),
        ("fswatch", "#!/bin/bash\nexit 0\n"),
        ("pgrep", PGREP_STUB),
        ("ps", PS_STUB),
        ("tmux", TMUX_STUB),
    ):
        p = bind / name; p.write_text(body); p.chmod(0o755)
    # A live pid the relay's `kill -0` guard accepts, so it does not start its
    # own loop either. Same purpose as the pgrep -f answer above.
    (ws / "state" / "core-supervisor-relay-loop.pid").write_text(str(os.getpid()))
    env = {
        "PATH": str(bind), "HOME": str(td),
        "SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(ws),
        "SUTANDO_TMUX_SOCKET": str(td / "sock"),
        "LAUNCH_OK": "1" if launch_ok else "0",
    }
    r = subprocess.run(["/bin/bash", str(SCRIPT)], env=env,
                       capture_output=True, text=True, timeout=120)
    return ws, r


def _marker(ws: Path) -> Path:
    return ws / "state" / "core-runtime.json"


failures = []

# NEGATIVE: the healed window never came up -> no marker, and a non-zero exit
# rather than the "Healed core window" success line.
ws, r = _run(launch_ok=False)
if _marker(ws).exists():
    failures.append("a FAILED heal wrote core-runtime.json (publish is not gated)")
else:
    print("  ok  failed heal leaves no core-runtime.json")
if r.returncode == 0:
    failures.append(f"a FAILED heal exited 0 — it reports success (rc={r.returncode})")
else:
    print(f"  ok  failed heal exits non-zero (rc={r.returncode})")

# POSITIVE: without this the negative passes by construction and certifies
# nothing — the heal path might simply never write a marker at all.
ws, r = _run(launch_ok=True)
if _marker(ws).exists():
    print("  ok  successful heal DOES write core-runtime.json")
else:
    failures.append("successful heal wrote no marker — the negative case proves nothing")

if failures:
    for f in failures:
        print(f"  FAIL  {f}")
    sys.exit(1)
print("all checks passed — claude heal marker published only after a verified live core")
