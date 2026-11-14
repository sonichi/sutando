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
jobs of the owner's own, such as a customised entry under a starter name. Before the per-host
file is published, the legacy file is kept as ``hosts/<host>/crons.legacy.json``
(``crons.legacy.<n>.json`` when that name holds other content, up to 20, then
``crons.legacy.<digest>.json``; no backup is ever replaced) and their names are printed on a
``not-enabled`` line, so the agent can ask the owner. If no copy can be written, the line says the
only copy is in the checkout, which an app update replaces, and later runs retry the copy while the
notice is pending, only if the legacy file still holds the bytes the notice was raised for;
if it changed or is gone, the notice says those jobs were lost. Nothing from it is
registered without the owner's word.

Owner notices (``restored`` and ``not-enabled``) are recorded in ``hosts/<host>/crons-notice.json``
before the per-host file is published, written whole, under a per-host lock that serializes
seeder runs. A notice already pending under the same id is merged, not repeated; a run that loses
the race to publish withdraws only the notices it created and still owns. A migrated notice is
saved with its episode before it is queued. Each notice is queued once to the
owner's DM through the proactive outbox (``results/proactive-crons-notice-*.txt``, named by the
notice's id and an episode id drawn when it is first recorded, so a recurrence after
``--notice-sent`` is a new outbox item and a retry is the same one); a failed
enqueue is reported on stderr and retried on a later run. Its line is printed at most once a day.
``--notice-sent`` clears only the notices already printed. An unreadable or wrong-shape record is
set aside (``crons-notice.bad.json``, numbered, then ``.bad.latest.json``) and reported as a notice
of its own; the earlier ``{"lines": [...]}`` format is migrated.

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
LOCK = ".crons-seed.lock"

