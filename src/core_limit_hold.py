#!/usr/bin/env python3
"""Hold the core's relaunch while its provider refuses it for a limit.

A core that dies on a usage limit, rate limit or quota comes straight back to
the same refusal, so a supervisor that revives it on sight loops. This module
is the one decision. It reads two records of the refusal: the provider's, which
the credential proxy keeps in `state/quota-state.json`, and the CLI's own last
API error, from the core's session transcript. From them it classifies a limit,
records a hold in `state/core-limit-hold.json`, and mirrors the reason into
`core-status.json`. The hold lasts until the reset time the provider or the CLI
gave, else a bounded backoff.

Evidence never re-arms a hold it already caused, so the try after a hold always
happens: a cleared limit resumes on its own, and a limit that is still there
leaves fresh evidence and a longer hold. A crash with no limit evidence is
never held, so it is revived as before.

    core_limit_hold.py gate [--cwd DIR]   exit 0: launch; exit 75: held, reason on stdout
    core_limit_hold.py status             the hold record, if any
    core_limit_hold.py clear              lift the hold now (the next revive launches)

While a hold is in force the gate also acts on the owner's bare replies to the notice,
"switch to <model>" and "retry now", since nothing else reads the inbox with the core down.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cli_wedge  # noqa: E402
import quota_availability as qa  # noqa: E402

from workspace_default import resolve_workspace, status_read_path, write_status  # noqa: E402

HOLD_FILE = "core-limit-hold.json"
HOLD_EXIT = 75  # EX_TEMPFAIL: not a failure, try again later
EVIDENCE_WINDOW_S = 15 * 60
BACKOFF_BASE_S = 5 * 60
BACKOFF_CAP_S = 60 * 60
RESET_GRACE_S = 60
MAX_RESET_WAIT_S = 8 * 24 * 3600
STRIKE_MEMORY_S = 2 * BACKOFF_CAP_S
# A reset more than a day out is honored for 6 hours, then one launch re-reads the provider.
LONG_RESET_S = 24 * 3600
LONG_RESET_RECHECK_S = 6 * 3600
TRANSCRIPT_TAIL_BYTES = 256 * 1024
# The 429's own headers land a moment after it; only a later response says the provider served.
SERVED_SLACK_S = 5

_CLI_LIMIT = tuple(rx for name, rx in cli_wedge.ABNORMAL_PATTERNS
                   if name in cli_wedge.PROVIDER_LIMIT_PATTERNS)
# The window-named banner ("5-hour limit reached") and the API's own words for a refusal.
_USAGE_EXTRA = re.compile(r"\b\d+-hour limit (?:reached|exceeded|hit)\b|\bquota\b.{0,20}\b(?:exceeded|exhausted)\b"
                          r"|\binsufficient_quota\b", re.I)
# A 429 counts only as a status (API Error, HTTP, a status field), never as a number in a URL or id.
_RATE = re.compile(r"\brate_limit_error\b|\brate[ -]?limit(?:ed)?\b|\btoo many requests\b"
                   r"|\b(?:API ?Error|HTTP(?:/[\d.]+)?|status(?:_?code)?)\"?\s*[:=]?\s*429\b", re.I)

_RESET_EPOCH = re.compile(r"\|(\d{10})\b")
_RESET_CLOCK = re.compile(r"\bresets?\s+(?:at\s+|on\s+)?(?:([A-Z][a-z]{2})[a-z]*\.?\s+(\d{1,2}),?\s+(?:at\s+)?)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)\b(?:\s*\(([A-Za-z_]+(?:/[A-Za-z0-9_+-]+)*)\))?", re.I)
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_RESET_IN = re.compile(r"\bresets?\s+in\s+(\d+)\s*(h|hours?|m|mins?|minutes?)\b", re.I)
_RETRY_AFTER = re.compile(r"\bretry[- ]after\D{0,3}(\d+)\b", re.I)


@dataclass(frozen=True)
class Limit:
    """One observed refusal: `kind` usage-limit or rate-limit, `at` when it was seen,
    `reset_at` when the provider or CLI said it lifts, `source` which record said so."""
    kind: str
    at: float
    reset_at: Optional[float]
    model: Optional[str]
    source: str


def _zone(name: Optional[str]):
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 -- an unknown zone reads as local time
        return None


def parse_reset(text: str, at: float) -> Optional[float]:
    """The lift time a refusal message states, as epoch seconds after `at`, else None."""
    m = _RESET_EPOCH.search(text)
    if m:
        return float(m.group(1))
    m = _RESET_CLOCK.search(text)
    if m:
        month, day, hour, minute, ampm, zone = m.groups()
        hour = int(hour) % 12 + (12 if ampm.lower() == "pm" else 0)
        tz = _zone(zone)
        base = datetime.fromtimestamp(at, tz) if tz else datetime.fromtimestamp(at)
        when = base.replace(hour=hour, minute=int(minute or 0), second=0, microsecond=0)
        if month:
            try:
                when = when.replace(month=_MONTHS.index(month.lower()) + 1, day=int(day))
            except ValueError:
                return None
            if when.timestamp() <= at:
                when = when.replace(year=when.year + 1)
        elif when.timestamp() <= at:
            when += timedelta(days=1)  # an hour off across a DST change; the grace absorbs it
        return when.timestamp()
    m = _RESET_IN.search(text)
    if m:
        unit = 3600 if m.group(2).lower().startswith("h") else 60
        return at + int(m.group(1)) * unit
    m = _RETRY_AFTER.search(text)
    if m:
        return at + int(m.group(1))
    return None


def classify_text(text: str, at: float, model: Optional[str] = None,
                  source: str = "cli-transcript") -> Optional[Limit]:
    """A provider limit named by refusal text, or None for any other error."""
    if not text:
        return None
    if any(rx.search(text) for rx in _CLI_LIMIT) or _USAGE_EXTRA.search(text):
        kind = "usage-limit"
    elif _RATE.search(text):
        kind = "rate-limit"
    else:
        return None
    return Limit(kind, at, parse_reset(text, at), model, source)


def _record_reset(headers: dict, now: float) -> Optional[float]:
    """The latest reset among the windows the provider reports rejected, if any is ahead."""
    resets = []
    for window, (_u, status) in qa.limit_windows(headers).items():
        if status == "rejected":
            resets.append(headers.get(f"anthropic-ratelimit-unified-{window}-reset"))
    if headers.get("anthropic-ratelimit-unified-status") == "rejected":
        resets.append(headers.get("anthropic-ratelimit-unified-reset"))
    ahead = []
    for value in resets:
        try:
            when = float(value)
        except (TypeError, ValueError):
            continue
        if when > now:
            ahead.append(when)
    return max(ahead) if ahead else None


def record_evidence(payload, now: float) -> Optional[Limit]:
    """A recent 429 in the proxy's record, unless the provider served since then."""
    if not isinstance(payload, dict):
        return None
    ledger = payload.get("recent_rejections")
    newest = None
    for rej in ledger if isinstance(ledger, list) else []:
        if not isinstance(rej, dict) or rej.get("status") != 429:
            continue
        at = qa._parse_when(rej.get("ts"))
        if at is None or not (0 <= now - at <= EVIDENCE_WINDOW_S):
            continue
        if newest is None or at > newest[0]:
            newest = (at, rej)
    if newest is None:
        return None
    at, rej = newest
    state = qa.classify_limit_state(payload)
    checked = qa._parse_when(payload.get("last_checked"))
    if state != "rejected" and checked is not None and checked > at + SERVED_SLACK_S:
        return None
    headers = payload.get("headers") if isinstance(payload.get("headers"), dict) else {}
    model = qa._norm_model(rej.get("model")) or qa.record_model(payload)
    found = classify_text(str(rej.get("snippet") or ""), at, model, "provider-record")
    kind = "usage-limit" if state == "rejected" else (found.kind if found else "rate-limit")
    reset = _record_reset(headers, now) if state == "rejected" else None
    if reset is None and found is not None and found.reset_at and found.reset_at > now:
        reset = found.reset_at
    return Limit(kind, at, reset, model, "provider-record")


