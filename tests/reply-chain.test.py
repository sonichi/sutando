#!/usr/bin/env python3
"""Tests for src/reply_chain.py — the pure reply-context formatter.

Regression + design test for the owner-reported truncation loss (Chi 2026-07-25:
"Remove the truncation", then "I'm not sure about full chain inline" → lean).

Lean contract:
  * inline the FULL immediate parent (no 400-char cap; size-clipped only for a
    pathological multi-KB body, and clipped LOUDLY);
  * do NOT inline deeper ancestors — those are referenced by reply_chain_ids;
  * reply_chain_ids carries the full walked spine, root-first.

Run: python3 tests/reply-chain.test.py   (exit 0 pass / non-zero on failure)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import reply_chain as rc  # noqa: E402

_fails = []


def check(name, cond):
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}")
        _fails.append(name)


def E(author, ts, content):
    return {"author": author, "ts": ts, "content": content}


# --- empty / nothing to inline ---
check("empty chain -> ''", rc.format_reply_chain([]) == "")
check("attachment-only parent (blank content) -> ''",
      rc.format_reply_chain([E("a", "t", "   ")]) == "")

# --- immediate parent: full content, backward-compatible shape, no truncation ---
out = rc.format_reply_chain([E("Sutando-Pro#8185", "2026-07-25 17:42", "the full answer")])
check("immediate parent shape",
      out == "\n\n[Replying to Sutando-Pro#8185 (2026-07-25 17:42): the full answer]")

# The old 400-char cap is GONE: a 900-char body survives whole.
long_body = "x" * 900
out = rc.format_reply_chain([E("a", "t", long_body)])
check("no 400-char truncation", long_body in out and "…[" not in out)

# --- LEAN: a deep chain inlines ONLY the immediate parent ---
chain = [
    E("Sutando-Pro", "17:42", "the immediate reply I am responding to"),   # parent
    E("sonichi", "17:41", "GRANDPARENT should NOT be inlined"),
    E("sonichi", "17:38", "ROOT should NOT be inlined"),
]
out = rc.format_reply_chain(chain)
check("deep chain: immediate parent inlined", "the immediate reply I am responding to" in out)
check("deep chain: grandparent NOT inlined", "GRANDPARENT should NOT be inlined" not in out)
check("deep chain: root NOT inlined", "ROOT should NOT be inlined" not in out)
check("deep chain: single-line block (no 'Reply chain')", "Reply chain" not in out)

# --- pathological immediate parent: clipped LOUDLY, never silent ---
huge = "y" * 5000
out = rc.format_reply_chain([E("a", "t", huge)], max_msg_chars=2000)
check("pathological parent clipped", "…[+3000 chars" in out and "re-fetch" in out)
check("pathological parent length bounded", len(out) < 2200)

# --- format_reply_chain_ids: full root-first spine for thread reconstruction ---
check("no chain (<2 ids) -> ''", rc.format_reply_chain_ids([111]) == "")
check("empty ids -> ''", rc.format_reply_chain_ids([]) == "")
check("Nones filtered to <2 -> ''", rc.format_reply_chain_ids([None, 5, None]) == "")
# ids arrive immediate-parent-first; emitted root-first (reversed) with trailing \n
out = rc.format_reply_chain_ids([300, 200, 100])   # parent=300 … root=100
check("ids root-first line", out == "reply_chain_ids: 100,200,300\n")
check("ids line has trailing newline", out.endswith("\n"))
# the id spine keeps EVERY ancestor even though only the parent was inlined
out = rc.format_reply_chain_ids([1530634946949943497, None, 1530631339764875396])
check("id spine spans full chain, None dropped",
      out == "reply_chain_ids: 1530631339764875396,1530634946949943497\n")

# --- format_reply_chain_truncation: deep-thread id spine is never silently cut ---
# reached_root=True → the spine is complete → no marker
check("reached root -> no marker", rc.format_reply_chain_truncation(True, 999) == "")
check("reached root, no id -> no marker", rc.format_reply_chain_truncation(True, None) == "")
# not reached_root → visible marker anchored on the oldest captured id
mk = rc.format_reply_chain_truncation(False, 1530631339764875396)
check("truncated -> visible marker", "truncated" in mk and "1530631339764875396" in mk)
check("truncated marker tells how to recover", "reply to an older message" in mk)
check("truncated marker is a single leading-newline line",
      mk.startswith("\n") and mk.count("\n") == 1)
# defensive: no oldest id (empty walk) → nothing to anchor → no marker
check("truncated but no id -> no marker", rc.format_reply_chain_truncation(False, None) == "")

# --- walk_reply_chain: the two cases reviewers flagged as untested -----------
# Both were previously inside the bridge handler behind `pragma: no cover`, so
# the paths where reply context is SILENTLY lost were the only ones never
# exercised. asyncio.run is used directly to keep this suite dependency-free
# and in-process (the diff-coverage gate does not trace subprocesses).
import asyncio


class _Ref:
    def __init__(self, message_id, resolved=None):
        self.message_id = message_id
        self.resolved = resolved


class _Msg:
    """Minimal stand-in for a discord.Message (only what the walk touches)."""

    def __init__(self, mid, content="body", parent_id=None):
        self.id = mid
        self.content = content
        self.author = f"user{mid}"
        self.created_at = None          # walk must tolerate a missing timestamp
        self.reference = _Ref(parent_id) if parent_id is not None else None


def _linear(n):
    """n messages, ids 1..n, each replying to the next (n = the root)."""
    return {i: _Msg(i, f"msg{i}", parent_id=(i + 1 if i < n else None)) for i in range(1, n + 1)}


def _fetcher(store, missing=()):
    async def fetch(mid):
        if mid in missing:
            raise RuntimeError("unfetchable")
        return store.get(mid)
    return fetch


# 1. Deeper than the CONTENT cap: content stops at 8, the id spine keeps going
#    to the root. This is the case that proves the spine is not merely a copy
#    of the inlined chain.
store = _linear(20)
chain, ids, reached = asyncio.run(rc.walk_reply_chain(
    store[1], _fetcher(store), max_content_depth=8, max_ids_depth=64))
check(">8 ancestors: content capped at max_content_depth", len(chain) == 8)
check(">8 ancestors: id spine walks past the content cap", len(ids) == 20)
check(">8 ancestors: spine reaches the true root", ids[-1] == 20)
check(">8 ancestors: clean root -> reached_root True", reached is True)
check(">8 ancestors: complete spine emits NO truncation marker",
      rc.format_reply_chain_truncation(reached, ids[-1]) == "")

# 2. Deeper than the ID cap too -> NOT a clean root, so the marker must fire.
chain, ids, reached = asyncio.run(rc.walk_reply_chain(
    store[1], _fetcher(store), max_content_depth=8, max_ids_depth=5))
check("id-cap exhausted: spine bounded", len(ids) == 5)
check("id-cap exhausted: reached_root False", reached is False)
check("id-cap exhausted: marker fires with oldest reached id",
      str(ids[-1]) in rc.format_reply_chain_truncation(reached, ids[-1]))

# 3. Unfetchable ancestor mid-walk: older context exists but is unreachable.
#    Must NOT read as a clean root — that silent-complete render is the bug.
store = _linear(10)
chain, ids, reached = asyncio.run(rc.walk_reply_chain(
    store[1], _fetcher(store, missing={4}), max_content_depth=8, max_ids_depth=64))
check("unfetchable ancestor: walk stops at the gap", ids == [1, 2, 3])
check("unfetchable ancestor: NOT reported as root", reached is False)
check("unfetchable ancestor: marker names the oldest REACHED id",
      "3" in rc.format_reply_chain_truncation(reached, ids[-1]))

# a fetch returning None (rather than raising) is the same event to the caller
async def _none_fetch(mid):
    return None

chain, ids, reached = asyncio.run(rc.walk_reply_chain(
    store[1], _none_fetch, max_content_depth=8, max_ids_depth=64))
check("fetch returning None == unfetchable", reached is False and ids == [1])

# 4. A resolved ancestor is used without any fetch at all (the common path).
root = _Msg(99, "root question")
kid = _Msg(1, "reply", parent_id=99)
kid.reference.resolved = root


async def _explode(mid):
    raise AssertionError("fetch must not be called when reference.resolved is set")

chain, ids, reached = asyncio.run(rc.walk_reply_chain(
    kid, _explode, max_content_depth=8, max_ids_depth=64))
check("resolved ancestor avoids a fetch", ids == [1, 99] and reached is True)

# 5. Single message, no reference -> immediately a clean root.
chain, ids, reached = asyncio.run(rc.walk_reply_chain(
    _Msg(7, "solo"), _none_fetch, max_content_depth=8, max_ids_depth=64))
check("no reference -> clean root, no marker",
      reached is True and ids == [7] and rc.format_reply_chain_truncation(reached, 7) == "")

# 6. The mention strip and a missing created_at both survive the walk.
chain, _, _ = asyncio.run(rc.walk_reply_chain(
    _Msg(1, "<@42> hello"), _none_fetch, max_content_depth=8, max_ids_depth=64,
    strip_mention="<@42>"))
# Behavior parity, deliberately: the bridge did `.strip()` BEFORE `.replace()`,
# so removing a LEADING mention leaves the separating space behind. That is
# pre-existing behavior; tightening it here would smuggle a behavior change into
# an extraction refactor, so the test pins what the bridge actually did.
check("mention stripped from walked content", chain[0]["content"] == " hello")
check("missing created_at renders as empty ts", chain[0]["ts"] == "")

# --- format_parent_reference: a forward's reference is NOT a reply parent -----
# A forward sets `message.reference` pointing at the original in its SOURCE
# channel. Emitted under `parent_message_id` it claimed a relationship that does
# not exist and produced an id that 404s from the channel the task was written
# in (observed 2026-08-04 on a real owner forward into #echo).
check("reply keeps parent_message_id",
      rc.format_parent_reference(111, is_forward=False) == "parent_message_id: 111\n")
check("forward is re-keyed",
      rc.format_parent_reference(111, is_forward=True) == "forwarded_from_message_id: 111\n")

# NEGATIVE CONTROLS — an unconditional swap in either direction passes one of
# the two cases above, so each key must be asserted ABSENT from the other shape.
check("a reply never emits forwarded_from_message_id",
      "forwarded_from" not in rc.format_parent_reference(111, is_forward=False))
check("a forward never emits parent_message_id",
      "parent_message_id" not in rc.format_parent_reference(111, is_forward=True))

# The channel is what makes the kept provenance usable: an id alone is not a
# handle, since the message lives in a channel the consumer was not reading.
_fwd = rc.format_parent_reference(111, is_forward=True, source_channel_id=222)
check("forward carries its source channel",
      _fwd == "forwarded_from_message_id: 111\nforwarded_from_channel_id: 222\n")
check("forward with no known channel omits the channel line",
      rc.format_parent_reference(111, is_forward=True, source_channel_id=None)
      == "forwarded_from_message_id: 111\n")
check("a reply never emits a channel line even when one is known",
      rc.format_parent_reference(111, is_forward=False, source_channel_id=222)
      == "parent_message_id: 111\n")

check("no id -> '' (reply)", rc.format_parent_reference(None, is_forward=False) == "")
check("no id -> '' (forward)", rc.format_parent_reference(None, is_forward=True) == "")
check("no id -> '' even with a channel",
      rc.format_parent_reference(None, is_forward=True, source_channel_id=222) == "")

# Task-file header contract: every emitted line is `key: value` and newline
# terminated, or the k:v parse of the task file breaks.
for _lbl, _out in (("reply", rc.format_parent_reference(1, is_forward=False)),
                   ("forward", _fwd)):
    _lines = [l for l in _out.split("\n") if l]
    check(f"{_lbl}: every line is k:v", all(": " in l for l in _lines))
    check(f"{_lbl}: newline terminated", _out.endswith("\n"))

# --- the ACTIVATED fetch path, not just the header --------------------------
# @john-the-dev and @bassilkhilo-ag2 both blocked the first version of #2633:
# it re-keyed the task-file header while `discord-bridge.py` still entered the
# reply-context block for every `message.reference` and called
# `channel.fetch_message()`. For a forward that target lives in the SOURCE
# channel, so the 404 and the wasted round trip — the two symptoms the PR body
# described — were still live. The header was relabelled; the behaviour was not.
check("a forward does NOT trigger a reply-context fetch",
      rc.should_fetch_reply_context(has_reference=True, has_message_id=True, is_forward=True) is False)
check("a genuine reply DOES trigger the fetch (the capability is not lost)",
      rc.should_fetch_reply_context(has_reference=True, has_message_id=True, is_forward=False) is True)
check("no reference -> no fetch",
      rc.should_fetch_reply_context(has_reference=False, has_message_id=False, is_forward=False) is False)
check("reference without a message_id -> no fetch",
      rc.should_fetch_reply_context(has_reference=True, has_message_id=False, is_forward=False) is False)

# The two keying decisions must agree: whatever is re-keyed as a forward must
# also be the thing that skips the fetch. A build where they disagree reintroduces
# exactly the reviewed defect from the other side.
for _is_fwd in (True, False):
    _hdr_is_forward = "forwarded_from_message_id" in rc.format_parent_reference(1, is_forward=_is_fwd)
    _skips_fetch = not rc.should_fetch_reply_context(True, True, _is_fwd)
    check(f"header re-key and fetch gate agree (is_forward={_is_fwd})",
          _hdr_is_forward == _skips_fetch)

# The bridge itself is not unit-importable, so assert the call site by source:
# the guard must WRAP the fetch, not sit beside it.
import pathlib as _pl
_bridge = (_pl.Path(__file__).resolve().parent.parent / "src" / "discord-bridge.py").read_text()
_guard_i = _bridge.find("should_fetch_reply_context(\n")
_fetch_i = _bridge.find("await message.channel.fetch_message(message.reference.message_id)")
check("the bridge calls the gate", _guard_i > 0)
check("the gate precedes the reply-context fetch it protects", 0 < _guard_i < _fetch_i)
check("no ungated `if message.reference and message.reference.message_id:` fetch remains",
      "if message.reference and message.reference.message_id:\n        try:" not in _bridge)

print()
if _fails:
    print(f"{len(_fails)} test(s) FAILED: {_fails}")
    sys.exit(1)
print("all tests passed")
