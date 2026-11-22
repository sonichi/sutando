#!/usr/bin/env python3
"""The core's recovery record (`core_recovery.v1`): one derivation, one writer.

What a remote client (phone, web, another computer) needs to show the core's
state and offer the fix that applies, with no access to the host. `derive()` is
pure: it folds the records other modules already keep into one small dict.
`publish()` is the only writer of `state/core-recovery.json`; the monitor tick
(`core-input-watch.py`) calls `update()`, which gathers the inputs and publishes.

    inputs                               owner
    state/core-supervisor.json           core-input-watch.py (the monitor)
    state/core-limit-hold.json           core_limit_hold.py
    state/supervisor-status.json         the desktop's backend supervisor
    state/quota-state.json               the credential proxy
    the pane's abnormal text             cli_wedge.frame_abnormal
    state/session-starts.log             the core launchers (restarts in the last 10 min)

The record is what the gateway bridge sends as `health.recovery`, so it carries
no prompt text, path, token or model output: `state`, times, the model alias,
an opaque account digest, counts and the fix actions. The literal wire example
is `contracts/core_recovery.v1/heartbeat.json`; `contracts/core_recovery.v1/`
also holds the owner's click (`action.json`) and its receipt (`receipt.json`),
whose vocabularies `parse_action_id` and `RECEIPT_OUTCOMES` own here.

    core_recovery_state.py show     the record as it would be written now (read-only)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cli_wedge
import core_gate_notice
import core_limit_hold
import worker_auth_state
import quota_availability as qa
from delivery.pane_gate import is_usage_limit
from workspace_default import resolve_workspace, status_path, status_read_path, write_status

RECORD_FILE = "core-recovery.json"
CAPABILITY = "core_recovery.v1"
VERSION = 1

STATES = ("running", "idle", "paused_limit", "out_of_credits", "signed_out",
          "blocked_prompt", "crash_loop", "starting", "unknown")
#: The core runtimes the launcher runs (src/agent/start-cli.sh), each with the CLI it is named for.
RUNTIMES = ("claude", "codex")
ACTIONS = ("retry_now", "switch_model", "switch_runtime", "restart_core", "escape_prompt")
#: Per runtime, the aliases a switch may offer (switch-model.sh's, Fable being a spend decision)
#: and the actions its host side carries out; Codex has no model switch or relaunch hold yet.
MODEL_ALIASES = {"claude": ("opus", "sonnet", "haiku"), "codex": ()}
#: A click may also name a stored account; it is never offered in the record before such a store
#: exists, and it is high impact: the executor asks for the owner's confirmation first.
STEP_UP_ACTIONS = frozenset({"switch_account"})
CLICK_VERBS = ACTIONS + tuple(sorted(STEP_UP_ACTIONS))
RUNTIME_ACTIONS = {"claude": frozenset(CLICK_VERBS),
                   "codex": frozenset({"switch_runtime", "restart_core", "escape_prompt"})}
#: An unknown runtime gets only what works on any pane.
NEUTRAL_ACTIONS = frozenset({"restart_core", "escape_prompt"})
RECEIPT_OUTCOMES = ("done", "refused", "stale", "failed")

MAX_STR = 64
MAX_ACTIONS = 8
MAX_SWITCH_MODELS = 6
#: The record on disk is a few hundred bytes; a reader refuses anything past this.
MAX_BYTES = 4096
#: An unchanged record is rewritten this often, so its mtime says the monitor is still running.
REFRESH_S = 60.0
#: A reader treats a record older than this as absent: the monitor that wrote it has stopped.
MAX_AGE_S = 5 * REFRESH_S

#: This many launches in RESTART_WINDOW_S with the core still not up is a crash loop.
CRASH_LOOP_RESTARTS = 4
RESTART_WINDOW_S = 600
SESSION_STARTS_TAIL_BYTES = 64 * 1024
QUOTA_EVIDENCE_WINDOW_S = 15 * 60

_ACTIONS_FOR = {
    "paused_limit": ("retry_now", "switch_model", "switch_runtime", "restart_core"),
    "out_of_credits": ("retry_now", "switch_model", "switch_runtime", "restart_core"),
    "signed_out": ("switch_runtime", "restart_core"),
    "blocked_prompt": ("escape_prompt", "restart_core"),
    "crash_loop": ("restart_core",),
    "unknown": ("restart_core",),
}
_LIMIT_KINDS = frozenset({"fable-limit", "fable-limit-unfocused", "fable-limit-refused"})
_LOGIN_KINDS = frozenset({"login"})
# The provider's own words for an exhausted prepaid balance, as opposed to a plan's usage window.
_CREDIT_TEXT = re.compile(r"\bout of (?:usage )?credits?\b|\bcredit balance\b|\binsufficient[_ ](?:credits?|quota)\b"
                          r"|\bbilling\b", re.I)
_ACTION_ID = re.compile(r"^(?:(retry_now|restart_core|escape_prompt)|(switch_model|switch_runtime):([a-z]{1,16})"
                        r"|(switch_account):(acct_[0-9a-f]{6}))$")
_HEX = re.compile(r"^[0-9a-f]{6,64}$")
_ALL_ALIASES = tuple(a for r in RUNTIMES for a in MODEL_ALIASES[r])


# ---- the vocabulary other modules share -------------------------------------------------

def parse_action_id(action_id: Any, runtime: Optional[str] = None, accounts: tuple = ()) -> Optional[tuple]:
    """(verb, arg) for an allowlisted click id (`contracts/core_recovery.v1/action.json`), else
    None. With `runtime`, the id must be one that runtime carries out: `switch_model:<alias>` an
    alias of its own, `switch_runtime:<runtime>` another runtime. `switch_account:<id>` takes only
    an id in `accounts`, the host's own stored list. Anything else is refused; no part of the id is
    ever passed on but a value from one of those lists."""
    if not isinstance(action_id, str):
        return None
    m = _ACTION_ID.fullmatch(action_id)
    if not m:
        return None
    simple, verb, arg, acct_verb, acct = m.groups()
    if simple:
        verb, arg = simple, None
    elif acct_verb:
        verb, arg = acct_verb, acct
    if verb == "switch_account" and arg not in accounts:
        return None
    if runtime is not None and verb not in RUNTIME_ACTIONS.get(runtime, NEUTRAL_ACTIONS):
        return None
    if verb == "switch_model" and arg not in (MODEL_ALIASES.get(runtime, ()) if runtime else _ALL_ALIASES):
        return None
    if verb == "switch_runtime" and (arg not in RUNTIMES or arg == runtime):
        return None
    return (verb, arg)


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _str(value: Any) -> Optional[str]:
    return value[:MAX_STR] if isinstance(value, str) and value else None


def coerce(record: Any) -> Optional[dict]:
    """The record with every field inside its bounds, else None. Unknown fields are dropped."""
    if not isinstance(record, dict) or record.get("v") != VERSION or record.get("state") not in STATES:
        return None
    out: dict = {"v": VERSION, "state": record["state"]}
    for key in ("incident", "model", "account"):
        value = _str(record.get(key))
        if value is not None:
            out[key] = value
    if record.get("runtime") in RUNTIMES:
        out["runtime"] = record["runtime"]
    for key in ("since", "reset_at", "waiting_tasks", "restarts_10m"):
        value = _int(record.get(key))
        if value is not None and value >= 0:
            out[key] = value
    acts = record.get("actions")
    if isinstance(acts, list):
        out["actions"] = [a for a in acts if a in ACTIONS][:MAX_ACTIONS]
    models = record.get("switch_models")
    if isinstance(models, list):
        allowed = MODEL_ALIASES.get(out.get("runtime"), _ALL_ALIASES)
        out["switch_models"] = [m for m in models if m in allowed][:MAX_SWITCH_MODELS]
    runtimes = record.get("switch_runtimes")
    if isinstance(runtimes, list):
        out["switch_runtimes"] = [r for r in runtimes if r in RUNTIMES and r != out.get("runtime")]
    return {k: out[k] for k in _ORDER if k in out}


_ORDER = ("v", "incident", "state", "since", "reset_at", "runtime", "model", "account",
          "waiting_tasks", "restarts_10m", "actions", "switch_models", "switch_runtimes")


# ---- derivation ---------------------------------------------------------------------------

def _hold_active(hold: Optional[dict], now: float) -> bool:
    return bool(isinstance(hold, dict) and hold.get("status") == "held"
                and isinstance(hold.get("until"), (int, float)) and now < hold["until"])


def _family(model: Optional[str]) -> Optional[str]:
    family = qa._model_family(qa._norm_model(model))
    return family or ("default" if qa._norm_model(model) == "default" else None)


def _account(hold: Optional[dict]) -> Optional[str]:
    acct = hold.get("account") if isinstance(hold, dict) else None
    return f"acct_{acct[:6]}" if isinstance(acct, str) and _HEX.match(acct) else None


def _wedge_names(wedge: Any) -> tuple:
    names = getattr(wedge, "names", None)
    return tuple(names) if isinstance(names, (list, tuple)) else ()


def _quota_says_credits(quota: Any, now: float) -> bool:
    ledger = quota.get("recent_rejections") if isinstance(quota, dict) else None
    for rej in ledger if isinstance(ledger, list) else []:
        if not isinstance(rej, dict):
            continue
        at = qa._parse_when(rej.get("ts"))
        if isinstance(at, (int, float)) and 0 <= now - at <= QUOTA_EVIDENCE_WINDOW_S \
                and _CREDIT_TEXT.search(str(rej.get("snippet") or "")):
            return True
    return False


def _credits(supervisor: dict, wedge: Any, quota: Any, now: float) -> bool:
    """Whether the refusal names an exhausted balance rather than a plan's window."""
    if "out-of-credits" in _wedge_names(wedge):
        return True
    prompt = supervisor.get("prompt")
    if isinstance(prompt, str) and prompt:
        if _CREDIT_TEXT.search(prompt) or "out-of-credits" in _wedge_names(cli_wedge.frame_abnormal(prompt)):
            return True
    return _quota_says_credits(quota, now)


