#!/usr/bin/env python3
"""Migrate stale SessionStop hooks -> SessionEnd in ~/.claude/settings.json.

SessionStop is not a valid Claude Code hook event — Claude Code silently
no-ops it ("Unknown hook event 'SessionStop' was ignored"). Any entry
under that key is dead regardless of the command it invokes, so the
migration is a universal key-rename: every SessionStop hook moves to
SessionEnd and the SessionStop key is dropped.

Dedup: hooks whose command already appears in SessionEnd are skipped (so
re-running the installer doesn't compound entries).

Idempotent: no-op when SessionStop is absent. Designed to be called on
every fresh-session bootstrap (catchup-after-startup.sh) so the rename
self-heals without requiring users to re-run install-hook.sh.

Usage:
  python3 migrate-settings-hooks.py [path/to/settings.json]

Default path is $HOME/.claude/settings.json.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _atomic_write(path: Path, content: str) -> None:
    """Write to a sibling tmp file then os.replace — same-fs atomic on POSIX.

    `~/.claude/settings.json` is read by every Claude Code session for hook
    config, allowed-tools, etc.; a half-written file breaks every subsequent
    shell. Crash-during-write becomes "tmp file may be left behind" instead
    of "settings.json corrupted." Mini's #1374 review catch.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def migrate(settings_path: Path) -> int:
    if not settings_path.exists():
        return 0
    try:
        s = json.loads(settings_path.read_text() or "{}")
    except json.JSONDecodeError as e:
        print(f"migrate-settings-hooks: skipped — {settings_path} is not valid JSON ({e})",
              file=sys.stderr)
        return 0
    if not isinstance(s, dict):
        return 0

    hooks = s.get("hooks")
    if not isinstance(hooks, dict):
        return 0

    had_key = "SessionStop" in hooks
    ss = hooks.pop("SessionStop", None)
    if not isinstance(ss, list) or not ss:
        # Was absent, null, empty, or wrong type. Persist the cleanup if
        # the key was present (so re-runs converge), but stay quiet — this
        # isn't a migration event.
        if had_key:
            s["hooks"] = hooks
            _atomic_write(settings_path, json.dumps(s, indent=2) + "\n")
        return 0

    # Ensure SessionEnd exists as a list (handles explicit null).
    if not isinstance(hooks.get("SessionEnd"), list):
        hooks["SessionEnd"] = []
    se = hooks["SessionEnd"]

    seen_cmds: set[str] = set()
    for g in se:
        if not isinstance(g, dict):
            continue
        for h in (g.get("hooks") or []):
            if isinstance(h, dict) and h.get("type") == "command":
                seen_cmds.add((h.get("command") or "").strip())

    moved = 0
    duplicates = 0
    for g in ss:
        if not isinstance(g, dict):
            continue
        new_hooks = []
        for h in (g.get("hooks") or []):
            if not isinstance(h, dict):
                continue
            cmd = (h.get("command") or "").strip() if h.get("type") == "command" else None
            if cmd is not None and cmd in seen_cmds:
                duplicates += 1
                continue
            new_hooks.append(h)
            if cmd is not None:
                seen_cmds.add(cmd)
            moved += 1
        if new_hooks:
            se.append({"hooks": new_hooks})

    s["hooks"] = hooks
    _atomic_write(settings_path, json.dumps(s, indent=2) + "\n")

    parts = [f"migrated {moved} SessionStop hook(s) -> SessionEnd"]
    if duplicates:
        parts.append(f"deduped {duplicates}")
    parts.append("SessionStop key removed")
    print("migrate-settings-hooks: " + "; ".join(parts))
    return moved


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else Path(os.path.expanduser("~/.claude/settings.json"))
    migrate(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
