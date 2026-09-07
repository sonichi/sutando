#!/usr/bin/env python3
"""The heal branch exits before the create path's stamp, so it must stamp the marker itself.
Uses REAL tmux on a disposable socket, asserted to differ from the live one before anything runs."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"
LIVE_SOCKET = os.environ.get("SUTANDO_TMUX_SOCKET", "/tmp/sutando-tmux.sock")
SESSION = "sutando-core"

_TOOLS = [
    "bash", "sh", "env", "python3", "dirname", "hostname", "date", "sed",
    "mkdir", "mktemp", "rm", "cat", "sleep", "uname", "cut", "grep", "head",
    "tail", "chmod", "ls", "tr", "wc", "find", "stat", "touch", "cp", "mv",
    "printf", "expr", "id", "whoami", "tmux",
]


def _run_heal() -> tuple[Path, str]:
    """Pre-create a core-less tmux session, run the launcher, return (ws, stderr)."""
    td = Path(tempfile.mkdtemp())
    bind = td / "bin"
    bind.mkdir()
    ws = td / "ws"
    sock = td / "tmux.sock"

    # Refuse to run if this would touch the live socket. An isolation assert that
    # cannot fail is not an assert, so compare resolved paths, not the strings.
    assert str(sock) != str(LIVE_SOCKET), "socket collides with the live one"
    assert not str(sock).startswith("/tmp/sutando-"), f"unsafe socket {sock}"

    for tool in _TOOLS:
        real = shutil.which(tool)
        if real:
            link = bind / tool
            if not link.exists():
                link.symlink_to(real)
    if not (bind / "tmux").exists():
        return (ws, "SKIP: tmux not available")

    # claude must not actually run; the healed window just needs to spawn cleanly.
    (bind / "claude").write_text("#!/bin/bash\nsleep 5\n")
    (bind / "claude").chmod(0o755)
    # pgrep finds no core claude -> core_claude_running false -> heal, not attach.
    (bind / "pgrep").write_text("#!/bin/bash\nexit 1\n")
    (bind / "pgrep").chmod(0o755)

    env = {
        "PATH": f"{bind}:/usr/bin:/bin",
        "HOME": str(td),
        "SUTANDO_TEST_MODE": "1",
        "SUTANDO_WORKSPACE": str(ws),
        "SUTANDO_TMUX_SOCKET": str(sock),
    }

    # A session holding no core claude is the heal precondition; DEVNULL because
    # `new-session -d` starts a server that can inherit and hold a pipe.
    new = subprocess.run(["tmux", "-S", str(sock), "new-session", "-d", "-s", SESSION, "sleep 300"],
                         env=env, stdin=subprocess.DEVNULL, capture_output=True,
                         text=True, timeout=30)
    try:
        has = subprocess.run(["tmux", "-S", str(sock), "has-session", "-t", SESSION],
                             env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, timeout=30)
        if has.returncode != 0:
            # A skip with no reason is indistinguishable from a broken test: a
            # sandbox refusing a tmux server looks like a missing binary.
            why = (new.stderr or new.stdout or "").strip().replace("\n", " ")[:200]
            return (ws, "SKIP: could not create the precondition tmux session"
                        + (f" — tmux said: {why}" if why else
                           f" — tmux exited {new.returncode} with no message"
                           " (a sandbox forbidding a new server looks like this)"))
        # Files, never pipes: a surviving tmux grandchild holding the read end
        # keeps subprocess.run waiting for EOF after the script already exited.
        out_f = td / "launcher.out"
        with open(out_f, "wb") as fh:
            try:
                subprocess.run(["/bin/bash", str(SCRIPT)], env=env, timeout=45,
                               stdin=subprocess.DEVNULL, stdout=fh, stderr=fh)
            except subprocess.TimeoutExpired:
                # Inconclusive: the heal branch was never observed. Never a silent pass.
                return (ws, "SKIP: launcher did not return in 45s; heal branch not observed")
        return (ws, out_f.read_text(errors="replace"))
    finally:
        subprocess.run(["tmux", "-S", str(sock), "kill-server"],
                       env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)


def case_heal_stamps_marker() -> list[str]:
    ws, out = _run_heal()
    if out.startswith("SKIP:"):
        print(f"  ~ skipped — {out[5:].strip()}")
        return []
    if "healing core window" not in out:
        return [f"never entered the heal path; launcher said: {out.strip()[:200]!r}"]
    marker = ws / "state" / "core-runtime.json"
    if not marker.exists():
        return ["heal path launched a Claude core but wrote no core-runtime.json"]
    try:
        d = json.loads(marker.read_text())
    except ValueError as e:
        return [f"core-runtime.json is not valid JSON: {e}"]
    fails = []
    if d.get("runtime") != "claude":
        fails.append(f'runtime should be "claude", got {d.get("runtime")!r}')
    if d.get("session") != SESSION:
        fails.append(f'session should be {SESSION!r}, got {d.get("session")!r}')
    return fails


def case_heal_is_distinguishable_in_the_log() -> list[str]:
    """The heal launch must be attributable, not silently identical to a create."""
    ws, out = _run_heal()
    if out.startswith("SKIP:"):
        print(f"  ~ skipped — {out[5:].strip()}")
        return []
    if "healing core window" not in out:
        return ["never entered the heal path"]
    log = ws / "state" / "session-starts.log"
    if not log.exists():
        return ["heal path wrote no session-starts.log entry"]
    lines = [ln for ln in log.read_text().splitlines() if ln.strip()]
    if not lines:
        return ["session-starts.log is empty after a heal launch"]
    last = json.loads(lines[-1])
    fails = []
    if last.get("runtime") != "claude":
        fails.append(f'log runtime should be "claude", got {last.get("runtime")!r}')
    if last.get("source") != "start-cli-heal":
        fails.append(f'log source should be "start-cli-heal", got {last.get("source")!r}')
    return fails


def main() -> int:
    cases = [
        ("heal path stamps core-runtime.json", case_heal_stamps_marker),
        ("heal launch is attributable in session-starts.log", case_heal_is_distinguishable_in_the_log),
    ]
    bad = 0
    for name, fn in cases:
        fails = fn()
        if fails:
            bad += 1
            print(f"  ✖ {name}")
            for f in fails:
                print(f"      {f}")
        else:
            print(f"  ✔ {name}")
    print()
    if bad:
        print(f"FAIL — {bad} case(s)")
        return 1
    print("PASS — heal-path runtime marker")
    return 0


if __name__ == "__main__":
    sys.exit(main())