def _terminal_failure(supervisor_status: Any) -> bool:
    services = supervisor_status.get("services") if isinstance(supervisor_status, dict) else None
    for svc in (services or {}).values() if isinstance(services, dict) else ():
        term = svc.get("terminal") if isinstance(svc, dict) else None
        if isinstance(term, dict) and term.get("state") == "failed":
            return True
    return False


def _state(supervisor: dict, hold_active: bool, supervisor_status: Any, quota: Any, wedge: Any,
           restarts: Optional[int], now: float, runtime: Optional[str]) -> str:
    sup = supervisor.get("state")
    kind = supervisor.get("kind")
    walled = getattr(wedge, "kind", None) == "provider-limit"
    if sup == "running":
        return "running"
    # The monitor reads a Claude refusal itself; a Codex one leaves its idle prompt, so only
    # the banner under its last answer (see `codex_tail`) says the turn was refused.
    if sup == "idle-ready" and not (runtime == "codex" and walled):
        return "idle"
    limited = sup in ("blocked-human", "blocked-known") and (
        kind in _LIMIT_KINDS or is_usage_limit(kind, supervisor.get("prompt")))
    if limited or walled or hold_active:
        return "out_of_credits" if _credits(supervisor, wedge, quota, now) else "paused_limit"
    if sup == "logged-out" or (sup == "blocked-human" and kind in _LOGIN_KINDS):
        return "signed_out"
    if sup == "blocked-human":
        return "blocked_prompt"
    if sup == "blocked-known":
        return "idle"  # a gate the monitor answers by itself
    if sup == "crashed":
        if (restarts or 0) >= CRASH_LOOP_RESTARTS or _terminal_failure(supervisor_status):
            return "crash_loop"
        return "starting"
    return "unknown"


