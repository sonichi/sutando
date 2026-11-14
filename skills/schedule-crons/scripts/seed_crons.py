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
are printed on a ``not-enabled`` line, so the agent can ask the owner; if that copy fails, the line
names the legacy file itself. Nothing from it is
registered without the owner's word.

Owner notices (``restored`` and ``not-enabled``) are recorded in ``hosts/<host>/crons-notice.json``
before the per-host file is published, written whole. Each is queued once to the owner's DM
through the proactive outbox (``results/proactive-crons-notice-*.txt``), retried on a later run
if queueing failed, and its line is printed at most once a day until ``--notice-sent`` clears the
record. Printing or queueing never clears it; a new notice is added beside pending ones. An
unreadable record is moved to ``crons-notice.bad.json`` and reported as a notice of its own.
At most 20 numbered legacy backups are kept; past that the notice names the legacy file.

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
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO = SKILL_DIR.parents[1]
MARKER = ".crons-seeded"
LEGACY_KEPT = "crons.legacy.json"
NOTICE = "crons-notice.json"
REPLAY_S = 86400
MAX_BACKUPS = 20
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
    """Return (status, target, source, not_enabled, kept). Never overwrites ``target``.

    The owner notices this seed owes are recorded before ``target`` is published, so a failed
    notice write commits nothing and the next run regenerates both."""
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

    not_enabled: list[str] = []
    raw = b""
    legacy_path = skill_dir / "crons.json"
    if legacy_path.is_file():
        raw = legacy_path.read_bytes()
        try:
            not_enabled = legacy_not_enabled(_entries(raw, legacy_path), published, example, shipped)
        except ValueError:
            not_enabled = []
    planned = []
    if restoring:
        planned.append(_restored_notice(target))
    if not_enabled:
        planned.append(_not_enabled_notice(not_enabled, legacy_path))
    prior = _read_record(host_dir) if planned else None
    if planned:
        _add_notices(host_dir, prior, planned)

    if not _publish(target, content):
        if planned:
            _write_record(host_dir, prior)
        return "exists", target, None, [], None
    marker.touch()

    kept = None
    if not_enabled:
        try:
            kept = keep_legacy(host_dir, raw)
        except OSError:
            kept = legacy_path
        if kept != legacy_path:
            _replace_notice(host_dir, _not_enabled_notice(not_enabled, legacy_path),
                            _not_enabled_notice(not_enabled, kept))
    return ("restored" if restoring else "seeded"), target, source, not_enabled, kept


def keep_legacy(host_dir: Path, raw: bytes) -> Path:
    """Write ``raw`` beside the host file under the first name free or already holding it."""
    for n in range(MAX_BACKUPS + 1):
        kept = host_dir / (LEGACY_KEPT if n == 0 else f"crons.legacy.{n}.json")
        if _publish(kept, raw) or kept.read_bytes() == raw:
            return kept
    raise OSError(f"{MAX_BACKUPS} legacy backups already kept in {host_dir}")


def _restored_notice(target: Path) -> dict:
    return {"line": f"restored {target}",
            "message": "This Mac's scheduled-jobs file had been set up before and was missing, so "
                       "I restored the starter jobs. Any jobs of your own that were in it need adding "
                       "back: tell me which ones you want."}


def _not_enabled_notice(names: list[str], kept: Path) -> dict:
    return {"line": f"not-enabled {','.join(names)} kept at {kept}",
            "message": f"I found an old schedule file with jobs I did not turn on: {', '.join(names)}. "
                       f"It may include jobs of your own. A copy is kept at {kept}; tell me if you "
                       "want any of them back."}


