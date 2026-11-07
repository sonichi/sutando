#!/usr/bin/env python3
"""Run an installed skill capability without importing provider policy."""
from __future__ import annotations

import sys
from pathlib import Path
import json
import os
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parent))
from skill_hooks import resolve_hook_command  # noqa: E402  (one containment owner)


def _roots(repo_dir: Path) -> list[Path]:
    """The loader's two-directory scan (`skills/MANIFEST.md`): repo, then the
    optional private skills dir, whose personal skills may also declare one."""
    out = [Path(repo_dir) / "skills"]
    private = os.environ.get("SUTANDO_MEMORY_DIR") or os.environ.get("SUTANDO_PRIVATE_DIR")
    if private:
        out.append(Path(private) / "skills")
    return list(dict.fromkeys(root.resolve() for root in out))


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
                raise ValueError(f"unusable {key} provider in {manifest}")
            found.append((str(data.get("name") or manifest.parent.name), target))
    return found


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 4 and args[0] == "--check":
        try:
            hits = providers(Path(args[1]), args[2])
            if not hits:
                return 0
            if len(hits) != 1:
                raise ValueError(f"{len(hits)} providers declare {args[2]}; refusing to choose")
            result = subprocess.run([str(hits[0][1]), args[3]], timeout=10,
                                    capture_output=True, text=True, check=False)
            print(result.stdout.strip() or result.stderr.strip() or
                  f"routing prerequisite returned {result.returncode}")
            return 0 if result.returncode == 0 else 1
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            print(f"routing prerequisite failed: {exc}", file=sys.stderr)
            return 1
    print("usage: skill_manifest_capability.py --check <repo> <config-key> <workspace>",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