def _incident(state: str, since: int, hold: Optional[dict], hold_active: bool) -> str:
    seed = f"hold:{hold.get('started_at')}" if hold_active and hold.get("started_at") else f"{state}:{since}"
    return "inc_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]


def derive(supervisor: Optional[dict], hold: Optional[dict], supervisor_status: Optional[dict],
           quota: Optional[dict], wedge: Any, *, now: float, prev: Optional[dict] = None,
           runtime: Optional[str] = None, model: Optional[str] = None,
           waiting_tasks: Optional[int] = None, restarts_10m: Optional[int] = None,
           runtimes_available: tuple = ()) -> dict:
    """The C1 recovery block for this moment. Pure: every input is passed in.

    `supervisor` is core-supervisor.json's payload, `hold` core-limit-hold.json's, `quota`
    quota-state.json's, `wedge` cli_wedge.frame_abnormal(pane). `prev` is the last record, so
    an unchanged state keeps its `since` and `incident`. `runtime` is the core's (RUNTIMES);
    `runtimes_available` the runtimes whose CLI this host has. The relaunch hold, the account, the
    model and the proxy's quota record are Claude's, so a Codex core reads none of them. An
    unknown input is left out of the record, never filled with a default."""
    supervisor = supervisor if isinstance(supervisor, dict) else {}
    runtime = runtime if runtime in RUNTIMES else None
    if runtime == "codex":
        hold, model, quota = None, None, None
    hold_active = _hold_active(hold, now)
    state = _state(supervisor, hold_active, supervisor_status, quota, wedge, restarts_10m, now, runtime)
    limited = state in ("paused_limit", "out_of_credits")
    if isinstance(prev, dict) and prev.get("state") == state and _int(prev.get("since")) is not None:
        since, incident = int(prev["since"]), _str(prev.get("incident"))
    else:
        started = _int(hold.get("started_at")) if hold_active else None
        since, incident = (started if started is not None else int(now)), None
    record: dict = {"v": VERSION, "incident": incident or _incident(state, since, hold, hold_active),
                    "state": state, "since": since}
    if limited and hold_active:
        reset = _int(hold.get("reset_at"))
        if reset is not None:
            record["reset_at"] = reset
    if runtime:
        record["runtime"] = runtime
    family = _family(model)
    if family:
        record["model"] = family
    account = _account(hold)
    if account:
        record["account"] = account
    if isinstance(waiting_tasks, int) and not isinstance(waiting_tasks, bool):
        record["waiting_tasks"] = waiting_tasks
    if isinstance(restarts_10m, int) and not isinstance(restarts_10m, bool):
        record["restarts_10m"] = restarts_10m
    can = RUNTIME_ACTIONS.get(runtime, NEUTRAL_ACTIONS)
    actions = [a for a in _ACTIONS_FOR.get(state, ()) if a in can]
    if "switch_model" in actions:
        held = _family(hold.get("model")) if hold_active else None
        record["switch_models"] = [a for a in MODEL_ALIASES[runtime] if a not in (family, held)]
        if not record["switch_models"]:
            actions.remove("switch_model")
            del record["switch_models"]
    if "switch_runtime" in actions:
        record["switch_runtimes"] = [r for r in RUNTIMES if r in runtimes_available and r != runtime]
        if not record["switch_runtimes"]:
            actions.remove("switch_runtime")
            del record["switch_runtimes"]
    record["actions"] = actions
    return coerce(record)


