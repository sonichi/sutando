#!/usr/bin/env python3
"""The Stop hook's same-queue repeat counter, per runtime instance.

How many consecutive turn ends the SAME unprocessed queue has blocked. The
signature is the caller's (the task names, in order); a different signature
starts the count over at one. Same file discipline as stop_hook_unwatched.py:
keyed by util_paths.instance_scope_key, temp file + os.replace, and a failure
to persist is an exit 1 with the reason on stderr, so the hook fails OPEN.

    stop_hook_repeat.py bump  --state <dir> --sig <text>   # prints the new count
    stop_hook_repeat.py clear --state <dir>
    stop_hook_repeat.py path  --state <dir>                # prints the counter path
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import util_paths  # noqa: E402

STEM = "stop-hook-repeat"


def counter_path(state_dir) -> Path:
    key = util_paths.instance_scope_key(state_dir)
    suffix = f"-{key}" if key else ""
    return Path(state_dir) / f"{STEM}{suffix}"


def _digest(sig: str) -> str:
    return hashlib.sha256(sig.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def bump(state_dir, sig: str) -> int:
    path = counter_path(state_dir)
    digest = _digest(sig)
    n = 0
    try:
        count, _, prev = path.read_text(encoding="utf-8").strip().partition(" ")
        if prev == digest:
            n = int(count)
    except (OSError, ValueError):
        n = 0
    n += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{n} {digest}\n")
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
    usage = "usage: stop_hook_repeat.py bump --state <dir> --sig <text> | clear|path --state <dir>"
    if len(args) < 3 or args[1] != "--state" or args[0] not in ("bump", "clear", "path"):
        print(usage, file=sys.stderr)
        return 2
    cmd, state_dir, rest = args[0], args[2], args[3:]
    if cmd == "bump" and (len(rest) != 2 or rest[0] != "--sig"):
        print(usage, file=sys.stderr)
        return 2
    if cmd != "bump" and rest:
        print(usage, file=sys.stderr)
        return 2
    try:
        if cmd == "bump":
            print(bump(state_dir, rest[1]))
        elif cmd == "clear":
            clear(state_dir)
        else:
            print(counter_path(state_dir))
    except Exception as e:  # noqa: BLE001 -- the reason is the output; the hook fails open on it
        print(f"stop_hook_repeat: {cmd} failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
