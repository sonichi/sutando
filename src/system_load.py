"""Sustained system load: one reading per health-check pass, a warning only when the last K passes
all ran above a ratio of the core count, the top CPU users, and their cause.

Generic core helper. The cause comes from a classifier script the EDGE injects (whatever installed
skill declares the manifest field `DECLARATION`, resolved with `src/skill_roots.py`); this module
never names, locates or imports a skill. The script contract it relies on:

- run with no arguments it only reports, exits 0, and prints one line per process group or pid it
  would stop, `would stop <kind>=<id> pids=<pid,pid,...> ...`, then a summary line carrying
  `mode=report ... candidates=<n>`; no summary line means it did nothing (busy or failed);
- exit status 2 is a usage error, never retried;
- `--reap --spotlight` stops the candidates and prints the same summary with `mode=reap ...
  terminated=<n> killed=<n> aborted=<n>`.

An episode is a run of consecutive high passes; it ends with the first pass at or below the
threshold, or after MAX_GAP_S without a pass. A pass without `--fix` never reaps. Any health-check
`--fix` run (the core's, a manual one, or the desktop app's 30-minute pass) calls `reap_once` when the
warning's action is reap-once: the reaper runs at most once per episode, recorded in the state file,
so a repeated `--fix` in the same episode stops nothing.

Config (health-check's `config_get` keys, read by the edge):
  SUTANDO_HEALTH_LOAD_RATIO   1-min load / cores above which a pass counts as high (default 1.5)
  SUTANDO_HEALTH_LOAD_PASSES  consecutive high passes before the warning (default 3)
  SUTANDO_HEALTH_LOAD_REAP    exactly 1 lets --fix reap; anything else, unset included, is off
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from result_publish import publish_text

try:
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX: the lock degrades to a no-op
    fcntl = None

NAME = "system-load"
DECLARATION = "orphan_reaper"
STATE_NAME = "health-system-load.json"
RATIO_KEY = "SUTANDO_HEALTH_LOAD_RATIO"
PASSES_KEY = "SUTANDO_HEALTH_LOAD_PASSES"
DEFAULT_RATIO = 1.5
DEFAULT_PASSES = 3
REAP_KEY = "SUTANDO_HEALTH_LOAD_REAP"
REAP_TIMEOUT_S = 600
REAP_MAX_ATTEMPTS = 3

HISTORY_LEN = 12
# A pass within this of the previous one replaces it: a burst of manual or dashboard runs is one pass.
MIN_SPACING_S = 60
# Readings further apart than this are not consecutive; the window restarts.
MAX_GAP_S = 3600
CLASSIFY_TTL_S = 300
CLASSIFY_TIMEOUT_S = 60
TOP_N = 5
CMD_WIDTH = 60

CAUSE_LEFTOVER = "leftover-test-processes"
CAUSE_OTHER = "other"
CAUSE_UNCLASSIFIED = "unclassified"
LEFTOVER_PHRASE = "leftover test processes from ended agent sessions"
ACTION_REAP = "reap-once"
ACTION_NONE = "none"

Runner = Callable[[Sequence[str], float], Tuple[int, str]]

_NAME_CHARS = re.compile(r"[^A-Za-z0-9._+-]")
_FLAG_NAME = re.compile(r"^--[A-Za-z][A-Za-z0-9-]{0,23}$")
_SHORT_FLAG = re.compile(r"^-[A-Za-z0-9]$")
PLACEHOLDER = "…"
_WOULD_STOP = re.compile(r"^(?:would stop|stopping) \S+=\S+ pids=([\d,]+)")
_SUMMARY = re.compile(r"\bmode=(report|reap)\b.*\bcandidates=(\d+)")
_COUNT = re.compile(r"\b(candidates|terminated|killed|aborted)=(\d+)")


def setting(raw: Optional[str], default, cast):
    """A positive number from config, or `default` when unset or malformed."""
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def read_state(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(path: Path, state: dict) -> None:
    """Whole in one rename, file and directory fsynced (result_publish's staged write)."""
    publish_text(path, json.dumps(state))


@contextlib.contextmanager
def locked(path: Path, blocking: bool = True) -> Iterator[bool]:
    """flock on `<path>.lock`; yields False when non-blocking and another pass holds it."""
    lock_path = Path(path).with_name(Path(path).name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as fh:
        if fcntl is None:  # pragma: no cover — non-POSIX
            yield True
            return
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def is_high(reading: dict, ratio: float) -> bool:
    try:
        return float(reading["load1"]) > ratio * float(reading["cores"])
    except (KeyError, TypeError, ValueError):
        return False


def record(state: dict, load1: float, cores: int, now: float) -> List[dict]:
    """Append this pass's reading to `state["readings"]` and return them, oldest first."""
    readings = [r for r in state.get("readings") or [] if isinstance(r, dict) and isinstance(r.get("ts"), (int, float))]
    if readings and now - readings[-1]["ts"] > MAX_GAP_S:
        readings = []
    reading = {"ts": now, "load1": round(load1, 2), "cores": cores}
    if readings and now - readings[-1]["ts"] < MIN_SPACING_S:
        readings[-1] = reading
    else:
        readings.append(reading)
    state["readings"] = readings[-HISTORY_LEN:]
    return state["readings"]


def track_episode(state: dict, previous: Optional[dict], current: dict, ratio: float) -> Optional[dict]:
    """Open an episode on the first high pass, keep it while passes stay high, end it on a normal one."""
    episode = state.get("episode") if isinstance(state.get("episode"), dict) else None
    if not is_high(current, ratio):
        episode = None
    elif episode is None or previous is None or not is_high(previous, ratio):
        episode = {"start": current["ts"], "reaped": None, "attempts": 0}
    state["episode"] = episode
    return episode


def sustained(readings: Sequence[dict], ratio: float, passes: int) -> bool:
    """True when the last `passes` readings were all high; one spike is not enough."""
    window = list(readings)[-passes:]
    return len(window) >= passes and all(is_high(r, ratio) for r in window)


def run_command(argv: Sequence[str], timeout: float) -> Tuple[int, str]:
    env = dict(os.environ, LC_ALL="C", LANG="C")
    proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, env=env,
                          stdin=subprocess.DEVNULL)
    return proc.returncode, proc.stdout


def render_command(command: str) -> str:
    """Only the executable's basename and flag names; every value and free word becomes a placeholder,
    so no argument text (a secret, or words addressed to the core reading this) leaves the host."""
    toks = command.split()
    if not toks:
        return ""
    exe = _NAME_CHARS.sub("", toks[0].rstrip("/").rsplit("/", 1)[-1])[:32] or PLACEHOLDER
    out = [exe]
    for tok in toks[1:]:
        name, sep, _ = tok.partition("=")
        if _FLAG_NAME.match(name) or _SHORT_FLAG.match(name):
            out.append(name if not sep else f"{name} {PLACEHOLDER}")
        elif tok.startswith("-") and len(tok) > 2 and _SHORT_FLAG.match(tok[:2]) and tok[1] != "-":
            out.append(f"{tok[:2]} {PLACEHOLDER}")
        else:
            out.append(PLACEHOLDER)
    text = " ".join(out)
    return text if len(text) <= CMD_WIDTH else text[:CMD_WIDTH - 1] + PLACEHOLDER


def top_cpu(ps_output: str, exclude: Sequence[int] = (), limit: int = TOP_N) -> List[dict]:
    """Parse `ps -Ao pid=,ppid=,%cpu=,etime=,command=` into the `limit` busiest processes."""
    rows = []
    for line in ps_output.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid, ppid, cpu = int(parts[0]), int(parts[1]), float(parts[2])
        except ValueError:
            continue
        if pid in exclude:
            continue
        rows.append({"pid": pid, "ppid": ppid, "cpu": cpu, "etime": parts[3], "command": parts[4]})
    rows.sort(key=lambda r: r["cpu"], reverse=True)
    return rows[:limit]


def read_top(runner: Runner) -> Optional[List[dict]]:
    try:
        rc, out = runner(["ps", "-Ao", "pid=,ppid=,%cpu=,etime=,command="], 10)
    except (OSError, subprocess.SubprocessError):
        return None
    if rc != 0:
        return None
    return top_cpu(out, exclude=(os.getpid(),))


def parse_report(output: str) -> Optional[set]:
    """Candidate pids from a classifier report, or None when it printed no summary."""
    if not any(_SUMMARY.search(line) for line in output.splitlines()):
        return None
    pids: set = set()
    for line in output.splitlines():
        m = _WOULD_STOP.match(line.strip())
        if m:
            pids.update(int(p) for p in m.group(1).split(",") if p)
    return pids


def classify(top: Sequence[dict], classifier: Optional[Path], runner: Runner,
             unavailable: Optional[str] = None) -> Tuple[str, str]:
    """(cause, note). Leftover test processes when the classifier's candidates hold at least half the top CPU."""
    if classifier is None:
        return CAUSE_UNCLASSIFIED, unavailable or "no installed skill declares an orphan reaper"
    try:
        rc, out = runner([sys.executable, str(classifier)], CLASSIFY_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return CAUSE_UNCLASSIFIED, "the orphan reaper's report timed out"
    except (OSError, subprocess.SubprocessError) as e:
        return CAUSE_UNCLASSIFIED, f"the orphan reaper could not run ({type(e).__name__})"
    if rc != 0:
        return CAUSE_UNCLASSIFIED, f"the orphan reaper's report exited {rc}"
    pids = parse_report(out)
    if pids is None:
        return CAUSE_UNCLASSIFIED, "the orphan reaper reported nothing (busy or failed)"
    total = sum(r["cpu"] for r in top)
    hit = [r for r in top if r["pid"] in pids]
    if total > 0 and sum(r["cpu"] for r in hit) * 2 >= total:
        return CAUSE_LEFTOVER, f"{len(hit)} of the top {len(top)}"
    return CAUSE_OTHER, f"only {len(hit)} of the top {len(top)}"


def describe_top(top: Sequence[dict]) -> str:
    return "; ".join(f"{r['pid']} (ppid {r['ppid']}, {r['cpu']:.0f}%, up {r['etime']}) {render_command(r['command'])}"
                     for r in top)


def _cause_text(cause: str, note: str) -> str:
    if cause == CAUSE_LEFTOVER:
        return f"{LEFTOVER_PHRASE} ({note})"
    if cause == CAUSE_OTHER:
        return f"not leftover test processes ({note})"
    return f"unclassified ({note})"


def _cached_cause(state: dict, now: float) -> Optional[Tuple[str, str]]:
    c = state.get("classification")
    if isinstance(c, dict) and isinstance(c.get("at"), (int, float)) and 0 <= now - c["at"] < CLASSIFY_TTL_S \
            and c.get("cause") in (CAUSE_LEFTOVER, CAUSE_OTHER, CAUSE_UNCLASSIFIED):
        return c["cause"], str(c.get("note") or "")
    return None


def check(state_path: Path, ratio: float = DEFAULT_RATIO, passes: int = DEFAULT_PASSES,
          classifier: Optional[Path] = None, unavailable: Optional[str] = None,
          now: Optional[float] = None, load_fn: Optional[Callable[[], Sequence[float]]] = None,
          cores_fn: Optional[Callable[[], Optional[int]]] = None, runner: Optional[Runner] = None,
          reaper_name: Optional[str] = None, reap_on: bool = False) -> Dict:
    """The `system-load` row. A warning carries `cause` and `action` (also as `cause=... action=...`
    at the end of its detail) so the core can decide; `reap-once` means the cause is leftover tests."""
    now = time.time() if now is None else now
    load_fn, cores_fn, runner = load_fn or os.getloadavg, cores_fn or os.cpu_count, runner or run_command
    try:
        load1 = float(load_fn()[0])
        cores = int(cores_fn() or 0)
    except (OSError, TypeError, ValueError, IndexError) as e:
        return {"name": NAME, "status": "warn", "alerting": False,
                "detail": f"load average unreadable on this host ({type(e).__name__}); not measured"}
    if cores <= 0:
        return {"name": NAME, "status": "warn", "alerting": False,
                "detail": "core count unreadable on this host; load not measured"}
    with locked(state_path):
        state = read_state(state_path)
        last = (state.get("readings") or [None])[-1]
        fresh = isinstance(last, dict) and isinstance(last.get("ts"), (int, float)) and now - last["ts"] <= MAX_GAP_S
        readings = record(state, load1, cores, now)
        episode = track_episode(state, last if fresh else None, readings[-1], ratio)
        write_state(state_path, state)
    reading = f"load {load1:.2f} on {cores} cores ({load1 / cores:.2f}x; threshold {ratio:g}x)"
    if not sustained(readings, ratio, passes):
        high = 0
        for r in reversed(readings):
            if not is_high(r, ratio):
                break
            high += 1
        tail = f", high for {high} of {passes} consecutive passes" if high else ""
        return {"name": NAME, "status": "ok", "detail": reading + tail}
    top = read_top(runner)
    cached = _cached_cause(state, now)
    if top is None:
        cause, note = CAUSE_UNCLASSIFIED, "ps could not be read"
    elif cached is not None:
        cause, note = cached
    else:
        cause, note = classify(top, classifier, runner, unavailable)
        with locked(state_path):
            fresh = read_state(state_path)
            fresh["classification"] = {"at": now, "cause": cause, "note": note}
            write_state(state_path, fresh)
    detail = f"{reading} for {passes} consecutive passes; cause: {_cause_text(cause, note)}"
    if top:
        detail += f"; top CPU: {describe_top(top)}"
    row = {"name": NAME, "status": "warn", "cause": cause, "_base": detail,
           "_reaper": reaper_name or "orphan", "_reap_on": reap_on}
    return annotate(row, episode)


def counts_text(counts: Dict[str, int]) -> str:
    stopped = counts.get("terminated", 0) + counts.get("killed", 0)
    return (f"stopped {stopped} of {counts.get('candidates', 0)} ({counts.get('terminated', 0)} terminated, "
            f"{counts.get('killed', 0)} killed, {counts.get('aborted', 0)} changed before stopping)")


def _stamp(at) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(at)) if isinstance(at, (int, float)) else "an unknown time"


def annotate(row: Dict, episode: Optional[dict], unreaped: Optional[str] = None) -> Dict:
    """Set a warning row's `action` and say in its detail what `--fix` will do, or did, this episode."""
    cause, name = row["cause"], row.get("_reaper") or "orphan"
    reaped = episode.get("reaped") if isinstance(episode, dict) else None
    action, tail = ACTION_NONE, ""
    if cause != CAUSE_LEFTOVER:
        pass
    elif isinstance(reaped, dict) and reaped.get("outcome") == "reaped":
        tail = f"; the {name} reaper already ran this episode at {_stamp(reaped.get('at'))}: {counts_text(reaped)}"
    elif isinstance(reaped, dict):
        tail = (f"; the {name} reaper failed this episode at {_stamp(reaped.get('at'))} ({reaped.get('outcome')}); "
                "not retried until load returns to normal")
    elif unreaped:
        tail = f"; --fix could not reap: {unreaped}"
    elif not row.get("_reap_on", False):
        tail = f"; reaping is off ({REAP_KEY}=1 enables it)"
    else:
        action = ACTION_REAP
        tail = f"; --fix runs the {name} reaper once for this episode"
        attempts = int((episode or {}).get("attempts") or 0)
        if attempts:
            tail += f" (it did nothing on {attempts} of {REAP_MAX_ATTEMPTS} tries)"
    row["action"] = action
    row["detail"] = f"{row['_base']}{tail}; cause={cause} action={action}"
    return row


def reap_enabled(raw: Optional[str]) -> bool:
    """Fail closed: only exactly `1` turns reaping on."""
    return raw == "1"


def parse_counts(output: str) -> Optional[Dict[str, int]]:
    """The reap summary's counts, or None when the reaper printed no `mode=reap` summary."""
    for line in reversed(output.splitlines()):
        m = _SUMMARY.search(line)
        if m and m.group(1) == "reap":
            return {k: int(v) for k, v in _COUNT.findall(line)}
    return None


def _save_episode(state_path: Path, episode: dict) -> None:
    with locked(state_path):
        state = read_state(state_path)
        current = state.get("episode")
        if isinstance(current, dict) and current.get("start") == episode["start"]:
            state["episode"] = episode
            write_state(state_path, state)


def reap_once(state_path: Path, reaper: Path, runner: Optional[Runner] = None,
              log: Callable[[str], None] = print, now: Optional[float] = None) -> Dict:
    """Run `reaper --reap --spotlight` at most once in the current episode (health-check --fix);
    the caller has established that the warning stands with cause leftover test processes."""
    now = time.time() if now is None else now
    runner = runner or run_command
    with locked(Path(str(state_path) + ".reap"), blocking=False) as held:
        if not held:
            log("system-load: another --fix is reaping; skipped")
            return {"action": "busy"}
        episode = read_state(state_path).get("episode")
        if not isinstance(episode, dict) or "start" not in episode:
            return {"action": "no-episode"}
        if episode.get("reaped"):
            log("system-load: the reaper already ran this episode; nothing to do")
            return {"action": "already", "reaped": episode["reaped"]}
        episode["attempts"] = int(episode.get("attempts") or 0) + 1
        try:
            rc, out = runner([sys.executable, str(reaper), "--reap", "--spotlight"], REAP_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as e:
            episode["reaped"] = {"at": now, "outcome": f"could not run: {type(e).__name__}"}
            _save_episode(state_path, episode)
            log(f"system-load: reaper failed ({type(e).__name__}); not retried this episode")
            return {"action": "failed"}
        if rc == 2:
            episode["reaped"] = {"at": now, "outcome": "usage error, exit 2"}
            _save_episode(state_path, episode)
            log("system-load: WARN reaper refused its arguments (exit 2); not retried this episode")
            return {"action": "usage-error"}
        counts = parse_counts(out) if rc == 0 else None
        if counts is None:
            if episode["attempts"] >= REAP_MAX_ATTEMPTS:
                episode["reaped"] = {"at": now, "outcome": f"did nothing in {REAP_MAX_ATTEMPTS} tries"}
            _save_episode(state_path, episode)
            log(f"system-load: reaper did nothing (exit {rc}, busy or failed); try {episode['attempts']} "
                f"of {REAP_MAX_ATTEMPTS} this episode")
            return {"action": "nothing", "rc": rc}
        episode["reaped"] = dict(counts, at=now, outcome="reaped")
        _save_episode(state_path, episode)
        log(f"system-load: --fix ran the reaper once this episode: {counts_text(counts)}")
        return {"action": "reaped", "counts": counts}
