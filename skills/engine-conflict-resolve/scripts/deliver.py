#!/usr/bin/env python3
"""deliver.py — deterministic proposal delivery with a fail-safe fallback.

The desktop-app task carries no originating room, so the destination must be
DECLARED, never inferred: the room comes from --room > $ENGINE_CONFLICT_NOTIFY_ROOM
> the manifest config default (skills/MANIFEST.md precedence). This script never
reads activity state to guess a "last active" room — a merge proposal is
owner-only material and a guessed room may be shared.

No room configured, or the room post fails for ANY reason → the fallback always
runs: macOS notification (best-effort) + a question section inserted into the
per-host pending-questions.md ABOVE the '# Resolved' divider (via the shared
src/pending_questions_md.py locator — never a private regex).

stdout: {"status": "posted", ...} or {"status": "fallback", "reason": ...};
exit 0 for both (delivery happened on some channel), 1 only when even the
pending-questions write failed.
"""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import os
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import die, emit, manifest_config, skill_dir  # noqa: E402

ROOM_CONFIG_KEY = "ENGINE_CONFLICT_NOTIFY_ROOM"


def _repo_root() -> Optional[Path]:
    for p in Path(__file__).resolve().parents:
        if (p / "src" / "pending_questions_md.py").is_file():
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


def notify_macos(title: str) -> bool:
    try:
        proc = subprocess.run(
            ["osascript", "-e",
             f'display notification "{title}" with title "Sutando"'],
            capture_output=True, timeout=10)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def pending_questions_target(override: Optional[Path]) -> Path:
    if override:
        return override
    repo = _repo_root()
    if repo is None:
        die("cannot locate the repo (src/pending_questions_md.py) for the pending-questions fallback",
            reason="no-repo")
    if str(repo / "src") not in sys.path:
        sys.path.insert(0, str(repo / "src"))
    from util_paths import personal_path, _host_label  # noqa: E402
    p = personal_path("pending-questions.md")
    if p.exists():
        return p
    from workspace_default import resolve_workspace  # noqa: E402
    return resolve_workspace() / "hosts" / _host_label() / "pending-questions.md"


def write_pending_question(path: Path, title: str, body: str) -> None:
    """Insert the section ABOVE the '# Resolved' divider (shared locator)."""
    repo = _repo_root()
    if repo is not None and str(repo / "src") not in sys.path:
        sys.path.insert(0, str(repo / "src"))
    section = "## %s\n- asked: %s\n- source: engine-conflict-resolve\n\n%s\n\n" % (
        title, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), body.rstrip())
    text = path.read_text() if path.is_file() else ""
    insert_at = len(text)
    try:
        import pending_questions_md as pq
        m = pq.DIVIDER_RE.search(pq.mask_markup(text))
        if m:
            insert_at = m.start()
    except Exception:
        pass  # divider location is best-effort; appending is still a valid file
    if insert_at == len(text) and text and not text.endswith("\n"):
        section = "\n" + section
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text[:insert_at] + section + text[insert_at:])
    os.replace(tmp, path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--message-file", required=True, type=Path,
                    help="file whose content is the proposal text to deliver")
    ap.add_argument("--title", default="Engine update conflict — proposal ready")
    ap.add_argument("--room", default=None,
                    help=f"owner-only room id (else ${ROOM_CONFIG_KEY} / manifest config)")
    ap.add_argument("--room-ops-dir", type=Path,
                    default=skill_dir().parent / "agent-room-ops")
    ap.add_argument("--pending-questions", type=Path, default=None,
                    help="override the per-host pending-questions.md target")
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

    target = pending_questions_target(args.pending_questions)
    try:
        write_pending_question(target, args.title, body)
    except OSError as e:
        die(f"fallback failed too — could not write {target}: {e}", reason="fallback-failed")
    notified = notify_macos(args.title)
    emit({"status": "fallback", "reason": reason,
          "pending_questions": str(target), "notified": notified})


if __name__ == "__main__":
    main()
