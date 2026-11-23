#!/usr/bin/env python3
"""The core's runtime profile (`runtime_profile.v1`): its model, the switches it offers, its plan name.

What the owner's clients show as "which model is my agent on, what can it switch to, on which
plan". The gateway bridge sends it as the heartbeat's top-level `runtime_profile` with the
`runtime_profile.v1` capability, through the reader the sutando adapter injects. It carries no
usage or limit state: the heartbeat's `health.quota` (`quota_status.v1`, src/quota_status.py) is
their one source. `derive()` is pure; `gather()` reads the records other modules already keep:

    input                                  owner
    state/core-runtime.json                the core launchers (which runtime)
    the seat's model (settings, switch)    core_limit_hold.launch_model
    state/runtime-models.<runtime>.json    runtime_models (the runtime's own model list)
    state/quota-state.json                 its last request's model only, for the core's own login
    state/core-limit-hold.json             core_limit_hold (the held model is not offered as a switch)
    the CLI's signed-in account            core_limit_hold.oauth_account (digest and plan name)

The quota record names the version of a model alias only when it is the signed-in account's: a
proxy record by its `acct_` label, a status line record by `quota_availability.from_statusline`.

Unknown is null, never guessed: a Codex core reports no plan, an unknown core model sends no
block at all (the schema requires one), and `workers` stays [] until a worker reports its own
model. A switch option's `token` is the runtime list's opaque row key, the id a recovery card's
`switch_model:<token>` click names, so a client's switch reaches the executor's own allowlist.
The literal wire examples are `contracts/runtime_profile.v1/heartbeat.*.json`.

    runtime_profile.py show     the block as the next heartbeat would send it
"""
from __future__ import annotations

import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

VERSION = 1
CAPABILITY = "runtime_profile.v1"
MAX_BYTES = 8192
MAX_OPTIONS = 6
MAX_WORKERS = 16
RUNTIMES = ("claude", "codex")

_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*(\[[A-Za-z0-9._-]+\])?$")
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ACCOUNT = re.compile(r"^acct_[A-Za-z0-9_-]{1,48}$")
_FAMILIES = ("opus", "sonnet", "haiku", "fable")
_CLAUDE_ID = re.compile(r"^claude-(opus|sonnet|haiku|fable)-(\d+(?:-\d+)*)$")
_ROW_MODEL = re.compile(r"^(Opus|Sonnet|Haiku|Fable) (\d+(?:\.\d+)*)$")
_TAG = re.compile(r"\[[^\]]*\]$")
#: A plan name is a few plain words ("Claude Max", "Ultra"); an id, email or path never fits.
_PLAN_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9+]*(?: [A-Za-z0-9+]+){0,3}$")
_ID_RUN = re.compile(r"[0-9A-Fa-f]{8,}|\d{5,}")


# ---- shapes ---------------------------------------------------------------------------------

