#!/usr/bin/env python3
"""Seed ``<workspace>/hosts/<host>/crons.json`` once, when it is missing.

The single owner of the first-run seed policy, called by every runtime: the
``/schedule-crons`` skill (Claude) and the Codex core launcher. Source
precedence: the interim ``<workspace>/crons/<host>.json``, else the legacy
``skills/schedule-crons/crons.json``, else ``crons.example.json``. An existing
file is never touched.

``--first-install-only NAME`` (Codex) seeds only the named entries when the
source is the shipped starter: ``crons.example.json`` itself, or an interim or
legacy file whose JSON equals it (``src/init.sh`` copies the example into the
legacy path). Any other source is an established schedule and is copied whole.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO = SKILL_DIR.parents[1]


def _config(key: str) -> str:
    return subprocess.run(
        ["bash", str(REPO / "scripts" / "sutando-config.sh"), key],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def seed_sources(workspace: Path, host_label: str, skill_dir: Path = SKILL_DIR) -> list[Path]:
    return [
        workspace / "crons" / f"{host_label}.json",
        skill_dir / "crons.json",
        skill_dir / "crons.example.json",
    ]


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def seed_bytes(source: Path, example: Path, first_install_only: tuple[str, ...] | None) -> bytes:
    """The seeded content: ``source`` whole, unless it is the starter and a filter is given."""
    raw = source.read_bytes()
    if not first_install_only:
        return raw
    if source != example:
        starter = _load(example)
        if starter is None or _load(source) != starter:
            return raw
    entries = json.loads(raw)
    if not isinstance(entries, list):
        raise ValueError(f"{source} is not a JSON list of cron entries")
    keep = [e for e in entries if isinstance(e, dict) and e.get("name") in first_install_only]
    return (json.dumps(keep, indent=2) + "\n").encode()


def seed(workspace: Path, host_label: str, skill_dir: Path = SKILL_DIR,
         first_install_only: tuple[str, ...] | None = None) -> tuple[str, Path, Path | None]:
    """Return ("exists"|"seeded", target, source). Never overwrites ``target``."""
    if not host_label.strip():
        raise ValueError("host label did not resolve")
    target = workspace / "hosts" / host_label / "crons.json"
    if target.exists():
        return "exists", target, None
    source = next((p for p in seed_sources(workspace, host_label, skill_dir) if p.is_file()), None)
    if source is None:
        raise FileNotFoundError(f"no crons seed source under {workspace} or {skill_dir}")
    content = seed_bytes(source, skill_dir / "crons.example.json", first_install_only)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_bytes(content)
    try:
        # link() refuses an existing name, so a concurrent seeder or the owner
        # cannot be clobbered between the exists() check and the publish.
        os.link(tmp, target)
    except FileExistsError:
        return "exists", target, None
    finally:
        tmp.unlink(missing_ok=True)
    return "seeded", target, source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace")
    parser.add_argument("--host-label")
    parser.add_argument("--first-install-only", action="append", metavar="NAME",
                        help="on a first install, seed only this entry (repeatable)")
    args = parser.parse_args()
    try:
        raw_ws = (args.workspace or "").strip() or _config("workspace")
        if not raw_ws:
            raise ValueError("workspace did not resolve")
        workspace = Path(raw_ws).resolve()
        host_label = (args.host_label or "").strip() or _config("host-label")
        status, target, source = seed(
            workspace, host_label,
            first_install_only=tuple(args.first_install_only) if args.first_install_only else None)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"seed-crons: {exc}", file=sys.stderr)
        return 1
    print(f"{status} {target}" + (f" from {source}" if source else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