if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))
from file_lock import locked_file  # noqa: E402


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

    The legacy backup and the owner notices are written before ``target`` is published, so a
    crash or a failed write commits no schedule and the next run redoes all of it."""
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
    kept = None
    planned = []
    if restoring:
        planned.append(_restored_notice(target))
    if not_enabled:
        host_dir.mkdir(parents=True, exist_ok=True)
        try:
            kept = keep_legacy(host_dir, raw)
        except OSError:
            kept = None
        planned.append(_not_enabled_notice(not_enabled, legacy_path, raw, kept))
    token = os.urandom(8).hex()
    if planned:
        _update_record(host_dir, lambda r: _merge(r, planned, token))

    if not _publish(target, content):
        if planned:
            _update_record(host_dir, lambda r: _drop_owned(r, token))
        return "exists", target, None, [], None
    marker.touch()
    return ("restored" if restoring else "seeded"), target, source, not_enabled, kept


def keep_legacy(host_dir: Path, raw: bytes) -> Path:
    """Write ``raw`` beside the host file under the first name free or already holding it;
    past ``MAX_BACKUPS`` names, under a name of its own digest. No backup is ever replaced."""
    for n in range(MAX_BACKUPS + 1):
        kept = host_dir / (LEGACY_KEPT if n == 0 else f"crons.legacy.{n}.json")
        if _publish(kept, raw):
            return kept
        try:
            if kept.read_bytes() == raw:
                return kept
        except OSError:
            continue
    kept = host_dir / f"crons.legacy.{hashlib.sha256(raw).hexdigest()[:16]}.json"
    if _publish(kept, raw) or kept.read_bytes() == raw:
        return kept
    raise OSError(f"{kept} holds other content")


def _restored_notice(target: Path) -> dict:
    return {"id": f"restored:{target}", "line": f"restored {target}",
            "message": "This Mac's scheduled-jobs file had been set up before and was missing, so "
                       "I restored the starter jobs. Any jobs of your own that were in it need adding "
                       "back: tell me which ones you want."}


def _not_enabled_notice(names: list[str], legacy: Path, raw: bytes, kept: Path | None) -> dict:
    notice = {"id": "not-enabled:" + hashlib.sha256(raw).hexdigest()[:24], "names": names,
              "legacy": str(legacy)}
    return _describe_not_enabled(notice, kept)


def _describe_not_enabled(notice: dict, kept: Path | None) -> dict:
    names = notice["names"]
    if kept is None:
        notice.update(kept=None, line=f"not-enabled {','.join(names)} only at {notice['legacy']} "
                                     "(an app update replaces it)",
                      message=f"I found an old schedule file with jobs I did not turn on: "
                              f"{', '.join(names)}. It may include jobs of your own. I could not save "
                              f"a copy, and the only one is {notice['legacy']}, which an app update "
                              "replaces; tell me if you want any of them back.")
    else:
        notice.update(kept=str(kept), line=f"not-enabled {','.join(names)} kept at {kept}",
                      message=f"I found an old schedule file with jobs I did not turn on: "
                              f"{', '.join(names)}. It may include jobs of your own. A copy is kept "
                              f"at {kept}; tell me if you want any of them back.")
    return notice


def _describe_lost(notice: dict) -> dict:
    names = notice["names"]
    notice.update(kept=None, lost=True,
                  line=f"not-enabled {','.join(names)} lost: {notice['legacy']} was replaced by an "
                       "app update before it could be backed up",
                  message=f"I found an old schedule file with jobs I did not turn on: {', '.join(names)}. "
                          "It may have held jobs of your own, but an app update replaced it before I "
                          "could save a copy, so those jobs are lost. Tell me if you want any of them "
                          "set up again.")
    return notice


def _migrate_line(line: str) -> dict | None:
    if line.startswith("restored "):
        return _restored_notice(Path(line[len("restored "):]))
    head, sep, kept = line.partition(" kept at ")
    if line.startswith("not-enabled ") and sep:
        names = head[len("not-enabled "):].split(",")
        notice = {"id": f"not-enabled-migrated:{line}", "names": names, "legacy": kept}
        return _describe_not_enabled(notice, Path(kept))
    return None


def _valid(notice) -> bool:
    return isinstance(notice, dict) and isinstance(notice.get("line"), str) \
        and isinstance(notice.get("id", notice.get("line")), str)


def _read_record(host_dir: Path) -> dict:
    """The pending notices. The format before ids (``{"lines": [...]}``) is migrated; anything
    else unreadable is set aside and reported, never dropped."""
    path = host_dir / NOTICE
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {"notices": [], "last_shown": 0}
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("lines"), list) and "notices" not in data:
        migrated = [_migrate_line(x) for x in data["lines"] if isinstance(x, str)]
        if all(migrated):
            return {"notices": [dict(n, queued=False, shown=False, episode=os.urandom(8).hex())
                                for n in migrated], "last_shown": 0}
    elif isinstance(data, dict) and isinstance(data.get("notices"), list) \
            and all(_valid(n) for n in data["notices"]):
        shown = data.get("last_shown")
        return {"notices": [dict(n, id=n.get("id", n["line"])) for n in data["notices"]],
                "last_shown": shown if isinstance(shown, (int, float)) else 0}
    bad = _set_aside(path)
    return {"notices": [{"id": f"unreadable:{bad}", "line": f"notice-record-unreadable kept at {bad}",
                         "queued": False, "shown": False, "episode": os.urandom(8).hex()}],
            "last_shown": 0}


def _set_aside(path: Path) -> Path:
    for n in range(MAX_BACKUPS + 1):
        bad = path.with_name("crons-notice.bad.json" if n == 0 else f"crons-notice.bad.{n}.json")
        try:
            os.link(path, bad)
        except FileExistsError:
            continue
        path.unlink()
        return bad
    latest = path.with_name("crons-notice.bad.latest.json")
    os.replace(path, latest)
    return latest


def _replace_whole(path: Path, data: bytes) -> None:
    """Replace ``path`` whole: a reader sees the old or the new bytes, never a prefix."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _write_record(host_dir: Path, record: dict) -> None:
    path = host_dir / NOTICE
    if not record["notices"]:
        path.unlink(missing_ok=True)
        return
    _replace_whole(path, (json.dumps(record) + "\n").encode())


