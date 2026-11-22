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
    core_limit_hold.py held               exit 0 and the reason while a hold is in force, else 1
    core_limit_hold.py clear              lift the hold now (the next revive launches)

A hold also records a digest of the account it was observed under; a launch under a different
account is not held by it, so signing in to an account with quota resumes at once. Every launch
passes this gate and leaves its account on record even when nothing is held (status "none"), so
the next launch can tell a switch: evidence older than the gate's first sight of an account is
not that account's. Without that record, a limit hit by one account followed by a sign-in to
another before the next launch was pinned on the new account.

While a hold is in force the gate also acts on the owner's bare replies to the notice,
"switch to <model>" and "retry now", since nothing else reads the inbox with the core down.
A core that stays alive at a limit screen never reaches the gate, so the supervisor monitor
records the hold from that screen (`hold_while_wedged`) and acts on the same replies by
restarting the core from outside it (`relaunch_core`); the hold lifts once the screen is gone.
A worker seat's monitor does the same for its seat, given the restart its pool injects: the
seat's hold is its own record (`seat`), and a reply reaches the seat whose notice it quotes.
Tasks that arrive during a hold are listed in its record (`held_tasks`), so the orphan check
on the next boot leaves them for the core; each hold incident sends its own notice. Given the
desktop's card presenter (`gate --card-entry`), that notice also presents the core-model card.
A hold binds only launches on the model family and account it was seen under (`binding_hold`).

A 429 whose own response reports the account's limits all allowed is a throttle, not a limit:
it holds nothing unless its text names a usage limit or a time to retry. The CLI's
"(not your usage limit)" error is the same case, read from the transcript.
"""
from __future__ import annotations

import argparse
import hashlib
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
HELD_TASKS_MAX = 256
CLOCK_SKEW_S = 30
# A reset more than a day out is honored for 6 hours, then one launch re-reads the provider.
LONG_RESET_S = 24 * 3600
LONG_RESET_RECHECK_S = 6 * 3600
TRANSCRIPT_TAIL_BYTES = 256 * 1024
# The 429's own headers land a moment after it; only a later response says the provider served.
SERVED_SLACK_S = 5
PANE_SOURCE = "cli-pane"
# The old limit screen outlives an owner's action by a restart's length; it must not re-arm.
REARM_GRACE_S = 180
RELAUNCH_TIMEOUT_S = 300
# A live seat's monitor sees a reply within seconds; one older than this waited out a dead monitor.
REPLY_TTL_S = 10 * 60
AUDIT_LOG = "core-limit-actions.jsonl"
# Every reply ever acted on, across incidents and seats: a replayed message never acts again.
ACTED_FILE = "core-limit-acted.json"
ACTED_MAX = 1024

_CLI_LIMIT = tuple(rx for name, rx in cli_wedge.ABNORMAL_PATTERNS
                   if name in cli_wedge.PROVIDER_LIMIT_PATTERNS)
# The window-named banner ("5-hour limit reached"), a per-model cap ("You've reached your Fable
# limit."), and the API's own words for a refusal.
_USAGE_EXTRA = re.compile(r"\b\d+-hour limit (?:reached|exceeded|hit)\b|\bquota\b.{0,20}\b(?:exceeded|exhausted)\b"
                          r"|\binsufficient_quota\b", re.I)
_NAMED_CAP = re.compile(r"\breached your ([A-Za-z][\w.-]{0,24}) limit\b", re.I)
# The CLI's words for a 429 the account's quota did not cause: the server is throttling.
_NOT_A_LIMIT = re.compile(r"\bnot your usage limit\b", re.I)
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
    if not text or _NOT_A_LIMIT.search(text):
        return None
    named = _NAMED_CAP.search(text)
    if named:
        # The banner names the capped model; an earlier turn may have run another one.
        model = qa._model_family(named.group(1).lower()) or model
    if named or any(rx.search(text) for rx in _CLI_LIMIT) or _USAGE_EXTRA.search(text):
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
    if state != "rejected" and _own_headers_allowed(rej) and not (
            found and (found.kind == "usage-limit" or (found.reset_at or 0) > now)):
        return None  # a throttle on an account with quota; the CLI retries it itself
    kind = "usage-limit" if state == "rejected" else (found.kind if found else "rate-limit")
    reset = _record_reset(headers, now) if state == "rejected" else None
    if reset is None and found is not None and found.reset_at and found.reset_at > now:
        reset = found.reset_at
    return Limit(kind, at, reset, model, "provider-record")


def _own_headers_allowed(rej: dict) -> bool:
    """Whether the 429's own response, as the proxy recorded it with the rejection, reported
    every limit allowed. Absent or status-less headers say nothing, so the old backoff applies."""
    own = rej.get("limit_headers")
    if not isinstance(own, dict):
        return False
    own = {k: str(v).strip().lower() for k, v in own.items()}
    if own.get("anthropic-ratelimit-unified-status") not in qa.OVERAGE_OK:
        return False  # absent or unrecognised: fail closed
    return qa.classify_limit_state({"headers": own}) == "allowed"


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

def _hold_file(seat: Optional[str] = None) -> str:
    """The core's record, or a worker seat's own, named by its tmux session."""
    if not seat:
        return HOLD_FILE
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", seat):
        raise ValueError(f"not a seat session name: {seat!r}")
    return f"core-limit-hold.{seat}.json"


def read_hold(workspace: Path, seat: Optional[str] = None) -> Optional[dict]:
    try:
        data = json.loads(status_read_path(_hold_file(seat), Path(workspace)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def active_hold(workspace: Path, now: Optional[float] = None, seat: Optional[str] = None) -> Optional[dict]:
    """The hold in force right now, else None. Read-only."""
    hold = read_hold(workspace, seat)
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


def _write_hold(workspace: Path, hold: dict, seat: Optional[str] = None) -> None:
    write_status(_hold_file(seat), hold, workspace=Path(workspace))


def _active_holds(workspace: Path, now: float) -> list:
    """Every hold in force, the core's and each seat's, as (seat, hold); the core's seat is None."""
    from workspace_default import status_path
    found = [(None, active_hold(workspace, now))]
    try:
        names = sorted(status_path(HOLD_FILE, Path(workspace)).parent.glob("core-limit-hold.*.json"))
    except OSError:
        names = []
    for path in names:
        seat = path.name[len("core-limit-hold."):-len(".json")]
        try:
            found.append((seat, active_hold(workspace, now, seat)))
        except ValueError:
            continue
    return [(seat, hold) for seat, hold in found if hold]


