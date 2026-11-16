#!/usr/bin/env python3
"""Map GitHub/Gmail/Slack connector results into `space.ag2.inbox.item` events, dedupe
them against what was already sent, and format a digest — the deterministic half of
the inbox-feed skill. Fetching from GitHub/Gmail/Slack (composio_exec), finding the
owner's Inbox room (room.state.read / room.list) and sending the mapped events
(room.event.send / room.message.send) are MCP tool calls the agent itself makes; this
script never calls them. See SKILL.md for the full loop.

Usage:
  inbox_feed.py rules show [--workspace PATH]
  inbox_feed.py rules init [--workspace PATH]
  inbox_feed.py map --source {github,gmail,slack} --kind KIND --file RAW.json
      [--account NAME] [--workspace PATH]
  inbox_feed.py dedupe --file ITEMS.json [--live] [--workspace PATH]
  inbox_feed.py find-room --file ROOMS.json --owner MXID
  inbox_feed.py cursors get --source {github,gmail,slack} [--workspace PATH]
  inbox_feed.py cursors set --source {github,gmail,slack} --value VALUE [--live] [--workspace PATH]
  inbox_feed.py log-sent --file ITEMS.json [--live] [--workspace PATH]
  inbox_feed.py digest [--hours N] [--workspace PATH]

`--live` is the one gate between "show what would happen" and "persist state" —
`map` and `find-room` are read-only either way. Default is dry-run everywhere `--live`
is accepted.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from workspace_default import resolve_workspace  # noqa: E402

SCHEMA_VERSION = 1
SUMMARY_MAX = 280
SENT_IDS_MAX_AGE_DAYS = 45
LOG_MAX_BYTES = 1_000_000
LOG_KEEP_BYTES = 200_000
TOPIC_MARKER = "Your private Inbox"
INBOX_ROOM_NAME = "Inbox"
INBOX_STATE_TYPE = "space.ag2.inbox"

RULES_FILE_NAME = "inbox-feed-rules.json"
CURSORS_FILE_NAME = "inbox-feed-cursors.json"
SENT_IDS_FILE_NAME = "inbox-feed-sent-ids.json"
SENT_LOG_FILE_NAME = "inbox-feed-sent-log.jsonl"

DEFAULT_RULES: List[Dict[str, str]] = [
    {"app": "github", "kind": "review_requested", "priority": "needs_you"},
    {"app": "github", "kind": "checks_failed", "priority": "needs_you"},
    {"app": "github", "kind": "mentioned", "priority": "fyi"},
    {"app": "github", "kind": "pr_merged", "priority": "fyi"},
    {"app": "gmail", "kind": "thread_needs_reply", "priority": "needs_you"},
    {"app": "gmail", "kind": "new_mail", "priority": "fyi"},
    {"app": "slack", "kind": "dm", "priority": "needs_you"},
    {"app": "slack", "kind": "mention", "priority": "needs_you"},
    {"app": "slack", "kind": "channel_activity", "priority": "fyi"},
    {"app": "*", "kind": "*", "priority": "fyi"},
]


# --- workspace paths ---------------------------------------------------------------


def state_dir(workspace: Path) -> Path:
    d = workspace / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def rules_path(workspace: Path) -> Path:
    return state_dir(workspace) / RULES_FILE_NAME


def cursors_path(workspace: Path) -> Path:
    return state_dir(workspace) / CURSORS_FILE_NAME


def sent_ids_path(workspace: Path) -> Path:
    return state_dir(workspace) / SENT_IDS_FILE_NAME


def sent_log_path(workspace: Path) -> Path:
    return state_dir(workspace) / SENT_LOG_FILE_NAME


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


# --- rules (editable, in the workspace — not in code) -------------------------------


def load_rules(workspace: Path) -> List[Dict[str, str]]:
    """The owner's/agent's own priority rules, or the built-in defaults when the
    workspace file is absent or unreadable — never an error, since the rules file is
    meant to be hand-edited and a bad edit shouldn't break polling."""
    path = rules_path(workspace)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return list(DEFAULT_RULES)
    if not isinstance(data, list):
        return list(DEFAULT_RULES)
    rules = [r for r in data if isinstance(r, dict) and "priority" in r]
    return rules or list(DEFAULT_RULES)


def init_rules(workspace: Path) -> bool:
    """Write the default rules file if none exists yet. Returns True if it wrote one."""
    path = rules_path(workspace)
    if path.exists():
        return False
    _atomic_write(path, json.dumps(DEFAULT_RULES, indent=2) + "\n")
    return True


