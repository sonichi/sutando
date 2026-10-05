#!/usr/bin/env python3
"""A worker's opt-in to owner-facing launch surfaces (currently: --chrome).

Every worker gets SURFACE_ARGS=() by design (launch-worker-session.sh) --
Chrome/remote-control are the canonical core's alone, because multiple pool
workers sharing one Chrome/CDP session would race for the same tabs/profile.
This is the explicit, per-instance opt-out of that default: a worker with an
entry here is a deliberate exception, never a change to the default for
workers in general.

State lives at <workspace>/state/workers/<instance-id>/surface-config.json --
alongside that worker's existing identity records (current.json,
sessions.json, incarnations.json) rather than a new top-level directory,
since it is the same per-worker-id scope those already use.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _config_path(workspace, instance_id: str) -> Path:
    return Path(workspace) / "state" / "workers" / instance_id / "surface-config.json"


def wants_chrome(workspace, instance_id: str) -> bool:
    """True only if the file exists AND explicitly says chrome: true. Any
    other state -- missing file, missing key, malformed JSON -- is False:
    the default (no browser tools) must survive every way this can fail to
    read, not just the expected one."""
    if not instance_id:
        return False
    p = _config_path(workspace, instance_id)
    try:
        return json.loads(p.read_text()).get("chrome") is True
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


def set_chrome(workspace, instance_id: str, enabled: bool) -> Path:
    p = _config_path(workspace, instance_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
    data["chrome"] = bool(enabled)
    p.write_text(json.dumps(data, indent=2))
    return p


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[0] == "wants-chrome":
        print("true" if wants_chrome(argv[1], argv[2]) else "false")
        return 0
    if len(argv) >= 4 and argv[0] == "set-chrome":
        p = set_chrome(argv[1], argv[2], argv[3].lower() in ("1", "true", "yes"))
        print(f"wrote {p}")
        return 0
    print(
        "usage: worker_surface_config.py wants-chrome <workspace> <instance-id>\n"
        "       worker_surface_config.py set-chrome <workspace> <instance-id> <true|false>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
