#!/usr/bin/env python3
"""Find Sutando helper processes that have burned CPU for a sustained stretch.

A helper is an interpreter (bash, sh, zsh, python, node) running a script from this
repo, such as a task notifier, its supervisor or a watcher. A Claude CLI session is not
one. Each health-check run records every helper's cumulative CPU time. Once a recorded
sample is at least WINDOW_S old, the CPU spent since then is divided by the wall time
since then. A helper above THRESHOLD over that whole window is reported by pid and
script. A helper that spun silently for hours (2026-11-01: a claude task notifier at
60-90% for about 9 h) then shows up in the next health check rather than nowhere.

Samples live in one small JSON file. A process is keyed by pid and start time, so a
reused pid starts over, and a process that is gone is dropped.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

THRESHOLD = 0.5
WINDOW_S = 600
INTERPRETERS = frozenset({"bash", "sh", "zsh", "dash", "python", "python3", "Python", "node"})
_PY_VER = re.compile(r"^python3\.\d+$")


def parse_clock(text: str) -> "float | None":
    """`[[dd-]hh:]mm:ss[.cc]`, the shape `ps` prints for etime and time, in seconds."""
    text = (text or "").strip()
    days = 0
    if "-" in text:
        d, _, text = text.partition("-")
        if not d.isdigit():
            return None
        days = int(d)
    parts = text.split(":")
    if not 1 <= len(parts) <= 3:
        return None
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    secs = 0.0
    for n in nums:
        secs = secs * 60 + n
    return days * 86400 + secs


def helper_script(command: str, repo: Path) -> "str | None":
    """The repo-relative script a helper runs, or None when `command` is no Sutando helper."""
    argv = command.split()
    if not argv:
        return None
    exe = os.path.basename(argv[0])
    if exe not in INTERPRETERS and not _PY_VER.match(exe):
        return None
    root = str(repo).rstrip("/") + "/"
    for arg in argv[1:]:
        if arg.startswith(root):
            return arg[len(root):]
    return None


def processes(runner=subprocess.run) -> "list[dict]":
    """pid, elapsed wall seconds, cumulative CPU seconds and command for every process of this user."""
    out = runner(["ps", "-U", str(os.getuid()), "-o", "pid=,etime=,time=,command="],
                 capture_output=True, text=True, timeout=10).stdout
    rows = []
    for line in out.splitlines():
        bits = line.split(None, 3)
        if len(bits) < 4 or not bits[0].isdigit():
            continue
        etime, cpu = parse_clock(bits[1]), parse_clock(bits[2])
        if etime is None or cpu is None:
            continue
        rows.append({"pid": int(bits[0]), "etime": etime, "cpu": cpu, "command": bits[3]})
    return rows


def evaluate(rows: "list[dict]", repo: Path, state_path: Path, now: "float | None" = None) -> "list[dict]":
    """Helpers above THRESHOLD over a window of at least WINDOW_S. Updates the sample file."""
    now = time.time() if now is None else now
    try:
        prior = json.loads(state_path.read_text(encoding="utf-8"))
        prior = prior if isinstance(prior, dict) else {}
    except (OSError, ValueError):
        prior = {}
    kept, hot = {}, []
    for r in rows:
        script = helper_script(r["command"], repo)
        if script is None:
            continue
        # Start time to the nearest 10 s: etime and the clock are read a moment apart.
        key = f"{r['pid']}:{int(round((now - r['etime']) / 10.0))}"
        old = prior.get(key)
        sample = {"cpu": r["cpu"], "at": now}
        if isinstance(old, dict) and isinstance(old.get("cpu"), (int, float)) and isinstance(old.get("at"), (int, float)):
            span = now - old["at"]
            if span >= WINDOW_S:
                rate = (r["cpu"] - old["cpu"]) / span
                if rate > THRESHOLD:
                    hot.append({"pid": r["pid"], "script": script, "cpu_pct": round(100 * rate),
                                "minutes": round(span / 60)})
            else:
                sample = old          # keep the older sample until the window is long enough
        kept[key] = sample
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_name(f".{state_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(kept), encoding="utf-8")
        os.replace(tmp, state_path)
    except OSError:
        pass
    return hot


def check(repo: Path, state_path: Path, runner=subprocess.run, now: "float | None" = None) -> dict:
    """The health-check probe: warn naming each hot helper, ok otherwise."""
    name = "helper-cpu"
    try:
        rows = processes(runner)
    except (OSError, subprocess.SubprocessError) as e:
        return {"name": name, "status": "ok", "detail": f"process table unreadable ({type(e).__name__}); not checked"}
    hot = evaluate(rows, repo, state_path, now)
    if not hot:
        return {"name": name, "status": "ok",
                "detail": f"no Sutando helper above {int(THRESHOLD * 100)}% CPU for {WINDOW_S // 60}+ min"}
    named = "; ".join(f"pid {h['pid']} {h['script']} at {h['cpu_pct']}% for {h['minutes']} min" for h in hot)
    return {"name": name, "status": "warn",
            "detail": f"Sutando helper(s) spinning: {named}. A busy loop; stop it (SIGKILL if TERM is ignored) "
                      "and report the script"}


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        sys.exit("usage: helper_cpu_guard.py <samples json path>")
    repo = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root (scripts this probe matches, not a data root)
    print(json.dumps(check(repo, Path(sys.argv[1]))))
