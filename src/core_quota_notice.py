#!/usr/bin/env python3
"""The core's quota notice: the account card, unasked, in the owner's DM once the core is out of usage.

A core at its limit cannot answer, so nobody learns why it went quiet. Each core adapter sees its own
runtime's limit (the Codex seat from the app-server's turn errors, the Claude Code monitor from the
pane's limit banner) and hands it here. One card per limit episode: the next only after the limit
cleared (a turn that answered, the banner gone, or the reset time given has passed) and came back.
The episode lives in `<workspace>/state/core-quota-card.json`, so a restart does not send it again.

The card is sent by a command the adapter injects (the account card's `post --owner-dm --lead`), which
posts it to the owner's checked DM only. A card that could not be posted is tried again no sooner
than RETRY_S, unless the caller tells the limit with a notice of its own instead (`yield_to_notice`):
the Claude Code monitor's card replaces its paused notice, so the owner gets one message per limit.
Nothing here changes an account. English only.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

FILE = ("state", "core-quota-card.json")
RETRY_S = 60.0
SEND_TIMEOUT_S = 90.0
# The runtime's own words for when the limit ends ("try again at 3:05 PM", "resets in 2 hours"); only
# time-like characters of them are kept, so nothing else of the error reaches the DM.
_RESET = re.compile(r"\b(?:resets?(?: after| at| in| on)?|try again (?:at|in|after)|retry (?:at|in|after))\s+"
                    r"([\w :,./+-]{1,40})", re.IGNORECASE)
_TIMEISH = re.compile(r"[^\w :,./+-]")


def reset_from_text(text: str) -> str:
    """When the limit resets, as the runtime's words say it ("in 2 days 3 hours", "3:05 PM"); ''."""
    found = _RESET.search(str(text or ""))
    if not found:
        return ""
    said = " ".join(_TIMEISH.sub("", found.group(1)).split()).rstrip(" ,:./+-")
    said = re.split(r"\.\s|\s(?:or|and|to|for|if)\s", said, maxsplit=1)[0]
    return said[:40]


def reset_from_epoch(at, now: Optional[float] = None) -> str:
    """A reset moment (epoch seconds) in local words: "3:05 PM" today, "Nov 24, 3:05 PM" otherwise; ''."""
    try:
        at = float(at)
    except (TypeError, ValueError):
        return ""
    if at <= 0:
        return ""
    when, today = time.localtime(at), time.localtime(time.time() if now is None else now)
    clock = time.strftime("%I:%M %p", when).lstrip("0")
    if (when.tm_year, when.tm_yday) == (today.tm_year, today.tm_yday):
        return clock
    return "%s %d, %s" % (time.strftime("%b", when), when.tm_mday, clock)


def words(name: str, reset: str) -> str:
    return "%s has reached its usage limit%s, so I can't answer until then. Switch to another account to go on now." % (
        name, " (resets %s)" % reset if reset else " (it does not say when that resets)")


def card_sender(argv: Sequence[str], workspace, log: Callable[..., None] = print,
                run=subprocess.run) -> Callable[[str], bool]:
    """send(text) -> posted: runs the injected account card command with `text` leading the card."""
    def send(text: str) -> bool:
        cmd = [*argv, "--workspace", str(workspace), "post", "--owner-dm", "--retry", "--lead", text]
        try:
            r = run(cmd, capture_output=True, text=True, timeout=SEND_TIMEOUT_S, stdin=subprocess.DEVNULL)
            out = json.loads((r.stdout or "").strip().splitlines()[-1])
        except (OSError, subprocess.SubprocessError, ValueError, IndexError) as e:
            log("quota card not sent:", type(e).__name__)
            return False
        if not isinstance(out, dict) or out.get("status") != "posted":
            log("quota card not posted:", str((out or {}).get("why") or "no answer")[:200])
            return False
        return True
    return send


class QuotaNotice:
    """`send(text) -> bool` posts the account card led by `text`; `name` is the runtime as the owner reads it."""

    def __init__(self, workspace, name: str, send: Callable[[str], bool], log: Callable[..., None] = print,
                 now: Callable[[], float] = time.time):
        self.path = Path(workspace).joinpath(*FILE)
        self.name, self.send, self.log, self.now = name, send, log, now

    def load(self) -> dict:
        try:
            rec = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return rec if isinstance(rec, dict) else {}

    def save(self, rec: dict) -> bool:
        from local_record import write_whole
        try:
            write_whole(self.path, rec)
            return True
        except OSError as e:
            self.log("quota record not kept:", type(e).__name__)
            return False

    def carded(self) -> bool:
        """Whether this limit's notice is the account card, posted."""
        rec = self.load()
        return bool(rec.get("since") and rec.get("told") and rec.get("via") != "notice")

    def yield_to_notice(self) -> None:
        """The card could not be posted and the caller's own notice tells this limit instead: no
        card is sent for it later, so the owner gets one message per limit."""
        rec = self.load()
        if rec.get("since") and not rec.get("told"):
            self.save(dict(rec, told=True, via="notice"))

    def cleared(self) -> str:
        """The core answered (or the limit screen is gone): a later limit is a new card."""
        if not self.load().get("since"):
            return ""
        self.save({})
        return "quota-cleared"

    def hit(self, reset: str = "", reset_at=None) -> str:
        """The runtime said it is out of usage. 'quota-card' (posted to the owner's DM), 'quota-not-posted'
        (tried again later), or '' (told already this episode, or tried too recently)."""
        rec, at = self.load(), self.now()
        until = rec.get("reset_at")
        if rec.get("told") and until is not None and at >= float(until):
            rec = {}                     # the earlier limit reset and this is a new one
        if not rec.get("since"):
            rec = {"since": at}
        if reset_at is not None:
            rec["reset_at"] = float(reset_at)
        if reset and not rec.get("reset"):
            rec["reset"] = reset[:60]
        if rec.get("told") or at - float(rec.get("tried") or 0) < RETRY_S:
            self.save(rec)
            return ""
        # Tried is kept before the send: a stop mid-send waits RETRY_S rather than posting twice at once.
        rec["tried"] = at
        if not self.save(rec):
            return "quota-not-posted"
        try:
            posted = bool(self.send(words(self.name, str(rec.get("reset") or ""))))
        except Exception as e:  # noqa: BLE001 - a notice that cannot be sent never stops the core's adapter
            self.log("quota card not sent:", type(e).__name__)
            posted = False
        if not posted:
            return "quota-not-posted"
        self.save(dict(rec, told=True, via="card"))
        return "quota-card"
