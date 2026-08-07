#!/usr/bin/env python3
"""Bee → broker watcher: the client half of the Bee inbound-only channel.

Subscribes to Bee's SSE stream (local authenticated proxy, or cloud-direct
when BEE_API_BASE+BEE_API_TOKEN are set), normalizes selected events into the
relay task shape (`source: bee`), and delivers via the configured sink
(broker /v1/ingest | local task files | durable EventInbox). Config
reference: the package README, "Bee wearable source". The last delivered SSE
id persists and replays as Last-Event-ID on reconnect — restarts never
re-forward history; a failed delivery halts the stream (no skip-ahead)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Shared runner; Bee's subscribe/normalize live in sources/bee.py.
# Re-exports below keep the public surface (and tests) stable.
from ag2_sparrow.sources import bee as _source

confine_user_content = _source.confine_user_content
_safe_task_id = _source._safe_task_id
event_to_task = _source.event_to_task
_DEFAULTS = _source.DEFAULTS


def _log(msg: str) -> None:
    print(f"[bee-watcher] {msg}", flush=True)


def _config(cli: argparse.Namespace) -> dict:
    """CLI > env > package default."""
    cfg = {}
    for key in _source.CONFIG_KEYS:
        cli_val = getattr(cli, key.lower(), None)
        cfg[key] = (cli_val if cli_val not in (None, "")
                    else os.environ.get(key, "").strip() or _DEFAULTS.get(key, ""))
    for _k in _source.VAULT_KEYS:
        if not cfg[_k]:
            try:  # vault is the preferred home for bearers
                from vault_intercept import get_vault_key
                cfg[_k] = get_vault_key(_k)
            except Exception:
                pass
    return cfg


def _cursor_path() -> Path:
    # BEE_CURSOR_FILE overrides the resume-cursor path (required headless —
    # no workspace to resolve); unset resolves under the local workspace.
    _explicit = os.environ.get("BEE_CURSOR_FILE", "").strip()
    if _explicit:
        return Path(_explicit)
    from ag2_sparrow import _dirs
    return _dirs.state_dir() / "bee-watcher-cursor.json"


def _read_cursor() -> str:
    try:
        return json.loads(_cursor_path().read_text()).get("last_event_id", "")
    except (OSError, ValueError):
        return ""


def _write_cursor(event_id: str) -> None:
    p = _cursor_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"last_event_id": event_id, "ts": int(time.time())}))
    os.replace(tmp, p)


def _write_local_task(task: dict) -> bool:
    """LOCAL sink: persist the event as a task file (atomic tmp+rename).

    access_tier ambient, never owner — device-captured speech is an
    observation, not a command; privileged actions must surface, not
    execute. priority low: ambient events don't preempt direct asks."""
    from ag2_sparrow import _dirs
    import datetime
    tasks_dir = _dirs.task_dir()
    tasks_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [f"id: {task['id']}", f"timestamp: {ts}", f"task: {task['task']}",
             "source: bee", "interaction_type: message",
             f"channel_id: {task['channel_id']}", f"user_id: {task['user_id']}",
             "room_name: Bee", "priority: low", "access_tier: ambient"]
    dest = tasks_dir / f"{task['id']}.txt"
    # Dedup on the task artifact (the delivery record) across its lifecycle:
    # find_task_file = live + claimed-core-N; find_archived_task = archives.
    from ag2_sparrow.task_archive import find_task_file
    from ag2_sparrow.local_task_protocol import find_archived_task
    if (find_task_file(tasks_dir, task["id"]) is not None
            or find_archived_task(tasks_dir, task["id"]) is not None):
        return True                     # already delivered — idempotent
    tmp = dest.with_suffix(".txt.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, dest)
    return True


class _InboxSink:
    """BEE_SINK=inbox: deliver into the durable EventInbox, drained via the
    shared TaskifyHandler (threshold=1); promoted tasks are ambient-tier.

    Must use its OWN sqlite file — sharing the gateway inbox corrupts that
    channel's MAX(cursor) resume anchor. insert() committing IS delivery;
    drain failures retry on later drains and never halt the stream."""

    def __init__(self, types: set):
        from ag2_sparrow import _dirs
        from ag2_sparrow.event_inbox import EventInbox
        from ag2_sparrow.event_consumer import EventConsumer, TaskifyHandler
        path = os.environ.get("BEE_INBOX_FILE", "").strip() \
            or str(_dirs.state_dir() / "bee-events.db")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._inbox = EventInbox(path)
        self._next = (self._inbox.durable_cursor() or 0) + 1
        handler = TaskifyHandler(str(_dirs.task_dir()), agent_mxid=None,
                                 threshold=1, log=_log, types=types)
        self._consumer = EventConsumer(self._inbox, handler)

    def deliver(self, task: dict, etype: str) -> bool:
        env = {"event_id": task["id"], "cursor": self._next,
               "type": f"bee.{etype}", "room_id": f"bee:{task['channel_id']}",
               "actor_id": "bee", "content": {"body": task["task"]}}
        try:
            self._inbox.insert(env)
        except Exception as e:  # noqa: BLE001 — sqlite failure = not durable, halt
            _log(f"inbox insert failed for {task['id']}: {e}")
            return False
        self._next += 1
        try:
            self._consumer.drain()
        except Exception as e:  # noqa: BLE001 — events stay durable; retried next drain
            _log(f"inbox drain error (event remains durable): {e}")
        return True


def _post_task(cfg: dict, task: dict) -> bool:
    req = urllib.request.Request(
        cfg["BEE_BROKER_URL"].rstrip("/") + "/v1/ingest",
        data=json.dumps({"agent_id": cfg["BEE_AGENT_ID"], "task": task}).encode(),
        headers={"Authorization": f"Bearer {cfg['BEE_BROKER_TOKEN']}",
                 "Content-Type": "application/json",
                 "User-Agent": "sutando-bee-watcher/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError) as e:
        _log(f"ingest POST failed for {task['id']}: {e}")
        return False


def _sse_frames(resp):
    """Yield (event, id, data) per SSE frame. Minimal parser: comment lines
    (:) skipped, frame dispatched on blank line, multi-line data joined."""
    etype, eid, data_lines = "message", "", []
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\n").rstrip("\r")
        if line.startswith(":"):
            continue
        if line == "":
            if data_lines:
                yield etype, eid, "\n".join(data_lines)
            etype, eid, data_lines = "message", eid, []
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            etype = value
        elif field == "id":
            eid = value
        elif field == "data":
            data_lines.append(value)
    if data_lines:  # stream ended without trailing blank line
        yield etype, eid, "\n".join(data_lines)


def run(cfg: dict, once: bool = False, max_events: int = 0) -> int:
    wanted = {t.strip() for t in cfg["BEE_EVENT_TYPES"].split(",") if t.strip()}
    _sink = (_InboxSink({f"bee.{t}" for t in wanted})
             if cfg.get("BEE_SINK") == "inbox" else None)
    url, base_headers = _source.stream_request(cfg)
    backoff, forwarded = 1, 0
    while True:
        headers = dict(base_headers)
        cursor = _read_cursor()
        if cursor:
            headers["Last-Event-ID"] = cursor
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers), timeout=300) as resp:
                backoff = 1
                for etype, eid, data_raw in _sse_frames(resp):
                    if wanted and etype not in wanted:
                        continue
                    try:
                        data = json.loads(data_raw)
                        if not isinstance(data, dict):
                            data = {"value": data}
                    except ValueError:
                        data = {"text": data_raw}
                    task = event_to_task(etype, eid or data_raw, data)
                    if _sink is not None:
                        ok = _sink.deliver(task, etype)
                    elif cfg.get("BEE_SINK") == "local":
                        ok = _write_local_task(task)
                    else:
                        ok = _post_task(cfg, task)
                    if not ok:
                        # Halt on failed delivery — advancing the cursor would
                        # tell the reconnect (Last-Event-ID) never to replay it.
                        _log(f"halting stream at {task['id']} — delivery "
                             "failed; will reconnect from last good cursor")
                        break
                    forwarded += 1
                    _log(f"forwarded {task['id']} ({etype}) → {cfg['BEE_AGENT_ID']}")
                    if eid:
                        _write_cursor(eid)
                    if max_events and forwarded >= max_events:
                        return 0
        except (urllib.error.URLError, OSError) as e:
            _log(f"SSE connection error: {e}")
        if once:
            return 0
        time.sleep(min(backoff, 60))
        backoff *= 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for key in ("bee_proxy_url", "bee_events_path", "bee_event_types",
                "bee_broker_url", "bee_broker_token", "bee_agent_id",
                "bee_sink", "bee_api_base", "bee_api_token"):
        ap.add_argument(f"--{key.replace('_', '-')}", dest=key)
    ap.add_argument("--once", action="store_true",
                    help="process one stream connection, then exit (tests)")
    ap.add_argument("--max-events", type=int, default=0)
    args = ap.parse_args()
    cfg = _config(args)
    required = []
    if not _source.source_configured(cfg):
        # neither a local proxy nor a direct API source is configured
        required = ["BEE_PROXY_URL (or BEE_API_BASE+BEE_API_TOKEN)"]
    if cfg.get("BEE_SINK") not in ("local", "inbox"):
        required += [k for k in ("BEE_BROKER_URL", "BEE_BROKER_TOKEN") if not cfg[k]]
    missing = required
    if missing:
        _log(f"not configured ({', '.join(missing)} unset) — set via CLI flags "
             "or BEE_* env (see this module's docstring); exiting")
        return 2
    return run(cfg, once=args.once, max_events=args.max_events)


if __name__ == "__main__":
    sys.exit(main())
