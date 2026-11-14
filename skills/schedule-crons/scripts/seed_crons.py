#!/usr/bin/env python3
"""Seed ``<workspace>/hosts/<host>/crons.json`` when it is missing, without enabling a job
the owner never opted into.

The rule, in order:

1. the per-host file exists: do nothing, except record that it existed
   (``hosts/<host>/.crons-seeded``);
2. it existed once (that marker is present): seed the starter set only. A copy kept anywhere
   else is older than what the owner had, so it is never reused;
3. the interim ``<workspace>/crons/<host>.json`` exists: it is this host's own workspace state
   from the pre-``hosts/`` layout, so it is published whole;
4. otherwise seed the starter set: ``crons.example.json`` without its ``"opt_in": true``
   entries (the morning briefing, the sample digest), which start only on the owner's word.

The in-checkout ``skills/schedule-crons/crons.json`` is never published. It is git-ignored and
older releases' installer filled it from the example, so its jobs prove no opt-in. Its entries
that are not a verbatim shipped example entry may be an owner's own schedule: the file is
copied once to ``hosts/<host>/crons.legacy.json`` and their names are printed on a
``not-enabled`` line, so the agent can ask the owner. Nothing from it is registered.

The per-host file is published with temp file + ``os.link``, which refuses an existing name,
so a concurrent seeder or the owner is never clobbered.
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
MARKER = ".crons-seeded"
LEGACY_KEPT = "crons.legacy.json"


def _config(key: str) -> str:
    return subprocess.run(
        ["bash", str(REPO / "scripts" / "sutando-config.sh"), key],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _entries(raw: bytes, source: Path) -> list:
    entries = json.loads(raw)
    if not isinstance(entries, list):
        raise ValueError(f"{source} is not a JSON list of cron entries")
    return entries


def _publish(target: Path, content: bytes) -> bool:
    """Create ``target`` with ``content`` atomically; False if it already exists."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_bytes(content)
    try:
        os.link(tmp, target)
        return True
    except FileExistsError:
        return False
    finally:
        tmp.unlink(missing_ok=True)


def starter_entries(example: list) -> list:
    return [e for e in example if isinstance(e, dict) and e.get("opt_in") is not True]


def legacy_not_enabled(legacy: list, published: list, example: list) -> list[str]:
    """Names in the legacy file that are neither published nor a verbatim shipped example entry."""
    live = {e.get("name") for e in published if isinstance(e, dict)}
    return [
        e["name"] for e in legacy
        if isinstance(e, dict) and isinstance(e.get("name"), str)
        and e["name"] not in live and e not in example
    ]


def seed(workspace: Path, host_label: str, skill_dir: Path = SKILL_DIR):
    """Return (status, target, source, not_enabled, kept). Never overwrites ``target``."""
    if not host_label.strip():
        raise ValueError("host label did not resolve")
    host_dir = workspace / "hosts" / host_label
    target = host_dir / "crons.json"
    marker = host_dir / MARKER
    if target.exists():
        if not marker.exists():
            marker.touch()
        return "exists", target, None, [], None

    example_path = skill_dir / "crons.example.json"
    example = _entries(example_path.read_bytes(), example_path)
    interim = workspace / "crons" / f"{host_label}.json"
    if not marker.exists() and interim.is_file():
        source, content = interim, interim.read_bytes()
        published = _entries(content, interim)
    else:
        published = starter_entries(example)
        source, content = example_path, (json.dumps(published, indent=2) + "\n").encode()

    if not _publish(target, content):
        return "exists", target, None, [], None
    marker.touch()

    not_enabled: list[str] = []
    kept = None
    legacy_path = skill_dir / "crons.json"
    if legacy_path.is_file():
        raw = legacy_path.read_bytes()
        try:
            not_enabled = legacy_not_enabled(_entries(raw, legacy_path), published, example)
        except ValueError:
            not_enabled = []
        if not_enabled:
            kept = host_dir / LEGACY_KEPT
            _publish(kept, raw)
    return "seeded", target, source, not_enabled, kept


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace")
    parser.add_argument("--host-label")
    parser.add_argument("--skill-dir", help=argparse.SUPPRESS)
    args = parser.parse_args()
    skill_dir = Path(args.skill_dir) if args.skill_dir else SKILL_DIR
    try:
        raw_ws = (args.workspace or "").strip() or _config("workspace")
        if not raw_ws:
            raise ValueError("workspace did not resolve")
        host_label = (args.host_label or "").strip() or _config("host-label")
        status, target, source, not_enabled, kept = seed(Path(raw_ws), host_label, skill_dir)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"seed-crons: {exc}", file=sys.stderr)
        return 1
    print(f"{status} {target}" + (f" from {source}" if source else ""))
    if not_enabled:
        print(f"not-enabled {','.join(not_enabled)} kept at {kept}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