# ---- the one writer, and its readers -------------------------------------------------------

def record_path(workspace: Path) -> Path:
    return status_path(RECORD_FILE, Path(workspace))


def read(workspace: Path, now: Optional[float] = None, max_age_s: Optional[float] = MAX_AGE_S) -> Optional[dict]:
    """The published record, or None when absent, oversized, malformed or older than `max_age_s`."""
    path = status_read_path(RECORD_FILE, Path(workspace))
    try:
        st = path.stat()
        if st.st_size > MAX_BYTES:
            return None
        if max_age_s is not None and (time.time() if now is None else now) - st.st_mtime > max_age_s:
            return None
        return coerce(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def publish(workspace: Path, record: dict, now: Optional[float] = None) -> bool:
    """Write `record` atomically when it changed or the file is due a refresh; True when written."""
    record = coerce(record)
    if record is None:
        raise ValueError("not a core_recovery.v1 record")
    at = time.time() if now is None else now
    path = record_path(workspace)
    try:
        if json.loads(path.read_text(encoding="utf-8")) == record and at - path.stat().st_mtime < REFRESH_S:
            return False
    except (OSError, ValueError):
        pass
    write_status(RECORD_FILE, record, workspace=Path(workspace))
    return True


# ---- gathering the inputs (the monitor's side) ---------------------------------------------

def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


_HOST_LABELS: Optional[frozenset] = None


def _host_labels() -> frozenset:
    global _HOST_LABELS
    if _HOST_LABELS is None:
        labels = {socket.gethostname().split(".")[0]}
        try:
            from util_paths import _host_label
            labels.add(_host_label())
        except Exception:  # noqa: BLE001 -- the short hostname is what the launchers stamp
            pass
        _HOST_LABELS = frozenset(x for x in labels if x)
    return _HOST_LABELS


def restarts_in_window(workspace: Path, now: float, window_s: float = RESTART_WINDOW_S,
                       hosts: Optional[frozenset] = None) -> Optional[int]:
    """Core launches on this host within `window_s`, from state/session-starts.log; None when
    the log cannot be read. A line from another host, or one that does not parse, is skipped."""
    hosts = _host_labels() if hosts is None else hosts
    path = status_read_path("session-starts.log", Path(workspace))
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - SESSION_STARTS_TAIL_BYTES))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except FileNotFoundError:
        return 0
    except OSError:
        return None
    count = 0
    for line in lines:
        try:
            entry = json.loads(line)
            at = float(entry.get("session_started_at"))
        except (ValueError, TypeError, AttributeError):
            continue
        if entry.get("host") in hosts and 0 <= now - at <= window_s:
            count += 1
    return count


