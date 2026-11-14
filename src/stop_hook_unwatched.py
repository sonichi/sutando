#!/usr/bin/env python3
"""The Stop hook's consecutive-unwatched-turn-end counter, per runtime instance.

One writer: the file is keyed by `util_paths.instance_scope_key` (the same
`(agent_id, instance_id)` discriminator every per-instance path uses), written
by temp file + `os.replace`, and any failure to persist is an exit 1 with the
reason on stderr — the hook then fails OPEN, because a counter that cannot be
written can never reach the cap that ends the blocking.

    stop_hook_unwatched.py bump  --state <dir>   # prints the new count
    stop_hook_unwatched.py clear --state <dir>
    stop_hook_unwatched.py path  --state <dir>   # prints the counter path
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import util_paths  # noqa: E402

STEM = "stop-hook-unwatched"


def counter_path(state_dir) -> Path:
    key = util_paths.instance_scope_key(state_dir)
    suffix = f"-{key}" if key else ""
    return Path(state_dir) / f"{STEM}{suffix}"


def bump(state_dir) -> int:
    path = counter_path(state_dir)
    try:
        n = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        n = 0
    n += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{n}\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return n


def clear(state_dir) -> None:
    counter_path(state_dir).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3 or args[1] != "--state" or args[0] not in ("bump", "clear", "path"):
        print("usage: stop_hook_unwatched.py bump|clear|path --state <dir>", file=sys.stderr)
        return 2
    cmd, state_dir = args[0], args[2]
    try:
        if cmd == "bump":
            print(bump(state_dir))
        elif cmd == "clear":
            clear(state_dir)
        else:
            print(counter_path(state_dir))
    except Exception as e:  # noqa: BLE001 — the reason is the output; the hook fails open on it
        print(f"stop_hook_unwatched: {cmd} failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
