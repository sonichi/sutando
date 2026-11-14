#!/usr/bin/env python3
"""Seed ``<workspace>/hosts/<host>/crons.json`` when it is missing, without enabling a job
the owner never opted into.

The rule, in order:

1. the per-host file exists: do nothing, except record that it existed
   (``hosts/<host>/.crons-seeded``);
2. it existed once (that marker is present): seed the starter set only, and print
   ``restored <path>`` so the owner learns their own jobs need adding back. A copy kept
   anywhere else is older than what the owner had, so it is never reused;
3. the interim ``<workspace>/crons/<host>.json`` exists: it is this host's own workspace state
   from the pre-``hosts/`` layout, so it is published whole;
4. otherwise seed the starter set: ``crons.example.json`` without its ``"opt_in": true``
   entries (the morning briefing, the sample digest), which start only on the owner's word.

``--first-install-only NAME`` (repeatable) narrows a starter-set seed to those entries; the
Codex launcher seeds only ``main-loop``. It never narrows a migrated interim file.

The in-checkout ``skills/schedule-crons/crons.json`` is never published. It is git-ignored and
older releases' installer filled it from the example, so its jobs prove no opt-in. Its entries
that match neither a published entry nor any template entry ever shipped
(``crons.example.history.json`` holds their digests; compared without the template-only ``opt_in`` key) may include
jobs of the owner's own, such as a customised entry under a starter name. The file is kept as
``hosts/<host>/crons.legacy.json`` (``crons.legacy.<n>.json`` when that name holds other content) and their names
are printed on a ``not-enabled`` line, so the agent can ask the owner. Nothing from it is
registered without the owner's word.

Owner notices (``restored`` and ``not-enabled`` lines) are also recorded in
``hosts/<host>/crons-notice.json``. The next run prints them once more unless
``--notice-sent`` cleared the record after the owner was told; then the record is dropped.

The per-host file is published with temp file + ``os.link``, which refuses an existing name,
so a concurrent seeder or the owner is never clobbered.
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
MARKER = ".crons-seeded"
LEGACY_KEPT = "crons.legacy.json"
NOTICE = "crons-notice.json"
HISTORY = "crons.example.history.json"


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


TEMPLATE_ONLY_KEYS = ("opt_in",)


def _job(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if k not in TEMPLATE_ONLY_KEYS}


def starter_entries(example: list, only: tuple[str, ...] | None = None) -> list:
    return [e for e in example if isinstance(e, dict) and e.get("opt_in") is not True
            and (not only or e.get("name") in only)]


def job_digest(entry: dict) -> str:
    blob = json.dumps(_job(entry), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def legacy_not_enabled(legacy: list, published: list, example: list,
                       shipped: frozenset = frozenset()) -> list[str]:
    """Names of legacy entries whose job is neither published nor any shipped template entry."""
    known = {job_digest(e) for e in published + example if isinstance(e, dict)} | shipped
    return [
        e["name"] for e in legacy
        if isinstance(e, dict) and isinstance(e.get("name"), str) and job_digest(e) not in known
    ]


def seed(workspace: Path, host_label: str, skill_dir: Path = SKILL_DIR,
         first_install_only: tuple[str, ...] | None = None):
    """Return (status, target, source, not_enabled, kept). Never overwrites ``target``."""
    if not host_label.strip():
        raise ValueError("host label did not resolve")
    host_dir = workspace / "hosts" / host_label
    target = host_dir / "crons.json"
    marker = host_dir / MARKER
    restoring = marker.exists()
    if target.exists():
        if not marker.exists():
            marker.touch()
        return "exists", target, None, [], None

    example_path = skill_dir / "crons.example.json"
    example = _entries(example_path.read_bytes(), example_path)
    history_path = skill_dir / HISTORY
    shipped = frozenset(
        h.get("sha256") for h in (_entries(history_path.read_bytes(), history_path)
                                  if history_path.is_file() else []) if isinstance(h, dict))
    interim = workspace / "crons" / f"{host_label}.json"
    if not marker.exists() and interim.is_file():
        source, content = interim, interim.read_bytes()
        published = _entries(content, interim)
    else:
        published = starter_entries(example, first_install_only)
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
            not_enabled = legacy_not_enabled(_entries(raw, legacy_path), published, example, shipped)
        except ValueError:
            not_enabled = []
        if not_enabled:
            kept = keep_legacy(host_dir, raw)
    return ("restored" if restoring else "seeded"), target, source, not_enabled, kept


def keep_legacy(host_dir: Path, raw: bytes) -> Path:
    """Write ``raw`` beside the host file under the first name free or already holding it."""
    n = 0
    while True:
        kept = host_dir / (LEGACY_KEPT if n == 0 else f"crons.legacy.{n}.json")
        if _publish(kept, raw) or kept.read_bytes() == raw:
            return kept
        n += 1


def owner_notices(host_dir: Path, new: list[str]) -> list[str]:
    """Record new notices, or replay an unacknowledged one a single more time."""
    record = host_dir / NOTICE
    if new:
        record.write_text(json.dumps({"lines": new}) + "\n")
        return new
    try:
        data = json.loads(record.read_text())
    except (OSError, ValueError):
        return []
    record.unlink(missing_ok=True)
    lines = data.get("lines") if isinstance(data, dict) else None
    return [x for x in lines if isinstance(x, str)] if isinstance(lines, list) else []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace")
    parser.add_argument("--host-label")
    parser.add_argument("--first-install-only", action="append", metavar="NAME",
                        help="seed only this starter entry when seeding the starter set (repeatable)")
    parser.add_argument("--notice-sent", action="store_true",
                        help="the owner has been told: clear the recorded notice and exit")
    parser.add_argument("--skill-dir", help=argparse.SUPPRESS)
    args = parser.parse_args()
    skill_dir = Path(args.skill_dir) if args.skill_dir else SKILL_DIR
    try:
        raw_ws = (args.workspace or "").strip() or _config("workspace")
        if not raw_ws:
            raise ValueError("workspace did not resolve")
        host_label = (args.host_label or "").strip() or _config("host-label")
        if not host_label.strip():
            raise ValueError("host label did not resolve")
        host_dir = Path(raw_ws) / "hosts" / host_label
        if args.notice_sent:
            (host_dir / NOTICE).unlink(missing_ok=True)
            return 0
        only = tuple(args.first_install_only) if args.first_install_only else None
        status, target, source, not_enabled, kept = seed(Path(raw_ws), host_label, skill_dir, only)
        new = []
        if status == "restored":
            new.append(f"restored {target}")
        if not_enabled:
            new.append(f"not-enabled {','.join(not_enabled)} kept at {kept}")
        notices = owner_notices(host_dir, new)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"seed-crons: {exc}", file=sys.stderr)
        return 1
    print(f"{'seeded' if status == 'restored' else status} {target}" + (f" from {source}" if source else ""))
    for line in notices:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
