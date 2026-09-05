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
    )
)

# Order matters: composite tokens (timestamps, durations) before bare digits.
_VOLATILE: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?Z?"), "<ts>"),
    (re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp][Mm])?\b"), "<clock>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|secs?|m|mins?|h|hrs?)\b"), "<dur>"),
    (re.compile(r"\b\d+(?:\.\d+)?[kKmM]?\s*tokens?\b"), "<tokens>"),
    (re.compile(r"\b\d+\s*/\s*\d+\b"), "<count>"),
    (re.compile(r"\b\d+(?:\.\d+)?%"), "<pct>"),
    (re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷◐◓◑◒◴◷◶◵]"), "<spin>"),
    (re.compile(r"(?:\.\s?){2,}|…+"), "<dots>"),
    (re.compile(r"[─━═]{2,}"), "<rule>"),
    (re.compile(r"\d+"), "#"),
)

# Provisional: min samples before case 2 speaks; novel/samples at or below
# which a window counts as repetitive; static seconds for high confidence.
PROVISIONAL_THRESHOLDS = {"min_samples": 10, "low_novelty_rate": 0.25, "static_high_conf_s": 300}


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


def classify(frames: list, work_outstanding: bool, duration_s: float,
             work_detail: str = "", thresholds: Optional[dict] = None,
             raw_static: Optional[bool] = None) -> dict:
    """Advisory verdict over a window of frames. kind ∈ idle | working |
    clock-only | static-with-work | retry-loop | low-novelty | unknown; the
    last three before unknown are warnings. `raw_static` is case 1's input
    (frame-for-frame equality); when None it is computed from `frames`."""
    if raw_static is None:
        raw_static = len(frames) >= 2 and len({raw_state_id(f) for f in frames}) == 1
    return classify_ids([state_id(f) for f in frames], raw_static, matched_patterns(frames),
                        work_outstanding, duration_s, work_detail, thresholds)


def classify_ids(state_ids: list, raw_static: bool, pats: list, work_outstanding: bool,
                 duration_s: float, work_detail: str = "", thresholds: Optional[dict] = None) -> dict:
    """The verdict from hashes and pattern names alone — what the persisted
    window carries, so no pane text is needed (or stored) to classify."""
    th = {**PROVISIONAL_THRESHOLDS, **(thresholds or {})}
    nov = novelty_of_ids(state_ids)
    clock_only = (not raw_static) and nov.static
    base = {
        "advisory": True,
        "note": "CLI progress detector — reads the pane, not the process; not a health guarantee",
        "duration": round(duration_s, 1),
        "sample_count": nov.sample_count,
        "novel_state_count": nov.novel_state_count,
        "novelty_rate": round(nov.novelty_rate, 3),
        "matched_patterns": pats,
        "work_outstanding": work_outstanding,
        "work_detail": work_detail,
        "raw_static": bool(raw_static),
        "clock_only": clock_only,
        "thresholds": th,
    }
    if nov.sample_count < 2:
        return {**base, "kind": "unknown", "confidence": "none", "warn": False,
                "reason": "fewer than 2 samples — nothing to compare"}
    # Retry text decides first: a loop whose only motion is counters/clocks
    # normalizes to ONE state and would otherwise read as merely static.
    if pats and (raw_static or nov.static or (nov.sample_count >= th["min_samples"] and nov.novelty_rate <= th["low_novelty_rate"])):
        return {**base, "kind": "retry-loop", "confidence": "high" if nov.sample_count >= th["min_samples"] else "medium", "warn": True,
                "reason": f"{nov.novel_state_count} distinct state(s) over {nov.sample_count} samples and retry text present ({', '.join(pats)})"}
    # Case 1 is pure static on the RAW pane (spec): no normalization here.
    if raw_static:
        if work_outstanding:
            high = duration_s >= th["static_high_conf_s"]
            return {**base, "kind": "static-with-work", "confidence": "high" if high else "low", "warn": True,
                    "reason": f"pane unchanged for {duration_s:.0f}s while work is outstanding ({work_detail or 'unspecified'})"}
        return {**base, "kind": "idle", "confidence": "high", "warn": False,
                "reason": "pane unchanged and nothing outstanding"}
    if clock_only:
        return {**base, "kind": "clock-only", "confidence": "medium", "warn": False,
                "reason": "only volatile fields (clock/counters) change — a live CLI, not a wedge (operator observation); recorded for the harness"}
    if work_outstanding and nov.sample_count >= th["min_samples"] and nov.novelty_rate <= th["low_novelty_rate"]:
        return {**base, "kind": "low-novelty", "confidence": "low", "warn": True,
                "reason": f"{nov.novel_state_count} distinct states over {nov.sample_count} samples with work outstanding, no retry text — repetitive, cause unknown"}
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


def work_outstanding(workspace: Path) -> tuple:
    """True when the core says it is running or a task file is queued."""
    reasons = []
    try:
        if json.loads((workspace / "state" / "core-status.json").read_text()).get("status") == "running":
            reasons.append("core-status running")
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
            and isinstance(e.get("state"), str) and isinstance(e.get("patterns", []), list))


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


def append_window(workspace: Path, frame: str, now: float, keep: int = 20) -> list:
    """Persist one sample into the rolling window so the statistic spans
    health-check passes; returns the window (oldest first)."""
    path = window_path(workspace)
    entries = load_window(path)
    # Hashes and pattern names only — no pane text is ever persisted here.
    entries.append({"ts": now, "state": state_id(frame), "raw_state": raw_state_id(frame),
                    "patterns": matched_patterns([frame])})
    entries = entries[-keep:]
    _write_private(path, "".join(json.dumps(e) + "\n" for e in entries))
    return entries


def classify_window(entries: list, work: tuple, now: float) -> dict:
    """Classify persisted samples (hashes only). Case 2 looks at the whole
    window; case 1 at the TRAILING run of one RAW state, since earlier
    different frames only say the pane moved before it stopped."""
    entries = [e for e in entries if _valid_entry(e)]
    if not entries:
        return classify_ids([], False, [], work[0], 0.0, work[1])
    ids = [e["state"] for e in entries]
    raws = [e.get("raw_state") for e in entries]
    pats = sorted({p for e in entries for p in e.get("patterns", []) if isinstance(p, str)})
    whole_raw_static = len(entries) >= 2 and all(raws) and len(set(raws)) == 1
    whole = classify_ids(ids, whole_raw_static, pats, work[0], max(0.0, now - entries[0]["ts"]), work[1])
    if whole["kind"] in ("retry-loop", "low-novelty"):
        return whole
    last = entries[-1].get("raw_state")
    run = []
    for e in reversed(entries):
        if not last or e.get("raw_state") != last:
            break
        run.append(e)
    if len(run) >= 2:
        run_pats = sorted({p for e in run for p in e.get("patterns", []) if isinstance(p, str)})
        trailing = classify_ids([e["state"] for e in run], True, run_pats, work[0], max(0.0, now - run[-1]["ts"]), work[1])
        if trailing["kind"] in ("idle", "static-with-work"):
            return {**trailing, "sample_count": whole["sample_count"], "novel_state_count": whole["novel_state_count"],
                    "novelty_rate": whole["novelty_rate"], "clock_only": whole["clock_only"], "trailing_static_samples": len(run)}
    return whole


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
    entries = append_window(ws, frame, time.time())
    print(json.dumps(classify_window(entries, work_outstanding(ws), time.time()), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
