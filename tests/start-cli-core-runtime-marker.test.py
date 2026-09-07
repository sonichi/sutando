#!/usr/bin/env python3
"""Pins that the Claude launcher writes core-runtime.json and a runtime field BEFORE exec.
Drives the real launcher through its no-tmux fallback with stub claude/pgrep."""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"

# A curated bin dir (coreutils + stub claude/pgrep, no tmux) forces the no-tmux
# fallback, so the run never touches the real socket; /usr/bin would re-add tmux.
_TOOLS = [
    "bash", "sh", "env", "python3", "dirname", "hostname", "date", "sed",
    "mkdir", "mktemp", "rm", "cat", "sleep", "uname", "cut", "grep", "head",
    "tail", "chmod", "ls", "tr", "wc", "find", "stat", "touch", "cp", "mv",
    "printf", "expr", "id", "whoami", "mktemp",
]


def _run_launcher() -> Path:
    """Run start-cli.sh (no-tmux fallback, no real tmux); return the tmp workspace."""
    td = Path(tempfile.mkdtemp())
    bind = td / "bin"
    bind.mkdir()
    ws = td / "ws"

    # Symlink each real tool into bind. Skip tmux entirely so `command -v tmux`
    # fails and the launcher takes the no-tmux path.
    for tool in _TOOLS:
        real = shutil.which(tool)
        if real:
            link = bind / tool
            if not link.exists():
                link.symlink_to(real)
    # Stub claude: exit cleanly (the launcher exec's it AFTER the marker block).
    (bind / "claude").write_text("#!/bin/bash\nexit 0\n")
    (bind / "claude").chmod(0o755)
    # Stub pgrep: always "no match" so the already-running guard passes.
    (bind / "pgrep").write_text("#!/bin/bash\nexit 1\n")
    (bind / "pgrep").chmod(0o755)

    env = {
        "PATH": str(bind),  # ONLY the curated bin — no tmux anywhere
        "HOME": str(td),
        # Sanctioned test escape hatch (src/sutando_config.py resolve_workspace):
        # redirect workspace to our tmp dir so we never touch the real one.
        "SUTANDO_TEST_MODE": "1",
        "SUTANDO_WORKSPACE": str(ws),
    }
    subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    return ws


def case_core_runtime_marker() -> list[str]:
    fails = []
    ws = _run_launcher()
    marker = ws / "state" / "core-runtime.json"
    if not marker.exists():
        return ["core-runtime.json was not written by the Claude launcher"]
    try:
        d = json.loads(marker.read_text())
    except ValueError as e:
        return [f"core-runtime.json is not valid JSON: {e}"]
    if d.get("runtime") != "claude":
        fails.append(f'runtime should be "claude", got {d.get("runtime")!r}')
    if d.get("session") != "sutando-core":
        fails.append(f'session should be "sutando-core", got {d.get("session")!r}')
    if not isinstance(d.get("started_at"), int):
        fails.append(f"started_at should be an int epoch, got {d.get('started_at')!r}")
    return fails


def case_session_starts_runtime_field() -> list[str]:
    fails = []
    ws = _run_launcher()
    log = ws / "state" / "session-starts.log"
    if not log.exists():
        return ["session-starts.log was not written"]
    lines = [ln for ln in log.read_text().splitlines() if ln.strip()]
    if not lines:
        return ["session-starts.log is empty"]
    try:
        last = json.loads(lines[-1])
    except ValueError as e:
        return [f"session-starts.log last line is not valid JSON: {e}"]
    if last.get("source") != "start-cli":
        fails.append(f'source should be "start-cli", got {last.get("source")!r}')
    if last.get("runtime") != "claude":
        fails.append(f'session-starts.log runtime should be "claude" (parity with Codex), got {last.get("runtime")!r}')
    return fails


def case_detached_publish_is_behind_the_liveness_gate() -> list[str]:
    """Ordering, not presence: a publish before the gate can replace a truthful
    marker with a runtime that never came up."""
    src = SCRIPT.read_text(encoding="utf-8")
    fails: list[str] = []
    # The heal path says "did not come up within" too and sits EARLIER, so a
    # global first-match pairs each branch with the wrong gate.
    try:
        region = src.index('if [ -t 1 ]; then\n  ensure_core_monitor')
    except ValueError as exc:
        return [f"could not locate the tmux launch block: {exc}"]
    launches = [i for i in range(region, len(src))
                if src.startswith("new-session -d", i)]
    if len(launches) != 2:
        return [f"expected 2 detached launches (tty + non-tty), found {len(launches)}"]
    for n, start in enumerate(launches, 1):
        end = launches[n] if n < len(launches) else len(src)
        block = src[start:end]
        gate = block.find("did not come up within")
        pub = block.find("stamp_runtime_claude")
        if gate < 0 or pub < 0:
            fails.append(f"branch {n}: missing gate ({gate}) or publish ({pub})")
        elif gate > pub:
            fails.append(f"branch {n} publishes BEFORE its liveness gate "
                         f"(gate={gate}, publish={pub})")
    return fails


def case_exec_paths_publish_adjacent_to_exec() -> list[str]:
    """`exec` replaces the process, so no post-launch point exists on those paths.
    Pinned so pre-exec publication cannot spread to a path that CAN verify."""
    src = SCRIPT.read_text(encoding="utf-8")
    fails: list[str] = []
    # `new-session -A` published blind; its ABSENCE is the property now.
    # Code lines only: a comment naming `new-session -A` is not a launch.
    revived = [ln for ln in src.splitlines()
               if "new-session -A" in ln and not ln.lstrip().startswith("#")]
    if revived:
        fails.append("an unverifiable `new-session -A` exec launch is back; it "
                     f"publishes before anything can confirm the core came up: {revived[0].strip()!r}")
    for anchor in ("exec claude --name",):
        i = src.find(anchor)
        if i < 0:
            fails.append(f"launch anchor vanished: {anchor!r}")
            continue
        if "stamp_runtime_claude" not in src[max(0, i - 260):i]:
            fails.append(f"no publish adjacent to {anchor!r}")
    return fails


def main() -> int:
    cases = [
        ("core-runtime-marker", case_core_runtime_marker),
        ("session-starts-runtime-field", case_session_starts_runtime_field),
        ("detached publish is behind the liveness gate", case_detached_publish_is_behind_the_liveness_gate),
        ("exec paths publish adjacent to exec", case_exec_paths_publish_adjacent_to_exec),
    ]
    all_failures = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as e:  # noqa: BLE001
            fails = [f"{label}) raised {type(e).__name__}: {e}"]
        if fails:
            all_failures.extend(fails)
            print(f"  ✗ case {label}")
            for f in fails:
                print(f"      {f}")
        else:
            print(f"  ✓ case {label}")
    if all_failures:
        print(f"\n{len(all_failures)} failure(s)")
        return 1
    print("\nstart-cli.sh writes the Claude runtime marker correctly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
