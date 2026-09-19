#!/usr/bin/env python3
"""Step 5.5 gate: does this boot need to start a heartbeat writer? Prints one word.

  skip     .alive is fresh, its `pid` is THIS core's pane and its `heartbeat_pid` is a live writer
           running THIS checkout's core_heartbeat.py
  start    anything else the probes could decide: no file, stale, another pane's pid, the writer
           dead, not a writer, or another checkout's. A live writer the record names is stopped
           first (--stop).
  unknown  tmux or ps did not answer (exit 2)

Exit 3: the named writer would not stop; the command to run is printed. A fresh mtime alone never
satisfies the gate: a writer orphaned by a previous session keeps the file fresh and records the
new pane's pid into it, so the file looked owned while nothing of this boot wrote it.

Usage:
  python3 skills/schedule-crons/scripts/heartbeat-gate.py            # decide, stopping a foreign writer
  python3 skills/schedule-crons/scripts/heartbeat-gate.py --no-stop  # decide only; print the stop command
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
import core_heartbeat as hb  # noqa: E402

STALE_S = 90.0  # the documented staleness window every reader of .alive uses
HB_SCRIPT = str(Path(hb.__file__).resolve())


def current_core_pid() -> tuple:
    """(pid, observed): the pane pid write_beat would record, and whether tmux answered."""
    sock = hb._socket_path()
    hb._LAST_SESSION_PROBE = False
    pid = hb.core_pid(sock, hb._observed_session(sock))
    return pid, hb._LAST_SESSION_PROBE is not None


def _recorded_script(pid) -> "str | None":
    """The script the writer itself recorded beside .alive (`<pid> <resolved path>`), if it is this pid's."""
    try:
        head, _, path = hb._pidfile().read_text().strip().partition(" ")
        return path if head == str(pid) and path else None
    except Exception:
        return None


def writer_state(pid) -> "str | None":
    """'own' (a live writer running THIS checkout's core_heartbeat.py), 'foreign' (a live writer whose
    script is another checkout's or unresolvable), 'dead', or None when ps cannot answer."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return "dead"
    try:
        r = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if r.returncode != 0:
        return "dead"
    args = (r.stdout or "").strip()
    if not hb._is_writer_argv(args, HB_SCRIPT):
        return "dead"
    script = _recorded_script(pid) or hb._WRITER_SCRIPT_RE.search(args).group(1)
    try:
        own = Path(script).is_absolute() and Path(script).resolve() == Path(HB_SCRIPT)
    except Exception:
        own = False
    return "own" if own else "foreign"


def decide(now: "float | None" = None) -> tuple:
    """(word, reason, live_writer_pid): live_writer_pid is set only when `start` must stop one first."""
    now = time.time() if now is None else now
    alive = hb._alive_path()
    try:
        record = json.loads(alive.read_text())
        age = now - alive.stat().st_mtime
    except FileNotFoundError:
        return "start", "no .alive", None
    except Exception as e:
        return "start", f"unreadable .alive ({e.__class__.__name__})", None
    hpid = record.get("heartbeat_pid") if isinstance(record, dict) else None
    state = writer_state(hpid)
    if state is None:
        return "unknown", f"ps could not answer for heartbeat_pid {hpid}", None
    live = hpid if state in ("own", "foreign") else None
    if age > STALE_S:
        return "start", f".alive is stale ({age:.0f}s)", live
    core, observed = current_core_pid()
    if not observed:
        return "unknown", "tmux could not answer for this core's pane", None
    if record.get("pid") != core:
        return "start", f".alive names pid {record.get('pid')}, this core's pane is {core}", live
    if live is None:
        return "start", f"heartbeat_pid {hpid} is not a live writer", None
    if state == "foreign":
        return "start", f"heartbeat_pid {hpid} runs another checkout's core_heartbeat.py", live
    return "skip", f"fresh, pid {core} is this pane, writer {hpid} is this checkout's", None


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--no-stop", action="store_true", help="never stop a writer; print the command instead")
    args = p.parse_args(argv)
    word, reason, live = decide()
    stop_cmd = [sys.executable, HB_SCRIPT, "--stop"]
    if word == "start" and live is not None:
        if args.no_stop:
            print(f"heartbeat-gate: writer {live} is alive; to stop it: {' '.join(stop_cmd)}", file=sys.stderr)
        else:
            try:
                r = subprocess.run(stop_cmd, capture_output=True, text=True, timeout=30)
                ok = r.returncode == 0 and writer_state(live) not in ("own", "foreign")
            except Exception:
                ok = False
            if not ok:
                print(f"heartbeat-gate: writer {live} would not stop; run: {' '.join(stop_cmd)}")
                return 3
            reason += f"; stopped writer {live}"
    print(f"heartbeat-gate: {reason}", file=sys.stderr)
    print(word)
    return 2 if word == "unknown" else 0


if __name__ == "__main__":
    sys.exit(main())