def codex_tail(pane: str) -> str:
    """The rows under the last Codex answer (a `•` line): a banner above it is one the core outran."""
    rows = pane.splitlines()
    for i in range(len(rows) - 1, -1, -1):
        if worker_auth_state._CODEX_RAN.match(rows[i]):
            return "\n".join(rows[i + 1:])
    return pane


def recorded_runtime(workspace: Path) -> Optional[str]:
    """The runtime the launcher last confirmed (state/core-runtime.json), for a session that is gone."""
    rec = _read_json(status_read_path("core-runtime.json", Path(workspace)))
    value = rec.get("runtime") if rec else None
    return value if value in RUNTIMES else None


def runtimes_on_host() -> tuple:
    """The runtimes whose CLI is on PATH: a switch is offered only to one this host can launch."""
    return tuple(r for r in RUNTIMES if shutil.which(r))


def gather(workspace: Path, supervisor: Optional[dict], *, now: float, runtime: Optional[str] = None,
           pane: Optional[str] = None) -> dict:
    """The record for this tick, from the files the other modules keep. Read-only."""
    workspace = Path(workspace)
    runtime = runtime if runtime in RUNTIMES else recorded_runtime(workspace)
    model = None
    if runtime != "codex":
        try:
            model = core_limit_hold.launch_identity(workspace)[0]
        except Exception:  # noqa: BLE001 -- an unreadable identity is an unknown model, not a failure
            model = None
    if pane and runtime == "codex":
        pane = codex_tail(pane)
    return derive(
        supervisor, core_limit_hold.read_hold(workspace),
        _read_json(status_read_path("supervisor-status.json", workspace)),
        _read_json(status_read_path("quota-state.json", workspace)),
        cli_wedge.frame_abnormal(pane) if pane else None,
        now=now, prev=read(workspace, now, max_age_s=None), runtime=runtime, model=model,
        waiting_tasks=core_gate_notice.queued_count(workspace),
        restarts_10m=restarts_in_window(workspace, now), runtimes_available=runtimes_on_host())


def update(workspace: Path, supervisor: Optional[dict], *, runtime: Optional[str] = None,
           pane: Optional[str] = None, now: Optional[float] = None) -> bool:
    """One monitor tick: gather, derive and publish. Never raises: the monitor must keep ticking."""
    at = time.time() if now is None else now
    try:
        return publish(workspace, gather(workspace, supervisor, now=at, runtime=runtime, pane=pane), at)
    except Exception as exc:  # noqa: BLE001
        print(f"core-recovery record not written: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("command", choices=("show",))
    ap.parse_args(argv)
    ws = resolve_workspace()
    sup = _read_json(status_read_path("core-supervisor.json", ws))
    print(json.dumps(gather(ws, sup, now=time.time()), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
