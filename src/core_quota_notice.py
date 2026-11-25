#!/usr/bin/env python3
"""The core's quota notice: the account card, unasked, in the owner's DM once the core is out of usage.

A core at its limit cannot answer, so nobody learns why it went quiet. Each core adapter sees its own
runtime's limit (the Codex seat from the app-server's turn errors, the Claude Code monitor from the
pane's limit banner) and hands it here. One card when the limit hits, then one more for every owner
message that arrives while it lasts (`tasks`), so no message waits silently behind the limit; a
message is carded once. A new episode starts after the limit cleared (a turn that answered, the banner
gone, or the reset time given has passed) and came back.
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
CARDED_KEEP = 100   # the owner messages already answered with the card, this episode
SEND_TIMEOUT_S = 90.0
AGAIN_TRIES = 5     # a follow-up card that failed is tried this many times in all, RETRY_S doubling between
# The runtime's own words for when the limit ends ("try again at 3:05 PM", "resets in 2 hours"); only
# time-like characters of them are kept, so nothing else of the error reaches the DM.
_RESET = re.compile(r"\b(?:resets?(?: after| at| in| on)?|try again (?:at|in|after)|retry (?:at|in|after)"
                    r"|continue automatically at)\s+"
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


SIGNED_OUT = "signed_out"   # `why` of a signed-out core: the same card, every owner message, its own words


# The plan's limit is reached but the account can go on with usage credits, which cost money: said, never
# chosen here (the recovery card keeps purchases behind its step-up). VM 2026-11-24: the card read
# as "no quota" for an account that had credits.
CREDITS_LINE = ("This account still has usage credits: to use them, choose \"Switch to usage credits\" in the "
                "core's terminal on that computer.")
_CREDITS_MENU = re.compile(r"\bswitch to usage credits\b", re.I)


def credits_offered(pane: str = "", workspace=None) -> bool:
    """Whether the account can go on with usage credits: the CLI's limit menu offers "Switch to usage
    credits", or the provider's last answer (state/quota-state.json) lists `overage` among its upgrade
    paths. Read only; never raises."""
    if _CREDITS_MENU.search(pane or ""):
        return True
    if workspace is None:
        return False
    try:
        from workspace_default import status_read_path
        data = json.loads(status_read_path("quota-state.json", Path(workspace)).read_text(encoding="utf-8"))
    except (OSError, ValueError, ImportError):
        return False
    headers = data.get("headers") if isinstance(data, dict) and isinstance(data.get("headers"), dict) else {}
    paths = str(headers.get("anthropic-ratelimit-unified-upgrade-paths") or "")
    return "overage" in [p.strip().lower() for p in paths.split(",")]


def words(name: str, reset: str, why: str = "quota", credits: bool = False) -> str:
    if why == SIGNED_OUT:
        return "%s is signed out, so I can't answer until it's signed in again. Sign in or pick an account below." % name
    said = "%s has reached its usage limit%s, so I can't answer until then. Switch to another account to go on now." % (
        name, " (resets %s)" % reset if reset else " (it does not say when that resets)")
    return said + (" " + CREDITS_LINE if credits else "")


def card_sender(argv: Sequence[str], workspace, log: Callable[..., None] = print,
                run=subprocess.run) -> Callable[[str], bool]:
    """send(text) -> posted: runs the injected account card command with `text` leading the card.
    `signed_out`: the core is signed out, so the card offers sign-in, not Retry."""
    def send(text: str, signed_out: bool = False) -> bool:
        cmd = [*argv, "--workspace", str(workspace), "post", "--owner-dm",
               "--signed-out" if signed_out else "--retry", "--lead", text]
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
                 now: Callable[[], float] = time.time, runtime: str = "",
                 account: Optional[Callable[[], str]] = None):
        self.path = Path(workspace).joinpath(*FILE)
        self.workspace = Path(workspace)
        self.name, self.send, self.log, self.now = name, send, log, now
        # With `runtime` ("codex" | "claude"), the limit is also recorded in core_account_health.
        self.runtime, self.account = runtime, account

    def health(self, state: str, resets_at=None) -> None:
        if not self.runtime:
            return
        try:
            import core_account_health
            account = ""
            if self.account is not None:
                account = self.account() or ""
            if state == "ok":
                core_account_health.mark_ok(self.workspace, self.runtime, account, now=self.now(), log=self.log)
            else:
                core_account_health.mark(self.workspace, state, self.runtime, account, resets_at,
                                         now=self.now(), log=self.log)
        except Exception as e:  # noqa: BLE001 - advisory: the health record never stops the notice
            self.log("account health not kept:", type(e).__name__)

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

    def carded(self, why: str = "quota") -> bool:
        """Whether this limit's (or, with `why` SIGNED_OUT, this sign-out's) notice is the account card, posted."""
        rec = self.load()
        return bool(rec.get("since") and rec.get("told") and rec.get("via") != "notice"
                    and (rec.get("why") or "quota") == why)

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
        self.health("ok")
        return "quota-cleared"

    def _send(self, rec: dict) -> bool:
        why = rec.get("why") or "quota"
        text = words(self.name, str(rec.get("reset") or ""), why, bool(rec.get("credits")))
        return bool(self.send(text, signed_out=True) if why == SIGNED_OUT else self.send(text))

    def hit(self, reset: str = "", reset_at=None, tasks: Sequence[str] = (), why: str = "quota",
            credits: bool = False) -> str:
        """The runtime said it is out of usage (`why` SIGNED_OUT: it is signed out, told the same way). 'quota-card' (posted to the owner's DM), 'quota-not-posted'
        (tried again later), or '' (told already this episode, or tried too recently). `tasks`: the
        owner's messages now waiting on the limit; each gets the card once ('quota-card-again')."""
        rec, at = self.load(), self.now()
        if rec.get("name") and rec.get("name") != self.name:
            rec = {}                     # another runtime's limit (the core switched): this one is new
        if rec.get("since") and (rec.get("why") or "quota") != why:
            rec = {}                     # signed out after a limit, or the other way: a new episode
        until = rec.get("reset_at")
        if rec.get("told") and until is not None and at >= float(until):
            rec = {}                     # the earlier limit reset and this is a new one
        if not rec.get("since"):
            rec = {"since": at}
        rec["name"] = self.name
        rec["why"] = why
        if reset_at is not None:
            rec["reset_at"] = float(reset_at)
        if reset and not rec.get("reset"):
            rec["reset"] = reset[:60]
        if credits:
            rec["credits"] = True        # the account can go on with usage credits: the card says how
        self.health(SIGNED_OUT if why == SIGNED_OUT else "quota", rec.get("reset_at"))
        if rec.get("told"):
            return self.again(rec, [str(t) for t in tasks if t])
        if at - float(rec.get("tried") or 0) < RETRY_S:
            self.save(rec)
            return ""
        # Tried is kept before the send: a stop mid-send waits RETRY_S rather than posting twice at once.
        rec["tried"] = at
        if not self.save(rec):
            return "quota-not-posted"
        try:
            posted = self._send(rec)
        except Exception as e:  # noqa: BLE001 - a notice that cannot be sent never stops the core's adapter
            self.log("quota card not sent:", type(e).__name__)
            posted = False
        if not posted:
            return "quota-not-posted"
        # The messages waiting now are answered by this card.
        self.save(dict(rec, told=True, via="card", carded=[str(t) for t in tasks if t][-CARDED_KEEP:]))
        return "quota-card"

    def again(self, rec: dict, tasks: list) -> str:
        """The card once more for each owner message that arrived since: told, never left to wait."""
        carded = [t for t in rec.get("carded") or [] if isinstance(t, str)]
        fresh = [t for t in tasks if t not in carded]
        failed = rec.get("again_failed") if isinstance(rec.get("again_failed"), dict) else {}
        if rec.get("via") == "notice" or not fresh or self.now() < float(failed.get("next") or 0):
            self.save(rec)
            return ""
        # Recorded before the send: a stop mid-send never posts it twice for the same message.
        done = dict(rec, carded=(carded + fresh)[-CARDED_KEEP:])
        done.pop("again_failed", None)
        self.save(done)
        try:
            posted = self._send(rec)
        except Exception as e:  # noqa: BLE001 - a notice that cannot be sent never stops the core's adapter
            self.log("quota card not sent:", type(e).__name__)
            posted = False
        if posted:
            return "quota-card-again"
        tries = int(failed.get("n") or 0) + 1
        if tries < AGAIN_TRIES:   # a confirmed failure: the messages stay uncarded and are tried again later
            self.save(dict(rec, again_failed={"n": tries, "next": self.now() + RETRY_S * 2 ** (tries - 1)}))
        return "quota-not-posted"


def owner_waiting(workspace) -> list:
    """Ids of the owner's messages waiting in the core's queue (`access_tier: owner`)."""
    out = []
    try:
        import task_queue
        files = task_queue.pending_files(Path(workspace))
    except Exception:  # noqa: BLE001 - no queue read: nobody to card
        return out
    for p in files:
        if is_owner_task(p):
            out.append(p.name[:-4] if p.name.endswith(".txt") else p.name)
    return out


def is_owner_task(path) -> bool:
    """The task's own header says the owner sent it."""
    try:
        with open(path, encoding="utf-8") as f:
            for _, line in zip(range(80), f):
                if line.startswith("access_tier:"):
                    return line.split(":", 1)[1].strip() == "owner"
    except OSError:
        pass
    return False
