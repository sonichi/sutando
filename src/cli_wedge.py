#!/usr/bin/env python3
"""CLI progress detector for the core's tmux pane — advisory only.

Two cases a heartbeat and a liveness probe both miss: (1) the RAW pane is
static while work is outstanding — pure static, no normalization, per the
spec; (2) the pane moves but revisits the same states (a retry loop), measured
as novelty over NORMALIZED frames — clocks, durations, counters, spinners and
token counts would fake novelty. A pane whose only motion is a clock is
ALIVE (Chi, from running these panes daily): reported as `clock-only`, never a
warning, and kept in every trace so the observation can be re-checked.

This reads the CLI, not the process. A green result here is not evidence the
core is healthy; it complements `.alive` and the runtime probes, never replaces
them. Nothing here restarts, kills or fails anything over: it warns.

Thresholds are PROVISIONAL (V1 is instrumentation): `record` writes real traces
under <workspace>/state/cli-wedge/traces/ so they can be tuned from behaviour.

Privacy: the rolling window persists hashes and pattern names only, never pane
text; traces carry text only with --keep-normalized / --keep-raw. Every file
this module writes is owner-only from birth (0600 in a 0700 directory).
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_default import resolve_workspace  # noqa: E402 — the one sanctioned resolver

RETRY_PATTERNS: tuple[tuple[str, re.Pattern], ...] = tuple(
    (name, re.compile(rx, re.IGNORECASE))
    for name, rx in (
        ("retrying", r"\bretry(ing)?\b"),
        ("rate-limit", r"\brate[ -]?limit"),
        ("overloaded", r"\boverloaded\b"),
        ("http-429", r"\b429\b"),
        ("http-529", r"\b529\b"),
        ("reconnecting", r"\breconnect(ing)?\b"),
        ("connection-error", r"\bconnection (error|reset|refused)\b"),
        ("timeout", r"\btimed? ?out\b"),
        # A CLI told to stop by its provider: every turn ends the same way while the
        # clock keeps moving, so only the text tells this from real work.
        ("quota-limit", r"\b(session|usage|weekly|daily|plan) limit\b|\bhit your\b.{0,24}\blimit\b|\busage-credits\b"),
    )
)

# Context-specific only (owner review: no blanket digit stripping — "shard 17" vs
# "shard 18" is progress). Composite tokens (timestamps, durations) come first.
_VOLATILE: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?Z?"), "<ts>"),
    (re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp][Mm])?\b"), "<clock>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|secs?|m|mins?|h|hrs?)\b"), "<dur>"),
    (re.compile(r"\b\d+(?:\.\d+)?[kKmM]?\s*tokens?\b"), "<tokens>"),
    (re.compile(r"\b\d+\s*/\s*\d+\b"), "<count>"),
    (re.compile(r"\b\d+(?:\.\d+)?%"), "<pct>"),
    (re.compile(r"\b(attempt|retry|retries|try|line|col|iteration|round|turn)\s+#?\d+\b", re.IGNORECASE), r"\1 #"),
    (re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷◐◓◑◒◴◷◶◵]"), "<spin>"),
    (re.compile(r"(?:\.\s?){2,}|…+"), "<dots>"),
    (re.compile(r"[─━═]{2,}"), "<rule>"),
)

# Provisional (V1 is instrumentation; real traces tune these) and reported in every
# verdict. status_ttl_s mirrors graceful-restart.sh busy() (GR_STATUS_TTL_S).
PROVISIONAL_THRESHOLDS = {
    "min_samples": 10,
    "low_novelty_rate": 0.25,
    "static_high_conf_s": 300,
    "static_high_conf_samples": 3,
    "pattern_min_consecutive": 2,
    "pattern_min_rate": 0.5,
    "continuity_gap_s": 2700,
    "status_ttl_s": 900,
}
PROVIDER_LIMIT_PATTERNS = ("quota-limit",)

def normalize(frame: str) -> str:
    """Strip volatile fields so two frames differing only in clocks, counters,
    durations or spinners compare equal."""
    lines = []
    for raw in frame.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        for rx, token in _VOLATILE:
            line = rx.sub(token, line)
        lines.append(line)
    return "\n".join(lines)


def state_id(frame: str) -> str:
    return hashlib.sha1(normalize(frame).encode("utf-8")).hexdigest()[:12]


def raw_state_id(frame: str) -> str:
    """Identity of the frame as displayed — what case 1 compares."""
    return hashlib.sha1(frame.encode("utf-8")).hexdigest()[:12]


def matched_patterns(frames: list) -> list:
    text = "\n".join(frames)
    return [name for name, rx in RETRY_PATTERNS if rx.search(text)]


@dataclass
class Novelty:
    sample_count: int
    novel_state_count: int
    novelty_rate: float
    static: bool
    states: list = field(default_factory=list)


def novelty(frames: list) -> Novelty:
    """A sample is novel when its normalized state was not seen earlier in the
    window; the first sample is always novel."""
    seen = set()
    ids = []
    novel = 0
    for f in frames:
        sid = state_id(f)
        ids.append(sid)
        if sid not in seen:
            seen.add(sid)
            novel += 1
    n = len(frames)
    return Novelty(n, novel, (novel / n) if n else 0.0, n >= 2 and novel == 1, ids)


def novelty_of_ids(state_ids: list) -> Novelty:
    seen = set()
    novel = 0
    for sid in state_ids:
        if sid not in seen:
            seen.add(sid)
            novel += 1
    n = len(state_ids)
    return Novelty(n, novel, (novel / n) if n else 0.0, n >= 2 and novel == 1, list(state_ids))


def pattern_stats(pattern_samples: list, th: dict) -> dict:
    """Retry text per SAMPLE, not a union over the window: a `timeout` in one old
    frame must not colour ten idle frames after it. Recurrent = the trailing
    samples all carry text, or enough of the run does; current = the newest does."""
    n = len(pattern_samples)
    current = sorted(set(pattern_samples[-1])) if pattern_samples else []
    with_pat = sum(1 for ps in pattern_samples if ps)
    consecutive = 0
    for ps in reversed(pattern_samples):
        if not ps:
            break
        consecutive += 1
    rate = (with_pat / n) if n else 0.0
    recurrent = bool(current) and (consecutive >= th["pattern_min_consecutive"]
                                   or (n >= th["min_samples"] and rate >= th["pattern_min_rate"]))
    return {"current_patterns": current, "pattern_sample_count": with_pat,
            "pattern_rate": round(rate, 3), "consecutive_pattern_samples": consecutive,
            "retry_current": recurrent}


def classify(frames: list, work_outstanding: bool, duration_s: float,
             work_detail: str = "", thresholds: Optional[dict] = None,
             raw_static: Optional[bool] = None) -> dict:
    """Advisory verdict over a window of frames. kind ∈ idle | working |
    clock-only | static-with-work | retry-loop | provider-limit | low-novelty |
    unknown (or, from the window, cadence-too-sparse); the four before unknown are warnings. `raw_static` is case 1's
    input (frame-for-frame equality); when None it is computed from `frames`."""
    if raw_static is None:
        raw_static = len(frames) >= 2 and len({raw_state_id(f) for f in frames}) == 1
    return classify_ids([state_id(f) for f in frames], raw_static, [matched_patterns([f]) for f in frames],
                        work_outstanding, duration_s, work_detail, thresholds)


def classify_ids(state_ids: list, raw_static: bool, pats, work_outstanding: bool,
                 duration_s: float, work_detail: str = "", thresholds: Optional[dict] = None,
                 gaps: Optional[list] = None) -> dict:
    """The verdict from hashes and pattern names alone — what the persisted
    window carries, so no pane text is needed (or stored) to classify. `pats`
    is one list of pattern names per sample (a flat list means every sample)."""
    th = {**PROVISIONAL_THRESHOLDS, **(thresholds or {})}
    nov = novelty_of_ids(state_ids)
    if pats and all(isinstance(x, str) for x in pats):
        pats = [list(pats) for _ in state_ids]
    ps = pattern_stats(list(pats or []), th)
    clock_only = (not raw_static) and nov.static
    spacing = {}
    if gaps:
        g = sorted(gaps)
        spacing = {"median_gap_s": round(g[len(g) // 2], 1), "max_gap_s": round(g[-1], 1)}
    base = {
        "advisory": True,
        "note": "CLI progress detector — reads the pane, not the process; not a health guarantee",
        "duration": round(duration_s, 1),
        "sample_count": nov.sample_count,
        "novel_state_count": nov.novel_state_count,
        "novelty_rate": round(nov.novelty_rate, 3),
        "matched_patterns": sorted({p for s in (pats or []) for p in s}),
        **ps,
        **spacing,
        "work_outstanding": work_outstanding,
        "work_detail": work_detail,
        "raw_static": bool(raw_static),
        "clock_only": clock_only,
        "thresholds": th,
    }
    if nov.sample_count < 2:
        return {**base, "kind": "unknown", "confidence": "none", "warn": False,
                "reason": "fewer than 2 samples in the current observation run — nothing to compare"}
    enough = nov.sample_count >= th["min_samples"]
    # A provider told the CLI to stop: not a retry loop, a blocked state of its own.
    # The pane keeps moving (clock, verb), so only current, recurrent text tells.
    if ps["retry_current"] and any(p in ps["current_patterns"] for p in PROVIDER_LIMIT_PATTERNS):
        return {**base, "kind": "provider-limit", "confidence": "high" if ps["consecutive_pattern_samples"] >= 3 else "medium",
                "warn": True, "reason": f"provider limit text on the last {ps['consecutive_pattern_samples']} sample(s) ({', '.join(ps['current_patterns'])})"}
    low_novelty = enough and nov.novelty_rate <= th["low_novelty_rate"]
    # Retry loop = low novelty AND retry text that is current and recurrent (not a stale residue).
    if ps["retry_current"] and (raw_static or nov.static or low_novelty):
        return {**base, "kind": "retry-loop", "confidence": "high" if enough else "medium", "warn": True,
                "reason": f"{nov.novel_state_count} distinct state(s) over {nov.sample_count} samples; retry text on the last {ps['consecutive_pattern_samples']} ({', '.join(ps['current_patterns'])})"}
    # Case 1 is pure static on the RAW pane (spec): no normalization here.
    if raw_static:
        if work_outstanding:
            high = duration_s >= th["static_high_conf_s"] and nov.sample_count >= th["static_high_conf_samples"]
            return {**base, "kind": "static-with-work", "confidence": "high" if high else "low", "warn": True,
                    "reason": f"pane unchanged across {nov.sample_count} samples over {duration_s:.0f}s while work is outstanding ({work_detail or 'unspecified'})"}
        return {**base, "kind": "idle", "confidence": "high", "warn": False,
                "reason": "pane unchanged and nothing outstanding"}
    if clock_only:
        return {**base, "kind": "clock-only", "confidence": "medium", "warn": False,
                "reason": "only volatile fields (clock/counters) change — a live CLI, not a wedge (operator observation); recorded for the harness"}
    if work_outstanding and low_novelty:
        return {**base, "kind": "low-novelty", "confidence": "low", "warn": True,
                "reason": f"{nov.novel_state_count} distinct states over {nov.sample_count} samples with work outstanding, no current retry text — repetitive, cause unknown"}
    return {**base, "kind": "working", "confidence": "medium" if nov.novelty_rate < 0.6 else "high", "warn": False,
            "reason": f"{nov.novel_state_count} distinct states over {nov.sample_count} samples"}

# ---- I/O edge -------------------------------------------------------------

def capture_pane(socket_path: str, target: str, tmux_bin: str = "tmux",
                 runner: Callable = subprocess.run) -> Optional[str]:
    """One pane frame, or None when tmux cannot be read (absent = no reading)."""
    try:
        proc = runner([tmux_bin, "-S", socket_path, "capture-pane", "-p", "-t", target],
                      capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 — a failed probe is an absent reading, never a verdict
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    return proc.stdout


_PANE_ID = re.compile(r"^\d+:\d+$")


def pane_identity(socket_path: str, target: str, tmux_bin: str = "tmux",
                  runner: Callable = subprocess.run) -> Optional[str]:
    """`pane_pid:session_created` for the target, or None when unknown. A new
    core process is a new observation run; None never resets anything."""
    try:
        proc = runner([tmux_bin, "-S", socket_path, "display-message", "-p", "-t", target,
                       "#{pane_pid}:#{session_created}"], capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 — identity unknown is not a reading
        return None
    out = (getattr(proc, "stdout", "") or "").strip()
    return out if getattr(proc, "returncode", 1) == 0 and _PANE_ID.match(out) else None


def work_outstanding(workspace: Path, now: Optional[float] = None, ttl_s: Optional[float] = None) -> tuple:
    """True when the core says it is running AND said so recently, or a task file
    is queued. Same contract as graceful-restart.sh busy(): a "running" older
    than the TTL is a crashed core's last word, not work."""
    now = time.time() if now is None else now
    ttl = PROVISIONAL_THRESHOLDS["status_ttl_s"] if ttl_s is None else ttl_s
    reasons = []
    try:
        st = json.loads((workspace / "state" / "core-status.json").read_text())
        if st.get("status") == "running":
            ts = st.get("ts")
            age = (now - ts) if isinstance(ts, (int, float)) else None
            if age is not None and 0 <= age <= ttl:
                reasons.append(f"core-status running ({age:.0f}s old)")
            else:
                # Not counted: a stale or unstamped "running" is not evidence of work.
                pass
    except (OSError, ValueError, AttributeError):
        pass
    tasks = list((workspace / "tasks").glob("task-*.txt"))
    if tasks:
        reasons.append(f"{len(tasks)} queued task(s)")
    return (bool(reasons), "; ".join(reasons))


