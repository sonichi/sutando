#!/usr/bin/env python3
"""Report sustained helper CPU use from locked samples keyed by process birth.
High CPU alone does not establish a busy loop or justify termination."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from file_lock import locked_file  # noqa: E402

THRESHOLD = 0.5
WINDOW_S = 600
RETAIN_S = 3600
SCRIPT_SUFFIXES = (".sh", ".py", ".mjs", ".cjs", ".js")
# Per interpreter: options naming inline code or a module (not a script file), and options taking a value.
INLINE = {"bash": {"-c"}, "sh": {"-c"}, "zsh": {"-c"}, "dash": {"-c"},
          "python": {"-c", "-m"}, "node": {"-e", "--eval", "-p", "--print"}}
VALUED = {"bash": {"-o", "-O", "--rcfile", "--init-file"}, "sh": {"-o"}, "zsh": {"-o"}, "dash": {"-o"},
          "python": {"-W", "-X", "-Q"}, "node": {"-r", "--require", "--import", "--loader", "--experimental-loader",
                                                 "--input-type", "--env-file"}}
PS_ENV = {"PATH": "/bin:/usr/bin", "LC_ALL": "C", "LANG": "C", "TZ": "UTC"}


class Unobserved(Exception):
    """The process table could not be read."""


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


def _family(exe: str) -> "str | None":
    """The interpreter family an executable's basename names, or None."""
    name = os.path.basename(exe)
    if name in ("bash", "sh", "zsh", "dash", "node"):
        return name
    if name in ("python", "python3", "Python") or (name.startswith("python3.") and name[8:].isdigit()):
        return "python"
    return None


def helper_script(command: str, repo: Path) -> "str | None":
    """The repo-relative script a helper process runs, from `ps`'s space-joined argv; None for any other process."""
    words = command.split(" ")
    root = os.path.realpath(str(repo))
    for i in range(1, len(words) + 1):
        family = _family(" ".join(words[:i]))
        if family is not None:
            break
    else:
        return None
    j = i
    while j < len(words) and words[j].startswith("-"):
        opt = words[j]
        if opt in INLINE[family]:
            return None
        j += 2 if opt in VALUED[family] else 1
    for k in range(j + 1, len(words) + 1):
        operand = " ".join(words[j:k])
        if not operand.endswith(SCRIPT_SUFFIXES):
            continue
        path = os.path.realpath(operand if os.path.isabs(operand) else os.path.join(root, operand))
        if path.startswith(root + os.sep) and os.path.isfile(path):
            return os.path.relpath(path, root)
    return None


def processes(runner=subprocess.run) -> "list[dict]":
    """pid, exact start, cumulative CPU seconds and command for this user's processes; Unobserved if unreadable."""
    try:
        uid = os.getuid()
    except AttributeError as e:
        raise Unobserved("no POSIX process table on this platform") from e
    try:
        res = runner(["ps", "-U", str(uid), "-o", "pid=,lstart=,time=,command="],
                     capture_output=True, text=True, timeout=10, env=PS_ENV)
    except (OSError, subprocess.SubprocessError) as e:
        raise Unobserved(f"ps failed ({type(e).__name__})") from e
    if res.returncode != 0:
        raise Unobserved(f"ps exited {res.returncode}")
    rows = []
    for line in res.stdout.splitlines():
        bits = line.split(None, 7)  # pid, five lstart fields, time, command
        if len(bits) < 8 or not bits[0].isdigit():
            continue
        cpu = parse_clock(bits[6])
        if cpu is None:
            continue
        rows.append({"pid": int(bits[0]), "start": " ".join(bits[1:6]), "cpu": cpu, "command": bits[7]})
    return rows


def evaluate(rows: "list[dict]", repo: Path, state_path: Path, now: "float | None" = None) -> "list[dict]":
    """Helpers above THRESHOLD over a window of at least WINDOW_S; updates the sample file under its lock."""
    now = time.time() if now is None else now
    with locked_file(state_path.with_name(state_path.name + ".lock"), create_mode=0o600):
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
            key = f"{r['pid']}:{' '.join(str(r['start']).split())}"
            old = prior.get(key)
            sample = {"cpu": r["cpu"], "at": now}
            if isinstance(old, dict) and isinstance(old.get("cpu"), (int, float)) \
                    and isinstance(old.get("at"), (int, float)):
                span = now - old["at"]
                if span >= WINDOW_S:
                    rate = (r["cpu"] - old["cpu"]) / span
                    if rate > THRESHOLD:
                        hot.append({"pid": r["pid"], "script": script, "cpu_pct": round(100 * rate),
                                    "minutes": round(span / 60)})
                else:
                    sample = old          # keep the older sample until the window is long enough
            kept[key] = sample
        for key, old in prior.items():
            if key not in kept and isinstance(old, dict) and isinstance(old.get("at"), (int, float)) \
                    and now - old["at"] < RETAIN_S:
                kept[key] = old
        tmp = state_path.with_name(f".{state_path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(kept), encoding="utf-8")
            os.replace(tmp, state_path)
        finally:
            tmp.unlink(missing_ok=True)
    return hot


def check(repo: Path, state_path: Path, runner=subprocess.run, now: "float | None" = None) -> dict:
    """The health-check probe: warn naming each helper with sustained CPU; warn when it could not look."""
    name = "helper-cpu"
    try:
        rows = processes(runner)
    except Unobserved as e:
        status = "ok" if "platform" in str(e) else "warn"
        return {"name": name, "status": status, "detail": f"not checked: {e}"}
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        hot = evaluate(rows, repo, state_path, now)
    except OSError as e:
        return {"name": name, "status": "warn", "detail": f"not checked: samples unwritable ({type(e).__name__})"}
    if not hot:
        return {"name": name, "status": "ok",
                "detail": f"no Sutando helper above {int(THRESHOLD * 100)}% CPU for {WINDOW_S // 60}+ min"}
    named = "; ".join(f"pid {h['pid']} {h['script']} at {h['cpu_pct']}% for {h['minutes']} min" for h in hot)
    return {"name": name, "status": "warn",
            "detail": f"sustained CPU in Sutando helper(s): {named}. Possibly a busy loop; check its log "
                      "before stopping it"}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: helper_cpu_guard.py <samples json path>")
    repo = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root (scripts this probe matches, not a data root)
    print(json.dumps(check(repo, Path(sys.argv[1]))))
