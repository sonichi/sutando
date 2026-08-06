#!/usr/bin/env python3
"""Bee → broker watcher: the client half of the Bee inbound-only channel.

Bee's developer surface (docs.bee.computer) is an authenticated LOCAL proxy —
REST + an SSE event stream bound to 127.0.0.1 after `bee login`. Bee pushes
no webhooks, so the ag2space broker cannot receive from Bee directly; this
watcher runs where the bee credentials live, subscribes to the proxy's SSE
stream, normalizes selected events into the relay task shape (`source: bee`),
and POSTs them through the broker's authenticated inbound hop (/v1/ingest).
Results route broker-side to the Bee fallback DM room — the asymmetric-channel
rule shipped in backend#444 (`integrations/bee.py` + `fallback_room_for`).

Config (manifest `config` block; env overrides manifest; CLI overrides env —
per skills/MANIFEST.md read precedence):
  BEE_PROXY_URL     Bee local proxy base (e.g. http://127.0.0.1:<port>).
                    REQUIRED — empty means the skill is not configured and the
                    watcher exits 2 with a clear message instead of guessing.
  BEE_EVENTS_PATH   SSE endpoint path on the proxy (default /v1/stream —
                    VERIFIED against a live authenticated proxy 2026-08-06;
                    /v1/events, the docs-derived guess, 404s).
  BEE_EVENT_TYPES   comma-list of SSE event types to forward
                    (default todo-created,todo-updated — conservative; the
                    per-utterance stream would flood the task queue).
  BEE_BROKER_URL    broker base (e.g. https://chat.ag2.space/relay). REQUIRED.
  BEE_BROKER_TOKEN  bearer for /v1/ingest — the caller's agent record must be
                    flagged `"ingest": true`. REQUIRED. Prefer the vault
                    (`vault set BEE_BROKER_TOKEN …`); env accepted.
  BEE_AGENT_ID      relay agent whose queue receives Bee tasks (dedicated
                    lane identity, same pattern as TEAMS_AGENT_ID).

Cursor: last delivered SSE event id persists to
<workspace>/state/bee-watcher-cursor.json and is replayed as Last-Event-ID on
reconnect, so a restart never re-forwards history the broker already has (and
HUB.enqueue is idempotent by task id as the second line of defense).
"""

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

# parents: [0]=scripts, [1]=bee-channel, [2]=skills, [3]=repo root. The
# suite's cursor-path test imports through this for real — a wrong index
# fails loudly there instead of silently degrading vault + cursor in prod.
_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO / "src"))

_SAFE_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,48}")
_MANIFEST = Path(__file__).resolve().parents[1] / "manifest.json"


def _log(msg: str) -> None:
    print(f"[bee-watcher] {msg}", flush=True)


def _config(cli: argparse.Namespace) -> dict:
    """CLI > env > manifest, per the skill-config convention."""
    try:
        manifest = json.loads(_MANIFEST.read_text()).get("config", {})
    except (OSError, ValueError):
        manifest = {}
    cfg = {}
    for key in ("BEE_PROXY_URL", "BEE_EVENTS_PATH", "BEE_EVENT_TYPES",
                "BEE_BROKER_URL", "BEE_BROKER_TOKEN", "BEE_AGENT_ID"):
        cli_val = getattr(cli, key.lower(), None)
        cfg[key] = (cli_val if cli_val not in (None, "")
                    else os.environ.get(key, "").strip() or str(manifest.get(key, "")))
    if not cfg["BEE_BROKER_TOKEN"]:
        try:  # vault is the preferred home for the bearer
            from vault_intercept import get_vault_key
            cfg["BEE_BROKER_TOKEN"] = get_vault_key("BEE_BROKER_TOKEN")
        except Exception:
            pass
    return cfg


def _cursor_path() -> Path:
    from workspace_default import resolve_workspace
    return Path(resolve_workspace()) / "state" / "bee-watcher-cursor.json"


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


def _safe_task_id(raw: str) -> str:
    """Mirror of the broker-side id rule: sparrow's task-id contract is
    [A-Za-z0-9._-]{1,64}; in-alphabet ids pass through, anything else becomes
    a stable sha256 slug so no task can be lane-rejected."""
    if _SAFE_ID_RE.fullmatch(raw):
        return f"task-bee-{raw}"
    return f"task-bee-{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


def event_to_task(etype: str, event_id: str, data: dict) -> dict:
    """Normalize one SSE event into the relay task shape.

    Field mapping VERIFIED against a live authenticated stream (2026-08-06,
    first real capture): utterance events nest text under `utterance.text`
    with the stable id at `utterance.id`; the conversation key is
    `conversation_uuid` (not conversation_id); and the stream sends NO SSE
    `id:` field, so the stable entity id inside the payload is the dedupe
    key. Unknown shapes still fall back to compact JSON — visible to the
    core rather than dropped."""
    utt = data.get("utterance") if isinstance(data.get("utterance"), dict) else {}
    todo = data.get("todo") if isinstance(data.get("todo"), dict) else {}
    text = ""
    for v in (utt.get("text"), todo.get("text"), data.get("text"),
              data.get("summary"), data.get("name"), data.get("title")):
        if isinstance(v, str) and v.strip():
            text = v.strip()
            break
    if not text:
        text = json.dumps(data, separators=(",", ":"))[:500]
    conv = str(data.get("conversation_uuid") or data.get("conversation_id")
               or data.get("id") or event_id)[:120]
    stable = str(utt.get("id") or todo.get("id") or data.get("id") or event_id)
    return {
        "id": _safe_task_id(f"{etype}-{stable}"),
        "task": f"[Bee {etype}] {text}",
        "source": "bee",
        "user_id": "bee",
        "sender_name": "Bee",
        "channel_id": conv,
        "room_name": "Bee",
        "interaction_type": "message",
    }


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
    url = cfg["BEE_PROXY_URL"].rstrip("/") + cfg["BEE_EVENTS_PATH"]
    backoff, forwarded = 1, 0
    while True:
        headers = {"Accept": "text/event-stream",
                   "User-Agent": "sutando-bee-watcher/1.0"}
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
                    if not _post_task(cfg, task):
                        # Contiguous-prefix cursor discipline (review P1
                        # 2026-08-06): a failed delivery HALTS the stream.
                        # Processing a later event would advance the cursor
                        # past this one, and the reconnect's Last-Event-ID
                        # would tell Bee never to replay it — silent data
                        # loss. Reconnect resumes from the last fully
                        # delivered prefix; broker enqueue idempotency
                        # absorbs any replays of already-accepted events.
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
                "bee_broker_url", "bee_broker_token", "bee_agent_id"):
        ap.add_argument(f"--{key.replace('_', '-')}", dest=key)
    ap.add_argument("--once", action="store_true",
                    help="process one stream connection, then exit (tests)")
    ap.add_argument("--max-events", type=int, default=0)
    args = ap.parse_args()
    cfg = _config(args)
    missing = [k for k in ("BEE_PROXY_URL", "BEE_BROKER_URL", "BEE_BROKER_TOKEN")
               if not cfg[k]]
    if missing:
        _log(f"not configured ({', '.join(missing)} unset) — see "
             "skills/bee-channel/manifest.json; exiting")
        return 2
    return run(cfg, once=args.once, max_events=args.max_events)


if __name__ == "__main__":
    sys.exit(main())