def _live_tasks(workspace: Path) -> list:
    try:
        return sorted(p.stem for p in (Path(workspace) / "tasks").glob("*.txt") if p.is_file())
    except OSError:
        return []


def _admit_arrivals(workspace: Path, hold: dict) -> dict:
    """The hold with every task that arrived since it opened added to `held_tasks`.
    Those wait for the core, so the next boot's orphan check must leave them."""
    before = set(hold.get("present_at_hold") or [])
    held = [t for t in hold.get("held_tasks") or [] if isinstance(t, str)]
    new = [t for t in _live_tasks(workspace) if t not in before and t not in held]
    return dict(hold, held_tasks=(held + new)[-HELD_TASKS_MAX:]) if new else hold


def held_task_ids(workspace: Path) -> frozenset:
    """Ids of the tasks the last hold took in; they wait for the core, never orphans. Read-only.

    Kept after release on purpose: the boot that follows a release runs the orphan check, and these
    tasks arrived during the hold, so the age rule alone would archive them unanswered. The core's
    watcher hands every file in tasks/ to the core on that boot; the next hold carries only the ids
    still live, so a stale id never outlives its task file."""
    hold = read_hold(workspace) or {}
    return frozenset(t for t in hold.get("held_tasks") or [] if isinstance(t, str))


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


def account_fingerprint(config_dir: Optional[str]) -> Optional[str]:
    """A non-reversible digest of the account the CLI is signed in to, else None.
    Only non-secret identity fields of `.claude.json` are read; tokens never are."""
    if not config_dir:
        return None
    try:
        acct = json.loads((Path(config_dir) / ".claude.json").read_text(encoding="utf-8")).get("oauthAccount")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(acct, dict):
        return None
    ident = [str(acct.get(k) or "") for k in ("accountUuid", "organizationUuid", "emailAddress")]
    if not any(ident):
        return None
    return hashlib.sha256("\0".join(ident).encode("utf-8")).hexdigest()[:16]


def _same_account(held_account: Optional[str], launch_account: Optional[str]) -> bool:
    """A hold binds only the account it was seen under; an unknown side keeps it binding."""
    return not (held_account and launch_account and held_account != launch_account)


def _binds(hold: dict, launch_model: Optional[str], launch_account: Optional[str]) -> bool:
    return _same_family(hold.get("model"), launch_model) and _same_account(hold.get("account"), launch_account)


def _launch_config_dir() -> Optional[str]:
    """The config dir the launcher runs the core in: its export when set, else the resolver it
    exports from, so a caller the launcher did not start (the watchdog) reads the same seat."""
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        return os.environ["CLAUDE_CONFIG_DIR"]
    try:
        from sutando_config import resolve_claude_sutando_config_dir
        return str(resolve_claude_sutando_config_dir())
    except Exception:
        return None


