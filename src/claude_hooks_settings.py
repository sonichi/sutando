#!/usr/bin/env python3
"""Sutando-owned hook entries in a project-level Claude Code settings.json: install one idempotently and prune dead copies of the same hook.

Two installers (``scripts/install-personal-claude-hook.sh``,
``scripts/install-session-start-hook.sh``) carried their own inline merge. Each
could add its entry but neither could remove one, so a test run from a copy of
the repo under a temp dir left the live settings pointing at three deleted
``/var/folders/…/repo/src/…`` scripts, and every compaction fired all four.

Pruning is scoped to the SAME FAMILY as the hook being installed — entries whose
command runs a script with the same basename — and only when that script no
longer exists. A user's own hooks, dead or alive, are never touched.

    python3 src/claude_hooks_settings.py install --settings <path> \\
        --event SessionStart --command 'bash "<repo>/src/x.sh"' \\
        [--matcher compact] [--prepend] [--label "x hook"]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Iterable, Optional

_INTERPRETERS = {"bash", "sh", "zsh", "python", "python3", "node"}


def script_path_of(command: str) -> Optional[str]:
    """The script a hook command runs: the first token after an interpreter, else the first token."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return None
    for index, token in enumerate(tokens):
        if os.path.basename(token) in _INTERPRETERS:
            return tokens[index + 1] if index + 1 < len(tokens) else None
        if not token.startswith("-"):
            return token
    return None


def family_of(command: str) -> str:
    path = script_path_of(command)
    return os.path.basename(path) if path else ""


def load(settings_path: Path) -> dict:
    if not settings_path.is_file():
        return {"hooks": {}}
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{settings_path}: top level is not an object")
    data.setdefault("hooks", {})
    return data


def save(settings_path: Path, settings: dict) -> None:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings_path.with_suffix(settings_path.suffix + ".tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, settings_path)


def prune_dead(settings: dict, event: str, family: str) -> list[str]:
    """Remove hooks under ``event`` that run a script named ``family`` which no longer exists.

    Returns the removed commands. Entries left with no hooks are dropped too.
    """
    if not family:
        return []
    removed: list[str] = []
    kept_entries = []
    for entry in settings.get("hooks", {}).get(event, []) or []:
        if not isinstance(entry, dict):
            kept_entries.append(entry)
            continue
        kept_hooks = []
        for hook in entry.get("hooks", []) or []:
            command = str(hook.get("command", "")) if isinstance(hook, dict) else ""
            path = script_path_of(command)
            if family_of(command) == family and path and not os.path.exists(path):
                removed.append(command)
                continue
            kept_hooks.append(hook)
        if kept_hooks:
            kept_entries.append(dict(entry, hooks=kept_hooks))
    if removed:
        settings.setdefault("hooks", {})[event] = kept_entries
    return removed


def install(
    settings_path: Path,
    *,
    event: str,
    command: str,
    matcher: str = "",
    prepend: bool = False,
) -> tuple[str, list[str]]:
    """Prune dead same-family entries, then add ``command`` once. Returns (status, removed)."""
    settings = load(settings_path)
    removed = prune_dead(settings, event, family_of(command))
    entries = settings.setdefault("hooks", {}).setdefault(event, [])
    present = any(
        isinstance(h, dict) and h.get("command", "") == command
        for entry in entries if isinstance(entry, dict)
        for h in entry.get("hooks", []) or []
    )
    if present:
        status = "already installed"
    else:
        new_entry = {"matcher": matcher, "hooks": [{"type": "command", "command": command}]}
        if prepend:
            entries.insert(0, new_entry)
        else:
            entries.append(new_entry)
        status = "installed"
    if status == "installed" or removed or not settings_path.is_file():
        save(settings_path, settings)
    return status, removed


def _describe_removed(removed: Iterable[str], family: str) -> str:
    items = list(removed)
    noun = "entry" if len(items) == 1 else "entries"
    paths = ", ".join(script_path_of(c) or c for c in items)
    return f"  ✂ removed {len(items)} dead {family} {noun}: {paths}"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install")
    p.add_argument("--settings", required=True)
    p.add_argument("--event", default="SessionStart")
    p.add_argument("--command", required=True)
    p.add_argument("--matcher", default="")
    p.add_argument("--prepend", action="store_true")
    p.add_argument("--label", default=None)
    args = parser.parse_args(argv)
    label = args.label or f"{family_of(args.command)} {args.event} hook"
    try:
        status, removed = install(
            Path(args.settings), event=args.event, command=args.command,
            matcher=args.matcher, prepend=args.prepend,
        )
    except (OSError, ValueError) as exc:
        print(f"  ✗ {label}: {exc}", file=sys.stderr)
        return 1
    if removed:
        print(_describe_removed(removed, family_of(args.command)))
    print(f"  ✓ {label} ({status})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
