#!/usr/bin/env python3
"""Seed ``<workspace>/hosts/<host>/crons.json`` once, when it is missing.

The single owner of the first-run seed policy, called by every runtime: the
``/schedule-crons`` skill (Claude) and the Codex core launcher. Source
precedence: the interim ``<workspace>/crons/<host>.json``, else the legacy
``skills/schedule-crons/crons.json``, else ``crons.example.json``. An existing
file is never touched.
"""

from __future__ import annotations

import argparse
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


def seed(workspace: Path, host_label: str, skill_dir: Path = SKILL_DIR) -> tuple[str, Path, Path | None]:
    """Return ("exists"|"seeded", target, source). Never overwrites ``target``."""
    if not host_label.strip():
        raise ValueError("host label did not resolve")
    target = workspace / "hosts" / host_label / "crons.json"
    if target.exists():
        return "exists", target, None
    source = next((p for p in seed_sources(workspace, host_label, skill_dir) if p.is_file()), None)
    if source is None:
        raise FileNotFoundError(f"no crons seed source under {workspace} or {skill_dir}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_bytes(source.read_bytes())
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
    args = parser.parse_args()
    try:
        raw_ws = (args.workspace or "").strip() or _config("workspace")
        if not raw_ws:
            raise ValueError("workspace did not resolve")
        workspace = Path(raw_ws).resolve()
        host_label = (args.host_label or "").strip() or _config("host-label")
        status, target, source = seed(workspace, host_label)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"seed-crons: {exc}", file=sys.stderr)
        return 1
    print(f"{status} {target}" + (f" from {source}" if source else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
