"""Slack channel thread replies the bot is party to — companion to `slack-bridge.py`.

A channel message reaches the bridge as an `app_mention` only when it @-mentions
the bot, so a plain reply in a thread the bot started or answered in was never
turned into a task. This module decides when such a reply is addressed to the
bot, and keeps the bounded record of threads the bot has posted into.

The decision is a pure function so its truth table is unit-tested directly; the
bridge resolves the bot's user id and the record lookup and gates on it.
Access-tier and allowlist checks are NOT made here: an addressed reply goes
through the same `_write_task` gate as a mention.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

# Subtypes a human thread reply can carry; joins, topic changes, edits and bot
# posts are never instructions.
_HUMAN_SUBTYPES = (None, "", "thread_broadcast", "file_share")
_CHANNEL_TYPES = ("channel", "group")

DEFAULT_MAX_ENTRIES = 2000
DEFAULT_MAX_AGE_S = 30 * 86400


def mentions_user(text: str, user_id: str) -> bool:
    """True when `text` carries Slack's `<@U…>` / `<@U…|name>` mention of `user_id`."""
    if not text or not user_id:
        return False
    return re.search(rf"<@{re.escape(user_id)}(?:\|[^>]*)?>", text) is not None


def is_addressed_thread_reply(event: dict, bot_user_id: str | None,
                              bot_posted_in_thread: bool) -> bool:
    """Return True iff a channel `message` event is a human reply in a thread
    the bot is party to, and is not already delivered as an `app_mention`.

    Party to the thread means the root is the bot's (`parent_user_id`, set by
    Slack on every thread reply) or the bot has posted in it
    (`bot_posted_in_thread`, from the bridge's record). Unknown bot identity
    fails closed: without it neither own-message nor mention filtering works.
    """
    if not bot_user_id:
        return False
    if event.get("channel_type") not in _CHANNEL_TYPES:
        return False
    if event.get("subtype") not in _HUMAN_SUBTYPES:
        return False
    if event.get("bot_id") or event.get("bot_profile"):
        return False
    user = event.get("user")
    if not user or user == bot_user_id:
        return False
    thread_ts = event.get("thread_ts")
    if not thread_ts or thread_ts == event.get("ts"):
        return False
    if mentions_user(event.get("text") or "", bot_user_id):
        return False
    return event.get("parent_user_id") == bot_user_id or bool(bot_posted_in_thread)


class BotThreadRecord:
    """Bounded, persisted set of (channel, thread root ts) the bot has posted into."""

    def __init__(self, path: Path, *, max_entries: int = DEFAULT_MAX_ENTRIES,
                 max_age_s: int = DEFAULT_MAX_AGE_S, clock=time.time):
        self._path = Path(path)
        self._max_entries = max_entries
        self._max_age_s = max_age_s
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, float] | None = None

    @staticmethod
    def _key(channel: str, thread_ts: str) -> str:
        return f"{channel}:{thread_ts}"

    def _load(self) -> dict[str, float]:
        if self._entries is None:
            try:
                data = json.loads(self._path.read_text())
                self._entries = {str(k): float(v) for k, v in data.items()} if isinstance(data, dict) else {}
            except (OSError, ValueError, TypeError, AttributeError):
                self._entries = {}
        return self._entries

    def _prune(self, entries: dict[str, float]) -> None:
        cutoff = self._clock() - self._max_age_s
        for k in [k for k, v in entries.items() if v < cutoff]:
            del entries[k]
        if len(entries) > self._max_entries:
            for k, _ in sorted(entries.items(), key=lambda kv: kv[1])[: len(entries) - self._max_entries]:
                del entries[k]

    def record(self, channel: str, thread_ts: str) -> None:
        if not channel or not thread_ts:
            return
        with self._lock:
            entries = self._load()
            entries[self._key(channel, thread_ts)] = self._clock()
            self._prune(entries)
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(entries))
                tmp.replace(self._path)
            except OSError as e:
                print(f"  [bot-threads] could not persist {self._path}: {e}", flush=True)

    def contains(self, channel: str, thread_ts: str) -> bool:
        if not channel or not thread_ts:
            return False
        with self._lock:
            ts = self._load().get(self._key(channel, thread_ts))
        return ts is not None and ts >= self._clock() - self._max_age_s