def classify_priority(app: str, kind: str, rules: List[Dict[str, str]]) -> str:
    """First rule whose app and kind match (an exact value or '*') wins; 'fyi' if none do."""
    for rule in rules:
        rule_app = rule.get("app", "*")
        rule_kind = rule.get("kind", "*")
        if rule_app in (app, "*") and rule_kind in (kind, "*"):
            priority = rule.get("priority")
            if priority in ("needs_you", "fyi"):
                return priority
    return "fyi"


# --- mapping: one source payload -> one InboxItem -----------------------------------


def _one_line(text: Any, max_len: int = SUMMARY_MAX) -> str:
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return f"{flat[: max_len - 1].rstrip()}…" if len(flat) > max_len else flat


def _repo_full_name(raw: Dict[str, Any]) -> str:
    repo = raw.get("repository")
    if isinstance(repo, dict) and repo.get("full_name"):
        return str(repo["full_name"])
    url = raw.get("repository_url") or ""
    m = re.search(r"/repos/([^/]+/[^/]+)$", url)
    return m.group(1) if m else "unknown/unknown"


def map_github(
    raw: Dict[str, Any], kind: str, rules: List[Dict[str, str]], account: Optional[str] = None
) -> Dict[str, Any]:
    """One GitHub search/list hit (GITHUB_SEARCH_ISSUES_AND_PULL_REQUESTS,
    GITHUB_LIST_ISSUES_ASSIGNED_TO_THE_AUTHENTICATED_USER shape) -> one InboxItem.
    `kind` is supplied by the caller (it names which query produced this hit —
    review_requested, mentioned, checks_failed, pr_merged — the payload alone does
    not say which)."""
    full_name = _repo_full_name(raw)
    number = raw.get("number")
    external_id = f"github:{full_name}#{number}"
    user = raw.get("user") or {}
    pr = raw.get("pull_request") or {}
    merged = bool(pr.get("merged_at"))
    state = "resolved" if merged else ("closed" if raw.get("state") == "closed" else "open")
    created = raw.get("created_at") or ""
    updated = raw.get("updated_at") or created
    actor: Dict[str, Any] = {"id": user.get("login") or "", "display_name": user.get("login") or ""}
    if user.get("avatar_url"):
        actor["avatar_url"] = user["avatar_url"]
    source = {"app": "github"}
    if account:
        source["account"] = account
    return {
        "schema_version": SCHEMA_VERSION,
        "id": external_id,
        "external_id": external_id,
        "source": source,
        "actor": actor,
        "what": {"kind": kind, "summary": _one_line(raw.get("title"))},
        "link": raw.get("html_url") or "",
        "priority": classify_priority("github", kind, rules),
        "thread_key": raw.get("html_url") or external_id,
        "created_at": created,
        "updated_at": updated,
        "state": state,
    }


def map_gmail(
    raw: Dict[str, Any], kind: str, rules: List[Dict[str, str]], account: Optional[str] = None
) -> Dict[str, Any]:
    """One normalized Gmail thread summary -> one InboxItem. Expected input shape
    (reduced from GMAIL_FETCH_EMAILS / GMAIL_LIST_THREADS by the caller):
    {thread_id, from_email, from_name?, subject?, snippet?, last_message_ts (epoch ms
    or ISO8601), is_unread (bool)}."""
    thread_id = raw.get("thread_id") or ""
    external_id = f"gmail:{thread_id}"
    ts = raw.get("last_message_ts")
    created = updated = _as_iso(ts)
    source = {"app": "gmail"}
    if account:
        source["account"] = account
    return {
        "schema_version": SCHEMA_VERSION,
        "id": external_id,
        "external_id": external_id,
        "source": source,
        "actor": {
            "id": raw.get("from_email") or "",
            "display_name": raw.get("from_name") or raw.get("from_email") or "",
        },
        "what": {"kind": kind, "summary": _one_line(raw.get("subject") or raw.get("snippet"))},
        "link": f"https://mail.google.com/mail/u/0/#all/{thread_id}",
        "priority": classify_priority("gmail", kind, rules),
        "thread_key": external_id,
        "created_at": created,
        "updated_at": updated,
        "state": "open" if raw.get("is_unread") else "read",
    }


