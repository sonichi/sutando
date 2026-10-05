#!/usr/bin/env python3
"""Read or record the owner's authority rulings in <workspace>/state/authority.json.

    authority.py get [github_formal_review]
    authority.py set github_formal_review <hold|findings-only|allow> --source "<where the owner said it>"

The file is the one hooks/review-authority-guard.py enforces; key, modes, workspace
resolution and the read rule are imported from that hook, so the writer cannot drift
from the reader. `set` validates the mode, keeps other keys, stamps `granted_at` (UTC)
and `source`, and replaces the file atomically. SUTANDO_HOOK_WORKSPACE pins the workspace.
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


def cmd_get(key: str) -> int:
    ws = guard._workspace()
    path = os.path.join(ws, guard.STATE_REL)
    mode = guard.read_state(ws)
    if not os.path.exists(path):
        print(f"{key}: {mode} (default — {path} does not exist; no ruling recorded)")
        return 0
    try:
        with open(path) as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    extra = ", ".join(f"{k}={data[k]}" for k in ("granted_at", "source") if k in data)
    print(f"{key}: {mode} ({path}{'; ' + extra if extra else ''})")
    return 0


def cmd_set(key: str, mode: str, source: str) -> int:
    mode = mode.strip().lower()
    if mode not in KEYS[key]:
        print(f"authority: invalid mode {mode!r} for {key}; one of {', '.join(KEYS[key])}", file=sys.stderr)
        return 2
    if not source.strip():
        print("authority: --source must say where the owner gave the ruling", file=sys.stderr)
        return 2
    path = os.path.join(guard._workspace(), guard.STATE_REL)
    data = {}
    if os.path.exists(path):
        try:
            with open(path) as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            pass  # an unreadable file reads as 'hold'; the ruling being recorded supersedes it
    data[key] = mode
    data["granted_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["source"] = source.strip()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".authority.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    print(f"{key}: {mode} recorded in {path} (source: {data['source']})")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="authority.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get")
    g.add_argument("key", nargs="?", default=guard.KEY, choices=sorted(KEYS))
    s = sub.add_parser("set")
    s.add_argument("key", choices=sorted(KEYS))
    s.add_argument("mode")
    s.add_argument("--source", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "get":
        return cmd_get(args.key)
    return cmd_set(args.key, args.mode, args.source)


if __name__ == "__main__":
    sys.exit(main())
