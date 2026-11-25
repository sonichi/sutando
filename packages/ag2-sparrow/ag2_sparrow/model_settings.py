"""The model a connector runs on, as the app's settings page reads and sets it: the same
`state/model.json` the model card keeps (antigravity_connector, model_switch), so a pick made on the
page is a pick made on the card. Only the owner's own machine runs this (the `model` verb of an
adapter's CLI); the card's rules about who may pick are the DM's, not this file's.

    {"current": id | null, "available": [{"id", "name"}], "pending": id | null, "refused": id | null}

`current` is what the connector last saw its runtime run on (it alone writes it: from the runtime's
own report, or once a turn ran on a pick), never the page's pick. `pending` is a pick no turn has
applied yet: the connector runs every next turn on it, whoever sent it, then settles it (`current` on a
switch; dropped, and named in `refused`, when the runtime would not take it). A page pick names no
room (`asked_in` is ''), so its outcome is not posted as a card in any DM: the page reads it here.
"""
from __future__ import annotations

import json
import os

from .model_switch import FILE


def _read(state_dir) -> dict:
    try:
        with open(os.path.join(str(state_dir), FILE), encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _offered(state: dict, allowed=None) -> list:
    """What may be picked: what the runtime last listed (within `allowed`, when the connector has an
    allowlist); before it has listed any, the allowlist itself, each id named as itself."""
    listed = [{"id": str(m["id"]), "name": str(m.get("name") or m["id"])}
              for m in state.get("available") or [] if isinstance(m, dict) and m.get("id")
              and (allowed is None or m["id"] in allowed)]
    return listed or [{"id": str(m), "name": str(m)} for m in allowed or () if m]


def pending(state: dict) -> str:
    wanted = str(state.get("wanted") or "")
    return wanted if wanted and state.get("told") != wanted else ""


def status(state_dir, allowed=None) -> dict:
    state = _read(state_dir)
    current, told = str(state.get("current") or ""), str(state.get("told") or "")
    refused = told if told and told != current and not state.get("wanted") else ""
    return {"current": current or None, "available": _offered(state, allowed), "pending": pending(state) or None,
            "refused": refused or None}


def pick(state_dir, model_id: str, allowed=None) -> "tuple[dict, str]":
    """(status, '') once `model_id` is the pick the next turn runs on, else (status, why not). Picking the
    model in use, or the one already pending, changes nothing: as on the card."""
    state = _read(state_dir)
    model_id = (model_id or "").strip()
    if model_id not in [m["id"] for m in _offered(state, allowed)]:
        return status(state_dir, allowed), "%r is not one of this agent's models" % model_id
    if model_id != str(state.get("wanted") or state.get("current") or ""):
        state.update(wanted=model_id, asked_in="", told="")
        path = os.path.join(str(state_dir), FILE)
        os.makedirs(str(state_dir), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(state, f, sort_keys=True)
        os.replace(path + ".tmp", path)
    return status(state_dir, allowed), ""


def command(state_dir, model_id: "str | None", allowed=None, out=print) -> int:
    """The adapter CLI's `model [--set <id>]`: one JSON line, exit 0, or 1 with `error` when refused."""
    if not model_id:
        out(json.dumps(status(state_dir, allowed)))
        return 0
    shown, problem = pick(state_dir, model_id, allowed)
    out(json.dumps(dict(shown, **({"error": problem} if problem else {}))))
    return 1 if problem else 0
