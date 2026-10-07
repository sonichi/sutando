#!/usr/bin/env python3
"""Seed ``<workspace>/hosts/<host>/crons.json`` once, when it is missing.

The single owner of the first-run seed policy, called by every runtime: the
``/schedule-crons`` skill (Claude) and the Codex core launcher. Source
precedence: the interim ``<workspace>/crons/<host>.json``, else the legacy
``skills/schedule-crons/crons.json``, else ``crons.example.json``. An existing
file is never touched.

``--first-install-only NAME`` (Codex) seeds only the named entries when the
source is an unactivated starter, and copies every other source whole. A
source is a starter only on durable install provenance, never on its content:

* ``crons.example.json`` itself; or
* a file this module's ``--install-starter`` wrote, proven by its sidecar
  marker ``<name>.installer-seed`` whose digest still equals the source bytes.

Content identity is not provenance: older releases told owners to copy the
template to the legacy path and register every entry from it, so an unchanged
template can be a live schedule. No marker, an unreadable marker or a digest
mismatch is therefore ambiguous, and an ambiguous source is copied whole.

``--install-starter`` is the one writer of the marked copy (``src/init.sh``
calls it). It publishes the marker first and the copy second, both atomically,
and withdraws the marker if it loses the copy to another writer: the copy never
exists without its marker, and a marker without a copy is inert because only a
file that exists is ever a source.

The marker is consumed by the first seed published from its source, on either
runtime. The seed is the only path from a legacy or interim file into
registration (``/schedule-crons`` registers from the per-host file, which only
the seed creates), so after that publish the source can no longer be proven
unactivated: a later re-seed, after the per-host file is deleted, copies it
whole. Registration itself therefore needs no marker hook.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO = SKILL_DIR.parents[1]
MARKER_SUFFIX = ".installer-seed"
MARKER_STATE = "installer-seeded-not-activated"


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


def marker_path(source: Path) -> Path:
    return source.with_name(source.name + MARKER_SUFFIX)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def is_marked_starter(source: Path, raw: bytes) -> bool:
    """True only when an installer marker exists and still matches ``raw`` exactly."""
    try:
        marker = json.loads(marker_path(source).read_text())
    except (OSError, ValueError):
        return False
    return (isinstance(marker, dict) and marker.get("state") == MARKER_STATE
            and marker.get("sha256") == _digest(raw))


def _publish(target: Path, content: bytes) -> bool:
    """Create ``target`` with ``content`` atomically; False if it already exists."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_bytes(content)
    try:
        # link() refuses an existing name, so a concurrent writer or the owner
        # cannot be clobbered between any exists() check and the publish.
        os.link(tmp, target)
    except FileExistsError:
        return False
    finally:
        tmp.unlink(missing_ok=True)
    return True


def install_starter(skill_dir: Path = SKILL_DIR) -> str:
    """Create the legacy ``crons.json`` from the example with its marker; never overwrites."""
    dest = skill_dir / "crons.json"
    if dest.exists():
        return "exists"
    raw = (skill_dir / "crons.example.json").read_bytes()
    marker = marker_path(dest)
    tmp = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"state": MARKER_STATE, "sha256": _digest(raw)}) + "\n")
    os.replace(tmp, marker)
    if _publish(dest, raw):
        return "installed"
    try:
        theirs = dest.read_bytes()
    except OSError:
        theirs = None
    if theirs != raw:
        marker.unlink(missing_ok=True)
    return "exists"


def seed_bytes(source: Path, example: Path, first_install_only: tuple[str, ...] | None) -> bytes:
    """The seeded content: ``source`` whole, unless it is a starter and a filter is given."""
    raw = source.read_bytes()  # read once: classify and publish the same bytes
    if not first_install_only:
        return raw
    if source != example and not is_marked_starter(source, raw):
        return raw
    try:
        entries = json.loads(raw)
    except ValueError:
        if source == example:
            raise
        return raw
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
    marker_path(source).unlink(missing_ok=True)
    return "seeded", target, source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace")
    parser.add_argument("--host-label")
    parser.add_argument("--first-install-only", action="append", metavar="NAME",
                        help="on a first install, seed only this entry (repeatable)")
    parser.add_argument("--install-starter", action="store_true",
                        help="create the marked legacy crons.json from the example, then exit")
    parser.add_argument("--skill-dir", help=argparse.SUPPRESS)
    args = parser.parse_args()
    skill_dir = Path(args.skill_dir) if args.skill_dir else SKILL_DIR
    try:
        if args.install_starter:
            print(f"{install_starter(skill_dir)} {skill_dir / 'crons.json'}")
            return 0
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
