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
* the exact file object this module's ``--install-starter`` published, proven
  by the per-host marker ``<workspace>/hosts/<host>/state/crons-installer-seed.json``
  recording that object's identity (``st_dev``, ``st_ino`` and, where the
  platform has one, ``st_birthtime``) and the sha256 of its bytes.

Content identity is not provenance: older releases told owners to copy the
template to the legacy path and register every entry from it, so an unchanged
template can be a live schedule. No marker, an unreadable marker, or any
identity or digest mismatch is ambiguous, and an ambiguous source is copied
whole. An equal-byte file that replaced the installer's copy is a different
object and is therefore ambiguous; birth time keeps a later file that happens
to reuse the inode number from inheriting the record.

``--install-starter`` is the one writer of the marked copy (``src/init.sh``
calls it). It publishes the copy first with temp + ``os.link`` (which never
clobbers) and records the marker only when its own link won, atomically
(temp + ``os.replace``). Every failure path ends with no marker: a stale one is
removed before publishing, a lost race returns before any marker is written, and
a failed marker write removes the copy it just published (that exact inode
only), so an unmarked installer copy is never left to read as a live schedule.

Installation and seeding hold one per-host workspace lock
(``<workspace>/state/locks/crons-seed.<host>.lock``) from the existence check
through publish, marker write, marker validation and consumption, so a seed
never observes a fresh installer copy before its marker exists. The marker and
lock live in Workspace, not beside the copy: the engine tree is replaced on
update and must hold no mutable state.

The marker is consumed by the first seed published from the legacy copy, on
either runtime. The seed is the only path from a legacy or interim file into
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
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO = SKILL_DIR.parents[1]
sys.path.insert(0, str(REPO / "src"))
from file_lock import locked_file  # noqa: E402

MARKER_NAME = "crons-installer-seed.json"
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


def _host_dir(workspace: Path, host_label: str) -> Path:
    if not host_label.strip():
        raise ValueError("host label did not resolve")
    return workspace / "hosts" / host_label


def marker_path(workspace: Path, host_label: str) -> Path:
    return _host_dir(workspace, host_label) / "state" / MARKER_NAME


@contextmanager
def seed_lock(workspace: Path, host_label: str) -> Iterator[None]:
    """The one per-host lock serializing installation and seeding."""
    _host_dir(workspace, host_label)
    with locked_file(workspace / "state" / "locks" / f"crons-seed.{host_label}.lock"):
        yield


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _identity(st: os.stat_result) -> dict:
    return {"dev": st.st_dev, "ino": st.st_ino, "birthtime": getattr(st, "st_birthtime", None)}


def is_marked_starter(marker: Path, st: os.stat_result, raw: bytes) -> bool:
    """True only when the marker names this exact file object and these exact bytes."""
    try:
        record = json.loads(marker.read_text())
    except (OSError, ValueError):
        return False
    return (isinstance(record, dict) and record.get("state") == MARKER_STATE
            and all(record.get(k, object()) == v for k, v in _identity(st).items())
            and record.get("sha256") == _digest(raw))


def _publish(target: Path, content: bytes) -> os.stat_result | None:
    """Create ``target`` with ``content`` atomically; its stat, or None if it already exists."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_bytes(content)
    try:
        # link() refuses an existing name, so a concurrent writer or the owner
        # cannot be clobbered between any exists() check and the publish.
        os.link(tmp, target)
        return os.stat(tmp)  # the linked inode, read via the name only this writer uses
    except FileExistsError:
        return None
    finally:
        tmp.unlink(missing_ok=True)


def _write_marker(marker: Path, st: os.stat_result, raw: bytes) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    tmp = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps({"state": MARKER_STATE, **_identity(st), "sha256": _digest(raw)}) + "\n")
        os.replace(tmp, marker)
    finally:
        tmp.unlink(missing_ok=True)


def _unlink_if_same(path: Path, st: os.stat_result) -> None:
    """Remove ``path`` only while it is still the object ``st`` describes."""
    try:
        now = os.lstat(path)
    except FileNotFoundError:
        return
    if (now.st_dev, now.st_ino) == (st.st_dev, st.st_ino):
        path.unlink()


def install_starter(workspace: Path, host_label: str, skill_dir: Path = SKILL_DIR) -> str:
    """Create the legacy ``crons.json`` from the example and mark it; never overwrites."""
    dest = skill_dir / "crons.json"
    marker = marker_path(workspace, host_label)
    with seed_lock(workspace, host_label):
        if dest.exists():
            return "exists"
        raw = (skill_dir / "crons.example.json").read_bytes()
        marker.unlink(missing_ok=True)  # a record from an earlier attempt vouches for nothing
        st = _publish(dest, raw)
        if st is None:
            return "exists"  # an unknown writer won; its file is never marked
        try:
            _write_marker(marker, st, raw)
        except BaseException:
            _unlink_if_same(dest, st)
            raise
        return "installed"


def _read_source(source: Path) -> tuple[os.stat_result, bytes]:
    with source.open("rb") as handle:  # one open: the identity and the bytes are the same object's
        return os.fstat(handle.fileno()), handle.read()


def seed_bytes(source: Path, example: Path, first_install_only: tuple[str, ...] | None,
               marker: Path | None = None) -> bytes:
    """The seeded content: ``source`` whole, unless it is a starter and a filter is given."""
    st, raw = _read_source(source)  # read once: classify and publish the same bytes
    if not first_install_only:
        return raw
    if source != example and not (marker and is_marked_starter(marker, st, raw)):
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
    target = _host_dir(workspace, host_label) / "crons.json"
    marker = marker_path(workspace, host_label)
    with seed_lock(workspace, host_label):
        if target.exists():
            return "exists", target, None
        source = next((p for p in seed_sources(workspace, host_label, skill_dir) if p.is_file()), None)
        if source is None:
            raise FileNotFoundError(f"no crons seed source under {workspace} or {skill_dir}")
        content = seed_bytes(source, skill_dir / "crons.example.json", first_install_only, marker)
        if _publish(target, content) is None:
            return "exists", target, None
        if source == skill_dir / "crons.json":
            marker.unlink(missing_ok=True)
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
        raw_ws = (args.workspace or "").strip() or _config("workspace")
        if not raw_ws:
            raise ValueError("workspace did not resolve")
        workspace = Path(raw_ws).resolve()
        host_label = (args.host_label or "").strip() or _config("host-label")
        if args.install_starter:
            print(f"{install_starter(workspace, host_label, skill_dir)} {skill_dir / 'crons.json'}")
            return 0
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
