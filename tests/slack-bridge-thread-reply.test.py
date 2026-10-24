#!/usr/bin/env python3
"""Behavioral tests for #5549: a plain (no @mention) reply in a channel thread the
bot started or was @-mentioned in becomes a task, gated by the same allowlist/tier checks as
a mention; a human-rooted thread counts only once the bot was @-mentioned in it; replies in other threads, the bot's own posts and mentions are ignored.

Run: python3 tests/slack-bridge-thread-reply.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
BOT = "UBOT"

_tmp = tempfile.mkdtemp(prefix="sutando-thread-reply-test-")
os.environ["SUTANDO_WORKSPACE"] = _tmp
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-not-real"
os.environ["SLACK_APP_TOKEN"] = "xapp-test-not-real"
os.environ["CLAUDE_CONFIG_DIR"] = str(Path(_tmp) / "claude")
_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "slack"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / "access.json").write_text('{"allowFrom": []}')


class _FakeApp:
    def __init__(self, token=None):
        self.posts: list[dict] = []
        self.auth_calls = 0

        def _post(**k):
            self.posts.append(k)
            return {"ok": True, "ts": f"9{len(self.posts)}.000"}

        def _auth():
            self.auth_calls += 1
            return {"ok": True, "user_id": BOT}

        self.client = types.SimpleNamespace(
            chat_postMessage=_post,
            auth_test=_auth,
            conversations_replies=lambda **k: {"ok": True, "messages": []},
            conversations_history=lambda **k: {"ok": True, "messages": []},
            users_info=lambda **k: {"user": {"name": "n", "profile": {}}},
        )

    def _decorator(self, *a, **k):
        return lambda fn: fn

    event = message = command = action = shortcut = view = _decorator


_bolt = types.ModuleType("slack_bolt")
_bolt.App = _FakeApp
sys.modules["slack_bolt"] = _bolt
_adapter = types.ModuleType("slack_bolt.adapter")
_socket = types.ModuleType("slack_bolt.adapter.socket_mode")
_socket.SocketModeHandler = type("SocketModeHandler", (), {"__init__": lambda self, *a, **k: None})
sys.modules["slack_bolt.adapter"] = _adapter
sys.modules["slack_bolt.adapter.socket_mode"] = _socket

spec = importlib.util.spec_from_file_location("slackbridge_tr", REPO / "src" / "slack-bridge.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
TASKS_DIR = mod.TASKS_DIR

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


_seq = [0]


def deliver(event: dict, tiers: dict) -> list[Path]:
    """Feed one `message` event through handle_message; return task files it wrote."""
    _seq[0] += 1
    event = {"channel": "CCHAN", "channel_type": "channel", "team": "T1",
             "ts": f"2000.{_seq[0]:03d}", **event}
    before = set(TASKS_DIR.glob("*.txt"))
    with patch.object(mod, "load_allowed", lambda: set(tiers)), \
         patch.object(mod, "_ensure_tier_map_seeded", lambda: True), \
         patch.object(mod, "load_tier_map", lambda: dict(tiers)), \
         patch.object(mod, "write_owner_activity", lambda *a, **k: None):
        mod.handle_message(event, None)
    return sorted(set(TASKS_DIR.glob("*.txt")) - before)


TIERS = {"U_OWNER": "owner", "U_TEAM": "team"}

# 1. Owner replies, no mention, in a thread whose root is the bot's own post.
new = deliver({"user": "U_OWNER", "text": "yes, go ahead", "thread_ts": "1000.000",
               "parent_user_id": BOT}, TIERS)
check("owner reply in bot-root thread → one task", len(new) == 1, str(new))
if new:
    body = new[0].read_text()
    check("owner reply → access_tier: owner", "access_tier: owner" in body, body[:400])
    check("owner reply → reply text in task", "yes, go ahead" in body)
    check("owner reply → result routed into that thread",
          mod.pending_replies.get(new[0].stem, {}).get("thread_ts") == "1000.000",
          str(mod.pending_replies.get(new[0].stem)))

# 2. A team-tier sender keeps their own tier (never escalated).
new = deliver({"user": "U_TEAM", "text": "can you also check X", "thread_ts": "1000.000",
               "parent_user_id": BOT}, TIERS)
check("team reply in bot-root thread → task", len(new) == 1, str(new))
if new:
    check("team reply → access_tier: team (not owner)",
          "access_tier: team" in new[0].read_text())

# 3. Thread the bot never joined → ignored.
new = deliver({"user": "U_OWNER", "text": "chatting with a colleague",
               "thread_ts": "1500.000", "parent_user_id": "U_SOMEONE"}, TIERS)
check("reply in thread bot never joined → no task", new == [], str(new))

# 4. Human-rooted thread: a bot post alone does NOT make later unmentioned replies tasks.
mod.app.posts.clear()
mod._send_reply("CCHAN", "1600.000", "here is your answer", task_id="task-x")
check("bot post into a thread is not recorded", not mod.BOT_THREADS.contains("CCHAN", "1600.000"))
new = deliver({"user": "U_OWNER", "text": "thanks, one more thing",
               "thread_ts": "1600.000", "parent_user_id": "U_OWNER"}, TIERS)
check("human-rooted thread, bot posted but never mentioned → no task", new == [], str(new))
mod._send_reply("CCHAN", None, "Question for the owner?", task_id="task-y")
check("top-level bot post is not recorded",
      not mod.BOT_THREADS.contains("CCHAN", "9%d.000" % len(mod.app.posts)))

# 5. An @mention in a human-rooted thread records it; later unmentioned replies become tasks.


def mention(event: dict, tiers: dict = TIERS) -> list[Path]:
    _seq[0] += 1
    event = {"channel": "CCHAN", "channel_type": "channel", "team": "T1",
             "ts": f"3000.{_seq[0]:03d}", **event}
    before = set(TASKS_DIR.glob("*.txt"))
    with patch.object(mod, "load_allowed", lambda: set(tiers)), \
         patch.object(mod, "_ensure_tier_map_seeded", lambda: True), \
         patch.object(mod, "load_tier_map", lambda: dict(tiers)), \
         patch.object(mod, "write_owner_activity", lambda *a, **k: None):
        mod.handle_mention(event, None)
    return sorted(set(TASKS_DIR.glob("*.txt")) - before)


new = mention({"user": "U_OWNER", "text": f"<@{BOT}> look at this", "thread_ts": "1800.000"})
check("mention in thread → task", len(new) == 1, str(new))
check("mention records its thread root", mod.BOT_THREADS.contains("CCHAN", "1800.000"))
new = deliver({"user": "U_OWNER", "text": "and also this", "thread_ts": "1800.000",
               "parent_user_id": "U_OWNER"}, TIERS)
check("unmentioned reply in mentioned human-rooted thread → task", len(new) == 1, str(new))
disk = json.loads((Path(_tmp) / "state" / "slack-bot-threads.json").read_text())
check("record persisted under workspace state/", "CCHAN:1800.000" in disk, str(disk))
new = deliver({"user": "U_OWNER", "text": "other thread", "thread_ts": "1801.000",
               "parent_user_id": "U_OWNER"}, TIERS)
check("different human-rooted thread, no mention → no task", new == [], str(new))

# A top-level mention is the root of its own thread.
top = f"3999.{_seq[0] + 1:03d}"
mention({"user": "U_OWNER", "text": f"<@{BOT}> start", "ts": top})
check("top-level mention records its own ts", mod.BOT_THREADS.contains("CCHAN", top), top)
# A mention from a non-allowlisted sender writes no task and records nothing.
new = mention({"user": "U_STRANGER", "text": f"<@{BOT}> hi", "thread_ts": "1900.000"})
check("non-allowlisted mention → no task, thread not recorded",
      new == [] and not mod.BOT_THREADS.contains("CCHAN", "1900.000"), str(new))
# Mentions in a DM-style channel are never recorded.
mod._note_mention_thread({"channel": "D123", "ts": "5.0"})
check("DM channel not recorded", not mod.BOT_THREADS.contains("D123", "5.0"))

# 6. The bot's own message / other bots → ignored (no feedback loop).
new = deliver({"user": BOT, "text": "my own reply", "thread_ts": "1000.000",
               "parent_user_id": BOT}, TIERS)
check("bot's own message → no task", new == [], str(new))
new = deliver({"user": "U_OWNER", "bot_id": "B_OTHER", "text": "other bot",
               "thread_ts": "1000.000", "parent_user_id": BOT}, TIERS)
check("another bot's message → no task", new == [], str(new))
new = deliver({"subtype": "bot_message", "bot_id": "B_OTHER", "text": "x",
               "thread_ts": "1000.000", "parent_user_id": BOT}, TIERS)
check("bot_message subtype → no task", new == [], str(new))

# 7. A reply that @mentions the bot is left to app_mention (no double task).
new = deliver({"user": "U_OWNER", "text": f"<@{BOT}> do it", "thread_ts": "1000.000",
               "parent_user_id": BOT}, TIERS)
check("mentioning reply → not handled here (app_mention owns it)", new == [], str(new))

# 8. Top-level channel message without mention → still ignored.
new = deliver({"user": "U_OWNER", "text": "general chatter"}, TIERS)
check("top-level channel message w/o mention → no task", new == [], str(new))

# 9. Non-allowlisted sender in a bot thread → dropped silently (no ack spam).
mod.app.posts.clear()
mod._not_allowlisted_ack_at.clear()  # an earlier mention started the per-sender cooldown
new = deliver({"user": "U_STRANGER", "text": "hi all", "thread_ts": "1000.000",
               "parent_user_id": BOT}, TIERS)
check("non-allowlisted reply → no task", new == [], str(new))
check("non-allowlisted reply → no not-allowlisted ack posted",
      mod.app.posts == [], str(mod.app.posts))

# 10. Bot identity resolved once and cached.
check("auth.test called at most once", mod.app.auth_calls == 1,
      str(mod.app.auth_calls))

# 11. auth.test failure → fail closed (no task, nothing escapes), bounded, then retries.
_clock = [5000.0]
_boom = [0]


def _auth_fail():
    _boom[0] += 1
    raise RuntimeError("slack down")


mod._bot_user_id_cache.clear()
_real_auth = mod.app.client.auth_test
mod.app.client.auth_test = _auth_fail
_ev = {"user": "U_OWNER", "text": "still there?", "thread_ts": "1000.000", "parent_user_id": BOT}
with patch.object(mod.time, "monotonic", lambda: _clock[0]):
    try:
        new = deliver(dict(_ev), TIERS)
        raised = False
    except Exception:
        new, raised = [], True
    check("auth.test raising → handle_message does not raise", not raised)
    check("auth.test raising → no task written", new == [], str(new))
    deliver(dict(_ev), TIERS)
    deliver(dict(_ev), TIERS)
    check("auth.test outage → repeat replies bounded to one call", _boom[0] == 1, str(_boom[0]))
    _clock[0] += mod._BOT_ID_RETRY_S + 1
    deliver(dict(_ev), TIERS)
    check("auth.test retried after the negative cache expires", _boom[0] == 2, str(_boom[0]))
    mod.app.client.auth_test = _real_auth
    _clock[0] += mod._BOT_ID_RETRY_S + 1
    new = deliver(dict(_ev), TIERS)
    check("auth.test recovers → reply becomes a task", len(new) == 1, str(new))

# 12. Recording the thread fails → the mention still becomes a task, nothing raises.
with patch.object(mod.BOT_THREADS, "record", side_effect=RuntimeError("disk")):
    try:
        new = mention({"user": "U_OWNER", "text": f"<@{BOT}> again", "thread_ts": "1700.000"})
        raised = False
    except Exception:
        new, raised = [], True
check("record failure → handle_mention does not raise", not raised)
check("record failure → task still written", len(new) == 1, str(new))

if failures:
    print(f"\nFAIL — {len(failures)} check(s): {failures}")
    sys.exit(1)
print("\nPASS — slack thread-reply behavioral tests")