def map_slack(
    raw: Dict[str, Any], kind: str, rules: List[Dict[str, str]], account: Optional[str] = None
) -> Dict[str, Any]:
    """One normalized Slack message -> one InboxItem. Expected input shape (reduced
    from SLACK_GET_UNREAD_MESSAGES_FROM_USER / SLACK_LIST_UNREAD_CHANNEL_MESSAGES by
    the caller): {channel, ts, thread_ts?, user_id, user_name?, text?, permalink?}."""
    channel = raw.get("channel") or ""
    ts = raw.get("ts") or ""
    external_id = f"slack:{channel}:{ts}"
    thread_anchor = raw.get("thread_ts") or ts
    when = _slack_ts_to_iso(ts)
    source = {"app": "slack"}
    if account:
        source["account"] = account
    return {
        "schema_version": SCHEMA_VERSION,
        "id": external_id,
        "external_id": external_id,
        "source": source,
        "actor": {
            "id": raw.get("user_id") or "",
            "display_name": raw.get("user_name") or raw.get("user_id") or "",
        },
        "what": {"kind": kind, "summary": _one_line(raw.get("text"))},
        "link": raw.get("permalink") or "",
        "priority": classify_priority("slack", kind, rules),
        "thread_key": f"slack:{channel}:{thread_anchor}",
        "created_at": when,
        "updated_at": when,
        "state": "open",
    }


MAPPERS = {"github": map_github, "gmail": map_gmail, "slack": map_slack}