def launch_identity(workspace: Path, config_dir: Optional[str] = None) -> tuple:
    """(model, account) the next launch would run as, read the way the gate reads them."""
    config_dir = _launch_config_dir() if config_dir is None else config_dir
    model = qa.seat_model(qa.SeatEnv(True, None, config_dir), Path(workspace))
    return model, account_fingerprint(config_dir)


def binding_hold(workspace: Path, now: Optional[float] = None) -> Optional[dict]:
    """The hold in force that would stop the next launch, else None. Read-only.
    A hold the owner switched away from (another model family or account) does not bind."""
    hold = active_hold(workspace, now)
    if hold is None:
        return None
    model, account = launch_identity(workspace)
    return hold if _binds(hold, model, account) else None


def _relevant(limit: Optional[Limit], launch_model: Optional[str], since: Optional[float] = None) -> bool:
    return limit is not None and _same_family(limit.model, launch_model) \
        and not (isinstance(since, (int, float)) and limit.at <= since)


def _incident_ref(hold: dict) -> str:
    """A short id for this pause, so two pauses with the same reset time read differently."""
    return str(int(float(hold.get("started_at") or 0) * 1000))[-6:].zfill(6)


def _task_predates(path: Path, started: object) -> bool:
    """Whether a task's server timestamp is before the hold opened, beyond the clock-skew margin.
    A missing or unreadable timestamp reads as predating, so the task waits for the core."""
    from datetime import datetime
    if not isinstance(started, (int, float)):
        return False
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("timestamp:"):
                stamp = datetime.fromisoformat(line.split(":", 1)[1].strip().replace("Z", "+00:00"))
                return stamp.timestamp() < started - CLOCK_SKEW_S
    except (OSError, ValueError):
        return True
    return True


def _when_line(hold: dict) -> str:
    return (f"It resets {_when(hold['reset_at'])}" if isinstance(hold.get("reset_at"), (int, float))
            else f"It tries again {_when(hold['until'])}")


def _notice_body(hold: dict, card: bool = False) -> str:
    from result_markers import neutralize_markers
    if hold.get("seat"):
        who = hold.get("seat_label") or hold["seat"]
        return "[dm-only]\n" + neutralize_markers(
            f"Your worker {who} is paused: {hold['reason'].split(': ', 1)[-1]} (pause {_incident_ref(hold)}).\n\n"
            f"{_when_line(hold)}, and it continues by itself after that.\n\n"
            'To restart it now, reply to this message with "retry now". Its model cannot be switched '
            "from chat yet: workers share the core's model setting.\n")
    where = ('pick a model on the "Your agent\'s model" card I left you, or under Agent Settings, Runtime, Model'
             if card else "pick a model in the core-model card under Agent Settings, Runtime, Model")
    return "[dm-only]\n" + neutralize_markers(
        f"Your Sutando core is paused: {hold['reason'].split(': ', 1)[-1]} (pause {_incident_ref(hold)}).\n\n"
        f"{_when_line(hold)}, and the core starts again by itself after that. Messages you send meanwhile "
        "wait and are answered then.\n\n"
        'To keep working now, reply to this or just send "switch to sonnet" (or another model: '
        f"haiku, opus, fable), or {where}. "
        'Send "retry now" to try the current model again at once.\n')