def _epoch(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return int(value)


def _label(value: Any) -> Optional[str]:
    if isinstance(value, str) and 1 <= len(value) <= 64 and value == value.strip() and value.isprintable():
        return value
    return None


def _plan_label(value: Any) -> Optional[str]:
    if isinstance(value, str) and len(value) <= 32 and _PLAN_NAME.fullmatch(value) and not _ID_RUN.search(value):
        return value
    return None


def _model_id(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and len(value) <= 64 and _MODEL_ID.fullmatch(value) else None


def _model(value: Any) -> Optional[dict]:
    if not isinstance(value, dict) or set(value) != {"id", "display_name", "runtime"}:
        return None
    out = {"id": _model_id(value["id"]), "display_name": _label(value["display_name"]),
           "runtime": value["runtime"] if value["runtime"] in RUNTIMES else None}
    return out if all(out.values()) else None


def _options(value: Any) -> Optional[list]:
    if not isinstance(value, list) or len(value) > MAX_OPTIONS:
        return None
    out = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"token", "display_name"}:
            return None
        token, name = _model_id(item["token"]), _label(item["display_name"])
        if not (token and name):
            return None
        out.append({"token": token, "display_name": name})
    return out


def _plan(value: Any) -> Any:
    """A plan block, None for null, or False when it is not one."""
    if value is None:
        return None
    if not isinstance(value, dict) or not {"name"} <= set(value) <= {"name", "account"}:
        return False
    account_ok = "account" not in value or (isinstance(value["account"], str) and _ACCOUNT.fullmatch(value["account"]))
    return dict(value) if _label(value["name"]) and account_ok else False


def coerce(block: Any) -> Optional[dict]:
    """`block` when it is exactly a runtime_profile.v1 block (schema.json), else None."""
    keys = {"v", "reported_at", "model", "switch_options", "plan", "workers"}
    if not isinstance(block, dict) or set(block) != keys or block["v"] != VERSION or isinstance(block["v"], bool):
        return None
    if _epoch(block["reported_at"]) != block["reported_at"] or _model(block["model"]) is None:
        return None
    if _options(block["switch_options"]) is None or _plan(block["plan"]) is False:
        return None
    workers = block["workers"]
    if not isinstance(workers, list) or len(workers) > MAX_WORKERS:
        return None
    for w in workers:
        if not isinstance(w, dict) or not isinstance(w.get("worker_id"), str) \
                or not _WORKER_ID.fullmatch(w["worker_id"]) \
                or not set(w) <= {"worker_id", "model", "switch_options", "plan"}:
            return None
        if ("model" in w and _model(w["model"]) is None) or ("switch_options" in w and _options(w["switch_options"]) is None) \
                or ("plan" in w and _plan(w["plan"]) is False):
            return None
    return block


# ---- derivation -----------------------------------------------------------------------------

def _claude_model_from_id(model_id: str) -> Optional[dict]:
    hit = _CLAUDE_ID.fullmatch(model_id)
    if not hit:
        return None
    version = [p for p in hit.group(2).split("-") if len(p) < 8]  # a dated snapshot's date is no version
    if not version:
        return None
    display = f"Claude {hit.group(1).capitalize()} {'.'.join(version)}"
    return {"id": model_id, "display_name": display, "runtime": "claude"}


def _claude_model_from_row(row: dict) -> Optional[dict]:
    hit = _ROW_MODEL.fullmatch(str(row.get("model") or ""))
    if not hit:
        return None
    return _claude_model_from_id(f"claude-{hit.group(1).lower()}-{hit.group(2).replace('.', '-')}")


def _family_of(row_or_id: Any) -> Optional[str]:
    text = str(row_or_id or "").lower()
    head = text.split(" ", 1)[0]
    if head in _FAMILIES:
        return head
    hit = _CLAUDE_ID.fullmatch(_TAG.sub("", text))
    return hit.group(1) if hit else None


def claude_model(value: Optional[str], rows: List[dict], quota_model: Optional[str]) -> Optional[dict]:
    """The Claude model `value` (the seat's alias or id) names, by the runtime's own list or the
    proxy's last request of the same family; None when neither says which version it is."""
    raw = value.strip() if isinstance(value, str) and value.strip() else ""
    tag = _TAG.search(raw)
    model = _claude_untagged(_TAG.sub("", raw) or None, rows, quota_model)
    tagged = _model_id(model["id"] + tag.group(0)) if model and tag else None
    return dict(model, id=tagged) if tagged else model


def _claude_untagged(value: Optional[str], rows: List[dict], quota_model: Optional[str]) -> Optional[dict]:
    if value and _CLAUDE_ID.fullmatch(value):
        return _claude_model_from_id(value)
    if value in _FAMILIES:
        for row in rows:
            if _family_of(row.get("model")) == value and _claude_model_from_row(row):
                return _claude_model_from_row(row)
        quota_model = _TAG.sub("", quota_model) if isinstance(quota_model, str) else None
        if quota_model and _family_of(quota_model) == value:
            return _claude_model_from_id(quota_model)
        return None
    if value in (None, "default"):
        row = next((r for r in rows if r.get("arg") == "default"), None)
        return _claude_model_from_row(row) if row else None
    return None


def codex_model(rows: List[dict]) -> Optional[dict]:
    """The picker's current row, named by its validated model id ("gpt-5.5" reads "GPT-5.5")."""
    row = next((r for r in rows if r.get("current")), None)
    model_id = _model_id(row.get("model")) if row else None
    return {"id": model_id, "display_name": re.sub(r"^gpt-", "GPT-", model_id), "runtime": "codex"} \
        if model_id else None


def _switch_name(row: dict) -> Optional[str]:
    """A row's name from the model it runs, never the picker's own text."""
    model = _claude_model_from_row(row)
    if not model:
        return None
    return model["display_name"] + (" (1M context)" if str(row.get("arg") or "").endswith("[1m]") else "")


def switch_options(runtime: str, offered: List[dict]) -> List[dict]:
    """The rows a recovery card offers as switches (recovery_actions.switch_offer), by row key."""
    if runtime != "claude":
        return []  # a Codex core's model is a launch argument: no switch runs on it
    out, seen = [], set()
    for row in offered:
        token, name = _model_id(row.get("key")), _switch_name(row)
        if token and name and token not in seen:
            out.append({"token": token, "display_name": name})
            seen.add(token)
        if len(out) == MAX_OPTIONS:
            break
    return out


def derive(*, runtime: Optional[str], model: Optional[dict], offered: List[dict],
           plan_name: Optional[str], account_digest: Optional[str], now: float) -> Optional[dict]:
    """The block for one heartbeat, or None when the core's runtime or model is unknown."""
    if runtime not in RUNTIMES or _model(model) is None or model["runtime"] != runtime:
        return None
    import core_recovery_state
    plan = None
    name = _plan_label(plan_name)
    if runtime == "claude" and name:
        plan = {"name": name}
        account = core_recovery_state.account_label(account_digest)
        if account:
            plan = {"account": account, **plan}
    return coerce({"v": VERSION, "reported_at": int(now), "model": dict(model),
                   "switch_options": switch_options(runtime, offered), "plan": plan, "workers": []})


# ---- gathering ------------------------------------------------------------------------------

def _codex_rows(workspace: Path) -> List[dict]:
    """The Codex picker's rows only when read during the launch core-runtime.json records."""
    import core_recovery_state
    import runtime_models
    data = runtime_models.read(workspace, "codex") or {}
    launch = core_recovery_state.recorded_launch(workspace) or {}
    at, started = data.get("at"), launch.get("started_at")
    if launch.get("runtime") != "codex" or _epoch(at) is None or _epoch(started) is None or at < started:
        return []
    return runtime_models.entries(workspace, "codex")


def _runtime(workspace: Path) -> Optional[str]:
    import core_recovery_state
    recorded = core_recovery_state.recorded_runtime(workspace)
    if recorded:
        return recorded
    try:
        from sutando_config import resolve_core_runtime
        value = resolve_core_runtime()
    except Exception:  # noqa: BLE001 -- an unresolvable runtime is unknown
        return None
    return value if value in RUNTIMES else None


def _own_record(payload: Any, digest: Optional[str]) -> Optional[dict]:
    """The quota record when the account signed in as `digest` produced it, else None."""
    import quota_availability as qa
    if not isinstance(payload, dict) or not digest:
        return None
    if payload.get("source") == qa.STATUSLINE_SOURCE:
        return payload if qa.from_statusline(payload, digest) else None
    return payload if payload.get("account") == qa.account_label(digest) else None


def gather(workspace: Path, now: Optional[float] = None) -> Optional[dict]:
    """The block as the next heartbeat would send it. Read-only."""
    import core_limit_hold
    import quota_availability as qa
    import recovery_actions
    import runtime_models
    workspace = Path(workspace)
    at = time.time() if now is None else now
    runtime = _runtime(workspace)
    if runtime == "codex":
        return derive(runtime=runtime, model=codex_model(_codex_rows(workspace)), offered=[], plan_name=None,
                      account_digest=None, now=at)
    rows = runtime_models.entries(workspace, runtime) if runtime else []
    config_dir = core_limit_hold._launch_config_dir()
    acct = core_limit_hold.oauth_account(config_dir)
    digest = core_limit_hold.account_digest(acct)
    value = core_limit_hold.launch_model(workspace, config_dir)
    record = qa.read_quota_record(workspace, at)
    hold = core_limit_hold.binding_hold(workspace, at, identity=(value, digest))
    by_key = {r["key"]: r for r in rows}
    keys = recovery_actions.switch_offer(workspace, runtime, avoid=hold.get("model") if hold else None,
                                         launch_model=value)
    hint = qa.record_model(_own_record(record.payload if record else None, digest))
    return derive(runtime=runtime, model=claude_model(value, rows, hint),
                  offered=[by_key[k] for k in keys if k in by_key],
                  plan_name=acct.get("planDisplayName") if acct else None, account_digest=digest, now=at)


def read(workspace: Path, now: Optional[float] = None) -> Optional[dict]:
    """`gather` for the heartbeat: a valid block within MAX_BYTES, else None. Never raises."""
    try:
        block = coerce(gather(workspace, now))
        return block if block is not None and len(json.dumps(block)) <= MAX_BYTES else None
    except Exception:  # noqa: BLE001 -- the profile is advisory; the beat goes out without it
        return None


def main(argv: Optional[list] = None) -> int:
    import argparse
    from workspace_default import resolve_workspace
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("command", choices=("show",))
    ap.parse_args(argv)
    print(json.dumps(read(resolve_workspace()), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
