#!/usr/bin/env python3
"""core_account_health.py — the core's account health, for Settings and the profile tab to show.

`<workspace>/state/core-account-health.json`:

    {"state": "ok" | "signed_out" | "quota", "runtime": "codex" | "claude",
     "account": <email or null>, "resets_at": <epoch, quota only>, "since": <epoch>}

Written by the core's Codex seat (signed_out once a login is refused even after a restart; quota on a
usage limit; ok on the next completed turn or when the login on disk changes) and by the Claude Code
core's monitor (quota on a usage-limit screen; ok once the core is idle or working again), through the
core's quota notice. A worker never writes it. The file is rewritten only when what it says changes, so
`since` is when the state began. Never fatal to the writer: a record that cannot be kept is logged.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Callable, Optional

FILE = ("state", "core-account-health.json")
STATES = ("ok", "signed_out", "quota")
RUNTIMES = ("codex", "claude")


def path(workspace) -> Path:
    return Path(workspace).joinpath(*FILE)


def read(workspace) -> dict:
    try:
        rec = json.loads(path(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def mark(workspace, state: str, runtime: str, account: str = "", resets_at=None,
         now: Optional[float] = None, log: Callable[..., None] = print) -> bool:
    """Record `state`; True when the file changed. `resets_at` is kept only for quota."""
    if state not in STATES or runtime not in RUNTIMES:
        raise ValueError(f"state={state!r} runtime={runtime!r}")
    try:
        resets = float(resets_at) if state == "quota" and resets_at is not None else None
    except (TypeError, ValueError):
        resets = None
    old = read(workspace)
    rec = {"state": state, "runtime": runtime, "account": account or None}
    if resets is not None:
        rec["resets_at"] = resets
    if {k: old.get(k) for k in ("state", "runtime", "account", "resets_at")} == \
            {k: rec.get(k) for k in ("state", "runtime", "account", "resets_at")}:
        return False
    same_state = old.get("state") == state and old.get("runtime") == runtime
    rec["since"] = old.get("since") if same_state and old.get("since") else (time.time() if now is None else now)
    from local_record import write_whole
    try:
        write_whole(path(workspace), rec)
    except OSError as e:
        log("account health not kept:", type(e).__name__)
        return False
    return True


def mark_ok(workspace, runtime: str, account: str = "", **kw) -> bool:
    """Back to ok, keeping the account last named when this call names none."""
    old = read(workspace)
    if not account and old.get("runtime") == runtime:
        account = str(old.get("account") or "")
    return mark(workspace, "ok", runtime, account, **kw)


def codex_email(auth_file) -> str:
    """The email in a Codex `auth.json`'s id token, read only (its signature is not checked: this names
    the account for display, it authorizes nothing); '' when there is none."""
    try:
        tokens = json.loads(Path(auth_file).read_text(encoding="utf-8")).get("tokens") or {}
        raw = tokens.get("id_token")
        raw = raw.get("raw_jwt") if isinstance(raw, dict) else raw
        part = raw.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return str(claims.get("email") or "") if isinstance(claims, dict) else ""
    except (OSError, ValueError, AttributeError, IndexError, TypeError):
        return ""