def _present_card(hold: dict, card_entry: str, now: float) -> bool:
    """Start the desktop's core-model card for this hold, fail-open. True when it was started.
    Detached: the card's DM mirror may take its own deadline, and the launch must not wait on it."""
    import subprocess
    body = f"Paused: {hold['reason'].split(': ', 1)[-1]}. Pick another model to keep working now."
    # The card lasts as long as the hold: notice_queued stops a second present for this pause.
    ttl = int(max(600, float(hold.get("until") or now) - now))
    try:
        subprocess.Popen([sys.executable, card_entry, "present", "core-model", "--set", f"body={body}",
                          "--ttl", str(ttl)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    return True


def _ensure_notice(workspace: Path, hold: dict, card_entry: Optional[str] = None,
                   now: Optional[float] = None) -> bool:
    """Queue the hold's owed DM under its stable name, once, and present the core-model card
    with it when the launcher provides one. True when it is queued.
    A retry rewrites the same name, so it cannot become a second message."""
    if hold.get("notice_queued"):
        return False
    from local_record import write_text_whole
    card = bool(card_entry) and Path(card_entry).is_file()
    body = _notice_body(hold, card)
    try:
        write_text_whole(Path(workspace) / "results" / hold["notice"], body)
    except OSError:
        return False
    hold["notice_queued"] = True
    hold["notice_line"] = body.split("\n", 1)[1].split("\n", 1)[0]
    if card:
        hold["card_presented"] = _present_card(hold, card_entry, time.time() if now is None else now)
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
           projects_dir: Optional[Path] = None, launch_model: Optional[str] = None,
           launch_account: Optional[str] = None, card_entry: Optional[str] = None,
           evidence: tuple = (), seat: Optional[str] = None, seat_label: Optional[str] = None) -> Decision:
    """Launch, or hold and say why. The one writer of the hold record.
    `evidence` adds limits the caller observed itself, such as a live core's limit screen. A worker
    `seat` has its own record, and only that evidence: the provider record and status are the core's."""
    now = time.time() if now is None else now
    workspace = Path(workspace)
    hold = read_hold(workspace, seat)
    if hold and hold.get("status") == "held":
        admitted = _admit_arrivals(workspace, hold)
        if admitted is not hold:
            hold = admitted
            _write_hold(workspace, hold, seat)
    if active_hold(workspace, now, seat) and _binds(hold, launch_model, launch_account):
        if _ensure_notice(workspace, hold, card_entry, now):
            _write_hold(workspace, hold, seat)
        return Decision(False, hold, status_line(hold))
    # Who ran last: the record's account, else the launcher a released legacy hold remembered.
    last = (hold or {}).get("account") or (hold or {}).get("launch_account")
    switched = bool(launch_account and last and last != launch_account)
    since = now if switched else (hold or {}).get("account_at")
    if quota is None and not seat:
        rec = qa.read_quota_record(workspace, now)
        quota = rec.payload if rec else None
    own = () if seat else (record_evidence(quota, now), transcript_evidence(projects_dir, now))
    found = [lim for lim in (*own, *evidence) if _relevant(lim, launch_model, since)]
    limit = max(found, key=lambda lim: lim.at) if found else None
    seen = hold.get("evidence_at") if hold else None
    if limit is not None and (not isinstance(seen, (int, float)) or limit.at > seen):
        last_end = hold.get("until") if hold else None
        recent = isinstance(last_end, (int, float)) and now - last_end < STRIKE_MEMORY_S
        released = hold.get("released_at") if hold and hold.get("status") != "held" else None
        if isinstance(released, (int, float)):
            # After a release, only a limit that ends the very next run counts as the same trouble.
            recent = limit.at - released < BACKOFF_BASE_S
        strikes = int(hold.get("strikes") or 0) + 1 if hold and recent else 1
        new = {
            "status": "held", "kind": limit.kind, "model": limit.model, "source": limit.source,
            "reset_at": limit.reset_at, "evidence_at": limit.at, "set_at": now,
            "until": _hold_until(limit, strikes, now), "strikes": strikes,
            "account": launch_account, "account_at": since,
        }
        if seat:
            new.update(seat=seat, seat_label=seat_label or seat)
        new["reason"] = status_line(new)
        # An incident runs until the core is let out; a hold after a release is a new one.
        if hold and hold.get("status") == "held" and hold.get("notice"):
            for key in ("started_at", "notice", "notice_queued", "notice_line", "present_at_hold", "held_tasks"):
                new[key] = hold.get(key)
            new["started_at"] = new["started_at"] or hold.get("set_at") or now
        else:
            from proactive_routing import proactive_filename
            live = _live_tasks(workspace)
            # Tasks a previous hold took in that the core has not answered still wait.
            carried = [t for t in (hold or {}).get("held_tasks") or [] if t in live]
            new["started_at"] = now
            new["notice"] = proactive_filename(f"core-limit-hold-{int(now * 1000)}")
            new["notice_queued"] = False
            new["held_tasks"] = carried
            new["present_at_hold"] = [t for t in live if t not in carried][-HELD_TASKS_MAX:]
        _ensure_notice(workspace, new, card_entry, now)
        _write_hold(workspace, new, seat)
        if not seat:
            _mirror_core_status(workspace, "paused", new["reason"], now)
        return Decision(False, new, new["reason"])
    was_held = bool(hold and hold.get("status") == "held")
    if was_held:
        hold = dict(hold, status="released", released_at=now)
        if launch_account and not hold.get("account"):
            # Not the old limit's owner, only who launched: the next switch is seen against it.
            hold["launch_account"] = launch_account
    if switched:
        hold = dict(hold, account=launch_account, account_at=now)
    # Record the launch account on any record that carries none: a switch is only seen against it.
    # Never onto a hold this launch released: that limit is not this account's to claim.
    first_sight = bool(launch_account and not was_held and not (hold or {}).get("account"))
    if first_sight:
        hold = dict(hold or {"status": "none"}, account=launch_account)
    if was_held or switched or first_sight:
        _write_hold(workspace, hold, seat)
    if was_held and not seat:
        _mirror_core_status(workspace, "starting", "resuming after a provider limit", now)
    return Decision(True, hold, "")


def clear(workspace: Path, now: Optional[float] = None, by: Optional[str] = None,
          mirror: bool = True, seat: Optional[str] = None) -> Optional[dict]:
    """Lift the hold. Its evidence stays recorded, so only a new refusal re-arms it.
    `mirror` False leaves core-status alone, for a core that is running and writes its own."""
    hold = read_hold(workspace, seat)
    if not hold or hold.get("status") != "held":
        return hold
    hold = dict(_admit_arrivals(workspace, hold), status="cleared", released_at=time.time() if now is None else now)
    if by:
        hold["cleared_by"] = by
    _write_hold(workspace, hold, seat)
    if mirror and not seat:
        _mirror_core_status(workspace, "starting", "resuming after a provider limit", hold["released_at"])
    return hold


def hold_while_wedged(workspace: Path, text: Optional[str], now: Optional[float] = None,
                      seat: Optional[str] = None, seat_label: Optional[str] = None) -> Optional[dict]:
    """The hold in force for a live core (or worker `seat`) stuck at a provider-limit screen,
    recorded from that screen's text when none is, so the owner gets the notice and a bare reply
    counts. Else None."""
    now = time.time() if now is None else now
    workspace = Path(workspace)
    hold = read_hold(workspace, seat) or {}
    released = hold.get("released_at")
    if hold.get("status") != "held" and isinstance(released, (int, float)) and 0 <= now - released < REARM_GRACE_S:
        return None
    model, account = (None, None) if seat else launch_identity(workspace)
    # The caller has judged the screen a limit; text that names none is still this core's limit.
    limit = classify_text(text or "", now, model, PANE_SOURCE) or Limit("usage-limit", now, None, model, PANE_SOURCE)
    decide(workspace, now, evidence=(limit,), launch_model=limit.model or model, launch_account=account,
           seat=seat, seat_label=seat_label)
    return active_hold(workspace, now, seat)


def release_pane_hold(workspace: Path, now: Optional[float] = None,
                      seat: Optional[str] = None) -> Optional[dict]:
    """Lift a hold a live core's limit screen caused, once that core is idle or working again."""
    hold = read_hold(workspace, seat)
    if not hold or hold.get("status") != "held" or hold.get("source") != PANE_SOURCE:
        return None
    return clear(workspace, now, by=PANE_SOURCE, mirror=False, seat=seat)


# ---- the owner's way out while the core is down -----------------------------

# The whole message must be the action, so a sentence that mentions it is never one.
_SWITCH = re.compile(r"^(?:please\s+)?(?:switch|change)\s+(?:the\s+core\s+)?(?:model\s+)?to\s+"
                     r"(sonnet|haiku|opus|fable)\s*[.!]?$", re.I)
_RETRY = re.compile(r"^(?:please\s+)?retry(?:\s+now)?\s*[.!]?$", re.I)


def _quotes_notice(reply: dict, hold: dict, agent: Optional[str]) -> bool:
    """Whether a quoted message is the notice this incident sent: its opening line, from the agent."""
    first = hold.get("notice_line")
    if not agent or not first or str(reply.get("sender") or "").strip() != agent.strip():
        return False
    flat = lambda t: " ".join(str(t).split())
    return flat(first) in flat(reply.get("body") or "")


def _quotes_seat(reply: dict, hold: dict, agent: Optional[str]) -> bool:
    """Whether a quoted message is from the agent and names this hold's worker seat (its card)."""
    seat = hold.get("seat")
    if not seat or not agent or str(reply.get("sender") or "").strip() != agent.strip():
        return False
    return seat in str(reply.get("body") or "")


def _owner_action(path: Path, workspace: Path, hold: Optional[dict] = None,
                  sole: bool = True) -> Optional[tuple]:
    """("switch", model) or ("retry", None) for an owner's bare reply, else None.
    Given `hold`, a quoted reply counts only when it quotes that hold's notice (or a worker's
    card), from the agent; an unquoted one only when `sole`, no other hold being in force."""
    from local_task_protocol import canonical_access_tier, reply_action_text, sender_words, split_reply_context
    from policy.egress.result import resolve_access_tier
    from task_envelope import attested_task_headers
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if resolve_access_tier(path) != "owner":
        return None
    parsed = attested_task_headers(text, workspace)
    # A gateway file states its tier after `task:`, so it counts only when its envelope verifies.
    if (parsed.headers.get("task_layout") or "").strip() == "mid" and \
            canonical_access_tier(parsed.headers.get("access_tier") or "") != "owner":
        return None
    words = sender_words(parsed)
    if hold is not None:
        reply = split_reply_context(words)[0]
        agent = parsed.headers.get("receiving_instance")
        if reply is None and not sole:
            return None
        if reply is not None and not (_quotes_notice(reply, hold, agent) or _quotes_seat(reply, hold, agent)):
            return None
    body = reply_action_text(words)
    m = _SWITCH.match(body)
    if m:
        return ("switch", m.group(1).lower())
    return ("retry", None) if _RETRY.match(body) else None


def _refusal(path: Path, workspace: Path, hold: dict, sole: bool) -> Optional[str]:
    """Why an exact "switch"/"retry" reply is not this seat's to act on: "not-owner" or
    "ambiguous" (unquoted while several holds are in force); None for any other message."""
    from local_task_protocol import canonical_access_tier, reply_action_text, sender_words, split_reply_context
    from policy.egress.result import resolve_access_tier
    from task_envelope import attested_task_headers
    try:
        parsed = attested_task_headers(path.read_text(encoding="utf-8", errors="replace"), workspace)
    except OSError:
        return None
    words = sender_words(parsed)
    body = reply_action_text(words)
    if not (_SWITCH.match(body) or _RETRY.match(body)):
        return None
    if resolve_access_tier(path) != "owner" or (
            (parsed.headers.get("task_layout") or "").strip() == "mid"
            and canonical_access_tier(parsed.headers.get("access_tier") or "") != "owner"):
        return "not-owner"
    return "ambiguous" if split_reply_context(words)[0] is None and not sole else None


def _task_time(path: Path) -> Optional[float]:
    from datetime import datetime
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("timestamp:"):
                return datetime.fromisoformat(line.split(":", 1)[1].strip().replace("Z", "+00:00")).timestamp()
            if line.startswith("task:"):
                break
    except (OSError, ValueError):
        return None
    return None


def _audit(workspace: Path, path: Path, seat: Optional[str], action, result: str, now: float) -> None:
    """One line per action or refusal in <workspace>/logs: who, what, which seat, what came of it.
    Ids only: the sender's id and tier, never the message text."""
    from task_envelope import attested_task_headers
    try:
        head = attested_task_headers(path.read_text(encoding="utf-8", errors="replace"), workspace).headers
    except (OSError, ValueError):
        head = {}
    kind, model = action if action else (None, None)
    rec = {"at": int(now), "task": path.stem, "who": head.get("sender_id") or head.get("user_id") or None,
           "tier": head.get("access_tier") or None, "seat": seat or "core", "action": kind,
           "model": model, "result": result}
    try:
        logs = Path(workspace) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / AUDIT_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    except OSError:
        pass


def _acted(workspace: Path) -> list:
    try:
        data = json.loads(status_read_path(ACTED_FILE, Path(workspace)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [t for t in data.get("tasks") or [] if isinstance(t, str)] if isinstance(data, dict) else []


def _record_acted(workspace: Path, task: str) -> None:
    write_status(ACTED_FILE, {"tasks": (_acted(workspace) + [task])[-ACTED_MAX:]}, workspace=Path(workspace))


def _recorded_actions(hold: Optional[dict]) -> list:
    acts = (hold or {}).get("actions") or []
    return [a for a in acts if isinstance(a, dict) and isinstance(a.get("task"), str)]


_STEPS = ("answered", "lifted", "archived")


def _steps(entry: dict) -> tuple:
    return _STEPS + (("relaunched",) if "relaunch" in entry else ())


def _mark(workspace: Path, task: str, step: str, seat: Optional[str] = None) -> None:
    """Persist one finished step of a recorded action; the record, not a file, says it is done."""
    hold = read_hold(workspace, seat)
    acts = _recorded_actions(hold)
    for act in acts:
        if act["task"] == task:
            act[step] = True
    if hold is not None:
        _write_hold(workspace, dict(hold, actions=acts), seat)


def _finish(workspace: Path, entry: dict, now: float, relaunch: Optional[dict] = None,
            seat: Optional[str] = None) -> None:
    """Complete a recorded action from its record alone: answer, lift, archive, relaunch, each
    marked done in the record as it lands, so a replay skips it and never answers a second time.
    Without `relaunch` (the launcher's gate) the core is starting anyway, so none is spawned."""
    import local_record
    from local_task_protocol import find_result
    task = entry["task"]
    if not entry.get("answered"):
        # Unmarked means a crash may have hit after the write: a copy live or archived is that write.
        if find_result(workspace / "results", task) is None:
            local_record.write_text_whole(workspace / "results" / f"{task}.txt", entry["reply"] + "\n")
        _mark(workspace, task, "answered", seat)
    if not entry.get("lifted"):
        hold = read_hold(workspace, seat)
        # A relaunch onto another model needs the hold: it keeps the old model's restart down.
        keep = bool((entry.get("relaunch") or {}).get("model"))
        if entry.get("ok") and not keep and hold and hold.get("status") == "held":
            clear(workspace, now, by=task, seat=seat)
        _mark(workspace, task, "lifted", seat)
    if not entry.get("archived"):
        live = workspace / "tasks" / f"{task}.txt"
        if live.exists():
            _archive_task(workspace, live)
        _mark(workspace, task, "archived", seat)
    if "relaunch" in entry and not entry.get("relaunched"):
        # Marked first: a monitor killed by the restart it started must not start a second one.
        _mark(workspace, task, "relaunched", seat)
        if relaunch and not _spawn_relaunch(workspace, relaunch, entry["relaunch"].get("model")):
            _tell_owner(workspace, 'I could not start the restart, so it is still paused. Send "retry now" '
                        "to try again.")


def _spawn_relaunch(workspace: Path, relaunch: dict, model: Optional[str]) -> bool:
    """Start `relaunch` detached, so it outlives the monitor whose core it restarts."""
    import subprocess
    cmd = [sys.executable, str(Path(__file__).resolve()), "relaunch", "--workspace", str(workspace)]
    if relaunch.get("command"):
        cmd += ["--", *relaunch["command"]]
    else:
        cmd += ["--launcher", relaunch["launcher"]]
        if model:
            cmd += ["--model", model, "--switch-entry", relaunch["switch"]]
    # The monitor inherits the launcher's in-session marker, but it runs outside the core session.
    env = {k: v for k, v in os.environ.items() if k != "SUTANDO_CORE_SESSION"}
    try:
        subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    return True


def relaunch_core(workspace: Path, launcher: Optional[str], model: Optional[str] = None,
                  switch_entry: Optional[str] = None, run=None, command: Optional[list] = None) -> bool:
    """Restart a live core from outside it; with `model`, the restart is held by the hold still in
    force and the switch then writes the model and starts the core. A worker seat's restart is the
    `command` its pool injected. The owner hears of a failure."""
    import subprocess
    run = run or subprocess.run

    def rc(cmd):
        try:
            return run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       timeout=RELAUNCH_TIMEOUT_S).returncode
        except (OSError, subprocess.SubprocessError):
            return None
    if command:
        ok = rc(list(command)) == 0
        if not ok:
            _tell_owner(workspace, 'I could not restart the worker, so it is still paused. Send "retry now" to try again.')
        return ok
    first = rc(["bash", launcher, "--restart"])
    ok = first == 0 if not model else first in (0, HOLD_EXIT)
    if ok and model:
        ok = bool(switch_entry) and rc(["bash", switch_entry, model]) == 0
    if not ok:
        _tell_owner(workspace, f"I could not restart the core{' on ' + model if model else ''}, so it is "
                    "still paused. Pick a model in the core-model card under Agent Settings, Runtime, "
                    'Model, or send "retry now" to try again.')
    return ok


def _tell_owner(workspace: Path, text: str) -> None:
    from local_record import write_text_whole
    from proactive_routing import proactive_filename
    try:
        write_text_whole(Path(workspace) / "results" / proactive_filename(f"core-limit-relaunch-{int(time.time() * 1000)}"),
                         "[dm-only]\n" + text + "\n")
    except OSError:
        pass


def handle_owner_actions(workspace: Path, now: Optional[float] = None, *,
                         switch_cmd: Optional[str] = None, relaunch: Optional[dict] = None,
                         seat: Optional[str] = None) -> list:
    """Act on the owner's "switch to <model>" / "retry now" replies while a hold is in force.
    Each reply is answered in its own conversation and acted on once; returns their ids.
    `relaunch` ({"launcher", "switch"}, or a worker seat's {"command"}) is given for a live seat:
    each action restarts it, and a reply older than REPLY_TTL_S is refused. Every action and
    refusal is audited."""
    import subprocess
    now = time.time() if now is None else now
    workspace = Path(workspace)
    for entry in _recorded_actions(read_hold(workspace, seat)):
        if not all(entry.get(step) for step in _steps(entry)):
            _finish(workspace, entry, now, relaunch, seat)
    done = []
    for path in sorted((workspace / "tasks").glob("task-*.txt")):
        hold = active_hold(workspace, now, seat)
        if not hold:
            break
        # A message that predates this hold was about an earlier one; it waits for the core.
        if path.stem in set(hold.get("present_at_hold") or []) or _task_predates(path, hold.get("started_at")):
            continue
        sole = [s_ for s_, _h in _active_holds(workspace, now)] == [seat]
        action = _owner_action(path, workspace, hold, sole)
        if action is None:
            why = _refusal(path, workspace, hold, sole)
            if why and path.stem not in (hold.get("refused") or []):
                _audit(workspace, path, seat, None, f"refused: {why}", now)
                _write_hold(workspace, dict(hold, refused=((hold.get("refused") or []) + [path.stem])[-HELD_TASKS_MAX:]), seat)
                if why == "ambiguous" and _find_result_for(workspace, path.stem) is None:
                    _answer_and_archive(workspace, path, "Several of my seats are paused, so I did not act on that. "
                                        "Reply to the pause message of the one you mean.")
            continue
        if (path.stem in {a["task"] for a in _recorded_actions(hold)} or path.stem in _acted(workspace)
                or _find_result_for(workspace, path.stem)):
            _audit(workspace, path, seat, action, "refused: replay", now)
            _archive_task(workspace, path)
            continue
        if relaunch:
            stamp = _task_time(path)
            if stamp is None or now - stamp > REPLY_TTL_S:
                _audit(workspace, path, seat, action, "refused: stale", now)
                _answer_and_archive(workspace, path, "That reply is more than 10 minutes old, so I did not act on it. "
                                    "Send it again if you still want it.")
                continue
        kind, model = action
        who = (hold.get("seat_label") or seat) if seat else None
        if relaunch and seat:
            ok = kind == "retry" and bool(relaunch.get("command"))
            reply = (f"Retrying now: I am restarting {who}. If the limit is still in force it pauses again and I will tell you."
                     if ok else f"I can't switch {who}'s model from chat yet: workers share the core's model setting. "
                     'Send "retry now" to restart it.')
        elif relaunch:
            ok = kind == "retry" or bool(relaunch.get("switch"))
            reply = ("Retrying now: I am restarting the core. If the limit is still in force it pauses again and I will tell you."
                     if kind == "retry" else
                     f"Switching the core to {model}: I am restarting it on {model} now, and it answers your waiting "
                     "messages once it is up. If that fails I will tell you here."
                     if ok else "I could not switch the model from here; use the core-model card under Agent Settings.")
        elif kind == "retry":
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
        if relaunch and ok:
            entry["relaunch"] = {"model": model}
        _write_hold(workspace, dict(hold, actions=_recorded_actions(hold) + [entry]), seat)
        _record_acted(workspace, path.stem)
        _audit(workspace, path, seat, action, "done" if ok else "refused: unsupported", now)
        _finish(workspace, entry, now, relaunch, seat)
        done.append(path.stem)
    return done


def _find_result_for(workspace: Path, task: str):
    from local_task_protocol import find_result
    return find_result(Path(workspace) / "results", task)


def _answer_and_archive(workspace: Path, path: Path, text: str) -> None:
    import local_record
    if _find_result_for(workspace, path.stem) is None:
        local_record.write_text_whole(Path(workspace) / "results" / f"{path.stem}.txt", text + "\n")
    if path.exists():
        _archive_task(workspace, path)


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
    gate.add_argument("--card-entry", default="", help="the desktop's local-card presenter, when the launcher has one")
    sub.add_parser("status", help="print the hold record")
    held = sub.add_parser("held", help="exit 0 and print the reason while a hold is in force")
    held.add_argument("--workspace", default="", help="the workspace to read (default: the resolved one)")
    sub.add_parser("clear", help="lift the hold now")
    rel = sub.add_parser("relaunch", help="restart a live core from outside it (the monitor's action)")
    rel.add_argument("--workspace", required=True)
    rel.add_argument("--launcher", default="", help="the core launcher (start-cli.sh)")
    rel.add_argument("--model", default="", help="restart on this model")
    rel.add_argument("--switch-entry", default="", help="the model-switch entry")
    rel.add_argument("command", nargs=argparse.REMAINDER, help="after --: a worker seat's injected restart")
    args = ap.parse_args(argv)
    if args.cmd == "relaunch":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command and not args.launcher:
            ap.error("relaunch needs --launcher or a command after --")
        return 0 if relaunch_core(Path(args.workspace), args.launcher or None, args.model or None,
                                  args.switch_entry or None, command=command) else 1
    if args.cmd == "held":
        hold = active_hold(Path(args.workspace) if args.workspace else resolve_workspace())
        if hold:
            print(hold.get("reason") or status_line(hold))
        return 0 if hold else 1
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
    model, account = launch_identity(workspace)
    decision = decide(workspace, projects_dir=_projects_dir(args.cwd), launch_model=model,
                      launch_account=account, card_entry=args.card_entry or None)
    if decision.launch:
        return 0
    print(decision.line)
    return HOLD_EXIT


if __name__ == "__main__":
    sys.exit(main())
