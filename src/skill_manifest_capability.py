#!/usr/bin/env python3
"""Which installed skill provides a named capability? A generic lookup over
`skills/*/manifest.json` `config` blocks, so core asks for the capability and
never names a skill.

The declaring skill owns the path: a relative command is resolved inside that
skill's own directory (same containment rule as a declared hook), and anything
that escapes it, or that is not an executable regular file, is not a provider.
Two providers is ambiguity, not a choice: nothing is returned.

Precedence is `skills/MANIFEST.md`'s: an env override beats this, so callers
consult it only when their variable is unset.
"""
from __future__ import annotations

import sys
from pathlib import Path
import json
import os

sys.path.insert(0, str(Path(__file__).resolve().parent))
from skill_hooks import resolve_hook_command  # noqa: E402  (one containment owner)


def _roots(repo_dir: Path) -> list[Path]:
    """The loader's two-directory scan (`skills/MANIFEST.md`): repo, then the
    optional private skills dir, whose personal skills may also declare one."""
    out = [Path(repo_dir) / "skills"]
    for var in ("SUTANDO_MEMORY_DIR", "SUTANDO_PRIVATE_DIR"):
        d = os.environ.get(var)
        if d:
            out.append(Path(d) / "skills")
    return out


def providers(repo_dir: Path, key: str) -> list[tuple[str, Path]]:
    """(skill name, resolved executable) per enabled skill declaring `key`."""
    found: list[tuple[str, Path]] = []
    for root in _roots(repo_dir):
        for manifest in sorted(root.glob("*/manifest.json")):
            try:
                data = json.loads(manifest.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or data.get("enabled") is False:
                continue
            config = data.get("config")
            command = config.get(key) if isinstance(config, dict) else None
            if not isinstance(command, str) or not command:
                continue
            target = resolve_hook_command(manifest.parent, command)
            if target is None or not target.is_file() or not os.access(target, os.X_OK):
                continue
            found.append((str(data.get("name") or manifest.parent.name), target))
    return found


def resolve(repo_dir: Path, key: str) -> tuple[Path | None, str]:
    hits = providers(repo_dir, key)
    if not hits:
        return None, f"no installed skill provides {key}"
    if len(hits) > 1:
        return None, (f"{len(hits)} skills provide {key} "
                      f"({', '.join(n for n, _ in hits)}); refusing to choose")
    name, target = hits[0]
    return target, f"{key} provided by the {name} skill"


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: skill_manifest_capability.py <repo-dir> <config-key>",
              file=sys.stderr)
        return 2
    target, reason = resolve(Path(args[0]), args[1])
    if target is None:
        print(reason, file=sys.stderr)
        return 1
    print(str(target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