def _as_iso(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        ms = float(value)
        if ms > 1e12:  # looks like milliseconds
            ms /= 1000.0
        return datetime.fromtimestamp(ms, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return ""


def _slack_ts_to_iso(ts: str) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return ""


# --- dedupe: never send the same id twice -------------------------------------------


def load_sent_ids(workspace: Path) -> Dict[str, str]:
    try:
        data = json.loads(sent_ids_path(workspace).read_text())
    except (OSError, ValueError):
        return {}
    ids = data.get("ids") if isinstance(data, dict) else None
    return ids if isinstance(ids, dict) else {}


def prune_sent_ids(ids: Dict[str, str], now: Optional[datetime] = None) -> Dict[str, str]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=SENT_IDS_MAX_AGE_DAYS)
    kept: Dict[str, str] = {}
    for item_id, seen_at in ids.items():
        try:
            when = datetime.strptime(seen_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if when >= cutoff:
            kept[item_id] = seen_at
    return kept


def dedupe_new(
    items: List[Dict[str, Any]], sent_ids: Dict[str, str]
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Items not already in `sent_ids`, and the updated map (existing entries pruned,
    the new items' ids added) — the caller decides whether to persist it."""
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    pruned = prune_sent_ids(sent_ids, now)
    new_items = [i for i in items if i.get("id") not in pruned]
    updated = dict(pruned)
    for item in new_items:
        updated[item["id"]] = now_str
    return new_items, updated


# --- finding the owner's Inbox room ---------------------------------------------------


def find_inbox_room(rooms: List[Dict[str, Any]], owner_mxid: str) -> Dict[str, Any]:
    """`rooms`: [{room_id, joined (bool), state_events?: [{type, content}],
    topic?: str, name?: str}]. Picks by the task's own priority: a joined room whose
    `space.ag2.inbox` state names the owner; else its topic; else the name "Inbox".
    A matching room this agent is not joined to answers not_member, never found —
    the caller must ask to be invited and stop."""

    def names_owner(room: Dict[str, Any]) -> bool:
        for event in room.get("state_events") or []:
            if event.get("type") == INBOX_STATE_TYPE and (event.get("content") or {}).get(
                "owner"
            ) == owner_mxid:
                return True
        return False

    by_state = [r for r in rooms if names_owner(r)]
    by_topic = [r for r in rooms if TOPIC_MARKER in (r.get("topic") or "")]
    by_name = [r for r in rooms if (r.get("name") or "") == INBOX_ROOM_NAME]

    for candidates, matched_by in ((by_state, "state"), (by_topic, "topic"), (by_name, "name")):
        if not candidates:
            continue
        joined = [r for r in candidates if r.get("joined")]
        if joined:
            return {"status": "found", "room_id": joined[0]["room_id"], "matched_by": matched_by}
        return {"status": "not_member", "room_id": candidates[0]["room_id"], "matched_by": matched_by}
    return {"status": "not_found"}


# --- digest ---------------------------------------------------------------------------


def digest_text(items: List[Dict[str, Any]], hours: int) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    def recent(item: Dict[str, Any]) -> bool:
        stamp = item.get("updated_at") or item.get("created_at") or ""
        try:
            when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return False
        return when >= cutoff

    recent_items = [i for i in items if recent(i)]
    if not recent_items:
        return f"Nothing new in the last {hours}h."

    lines = [f"Inbox digest, last {hours}h ({len(recent_items)} item(s)):", ""]
    for priority, label in (("needs_you", "Needs you"), ("fyi", "FYI")):
        group = [i for i in recent_items if i.get("priority") == priority]
        if not group:
            continue
        lines.append(f"**{label}** ({len(group)})")
        for item in group:
            app = (item.get("source") or {}).get("app", "?")
            summary = (item.get("what") or {}).get("summary", "")
            link = item.get("link")
            link_str = link if isinstance(link, str) else (link or {}).get("room_id", "")
            lines.append(f"- [{app}] {summary} — {link_str}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def rotate_log(path: Path) -> None:
    try:
        if not path.exists() or path.stat().st_size <= LOG_MAX_BYTES:
            return
        tail = path.read_bytes()[-LOG_KEEP_BYTES:]
        _atomic_write(path, tail.decode("utf-8", "ignore"))
    except OSError:
        pass


# --- CLI --------------------------------------------------------------------------


def _read_json_file(path: str):
    text = sys.stdin.read() if path == "-" else Path(path).read_text()
    return json.loads(text)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    rules_p = sub.add_parser("rules")
    rules_p.add_argument("action", choices=["show", "init"])

    map_p = sub.add_parser("map")
    map_p.add_argument("--source", required=True, choices=list(MAPPERS))
    map_p.add_argument("--kind", required=True)
    map_p.add_argument("--file", required=True)
    map_p.add_argument("--account", default=None)

    dedupe_p = sub.add_parser("dedupe")
    dedupe_p.add_argument("--file", required=True)
    dedupe_p.add_argument("--live", action="store_true")

    find_room_p = sub.add_parser("find-room")
    find_room_p.add_argument("--file", required=True)
    find_room_p.add_argument("--owner", required=True)

    cursors_p = sub.add_parser("cursors")
    cursors_p.add_argument("action", choices=["get", "set"])
    cursors_p.add_argument("--source", required=True, choices=["github", "gmail", "slack"])
    cursors_p.add_argument("--value", default=None)
    cursors_p.add_argument("--live", action="store_true")

    log_sent_p = sub.add_parser("log-sent")
    log_sent_p.add_argument("--file", required=True)
    log_sent_p.add_argument("--live", action="store_true")

    digest_p = sub.add_parser("digest")
    digest_p.add_argument("--hours", type=int, default=12)
    digest_p.add_argument("--file", default=None)

    args = parser.parse_args(argv)
    workspace = Path(args.workspace) if args.workspace else resolve_workspace()

    if args.command == "rules":
        if args.action == "init":
            wrote = init_rules(workspace)
            print(json.dumps({"wrote_default": wrote, "path": str(rules_path(workspace))}))
        else:
            print(json.dumps(load_rules(workspace), indent=2))
        return 0

    if args.command == "map":
        rules = load_rules(workspace)
        raw_items = _read_json_file(args.file)
        if isinstance(raw_items, dict):
            raw_items = [raw_items]
        mapper = MAPPERS[args.source]
        mapped = [mapper(raw, args.kind, rules, args.account) for raw in raw_items]
        print(json.dumps(mapped, indent=2))
        return 0

    if args.command == "dedupe":
        items = _read_json_file(args.file)
        sent_ids = load_sent_ids(workspace)
        new_items, updated_ids = dedupe_new(items, sent_ids)
        if args.live:
            _atomic_write(sent_ids_path(workspace), json.dumps({"ids": updated_ids}))
        print(json.dumps({"new_items": new_items, "persisted": args.live}, indent=2))
        return 0

    if args.command == "find-room":
        rooms = _read_json_file(args.file)
        print(json.dumps(find_inbox_room(rooms, args.owner), indent=2))
        return 0

    if args.command == "cursors":
        path = cursors_path(workspace)
        try:
            cursors = json.loads(path.read_text())
        except (OSError, ValueError):
            cursors = {}
        if args.action == "get":
            print(json.dumps({"source": args.source, "value": cursors.get(args.source)}))
            return 0
        cursors[args.source] = args.value
        if args.live:
            _atomic_write(path, json.dumps(cursors))
        print(json.dumps({"source": args.source, "value": args.value, "persisted": args.live}))
        return 0

    if args.command == "log-sent":
        items = _read_json_file(args.file)
        if args.live:
            path = sent_log_path(workspace)
            rotate_log(path)
            with path.open("a") as fh:
                for item in items:
                    fh.write(json.dumps({"ts": time.time(), "item": item}) + "\n")
        print(json.dumps({"logged": len(items), "persisted": args.live}))
        return 0

    if args.command == "digest":
        if args.file:
            items = _read_json_file(args.file)
        else:
            items = []
            try:
                with sent_log_path(workspace).open() as fh:
                    for line in fh:
                        try:
                            items.append(json.loads(line)["item"])
                        except (ValueError, KeyError):
                            continue
            except OSError:
                pass
        print(digest_text(items, args.hours))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
