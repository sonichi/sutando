#!/usr/bin/env python3
"""Discovery for skill-declared Claude Code hooks (`hooks` in a skill manifest).

One owner: the installer registers what this returns and the health probe
verifies exactly that, so a drifted second copy cannot make them disagree.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path

RUNNERS = {".py": "python3", ".sh": "bash"}


def discover(repo_dir: Path) -> list[tuple[str, str, str]]:
    """(event, token, command) for every declared, present, enabled skill hook.

    Skips rather than raises on anything malformed: a broken manifest must not
    abort a whole install, and a hook declared but absent is not registrable.
    """
    out: list[tuple[str, str, str]] = []
    for manifest in sorted(Path(repo_dir).glob("skills/*/manifest.json")):
        try:
            data = json.loads(manifest.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get("enabled") is False:
            continue
        entries = data.get("hooks")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            event, command = entry.get("event"), entry.get("command")
            if not isinstance(event, str) or not isinstance(command, str):
                continue
            target = (manifest.parent / command).resolve()
            if not target.is_file():
                continue
            runner = RUNNERS.get(target.suffix, "bash")
            out.append((event, target.name, f"{runner} {shlex.quote(str(target))}"))
    return out


if __name__ == "__main__":
    import sys
    for row in discover(Path(sys.argv[1])):
        print("|".join(row))
