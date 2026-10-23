#!/usr/bin/env python3
"""deliver.py — deterministic proposal delivery with a fail-safe fallback.

The desktop-app task carries no originating room, so the destination must be
DECLARED, never inferred: the room comes from --room > $ENGINE_CONFLICT_NOTIFY_ROOM
> the manifest config default (skills/MANIFEST.md precedence). This script never
reads activity state to guess a "last active" room — a merge proposal is
owner-only material and a guessed room may be shared.

No room configured, or the room post fails for ANY reason → the fallback always
runs: the proposal is asked of the owner through scripts/ask-owner.py (the room
database, else the workspace outbox; it queues the owner's DM and the macOS
notification itself).

stdout: {"status": "posted", ...} or {"status": "fallback", "reason": ...};
exit 0 for both (delivery happened on some channel), 1 only when even ask-owner
could not run.
"""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import os
import sys
from pathlib import Path
from typing import Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import die, emit, manifest_config, skill_dir  # noqa: E402

ROOM_CONFIG_KEY = "ENGINE_CONFLICT_NOTIFY_ROOM"


def _repo_root() -> Optional[Path]:
    for p in Path(__file__).resolve().parents:
        if (p / "scripts" / "ask-owner.py").is_file():
            return p
    return None


def resolve_room(cli_room: Optional[str]) -> Optional[str]:
    """--room > declared env > manifest default. NEVER inferred from activity."""
    return cli_room or os.environ.get(ROOM_CONFIG_KEY) or manifest_config(ROOM_CONFIG_KEY) or None


def post_via_room_ops(room_ops_dir: Path, room_id: str, body: str) -> Tuple[bool, Optional[str]]:
    """Room posting delegates to the agent-room-ops gateway module (it owns the
    /v1 credential + HTTP contract). Absent or failing → (False, reason)."""
    gw = room_ops_dir / "_gateway.py"
    if not gw.is_file():
        return False, "room-ops-unavailable"
    try:
        spec = importlib.util.spec_from_file_location("ecr_gateway", gw)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        if str(room_ops_dir) not in sys.path:
            sys.path.insert(0, str(room_ops_dir))
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        base, headers = mod.gateway()
        if not base:
            return False, "no-gateway-configured"
        status, _parsed = mod.http_json(
            "POST", f"{base}/v1/room", headers,
            {"op": "message", "room_id": room_id, "body": body})
        if 200 <= int(status) < 300:
            return True, None
        return False, f"http-{status}"
    except Exception as e:  # any failure MUST reach the fallback, never crash
        return False, f"post-failed: {e.__class__.__name__}: {e}"


def ask_owner(title: str, body: str, workspace: Optional[Path]) -> dict:
    """The fallback: core's ask-owner records the question and queues the owner's DM.
    Raises OSError only when it could not run at all; a non-zero exit is reported."""
    repo = _repo_root()
    if repo is None:
        raise OSError("cannot locate the repo (scripts/ask-owner.py)")
    argv = [sys.executable, str(repo / "scripts" / "ask-owner.py"), title,
            "--context", body.rstrip(), "--urgency", "live"]
    if workspace:
        argv += ["--workspace", str(workspace)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        raise OSError(f"ask-owner did not run: {e.__class__.__name__}: {e}") from e
    if proc.returncode != 0:
        raise OSError(f"ask-owner exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[-300:]}")
    return {"ask": proc.stdout.strip().splitlines()}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--message-file", required=True, type=Path,
                    help="file whose content is the proposal text to deliver")
    ap.add_argument("--title", default="Engine update conflict — proposal ready")
    ap.add_argument("--room", default=None,
                    help=f"owner-only room id (else ${ROOM_CONFIG_KEY} / manifest config)")
    ap.add_argument("--room-ops-dir", type=Path,
                    default=skill_dir().parent / "agent-room-ops")
    ap.add_argument("--workspace", type=Path, default=None,
                    help="workspace for the fallback's ask-owner record (default: the resolved one)")
    args = ap.parse_args()

    try:
        body = args.message_file.read_text()
    except OSError as e:
        die(f"cannot read --message-file: {e}", reason="message-unreadable")

    room = resolve_room(args.room)
    reason = "no-room"
    if room:
        ok, fail = post_via_room_ops(args.room_ops_dir, room, body)
        if ok:
            emit({"status": "posted", "room": room, "via": "agent-room-ops gateway"})
        reason = fail or "post-failed"

    try:
        asked = ask_owner(args.title, body, args.workspace)
    except OSError as e:
        die(f"fallback failed too — {e}", reason="fallback-failed")
    emit({"status": "fallback", "reason": reason, **asked})


if __name__ == "__main__":
    main()
