#!/usr/bin/env python3
"""Pre-trust the Codex core's working directory in $CODEX_HOME/config.toml.

Codex asks "Trust this folder?" on first launch in a directory and blocks the
detached core until someone answers. This writes the same record the "Trust and
continue" answer writes, so the launcher never parks the core at that dialog.

An existing entry for the directory is left as it is: an explicit decision
already recorded there is the owner's to keep.

Usage: trust-seed.py <config.toml> <dir>   (exit 0 always; one status line on stdout)
"""
from __future__ import annotations

import os
import sys
import tomllib


def _toml_key(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def seed(config_path: str, directory: str) -> str:
    """Return "added", "present" or "skipped: <reason>"."""
    text = ""
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                text = f.read()
            projects = tomllib.loads(text).get("projects")
        except (OSError, tomllib.TOMLDecodeError) as exc:
            return f"skipped: unreadable config ({exc.__class__.__name__})"
        if isinstance(projects, dict) and directory in projects:
            return "present"
    block = f"[projects.{_toml_key(directory)}]\ntrust_level = \"trusted\"\n"
    sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    try:
        os.makedirs(os.path.dirname(config_path) or ".", exist_ok=True)
        with open(config_path, "a", encoding="utf-8") as f:
            f.write(sep + block)
    except OSError as exc:
        return f"skipped: not writable ({exc.__class__.__name__})"
    return "added"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("skipped: usage trust-seed.py <config.toml> <dir>")
        return 0
    print(seed(argv[1], argv[2]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