def window_path(workspace: Path) -> Path:
    return workspace / "state" / "cli-wedge" / "window.jsonl"


def _private_dir(path: Path) -> None:
    """Owner-only directory, created or normalized (a permissive umask must not widen it)."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _write_private(path: Path, text: str) -> None:
    """Atomic replace through an owner-only temp file; the target ends 0600."""
    _private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _valid_entry(e) -> bool:
    return (isinstance(e, dict) and isinstance(e.get("ts"), (int, float))
            and isinstance(e.get("state"), str) and isinstance(e.get("patterns", []), list)
            and isinstance(e.get("pane", ""), str))


def load_window(path: Path) -> list:
    """Persisted entries that pass validation; anything else is skipped, never raised."""
    entries = []
    if not path.exists():
        return entries
    for line in path.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if _valid_entry(e):
            entries.append(e)
    return entries


def append_window(workspace: Path, frame: str, now: float, keep: int = 20, pane: Optional[str] = None) -> list:
    """Persist one sample into the rolling window so the statistic spans
    health-check passes; returns the window (oldest first). `pane` is the pane's
    identity (pid:session-created) so a restarted core starts a new run.
    The whole load → append → truncate → replace runs under one lock: the app's
    health-check and the launchd fallback are independent writers, and an
    atomic replace alone lets the later loader overwrite the earlier append."""
    path = window_path(workspace)
    _private_dir(path.parent)
    lock = path.with_name(path.name + ".lock")
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        entries = load_window(path)
        # Hashes and pattern names only — no pane text is ever persisted here.
        entry = {"ts": now, "state": state_id(frame), "raw_state": raw_state_id(frame),
                 "patterns": matched_patterns([frame])}
        if pane:
            entry["pane"] = pane
        entries.append(entry)
        entries = entries[-keep:]
        _write_private(path, "".join(json.dumps(e) + "\n" for e in entries))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return entries


def observation_runs(entries: list, gap_s: float) -> list:
    """Split samples into runs of continuous observation: a gap longer than
    `gap_s` (host asleep, health-check not running) or a different pane
    identity starts a new run. Two identical frames eight hours apart are two
    glimpses, not eight hours of watching."""
    runs = []
    for e in entries:
        if runs:
            prev = runs[-1][-1]
            if e["ts"] - prev["ts"] > gap_s or (e.get("pane") and prev.get("pane") and e["pane"] != prev["pane"]):
                runs.append([e])
                continue
            runs[-1].append(e)
        else:
            runs.append([e])
    return runs


def classify_window(entries: list, work: tuple, now: float, thresholds: Optional[dict] = None) -> dict:
    """Classify the CURRENT observation run of persisted samples (hashes only).
    Case 2 looks at the whole run; case 1 at the TRAILING stretch of one RAW
    state, since earlier different frames only say the pane moved before it
    stopped. Duration is measured inside the run, never across a gap."""
    th = {**PROVISIONAL_THRESHOLDS, **(thresholds or {})}
    entries = sorted((e for e in entries if _valid_entry(e)), key=lambda e: e["ts"])
    if not entries:
        return classify_ids([], False, [], work[0], 0.0, work[1], th)
    runs = observation_runs(entries, th["continuity_gap_s"])
    run = runs[-1]
    all_gaps = sorted(b["ts"] - a["ts"] for a, b in zip(entries, entries[1:]))
    meta = {"observation_runs": len(runs), "run_started": run[0]["ts"], "run_samples": len(run),
            "window_samples": len(entries), "last_sample_age_s": round(max(0.0, now - run[-1]["ts"]), 1)}
    if all_gaps:
        meta["window_median_gap_s"] = round(all_gaps[len(all_gaps) // 2], 1)
    if now - run[-1]["ts"] > th["continuity_gap_s"]:
        v = classify_ids([], False, [], work[0], 0.0, work[1], th)
        return {**v, **meta, "reason": f"last sample {now - run[-1]['ts']:.0f}s ago — no current observation"}
    # Judge the RECENT cadence (gaps behind the trailing singleton runs), not the
    # whole window: a 30-min→hourly change must not stay "unknown" until old gaps age out.
    singletons = 0
    for r in reversed(runs):
        if len(r) != 1:
            break
        singletons += 1
    if len(run) < 2 and singletons >= 3:
        recent = sorted(b[0]["ts"] - a[0]["ts"] for a, b in zip(runs[-singletons:], runs[-singletons + 1:]))
        meta["recent_gap_s"] = round(recent[len(recent) // 2], 1)
        v = classify_ids([], False, [], work[0], 0.0, work[1], th)
        return {**v, **meta, "kind": "cadence-too-sparse",
                "reason": f"the last {singletons} samples arrived ~{meta['recent_gap_s']:.0f}s apart, past the {th['continuity_gap_s']}s continuity limit — a static pane cannot be observed at this rate"}
    gaps = [b["ts"] - a["ts"] for a, b in zip(run, run[1:])]
    ids = [e["state"] for e in run]
    raws = [e.get("raw_state") for e in run]
    pats = [[p for p in e.get("patterns", []) if isinstance(p, str)] for e in run]
    whole_raw_static = len(run) >= 2 and all(raws) and len(set(raws)) == 1
    whole = classify_ids(ids, whole_raw_static, pats, work[0], max(0.0, now - run[0]["ts"]), work[1], th, gaps)
    if whole["kind"] in ("retry-loop", "provider-limit", "low-novelty"):
        return {**whole, **meta}
    last = run[-1].get("raw_state")
    tail = []
    for e in reversed(run):
        if not last or e.get("raw_state") != last:
            break
        tail.append(e)
    if len(tail) >= 2:
        tail = list(reversed(tail))
        tail_gaps = [b["ts"] - a["ts"] for a, b in zip(tail, tail[1:])]
        trailing = classify_ids([e["state"] for e in tail], True,
                                [[p for p in e.get("patterns", []) if isinstance(p, str)] for e in tail],
                                work[0], max(0.0, now - tail[0]["ts"]), work[1], th, tail_gaps)
        if trailing["kind"] in ("idle", "static-with-work"):
            return {**trailing, **meta, "sample_count": whole["sample_count"], "novel_state_count": whole["novel_state_count"],
                    "novelty_rate": whole["novelty_rate"], "clock_only": whole["clock_only"], "trailing_static_samples": len(tail)}
    return {**whole, **meta}

# ---- CLI: record real traces / replay / one-shot probe ----------------------

def _default_workspace() -> Path:
    return Path(resolve_workspace())


def record(args, sampler: Callable, clock=time.time, sleep=time.sleep) -> Path:
    """Sample the pane every `interval` seconds for `seconds`, one JSON line per
    sample plus a summary line; the file is the tuning evidence."""
    out_dir = Path(args.workspace) / "state" / "cli-wedge" / "traces"
    _private_dir(out_dir)
    started = clock()
    path = out_dir / f"{args.label}-{int(started)}.jsonl"
    frames = []
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        while clock() - started < args.seconds and len(frames) < args.max_samples:
            frame = sampler()
            if frame is not None:
                frames.append(frame)
                # Text is opt-in: hashes and pattern names are enough to tune thresholds.
                entry = {"ts": clock(), "state": state_id(frame), "raw_state": raw_state_id(frame),
                         "patterns": matched_patterns([frame])}
                if getattr(args, "keep_normalized", False):
                    entry["normalized"] = normalize(frame)
                if getattr(args, "keep_raw", False):
                    entry["raw"] = frame
                fh.write(json.dumps(entry) + "\n")
            sleep(args.interval)
        verdict = classify(frames, False, clock() - started, "unknown (recording)")
        fh.write(json.dumps({"summary": True, "label": args.label, **verdict}) + "\n")
    return path


def replay(path: Path, work: bool = False) -> dict:
    entries = [e for e in load_window(Path(path)) if not e.get("summary")]
    ts = [e["ts"] for e in entries]
    duration = (max(ts) - min(ts)) if len(ts) >= 2 else 0.0
    return classify_window(entries, (work, "replay flag" if work else ""), max(ts) if ts else 0.0) if entries else \
        classify_ids([], False, [], work, 0.0, "replay flag" if work else "")


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("record", "probe"):
        p = sub.add_parser(name)
        p.add_argument("--socket", required=True)
        p.add_argument("--target", default="sutando-core")
        p.add_argument("--tmux", default="tmux")
        p.add_argument("--workspace", default=None, help="defaults to the configured workspace")
        if name == "record":
            p.add_argument("--label", required=True, help="idle | build | retry | rate-limit | …")
            p.add_argument("--seconds", type=float, default=60)
            p.add_argument("--interval", type=float, default=3)
            p.add_argument("--max-samples", type=int, default=200)
            p.add_argument("--keep-normalized", action="store_true", help="also store normalized frame text (opt-in)")
            p.add_argument("--keep-raw", action="store_true", help="also store the raw frame (opt-in)")
    r = sub.add_parser("replay")
    r.add_argument("path")
    r.add_argument("--work-outstanding", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "replay":
        print(json.dumps(replay(Path(a.path), a.work_outstanding), indent=1))
        return 0

    def sampler():
        return capture_pane(a.socket, a.target, a.tmux)

    if a.workspace is None:
        a.workspace = str(_default_workspace())
    if a.cmd == "record":
        print(record(a, sampler))
        return 0
    frame = sampler()
    if frame is None:
        print(json.dumps({"kind": "unknown", "warn": False, "reason": "pane not readable"}))
        return 0
    ws = Path(a.workspace)
    now = time.time()
    entries = append_window(ws, frame, now, pane=pane_identity(a.socket, a.target, a.tmux))
    print(json.dumps(classify_window(entries, work_outstanding(ws, now), now), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
