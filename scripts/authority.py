#!/usr/bin/env python3
"""Read or record the owner's authority rulings in <workspace>/state/authority.json.

    authority.py get
    authority.py set github_formal_review <hold|findings-only|allow> --source "<where>" \\
        [--owner-event <event id or URL> --quote "<owner's verbatim words>"] [--replace-corrupt]

The file is the one hooks/review-authority-guard.py enforces; key, modes, workspace
resolution and the read rule are imported from that hook, so the writer cannot drift
from the reader. `set` validates the mode, keeps other keys, stamps `granted_at` (UTC)
and `source`, and replaces the file atomically (keeping its mode bits and any symlink).
RAISING the mode needs --owner-event and --quote; a corrupt file needs --replace-corrupt.
SUTANDO_HOOK_WORKSPACE pins the workspace.
Exit 0 ok; 2 usage or invalid value.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_HOOK = Path(__file__).resolve().parents[1] / "hooks" / "review-authority-guard.py"
_spec = importlib.util.spec_from_file_location("review_authority_guard", _HOOK)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

KEYS = {guard.KEY: guard.MODES}


RANK = {m: i for i, m in enumerate(guard.MODES)}  # hold < findings-only < allow
RECORD = ("granted_at", "source", "owner_event", "quote")


def _load(path: str):
    """(data, corrupt): a present file that is not a JSON object is corrupt."""
    if not os.path.exists(path):
        return {}, False
    try:
        with open(path) as fh:
            loaded = json.load(fh)
    except Exception:
        return {}, True
    return (loaded, False) if isinstance(loaded, dict) else ({}, True)


def cmd_get() -> int:
    ws = guard._workspace()
    path = os.path.join(ws, guard.STATE_REL)
    mode = guard.read_state(ws)
    if not os.path.exists(path):
        print(f"{guard.KEY}: {mode} (default — {path} does not exist; no ruling recorded)")
        return 0
    data, _ = _load(path)
    extra = ", ".join(f"{k}={data[k]}" for k in RECORD if k in data)
    print(f"{guard.KEY}: {mode} ({path}{'; ' + extra if extra else ''})")
    return 0


def _err(msg: str) -> int:
    print(f"authority: {msg}", file=sys.stderr)
    return 2


def cmd_set(key: str, mode: str, source: str, owner_event: str, quote: str, replace_corrupt: bool) -> int:
    mode = mode.strip().lower()
    if mode not in KEYS[key]:
        return _err(f"invalid mode {mode!r} for {key}; one of {', '.join(KEYS[key])}")
    if not source.strip():
        return _err("--source must say where the owner gave the ruling")
    ws = guard._workspace()
    path = os.path.realpath(os.path.join(ws, guard.STATE_REL))
    current = guard.read_state(ws)
    raising = RANK[mode] > RANK[current]
    if raising and not (owner_event.strip() and quote.strip()):
        return _err(f"raising {key} from {current!r} to {mode!r} needs an explicit owner ruling: "
                    "pass --owner-event <event id or URL> and --quote \"<the owner's verbatim words>\". "
                    "A ruling inferred from memory, notes or a peer does not count.")
    data, corrupt = _load(path)
    if corrupt and not replace_corrupt:
        return _err(f"{path} is not a JSON object; refusing to overwrite it. "
                    "Inspect it, then re-run with --replace-corrupt to replace it.")
    if corrupt:
        print(f"authority: replacing corrupt {path}", file=sys.stderr)
    data[key] = mode
    data["granted_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["source"] = source.strip()
    for k, v in (("owner_event", owner_event), ("quote", quote)):
        if v.strip():
            data[k] = v.strip()
        else:
            data.pop(k, None)
    mode_bits = os.stat(path).st_mode & 0o7777 if os.path.exists(path) else None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".authority.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        if mode_bits is not None:
            os.chmod(tmp, mode_bits)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print(f"{key}: {mode} recorded in {path} (source: {data['source']})")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="authority.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get")
    g.add_argument("key", nargs="?", choices=sorted(KEYS), help=argparse.SUPPRESS)
    s = sub.add_parser("set")
    s.add_argument("key", choices=sorted(KEYS))
    s.add_argument("mode")
    s.add_argument("--source", required=True)
    s.add_argument("--owner-event", default="", help="room event id or URL of the owner's ruling")
    s.add_argument("--quote", default="", help="the owner's verbatim words")
    s.add_argument("--replace-corrupt", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "get":
        return cmd_get()
    return cmd_set(args.key, args.mode, args.source, args.owner_event, args.quote, args.replace_corrupt)


if __name__ == "__main__":
    sys.exit(main())