def _update_record(host_dir: Path, change) -> dict:
    record = change(_read_record(host_dir))
    _write_record(host_dir, record)
    return record


def _merge(record: dict, new: list[dict], token: str) -> dict:
    """Add ``new`` beside pending notices. A notice already pending under the same id is taken
    over by this run (so only the last writer's rollback can remove it) and keeps its state."""
    notices = list(record["notices"])
    for n in new:
        same = next((i for i, old in enumerate(notices) if old["id"] == n["id"]), None)
        if same is None:
            notices.append(dict(n, queued=False, shown=False, by=token, created=token,
                                episode=os.urandom(8).hex()))
        else:
            notices[same] = dict(notices[same], by=token)
    return {"notices": notices, "last_shown": 0}


def _drop_owned(record: dict, token: str) -> dict:
    """Withdraw what this run created and still owns; a notice it only took over stays."""
    return dict(record, notices=[n for n in record["notices"]
                                 if not (n.get("by") == token and n.get("created") == token)])


def _queue_owner_dm(workspace: Path, notice: dict) -> None:
    """Hand the notice to the durable proactive outbox, which delivers it to the owner's DM."""
    from proactive_routing import proactive_filename
    from result_publish import publish_text
    key = f"{notice['id']}#{notice.get('episode', '')}"
    digest = hashlib.sha256(key.encode()).hexdigest()[:24]
    path = workspace / "results" / proactive_filename(f"crons-notice-{digest}")
    path.parent.mkdir(parents=True, exist_ok=True)
    publish_text(path, f"[dm-only]\n{notice['message']}\n")


def due_notices(workspace: Path, host_dir: Path, now: float) -> list[str]:
    """Retry an unsaved legacy backup, queue each pending notice to the owner's DM once, and
    return the lines to print (at most once a day). The record is kept until ``--notice-sent``."""
    record = _read_record(host_dir)
    if not record["notices"]:
        return []
    _write_record(host_dir, record)  # a migrated episode is saved before anything is queued
    for notice in record["notices"]:
        if notice["id"].startswith("not-enabled:") and notice.get("kept") is None \
                and not notice.get("lost"):
            try:
                raw = Path(notice["legacy"]).read_bytes()
                if notice["id"] == "not-enabled:" + hashlib.sha256(raw).hexdigest()[:24]:
                    _describe_not_enabled(notice, keep_legacy(host_dir, raw))
                else:
                    _describe_lost(notice)
                notice["queued"] = False
            except FileNotFoundError:
                _describe_lost(notice)
                notice["queued"] = False
            except (OSError, KeyError):
                pass
        if not notice.get("queued") and "message" in notice:
            try:
                _queue_owner_dm(workspace, notice)
                notice["queued"] = True
            except (ImportError, OSError) as exc:
                print(f"seed-crons: could not queue the owner DM for {notice['line']!r}: {exc}",
                      file=sys.stderr)
    lines = []
    if now - record["last_shown"] >= REPLAY_S:
        lines = [n["line"] for n in record["notices"]]
        for n in record["notices"]:
            n["shown"] = True
        record["last_shown"] = now
    _write_record(host_dir, record)
    return lines


def _clear_shown(record: dict) -> dict:
    return dict(record, notices=[n for n in record["notices"] if not n.get("shown")])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace")
    parser.add_argument("--host-label")
    parser.add_argument("--first-install-only", action="append", metavar="NAME",
                        help="seed only this starter entry when seeding the starter set (repeatable)")
    parser.add_argument("--notice-sent", action="store_true",
                        help="the owner has answered the notices printed so far: clear them and exit")
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
        host_dir.mkdir(parents=True, exist_ok=True)
        with locked_file(host_dir / LOCK):
            if args.notice_sent:
                _update_record(host_dir, _clear_shown)
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
