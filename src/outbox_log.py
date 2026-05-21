"""Outbox visibility log — single append-only sink for outbound messages.

Every bridge (Discord, Slack, Telegram, …) and every owner-facing skill that
sends messages to a non-owner audience writes one JSON-line per delivery to
`<workspace>/state/outbox.log`. Owner can then audit what Sutando has said
on their behalf via `tail -f`, the dashboard Outbox tab (follow-up PR), or
ad-hoc `jq` queries.

Schema (one JSON object per line, newline-delimited):

    {
      "ts": 1779253245.123,
      "iso_ts": "2026-05-19T22:04:05Z",
      "core_id": "1" | "2" | "legacy" | "unknown",
      "channel_type": "discord_dm" | "discord_channel" |
                      "slack_dm" | "slack_channel" |
                      "telegram",
      "recipient": "<user_id or channel_id>",
      "recipient_label": "Qingyun (owner) DM" | "#dev" | ...   (optional, best-effort),
      "task_id": "<originating task id>"                       (optional),
      "body_preview": "<first 200 chars of body, single line>",
      "body_len": <int>
    }

Design choices:
- Append-only line-delimited JSON: tail/jq friendly, no rotation required at
  this volume (a few hundred entries a day).
- `body_preview` is a one-line collapse capped at 200 chars — surfaces enough
  context for audit without bloating the log or leaking long secrets.
- Calls into `append()` MUST NOT raise. Observability never blocks delivery.
- File location resolved through `workspace_default.resolve_workspace()` so
  bridges and dashboard read the same place.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Match the bridge convention — src/ is on path so a sibling import works.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from workspace_default import resolve_workspace  # noqa: E402


_PREVIEW_MAX = 200


def _outbox_path() -> Path:
    return resolve_workspace() / "state" / "outbox.log"


def _preview(body: str) -> str:
    """Collapse newlines into pilcrows and cap to _PREVIEW_MAX chars."""
    flat = body.replace("\r\n", "\n").replace("\n", " ¶ ").strip()
    if len(flat) > _PREVIEW_MAX:
        return flat[: _PREVIEW_MAX - 1] + "…"
    return flat


def append(
    *,
    channel_type: str,
    recipient: str,
    body: str,
    core_id: str | None = None,
    task_id: str | None = None,
    recipient_label: str | None = None,
) -> None:
    """Append one entry to the outbox log.

    Never raises — outbox visibility must not block message delivery. If the
    workspace path can't be resolved or the file can't be written, the call
    silently no-ops. Bridges should call this immediately after a successful
    `send` (not before — we log what actually went out, not what was attempted).
    """
    try:
        path = _outbox_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        entry: dict = {
            "ts": now,
            "iso_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "core_id": core_id or os.environ.get("SUTANDO_CORE_ID", "unknown"),
            "channel_type": channel_type,
            "recipient": recipient,
            "body_preview": _preview(body),
            "body_len": len(body),
        }
        if task_id:
            entry["task_id"] = task_id
        if recipient_label:
            entry["recipient_label"] = recipient_label
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        # Outbox is observability — never crash the send path.
        pass


def read_recent(limit: int = 50) -> list[dict]:
    """Return the last `limit` entries (oldest first), best-effort.

    Used by the dashboard Outbox tab (follow-up PR) and by ad-hoc operator
    queries. Silently skips malformed lines so a single corrupt entry can't
    blank the view.
    """
    try:
        path = _outbox_path()
        if not path.is_file():
            return []
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except Exception:
        return []
    out: list[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out
