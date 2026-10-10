#!/usr/bin/env python3
"""Seed ``<workspace>/hosts/<host>/crons.json`` once, when it is missing.

The single owner of the first-run seed policy, called by every runtime: the
``/schedule-crons`` skill (Claude) and the Codex core launcher. The rule:

1. the canonical per-host file exists: do nothing;
2. else an interim ``<workspace>/crons/<host>.json`` or a legacy
   ``skills/schedule-crons/crons.json`` exists (in that order): publish it whole;
3. else this is a fresh install: publish ``crons.example.json``, reduced to the
   ``--first-install-only`` entries when that flag is given (Codex passes
   ``main-loop``).

A legacy or interim file is never filtered. Nothing installs either one any
more, so every such file was written by an older release or by the owner, and
older releases told owners to copy the template there and register every entry
from it: an unchanged template can be a live schedule, so content never proves
a starter.

The canonical file is published in one step, temp file + ``os.link``: ``link``
refuses an existing name, so a concurrent seeder or the owner is never
clobbered, and a process killed at any point leaves either no canonical file or
a complete one. The source is read once, so the bytes classified are the bytes
published.
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


def _publish(target: Path, content: bytes) -> bool:
    """Create ``target`` with ``content`` atomically; False if it already exists."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_bytes(content)
    try:
        # link() refuses an existing name, so a concurrent writer or the owner
        # cannot be clobbered between any exists() check and the publish.
        os.link(tmp, target)
        return True
    except FileExistsError:
        return False
    finally:
        tmp.unlink(missing_ok=True)


def seed_bytes(source: Path, example: Path, first_install_only: tuple[str, ...] | None) -> bytes:
    """The seeded content: ``source`` whole, unless it is the example and a filter is given."""
    raw = source.read_bytes()  # read once: classify and publish the same bytes
    if not first_install_only or source != example:
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
    if not _publish(target, content):
        return "exists", target, None
    return "seeded", target, source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace")
    parser.add_argument("--host-label")
    parser.add_argument("--first-install-only", action="append", metavar="NAME",
                        help="on a fresh install, seed only this entry of the example (repeatable)")
    parser.add_argument("--skill-dir", help=argparse.SUPPRESS)
    args = parser.parse_args()
    skill_dir = Path(args.skill_dir) if args.skill_dir else SKILL_DIR
    try:
        raw_ws = (args.workspace or "").strip() or _config("workspace")
        if not raw_ws:
            raise ValueError("workspace did not resolve")
        workspace = Path(raw_ws).resolve()
        host_label = (args.host_label or "").strip() or _config("host-label")
        status, target, source = seed(
            workspace, host_label, skill_dir,
            first_install_only=tuple(args.first_install_only) if args.first_install_only else None)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"seed-crons: {exc}", file=sys.stderr)
        return 1
    print(f"{status} {target}" + (f" from {source}" if source else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
