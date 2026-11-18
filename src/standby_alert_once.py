#!/usr/bin/env python3
"""At most one owner alert per outage of a seat's task watcher, however often it is retried.

The alert-only standby (src/agent/codex/cli/task-notifier-supervisor.sh) asks here instead of
publishing directly, so a retry can never become a second DM:

* An outage is a durable episode, one record per inbox under <workspace>/state/standby-alert/,
  shared by every supervisor of that inbox and kept across a supervisor restart. Each poll that
  finds no watcher refreshes it; one not refreshed for longer than the grace belongs to an
  outage nobody saw end, so the next one starts afresh.
* Each message of an episode has one fixed result name. Under a lock on that name, it is
  published only if no copy exists live, archived or quarantined (`find_ready_result`,
  `undelivered_quarantine.find_quarantined`), so a publisher killed after its rename, a second
  supervisor or a restarted one finds it and does not send it again. A failure before the
  rename leaves nothing, and the retry publishes under the same name.
* The macOS notification is claimed once per episode, by whichever supervisor tries first.

    standby_alert_once.py observe --workspace W --inbox I --now T --stale S   prints the episode start
    standby_alert_once.py end --workspace W --inbox I
    standby_alert_once.py stem --workspace W --inbox I --episode E [--slot K]  prints the message's name
    standby_alert_once.py publish --workspace W --stem STEM                   body on stdin
    standby_alert_once.py claim-notify --workspace W --inbox I --episode E

`publish` exits 0 published now, 4 already published, 3 published but its directory sync
unconfirmed, 1 not published. `claim-notify` exits 0 for the one caller that may notify, else 1.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import undelivered_quarantine
from delivery.task_dispatch import find_ready_result
from file_lock import locked_file
from result_publish import PublishedUnconfirmed, publish_text
from workspace_default import status_path

PUBLISHED, ALREADY, UNCONFIRMED, FAILED = 0, 4, 3, 1


def inbox_key(inbox) -> str:
    """A name-safe key for an inbox: no path leaves in a result name."""
    return hashlib.sha256(os.fsencode(os.path.realpath(inbox))).hexdigest()[:12]


def stem(inbox, episode: int, slot: "int | None" = None) -> str:
    """The one result name for a message of an episode: the alert, or reminder `slot`."""
    return f"proactive-standby-unwatched-{inbox_key(inbox)}-{int(episode)}" + (f"-r{int(slot)}" if slot else "")


def _state(workspace) -> Path:
    return status_path("standby-alert", Path(workspace))


def observe(workspace, inbox, now: int, stale_after: int) -> int:
    """The start of the current outage of `inbox`, opening one if none is live."""
    key = inbox_key(inbox)
    record = _state(workspace) / f"{key}.json"
    with locked_file(_state(workspace) / f"{key}.lock"):
        try:
            rec = json.loads(record.read_text(encoding="utf-8"))
            since, seen = int(rec["since"]), int(rec["seen"])
        except (OSError, ValueError, KeyError, TypeError):
            since = seen = None
        if since is None or now - seen > stale_after or now < since:
            since = now
        tmp = record.with_name(f".{record.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"since": since, "seen": now}), encoding="utf-8")
        os.replace(tmp, record)
    return since


def end(workspace, inbox) -> None:
    """The watcher is back: the outage and its notification claim end."""
    key = inbox_key(inbox)
    with locked_file(_state(workspace) / f"{key}.lock"):
        for p in _state(workspace).glob(f"{key}.*"):
            if p.suffix != ".lock":
                p.unlink(missing_ok=True)
        # Each message name belongs to one episode and is never reused, so its lock can go.
        for p in _state(workspace).glob(f"proactive-standby-unwatched-{key}-*.lock"):
            p.unlink(missing_ok=True)


def already_published(results: Path, stem: str) -> bool:
    """Any copy of this result, live, delivered to an archive, or quarantined as undeliverable."""
    return find_ready_result(results, stem) is not None or bool(undelivered_quarantine.find_quarantined(results, stem))


def publish(workspace, stem: str, body: str) -> int:
    results = Path(workspace) / "results"
    if not results.is_dir():
        return FAILED
    with locked_file(_state(workspace) / f"{stem}.lock"):
        if already_published(results, stem):
            return ALREADY
        try:
            publish_text(results / f"{stem}.txt", body)
        except PublishedUnconfirmed:
            return UNCONFIRMED
        except OSError:
            return FAILED
    return PUBLISHED


def claim_notify(workspace, inbox, episode: int) -> bool:
    marker = _state(workspace) / f"{inbox_key(inbox)}.notified-{int(episode)}"
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return False
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="one standby alert per outage")
    ap.add_argument("cmd", choices=("observe", "end", "stem", "publish", "claim-notify"))
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--inbox")
    ap.add_argument("--stem")
    ap.add_argument("--now", type=int)
    ap.add_argument("--stale", type=int, default=300)
    ap.add_argument("--episode", type=int)
    ap.add_argument("--slot", type=int)
    a = ap.parse_args(argv)
    if a.cmd == "publish":
        if not a.stem or not a.stem.startswith("proactive-standby-unwatched-"):
            ap.error("--stem must name a standby alert")
        return publish(a.workspace, a.stem, sys.stdin.read())
    if not a.inbox:
        ap.error("--inbox is required")
    if a.cmd == "observe":
        if a.now is None:
            ap.error("--now is required")
        print(observe(a.workspace, a.inbox, a.now, a.stale))
        return 0
    if a.cmd == "end":
        end(a.workspace, a.inbox)
        return 0
    if a.episode is None:
        ap.error("--episode is required")
    if a.cmd == "stem":
        print(stem(a.inbox, a.episode, a.slot))
        return 0
    return 0 if claim_notify(a.workspace, a.inbox, a.episode) else 1


if __name__ == "__main__":
    sys.exit(main())
