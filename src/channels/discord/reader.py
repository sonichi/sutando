#!/usr/bin/env python3
"""Shared Discord message fetch + rendering — the single implementation behind
both reader CLIs.

`discord-read.py` (full-featured reader) and `read_discord_channel.py` (the
contextNotFrom-gated reader) each carried their own fetch and rendering. The
gated reader's copy was the weak one: it rendered `m.get("content")` only, so a
forwarded message printed as a blank line — the exact defect class measured on
2026-07-31 (two forwards holding the only record of a live failure rendered
empty and were nearly deleted on the strength of that blank output). One
renderer means one place where forwards, reply context, and clipping are right.

The CLIs re-bind these names as module globals so tests can keep patching them
per-CLI (`dr._render = ...`); the implementations live here.
"""
from __future__ import annotations

import urllib.parse
import urllib.request

from channels.discord.http import request_json
from chat_secret_filter import filter_chat_secrets
from vault_intercept import redact_vault_commands

# Runaway backstop only (not a depth target — depth is governed by --until):
# 200 pages * 100 = 20k messages before we refuse to loop forever.
MAX_PAGES = 200

# Ordinary messages are clipped so a long scroll stays scannable; FORWARDS are
# exempt (see _render) because a forward is usually the substance, not chatter.
CLIP = 200
# Reply targets are clipped harder than bodies: the point is to identify WHICH
# message is being answered, not to re-read it.
REPLY_CLIP = 110

API = "https://discord.com/api/v10"


def _fetch(extra, channel_id, page, headers, rj=None):
    p = {"limit": str(page)}
    p.update({k: v for k, v in extra.items() if v})
    url = f"{API}/channels/{channel_id}/messages?" + urllib.parse.urlencode(p)
    req = urllib.request.Request(url, headers=headers)
    # request_json honors 429 Retry-After + retries transient 5xx, so a rate
    # limit mid-pagination no longer aborts the read (2026-07-24 truncation fix).
    return (rj or request_json)(req, timeout=10)


def _redact(text):
    """The same two filters `discord-bridge.py` applies, in the same order (#2893).

    The bridge redacts what it writes into a task file, but these readers read
    the channel raw, so a secret the bridge had just stripped came straight back
    through the other door. One copy HERE covers both CLIs — the fix landed on
    main as per-CLI copies; the extraction folds them to the shared renderer.
    """
    if not text:
        return text
    return redact_vault_commands(filter_chat_secrets(text).text)


def _reply_context(msg, clip=REPLY_CLIP):
    """The message this one is REPLYING to, or None.

    A terse reply is uninterpretable without its target. Measured 2026-08-04 in
    the owner channel: `2 merge` and `2y\n3 I didn't delete it.` are both
    replies, and both rendered here as bare text with nothing indicating what
    they answered. Read from the channel alone they are unreadable; they only
    parsed because the task file happened to carry a `[Replying to ...]` block,
    which exists only when a message becomes a task.

    That matters because this is the reader `context-reconstruct` runs on every
    pass — the same reason the forward bug above was worth fixing. A peer hit
    the consequence directly: it re-asked an owner the same question twice after
    missing that a bare `2` was the answer to an enumerated question.

    LABELLED, never inlined — for the same reason forwards are labelled:
    attributing a quoted message to the replier is its own misreading.
    """
    ref = msg.get("referenced_message")
    if not isinstance(ref, dict):
        return None
    author = (ref.get("author") or {}).get("username", "?")
    body = " ".join((_render(ref, clip) or "").split())
    return f"↳ replying to {author}: {body[:clip] if clip is not None else body}" if body else \
           f"↳ replying to {author}: (no readable body)"


def _attachment_marks(atts):
    """``<attachment: name url>`` for each attachment, name-only if it has no url.

    The url is the ONLY retrievable handle the API gives; dropping it left a
    reader that can name a file and can never open it.
    """
    out = []
    for a in atts or []:
        name = a.get("filename") or "?"
        url = a.get("url") or ""
        out.append(f"<attachment: {name} {url}>" if url else f"<attachment: {name}>")
    return out


def _render(msg, clip=CLIP):
    """One message's readable body, INCLUDING forwarded content.

    A Discord *forward* carries empty top-level `content` and puts the real
    payload in `message_snapshots[0].message` (the bridge already knows this —
    `discord-bridge.py` reads it, `discord_addressee.py` documents it). This
    reader did not, so every forwarded message rendered as a BLANK LINE — and
    this is the reader the context-reconstruct step runs on every pass.

    Measured 2026-07-31: two forwards in #research-eval printed as empty while
    holding the only record of a live AMA failure (a 1,602-char transcript plus
    the reporter's own words). They were nearly deleted on the strength of that
    blank output — see feedback_forwarded_discord_msgs_hide_content_in_message_snapshots.

    The forward is LABELLED rather than silently inlined: attributing a quoted
    message to the forwarder is its own misreading. Forwards are also exempt
    from the 200-char clip — the clip exists to keep ordinary chatter scannable,
    and a forward is usually carrying the substance someone moved deliberately.
    """
    # Redact BEFORE clipping: the clip can land mid-token, and half a secret is
    # still a leak that the pattern would no longer match.
    body = _redact((msg.get("content") or "").strip())
    snaps = msg.get("message_snapshots") or []
    if not snaps:
        # A top-level attachment lives outside `content` (file-only messages
        # rendered blank); its FILENAME is user-supplied, so the marks redact too.
        marks = [_redact(m) for m in _attachment_marks(msg.get("attachments"))]
        body = body[:clip] if clip is not None else body
        return " ".join(x for x in (body, *marks) if x)
    fwd = (snaps[0].get("message") or {})
    fwd_body = (fwd.get("content") or "").strip()
    extra = []
    extra.extend(_attachment_marks(fwd.get("attachments")))
    for e in fwd.get("embeds") or []:
        extra.append(f"<embed: {e.get('title') or e.get('type') or '?'}>")
    # Redact the COMPOSED inner: filenames and embed titles are user-supplied too.
    inner = _redact(" ".join(x for x in (fwd_body, *extra) if x)) \
        or "(forward with no readable body)"
    prefix = f"{body} " if body else ""
    return f"{prefix}[forwarded] {inner}"


def _at_or_before_boundary(msg, until):
    """True once a message is at/older-than --until (id or ISO prefix)."""
    if until.isdigit():
        try:
            return int(msg["id"]) <= int(until)
        except (KeyError, ValueError):
            return False
    return (msg.get("timestamp", "") or "")[:len(until)] <= until


def _strictly_older_than_boundary(msg, until):
    if until.isdigit():
        try:
            return int(msg["id"]) < int(until)
        except (KeyError, ValueError):
            return False
    return (msg.get("timestamp", "") or "")[:len(until)] < until


def render_line(msg, full=False):
    """One CLI output line (timestamp, author, rendered body) — no reply line."""
    author = msg.get("author", {}).get("username", "?")
    ts = msg.get("timestamp", "")[:19]
    return f"[{ts}] {author}: {_render(msg, None if full else CLIP)}"
