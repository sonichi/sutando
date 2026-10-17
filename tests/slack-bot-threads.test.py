#!/usr/bin/env python3
"""Truth table + record bounds for src/slack_bot_threads.py (#5549).

Run: python3 tests/slack-bot-threads.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from slack_bot_threads import BotThreadRecord, is_addressed_thread_reply, mentions_user  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("  ok  " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


BOT = "UBOT"
BASE = {"channel": "C1", "channel_type": "channel", "user": "U1", "text": "ok",
        "ts": "2.0", "thread_ts": "1.0", "parent_user_id": BOT}


def ev(**k):
    e = dict(BASE)
    for key, v in k.items():
        if v is None:
            e.pop(key, None)
        else:
            e[key] = v
    return e


check("bot-root reply → addressed", is_addressed_thread_reply(ev(), BOT, False))
check("private channel (group) → addressed", is_addressed_thread_reply(ev(channel_type="group"), BOT, False))
check("thread_broadcast → addressed", is_addressed_thread_reply(ev(subtype="thread_broadcast"), BOT, False))
check("bot posted in human-rooted thread → addressed",
      is_addressed_thread_reply(ev(parent_user_id="U9"), BOT, True))
check("human-rooted thread, bot absent → not", not is_addressed_thread_reply(ev(parent_user_id="U9"), BOT, False))
check("unknown bot id → fail closed", not is_addressed_thread_reply(ev(), None, True))
check("DM channel_type → not (DM path owns it)", not is_addressed_thread_reply(ev(channel_type="im"), BOT, True))
check("top-level (no thread_ts) → not", not is_addressed_thread_reply(ev(thread_ts=None), BOT, True))
check("thread root itself (ts == thread_ts) → not", not is_addressed_thread_reply(ev(ts="1.0"), BOT, True))
check("own message → not", not is_addressed_thread_reply(ev(user=BOT), BOT, True))
check("bot_id set → not", not is_addressed_thread_reply(ev(bot_id="B1"), BOT, True))
check("bot_profile set → not", not is_addressed_thread_reply(ev(bot_profile={"id": "B"}), BOT, True))
check("no user → not", not is_addressed_thread_reply(ev(user=None), BOT, True))
check("edit subtype → not", not is_addressed_thread_reply(ev(subtype="message_changed"), BOT, True))
check("join subtype → not", not is_addressed_thread_reply(ev(subtype="channel_join"), BOT, True))
check("mention of bot → not (app_mention owns it)",
      not is_addressed_thread_reply(ev(text=f"hi <@{BOT}>"), BOT, True))
check("mention w/ label form detected", mentions_user(f"<@{BOT}|sutando> x", BOT))
check("mention of someone else still addressed",
      is_addressed_thread_reply(ev(text="<@U7> see this"), BOT, False))
check("prefix id is not a mention", not mentions_user("<@UBOTX>", BOT))

d = Path(tempfile.mkdtemp(prefix="bot-threads-"))
now = [1000.0]
rec = BotThreadRecord(d / "state" / "t.json", max_entries=3, max_age_s=100, clock=lambda: now[0])
rec.record("C1", "1.0")
check("recorded thread found", rec.contains("C1", "1.0"))
check("other channel same ts not found", not rec.contains("C2", "1.0"))
check("empty args ignored", not rec.contains("", "") and rec.record("", "1") is None)
rec2 = BotThreadRecord(d / "state" / "t.json", clock=lambda: now[0])
check("persisted across instances", rec2.contains("C1", "1.0"))
for i in range(5):
    now[0] += 1
    rec.record("C1", f"{i + 10}.0")
check("capped to max_entries (oldest dropped)", not rec.contains("C1", "1.0") and rec.contains("C1", "14.0"))
now[0] += 200
check("aged-out entry not found", not rec.contains("C1", "14.0"))
(d / "bad.json").write_text("{not json")
check("corrupt file → empty, no raise", not BotThreadRecord(d / "bad.json").contains("C1", "1.0"))

if failures:
    print(f"\nFAIL — {len(failures)}: {failures}")
    sys.exit(1)
print("\nPASS — slack_bot_threads")
