#!/usr/bin/env python3
"""Behavioral regression for Slack mention context and identity prefetch.

Set SUTANDO_TEST_SOURCE_ROOT to run this test against another checkout.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch


TEST_REPO = Path(__file__).resolve().parent.parent
SOURCE_REPO = Path(os.environ.get("SUTANDO_TEST_SOURCE_ROOT", TEST_REPO))
sys.path.insert(0, str(SOURCE_REPO / "src"))

USERS = {
    "UCLIENT": "client",
    "URUI": "rui_support",
    "UAGENT": "sutando_agent",
    "UROOT": "root",
    "UTEAM": "team_member",
}
FAKE_GITHUB_TOKEN = "ghp_" + ("A" * 36)


class _FakeClient:
    def __init__(self):
        self.history_handler = lambda **_kwargs: {"ok": True, "messages": []}
        self.replies_handler = lambda **_kwargs: {"ok": True, "messages": []}
        self.reply_calls = []

    def chat_postMessage(self, **_kwargs):
        return {"ok": True}

    def conversations_history(self, **kwargs):
        return self.history_handler(**kwargs)

    def conversations_replies(self, **kwargs):
        self.reply_calls.append(kwargs)
        return self.replies_handler(**kwargs)

    def users_info(self, user):
        name = USERS.get(user, user)
        return {"user": {"name": name, "profile": {"display_name": name}}}


class _FakeApp:
    def __init__(self, token=None):
        self.client = _FakeClient()

    def _decorator(self, *_args, **_kwargs):
        return lambda fn: fn

    event = message = command = action = shortcut = view = _decorator


_bolt = types.ModuleType("slack_bolt")
_bolt.App = _FakeApp
sys.modules["slack_bolt"] = _bolt
_adapter = types.ModuleType("slack_bolt.adapter")
_socket = types.ModuleType("slack_bolt.adapter.socket_mode")
_socket.SocketModeHandler = type(
    "SocketModeHandler", (), {"__init__": lambda self, *_args, **_kwargs: None}
)
sys.modules["slack_bolt.adapter"] = _adapter
sys.modules["slack_bolt.adapter.socket_mode"] = _socket

tmp = Path(tempfile.mkdtemp(prefix="sutando-slack-context-test-"))
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-not-real"
os.environ["SLACK_APP_TOKEN"] = "xapp-test-not-real"
os.environ["CLAUDE_CONFIG_DIR"] = str(tmp / "claude")

spec = importlib.util.spec_from_file_location(
    "slackbridge_context_test", SOURCE_REPO / "src" / "slack-bridge.py"
)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
bridge.TASKS_DIR = tmp / "tasks"
bridge.TASKS_DIR.mkdir(parents=True, exist_ok=True)

failures = []


def check(name, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + name + (f": {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def write_owner_task(event, text):
    captured = {}

    def _write(task_file, content, task_id, pending_info):
        task_file.write_text(content)
        captured["body"] = content

    with patch.object(bridge, "load_allowed", lambda: {event["user"]}), \
         patch.object(bridge, "_ensure_tier_map_seeded", lambda: True), \
         patch.object(bridge, "load_tier_map", lambda: {event["user"]: "owner"}), \
         patch.object(bridge, "write_owner_activity", lambda *_args, **_kwargs: None), \
         patch.object(bridge, "_write_routed_task", _write):
        task_id = bridge._write_task(event, "Slack mention", text, USERS[event["user"]])
    return task_id, captured.get("body", "")


# The real failure shape: a support engineer speaks in a customer-facing
# channel and mentions Sutando as the supporting agent.
bridge.app.client.history_handler = lambda **_kwargs: {
    "ok": True,
    "messages": [
        {
            "ts": "3001.000",
            "user": "URUI",
            "text": "I support your company. My <@UAGENT> is standing by.",
        },
        {
            "ts": "3000.000",
            "user": "UCLIENT",
            "text": (
                "We are evaluating Sutando for our team. Temporary token: "
                + FAKE_GITHUB_TOKEN
            ),
        },
    ],
}
top_event = {
    "user": "URUI",
    "channel": "C_CUSTOMER",
    "channel_type": "channel",
    "ts": "3002.000",
    "text": "<@UAGENT> How was your use of Sutando so far? <@UAGENT>",
}
top_task_id, top_body = write_owner_task(
    top_event, "How was your use of Sutando so far?"
)
check("top-level mention writes a task", bool(top_task_id))
check(
    "top-level mention embeds preceding customer/support context",
    "@client (UCLIENT): We are evaluating Sutando for our team." in top_body
    and "@rui_support (URUI): I support your company." in top_body,
)
check(
    "trigger identity map resolves the mentioned supporting agent",
    top_body.count("mentioned: @sutando_agent (UAGENT)") == 1,
)
check(
    "inline context mention is identity-resolved without dropping its ID",
    "@sutando_agent (<@UAGENT>) is standing by" in top_body,
)
check(
    "prefetched context is secret-redacted with the security notice retained",
    FAKE_GITHUB_TOKEN not in top_body
    and "[REDACTED-GitHub Token]" in top_body
    and "===SUTANDO SECURITY NOTICE" in top_body,
)


# A >20-message thread must retain the root and the recent tail closest to the
# trigger, not only Slack's first (oldest) page.
page_one = [{"ts": "4000.000", "user": "UROOT", "text": "thread root"}] + [
    {"ts": f"4000.{index:03d}", "user": "UTEAM", "text": f"message {index}"}
    for index in range(1, 15)
]
page_two = [
    {"ts": f"4000.{index:03d}", "user": "UTEAM", "text": f"message {index}"}
    for index in range(15, 25)
] + [{"ts": "4001.000", "user": "URUI", "text": "<@UAGENT> summarize"}]


def replies_handler(**kwargs):
    if kwargs.get("cursor") == "page-2":
        return {"ok": True, "messages": page_two, "response_metadata": {}}
    return {
        "ok": True,
        "messages": page_one,
        "response_metadata": {"next_cursor": "page-2"},
    }


bridge.app.client.reply_calls.clear()
bridge.app.client.replies_handler = replies_handler
thread_event = {
    "user": "URUI",
    "channel": "C_CUSTOMER",
    "channel_type": "channel",
    "thread_ts": "4000.000",
    "ts": "4001.000",
    "text": "<@UAGENT> summarize",
}
thread_task_id, thread_body = write_owner_task(thread_event, "summarize")
check("long threaded mention writes a task", bool(thread_task_id))
check(
    "long thread pagination fetches the next page",
    len(bridge.app.client.reply_calls) == 2
    and bridge.app.client.reply_calls[1].get("cursor") == "page-2",
)
check(
    "long thread keeps root and immediate preceding context",
    "@root (UROOT): thread root" in thread_body
    and "@team_member (UTEAM): message 24" in thread_body,
)
check(
    "long thread drops old middle messages to enforce the output bound",
    "@team_member (UTEAM): message 1\n" not in thread_body,
)
check(
    "triggering mention is not duplicated in prefetched context",
    thread_body.count("summarize") == 1,
)


# Slack read failures must be explicit rather than silently inviting a role
# guess from the isolated mention.
def history_failure(**_kwargs):
    raise RuntimeError("simulated Slack history failure")


bridge.app.client.history_handler = history_failure
# Fresh ts: reusing top_event's ts would be replay-dropped by already_admitted.
failure_event = dict(top_event, ts="3003.000")
failure_task_id, failure_body = write_owner_task(failure_event, "Who is the client?")
check("history failure still writes the task", bool(failure_task_id))
check(
    "history failure tells the agent not to assume client/support roles",
    "Slack channel context unavailable; do not assume client/support roles"
    in failure_body,
)

if hasattr(bridge, "_slack_context_note"):
    with patch.object(bridge, "_resolve_username", lambda _user_id: None):
        check(
            "unresolved inline mentions retain the original Slack ID",
            bridge._resolve_slack_mentions("<@UUNKNOWN>") == "<@UUNKNOWN>",
        )

    empty_note, empty_secret_types = bridge._format_slack_context(
        [{"ts": "1", "user": "UCLIENT", "text": ""}], "channel"
    )
    check(
        "empty fetched messages produce no fabricated context",
        empty_note == "" and not empty_secret_types,
    )
    missing_note, _missing_types = bridge._slack_context_note(
        {"channel_type": "channel", "channel": "C_CUSTOMER"}
    )
    check(
        "missing event timestamp produces the explicit unavailable note",
        "Slack channel context unavailable" in missing_note,
    )

    bridge.app.client.replies_handler = lambda **_kwargs: {
        "ok": False,
        "messages": [],
    }
    failed_reply_note, _failed_reply_types = bridge._slack_context_note(thread_event)
    check(
        "Slack replies ok=false produces the explicit unavailable note",
        "Slack channel context unavailable" in failed_reply_note,
    )

    bridge.app.client.replies_handler = lambda **_kwargs: {
        "ok": True,
        "messages": [],
        "response_metadata": {},
    }
    empty_reply_note, _empty_reply_types = bridge._slack_context_note(thread_event)
    check(
        "empty thread response produces the explicit unavailable note",
        "Slack channel context unavailable" in empty_reply_note,
    )

    bridge.app.client.replies_handler = lambda **_kwargs: {
        "ok": True,
        "messages": page_one,
        "response_metadata": {"next_cursor": "still-more"},
    }
    with patch.object(bridge, "_THREAD_CONTEXT_MAX_PAGES", 1):
        truncated_note, _truncated_types = bridge._slack_context_note(thread_event)
    check(
        "page cap marks thread context as truncated",
        "truncated before the triggering reply" in truncated_note,
    )

    bridge.app.client.history_handler = lambda **_kwargs: {
        "ok": False,
        "messages": [],
    }
    failed_history_note, _failed_history_types = bridge._slack_context_note(top_event)
    check(
        "Slack history ok=false produces the explicit unavailable note",
        "Slack channel context unavailable" in failed_history_note,
    )
else:
    check("context helper exists on the patched bridge", False)

if failures:
    print(f"\nFAIL — {len(failures)} check(s): {', '.join(failures)}")
    raise SystemExit(1)

print("\nPASS — Slack mention context/identity prefetch")
