"""Atomic publication of a confirmed core launch observation shared by CLI adapters."""
import argparse
import json
import math
from pathlib import Path

from result_publish import publish_text


def publish(workspace, runtime, session, started_at):
    if runtime not in ("claude", "codex"):
        raise ValueError("unsupported core runtime")
    if not isinstance(session, str) or not 1 <= len(session) <= 200 or any(
        ord(c) < 32 or ord(c) == 127 for c in session
    ):
        raise ValueError("invalid core session")
    if not math.isfinite(started_at) or started_at <= 0:
        raise ValueError("invalid session start time")
    record = {"runtime": runtime, "session": session, "started_at": started_at}
    return publish_text(Path(workspace) / "state/core-runtime.json",
                        json.dumps(record) + "\n", mode=0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("runtime", choices=("claude", "codex"))
    parser.add_argument("session")
    parser.add_argument("started_at", type=float)
    args = parser.parse_args()
    try:
        publish(args.workspace, args.runtime, args.session, args.started_at)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"core runtime record: {exc}\n")


if __name__ == "__main__":
    main()