def _entry_text(entry: dict) -> str:
    msg = entry.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    parts = [c.get("text", "") for c in content if isinstance(c, dict)] if isinstance(content, list) else []
    return " ".join(p for p in parts if isinstance(p, str))


def transcript_evidence(projects_dir: Optional[Path], now: float) -> Optional[Limit]:
    """The CLI's own last API error in the core's newest transcript, when it names a limit."""
    if projects_dir is None or not projects_dir.is_dir():
        return None
    try:
        files = sorted(projects_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]
    except OSError:
        return None
    for path in files:
        try:
            if now - path.stat().st_mtime > EVIDENCE_WINDOW_S:
                continue
            with open(path, "rb") as fh:
                fh.seek(max(0, path.stat().st_size - TRANSCRIPT_TAIL_BYTES))
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        entries = []
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
        for i in range(len(entries) - 1, -1, -1):
            entry = entries[i]
            if _served_turn(entry) is not None:
                return None  # the provider served a turn after any error: served-since
            if not entry.get("isApiErrorMessage"):
                continue
            at = qa._parse_when(entry.get("timestamp"))
            if at is None or not (0 <= now - at <= EVIDENCE_WINDOW_S):
                break
            model = next((m for m in map(_served_turn, reversed(entries[:i])) if m), None)
            return classify_text(_entry_text(entry), at, model)
    return None