def _read_record(host_dir: Path) -> dict:
    """The pending notices; an unreadable record is set aside and reported, never dropped."""
    path = host_dir / NOTICE
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {"notices": [], "last_shown": 0}
    except ValueError:
        data = None
    if not isinstance(data, dict):
        bad = _set_aside(path)
        return {"notices": [{"line": f"notice-record-unreadable kept at {bad}"}], "last_shown": 0}
    notices = data.get("notices") if isinstance(data, dict) else None
    notices = [n for n in notices if isinstance(n, dict) and isinstance(n.get("line"), str)] \
        if isinstance(notices, list) else []
    shown = data.get("last_shown") if isinstance(data, dict) else 0
    return {"notices": notices, "last_shown": shown if isinstance(shown, (int, float)) else 0}


def _set_aside(path: Path) -> Path:
    for n in range(MAX_BACKUPS + 1):
        bad = path.with_name("crons-notice.bad.json" if n == 0 else f"crons-notice.bad.{n}.json")
        try:
            os.link(path, bad)
        except FileExistsError:
            continue
        path.unlink()
        return bad
    raise OSError(f"{MAX_BACKUPS} unreadable notice records already kept beside {path}")


def _write_record(host_dir: Path, record: dict) -> None:
    """Replace the record whole: a reader sees the old or the new one, never a prefix."""
    path = host_dir / NOTICE
    if not record["notices"]:
        path.unlink(missing_ok=True)
        return
    host_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _add_notices(host_dir: Path, prior: dict, new: list[dict]) -> None:
    known = {n["line"] for n in prior["notices"]}
    added = [dict(n, queued=False) for n in new if n["line"] not in known]
    _write_record(host_dir, {"notices": prior["notices"] + added, "last_shown": 0})


def _replace_notice(host_dir: Path, old: dict, new: dict) -> None:
    record = _read_record(host_dir)
    record["notices"] = [dict(new, queued=n.get("queued", False)) if n["line"] == old["line"] else n
                         for n in record["notices"]]
    _write_record(host_dir, record)


def _queue_owner_dm(workspace: Path, notice: dict) -> None:
    """Hand the notice to the durable proactive outbox, which delivers it to the owner's DM."""
    if str(REPO / "src") not in sys.path:
        sys.path.insert(0, str(REPO / "src"))
    from proactive_routing import proactive_filename
    from result_publish import publish_text
    digest = hashlib.sha256(notice["line"].encode()).hexdigest()[:24]
    path = workspace / "results" / proactive_filename(f"crons-notice-{digest}")
    path.parent.mkdir(parents=True, exist_ok=True)
    publish_text(path, f"[dm-only]\n{notice['message']}\n")


def due_notices(workspace: Path, host_dir: Path, now: float) -> list[str]:
    """Queue each pending notice to the owner's DM once; print them at most once a day.
    The record is kept until ``--notice-sent``."""
    record = _read_record(host_dir)
    if not record["notices"]:
        return []
    for notice in record["notices"]:
        if not notice.get("queued") and "message" in notice:
            try:
                _queue_owner_dm(workspace, notice)
                notice["queued"] = True
            except (ImportError, OSError):
                pass
    lines = []
    if now - record["last_shown"] >= REPLAY_S:
        lines = [n["line"] for n in record["notices"]]
        record["last_shown"] = now
    _write_record(host_dir, record)
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace")
    parser.add_argument("--host-label")
    parser.add_argument("--first-install-only", action="append", metavar="NAME",
                        help="seed only this starter entry when seeding the starter set (repeatable)")
    parser.add_argument("--notice-sent", action="store_true",
                        help="the owner has answered the notices: clear them and exit")
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
        workspace = Path(raw_ws)
        host_dir = workspace / "hosts" / host_label
        if args.notice_sent:
            (host_dir / NOTICE).unlink(missing_ok=True)
            return 0
        only = tuple(args.first_install_only) if args.first_install_only else None
        status, target, source, _, _ = seed(workspace, host_label, skill_dir, only)
        notices = due_notices(workspace, host_dir, time.time())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"seed-crons: {exc}", file=sys.stderr)
        return 1
    print(f"{'seeded' if status == 'restored' else status} {target}" + (f" from {source}" if source else ""))
    for line in notices:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
