#!/usr/bin/env python3
"""Which tasks the task-watch Mod may name to a running turn: the human owner's, from a person's channel.

    python3 src/task_watch_owner.py --workspace DIR -- <task file>...

Prints one line per path, in order: `owner`, or an empty line for any other task, an unreadable or
oversized file, or a header that cannot be trusted. Every task still reaches the session at its turn's
end; this only decides which may interrupt a turn in progress, so anything in doubt answers no.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_task_protocol import canonical_access_tier  # noqa: E402
from task_envelope import attested_task_headers  # noqa: E402

# The channels a person types or speaks into. Machine writers (cron, health-check, the runtime API,
# connectors, workstream grouping) use other sources and keep the turn-end path.
HUMAN_SOURCES = frozenset({"ag2space", "discord", "telegram", "slack", "voice", "phone", "chat"})
# The envelope HMAC covers the whole file, so it is read whole; a larger file is not a mid-turn candidate.
MAX_BYTES = 262_144


def is_human_owner(text: str, workspace: Path) -> bool:
    """Owner tier, a human channel, and a sender that is not an agent, from headers a body cannot forge."""
    headers = attested_task_headers(text, workspace).headers
    if canonical_access_tier(headers.get("access_tier")) != "owner":
        return False
    if (headers.get("source") or "").strip().lower() not in HUMAN_SOURCES:
        return False
    return ".agent:" not in (headers.get("user_id") or "")


def _verdict(path: Path, workspace: Path) -> str:
    try:
        if path.stat().st_size > MAX_BYTES:
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        return "owner" if is_human_owner(text, workspace) else ""
    except Exception:
        return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="workspace root (its task-hmac key verifies envelopes)")
    ap.add_argument("paths", nargs="*", help="task files")
    args = ap.parse_args(argv)
    workspace = Path(args.workspace)
    for p in args.paths:
        print(_verdict(Path(p), workspace))
    return 0


if __name__ == "__main__":
    sys.exit(main())