def _served_turn(entry: dict) -> Optional[str]:
    """The model of a real assistant turn the provider served, else None. The CLI writes its
    own errors as assistant entries with the model `<synthetic>`; those are not served turns."""
    msg = entry.get("message")
    if entry.get("type") != "assistant" or entry.get("isApiErrorMessage") or not isinstance(msg, dict):
        return None
    model = qa._norm_model(msg.get("model"))
    return model if model and model != "<synthetic>" else None


# ---- the hold record ----------------------------------------------------------

def read_hold(workspace: Path) -> Optional[dict]:
    try:
        data = json.loads(status_read_path(HOLD_FILE, Path(workspace)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def active_hold(workspace: Path, now: Optional[float] = None) -> Optional[dict]:
    """The hold in force right now, else None. Read-only."""
    hold = read_hold(workspace)
    at = time.time() if now is None else now
    if hold and hold.get("status") == "held" and isinstance(hold.get("until"), (int, float)) and at < hold["until"]:
        return hold
    return None


def _when(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(epoch))


def status_line(hold: dict) -> str:
    """The reason in the words a user reads instead of "Connecting"."""
    what = "usage limit" if hold.get("kind") == "usage-limit" else "rate limit"
    model = hold.get("model") or "its model"
    if isinstance(hold.get("reset_at"), (int, float)):
        tail = f"resets {_when(hold['reset_at'])}"
    else:
        tail = f"retrying {_when(hold['until'])}" if isinstance(hold.get("until"), (int, float)) else "retrying later"
    return f"core paused: {what} on {model}, {tail}"


def _write_hold(workspace: Path, hold: dict) -> None:
    write_status(HOLD_FILE, hold, workspace=Path(workspace))


def _mirror_core_status(workspace: Path, status: str, step: str, now: float) -> None:
    try:
        write_status("core-status.json", {"status": status, "step": step, "ts": int(now)},
                     workspace=Path(workspace))
    except OSError:
        pass  # the hold record is the decision; the mirror only shows it


def _same_family(limited_model: Optional[str], launch_model: Optional[str]) -> bool:
    """A limit on another model family does not stop a core that runs this one."""
    mine, theirs = qa._model_family(launch_model), qa._model_family(limited_model)
    return not (mine and theirs and mine != theirs)


def _relevant(limit: Optional[Limit], launch_model: Optional[str]) -> bool:
    return limit is not None and _same_family(limit.model, launch_model)


def _notice_body(hold: dict) -> str:
    from result_markers import neutralize_markers
    when = (f"It resets {_when(hold['reset_at'])}" if isinstance(hold.get("reset_at"), (int, float))
            else f"It tries again {_when(hold['until'])}")
    return "[dm-only]\n" + neutralize_markers(
        f"Your Sutando core is paused: {hold['reason'].split(': ', 1)[-1]}.\n\n"
        f"{when}, and the core starts again by itself after that. Messages you send meanwhile "
        "wait and are answered then.\n\n"
        'To keep working now, reply "switch to sonnet" (or another model: haiku, opus, fable), '
        "or pick a model in the core-model card under Agent Settings, Runtime, Model. "
        'Reply "retry now" to try the current model again at once.\n')


def _ensure_notice(workspace: Path, hold: dict) -> bool:
    """Queue the hold's owed DM under its stable name, once. True when it is queued.
    A retry rewrites the same name, so it cannot become a second message."""
    if hold.get("notice_queued"):
        return False
    from local_record import write_text_whole
    try:
        write_text_whole(Path(workspace) / "results" / hold["notice"], _notice_body(hold))
    except OSError:
        return False
    hold["notice_queued"] = True
    return True


def _hold_until(limit: Limit, strikes: int, now: float) -> float:
    if limit.reset_at and now < limit.reset_at <= now + MAX_RESET_WAIT_S:
        if limit.reset_at - now > LONG_RESET_S:
            return now + LONG_RESET_RECHECK_S
        return limit.reset_at + RESET_GRACE_S
    return now + min(BACKOFF_BASE_S * 2 ** (strikes - 1), BACKOFF_CAP_S)


@dataclass(frozen=True)
class Decision:
    launch: bool
    hold: Optional[dict]
    line: str


def decide(workspace: Path, now: Optional[float] = None, *, quota=None,
           projects_dir: Optional[Path] = None, launch_model: Optional[str] = None) -> Decision:
    """Launch, or hold and say why. The one writer of the hold record."""
    now = time.time() if now is None else now
    workspace = Path(workspace)
    hold = read_hold(workspace)
    if active_hold(workspace, now) and _same_family(hold.get("model"), launch_model):
        if _ensure_notice(workspace, hold):
            _write_hold(workspace, hold)
        return Decision(False, hold, status_line(hold))
    if quota is None:
        rec = qa.read_quota_record(workspace, now)
        quota = rec.payload if rec else None
    found = [lim for lim in (record_evidence(quota, now), transcript_evidence(projects_dir, now))
             if _relevant(lim, launch_model)]
    limit = max(found, key=lambda lim: lim.at) if found else None
    seen = hold.get("evidence_at") if hold else None
    if limit is not None and (not isinstance(seen, (int, float)) or limit.at > seen):
        last_end = hold.get("until") if hold else None
        recent = isinstance(last_end, (int, float)) and now - last_end < STRIKE_MEMORY_S
        strikes = int(hold.get("strikes") or 0) + 1 if hold and recent else 1
        new = {
            "status": "held", "kind": limit.kind, "model": limit.model, "source": limit.source,
            "reset_at": limit.reset_at, "evidence_at": limit.at, "set_at": now,
            "until": _hold_until(limit, strikes, now), "strikes": strikes,
        }
        new["reason"] = status_line(new)
        if hold and recent and hold.get("notice"):
            new["notice"], new["notice_queued"] = hold["notice"], bool(hold.get("notice_queued"))
        else:
            from proactive_routing import proactive_filename
            new["notice"] = proactive_filename(f"core-limit-hold-{int(now * 1000)}")
            new["notice_queued"] = False
        _ensure_notice(workspace, new)
        _write_hold(workspace, new)
        _mirror_core_status(workspace, "paused", new["reason"], now)
        return Decision(False, new, new["reason"])
    if hold and hold.get("status") == "held":
        hold = dict(hold, status="released", released_at=now)
        _write_hold(workspace, hold)
        _mirror_core_status(workspace, "starting", "resuming after a provider limit", now)
    return Decision(True, hold, "")


def clear(workspace: Path, now: Optional[float] = None, by: Optional[str] = None) -> Optional[dict]:
    """Lift the hold. Its evidence stays recorded, so only a new refusal re-arms it."""
    hold = read_hold(workspace)
    if not hold or hold.get("status") != "held":
        return hold
    hold = dict(hold, status="cleared", released_at=time.time() if now is None else now)
    if by:
        hold["cleared_by"] = by
    _write_hold(workspace, hold)
    _mirror_core_status(workspace, "starting", "resuming after a provider limit", hold["released_at"])
    return hold


# ---- the owner's way out while the core is down -----------------------------

# The whole message must be the action, so a sentence that mentions it is never one.
_SWITCH = re.compile(r"^(?:please\s+)?(?:switch|change)\s+(?:the\s+core\s+)?(?:model\s+)?to\s+"
                     r"(sonnet|haiku|opus|fable)\s*[.!]?$", re.I)
_RETRY = re.compile(r"^(?:please\s+)?retry(?:\s+now)?\s*[.!]?$", re.I)


def _owner_action(path: Path) -> Optional[tuple]:
    """("switch", model) or ("retry", None) for an owner's bare reply, else None."""
    from local_task_protocol import parse_task_headers_lenient, split_reply_context
    from policy.egress.result import resolve_access_tier
    if resolve_access_tier(path) != "owner":
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    body = split_reply_context(parse_task_headers_lenient(text).body)[1].strip()
    m = _SWITCH.match(body)
    if m:
        return ("switch", m.group(1).lower())
    return ("retry", None) if _RETRY.match(body) else None


def _recorded_actions(hold: Optional[dict]) -> list:
    acts = (hold or {}).get("actions") or []
    return [a for a in acts if isinstance(a, dict) and isinstance(a.get("task"), str)]


_STEPS = ("answered", "lifted", "archived")


def _mark(workspace: Path, task: str, step: str) -> None:
    """Persist one finished step of a recorded action; the record, not a file, says it is done."""
    hold = read_hold(workspace)
    acts = _recorded_actions(hold)
    for act in acts:
        if act["task"] == task:
            act[step] = True
    if hold is not None:
        _write_hold(workspace, dict(hold, actions=acts))


def _finish(workspace: Path, entry: dict, now: float) -> None:
    """Complete a recorded action from its record alone: answer, lift, archive, each marked
    done in the record as it lands, so a replay skips it and never answers a second time."""
    import local_record
    from local_task_protocol import find_result
    task = entry["task"]
    if not entry.get("answered"):
        # Unmarked means a crash may have hit after the write: a copy live or archived is that write.
        if find_result(workspace / "results", task) is None:
            local_record.write_text_whole(workspace / "results" / f"{task}.txt", entry["reply"] + "\n")
        _mark(workspace, task, "answered")
    if not entry.get("lifted"):
        hold = read_hold(workspace)
        if entry.get("ok") and hold and hold.get("status") == "held":
            clear(workspace, now, by=task)
        _mark(workspace, task, "lifted")
    if not entry.get("archived"):
        live = workspace / "tasks" / f"{task}.txt"
        if live.exists():
            _archive_task(workspace, live)
        _mark(workspace, task, "archived")


def handle_owner_actions(workspace: Path, now: Optional[float] = None, *,
                         switch_cmd: Optional[str] = None) -> list:
    """Act on the owner's "switch to <model>" / "retry now" replies while a hold is in force.
    Each reply is answered in its own conversation and acted on once; returns their ids."""
    import subprocess
    now = time.time() if now is None else now
    workspace = Path(workspace)
    for entry in _recorded_actions(read_hold(workspace)):
        if not all(entry.get(step) for step in _STEPS):
            _finish(workspace, entry, now)
    done = []
    for path in sorted((workspace / "tasks").glob("task-*.txt")):
        hold = active_hold(workspace, now)
        if not hold:
            break
        action = _owner_action(path)
        if action is None:
            continue
        if (workspace / "results" / path.name).exists():
            _archive_task(workspace, path)
            continue
        kind, model = action
        if kind == "retry":
            ok, reply = True, "Retrying now: the core is starting. If the limit is still in force it pauses again and I will tell you."
        elif not switch_cmd:
            ok, reply = False, "I could not switch the model from here; use the core-model card under Agent Settings."
        else:
            # A crash before the record below re-runs this switch: the same model written twice.
            run = subprocess.run(["bash", switch_cmd, model, "--offline"], capture_output=True, text=True, timeout=60)
            ok = run.returncode == 0
            reply = (f"Switched the core to {model}. It is starting now and will answer your waiting messages."
                     if ok else f"I could not switch the core to {model}; it stays paused. Try the core-model card under Agent Settings.")
        entry = {"task": path.stem, "kind": kind, "model": model, "ok": ok, "reply": reply, "at": now}
        _write_hold(workspace, dict(hold, actions=_recorded_actions(hold) + [entry]))
        _finish(workspace, entry, now)
        done.append(path.stem)
    return done


def _archive_task(workspace: Path, path: Path) -> None:
    """Take an answered reply out of the live queue so a returning core never acts on it again.
    The shared never-delete archive keeps it findable for the bridge that delivers the answer."""
    from task_archive import archive_file
    archive_file(path, "tasks", path.stem, tasks_dir=workspace / "tasks" / "archive",
                 results_dir=workspace / "results" / "archive", log=lambda m: None)


def _projects_dir(cwd: Optional[str]) -> Optional[Path]:
    if not cwd:
        return None
    from util_paths import claude_home_path, claude_project_slug
    return claude_home_path("projects", claude_project_slug(cwd))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    gate = sub.add_parser("gate", help="decide whether the core may launch now")
    gate.add_argument("--cwd", default="", help="the core's working directory (finds its transcripts)")
    gate.add_argument("--switch-entry", default="", help="the model-switch entry the launcher provides")
    sub.add_parser("status", help="print the hold record")
    sub.add_parser("clear", help="lift the hold now")
    args = ap.parse_args(argv)
    workspace = resolve_workspace()
    if args.cmd == "status":
        hold = read_hold(workspace)
        print(json.dumps(hold or {}, indent=2))
        return 0
    if args.cmd == "clear":
        hold = clear(workspace)
        print("hold lifted" if hold and hold.get("status") == "cleared" else "no hold in force")
        return 0
    handle_owner_actions(workspace, switch_cmd=args.switch_entry or None)
    model = qa.seat_model(qa.SeatEnv(True, None, os.environ.get("CLAUDE_CONFIG_DIR")), workspace)
    decision = decide(workspace, projects_dir=_projects_dir(args.cwd), launch_model=model)
    if decision.launch:
        return 0
    print(decision.line)
    return HOLD_EXIT


if __name__ == "__main__":
    sys.exit(main())
