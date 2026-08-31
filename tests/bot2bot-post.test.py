#!/usr/bin/env python3
"""Tests for skills/bot2bot-post/post.py — the bot2bot-channel scope guard.

Regression for the 2026-07-27 dead-letter: `bot2bot-post --to <X>` silently
posted to the bot2bot channel even when X wasn't a member there, so the ping
went nowhere. bot2bot-post only posts to that one channel; the guard makes it
REFUSE a recipient who isn't a member (and thus the bot-vs-human-owner id
mix-up), instead of dead-lettering. Where to route a non-bot2bot message is the
caller's judgment — the guard doesn't prescribe a destination.

(Ids/names below are generic placeholders — this is shared-repo code.)

Run: python3 tests/bot2bot-post.test.py   (exit 0 pass / non-zero on failure)
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

_POST = Path(__file__).resolve().parents[1] / "skills" / "bot2bot-post" / "post.py"
_spec = importlib.util.spec_from_file_location("b2b_post", _POST)
b2b = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(b2b)

_fails = []


def check(name, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails.append(name)


# Numeric ids so main()'s resolve_to_target accepts a raw --to value.
# Generic placeholders — MEMBER_* are in the bot2bot channel, OUTSIDER_* are not.
MEMBER_A, MEMBER_B, MEMBER_SELF = "100", "200", "300"
OUTSIDER_A, OUTSIDER_B = "900", "901"  # only in another channel, not bot2bot
ACCESS = {
    "allowFrom": ["1"],
    "groups": {
        "chan_bot2bot": {"role": "bot2bot", "allowFrom": [MEMBER_A, MEMBER_B, MEMBER_SELF]},
        "chan_other": {"requireMention": True, "allowFrom": ["1", OUTSIDER_A, OUTSIDER_B]},
    },
}
BOT2BOT = "chan_bot2bot"

# --- _recipient_in_channel: the scope check ---
check("channel member → in channel", b2b._recipient_in_channel(ACCESS, BOT2BOT, MEMBER_B) is True)
check("non-member (other channel) → NOT in channel", b2b._recipient_in_channel(ACCESS, BOT2BOT, OUTSIDER_A) is False)
check("unknown id → NOT in channel", b2b._recipient_in_channel(ACCESS, BOT2BOT, "nobody") is False)
check("missing channel → False", b2b._recipient_in_channel(ACCESS, "no_such", MEMBER_B) is False)
# int allowFrom entry still matches a str recipient
check("int allowFrom matches str recipient",
      b2b._recipient_in_channel({"groups": {"c": {"allowFrom": [42]}}}, "c", "42") is True)

# --- main(): the guard refuses a non-member recipient, allows a member ---
_orig = {k: getattr(b2b, k) for k in ("load_token", "load_access", "get_self_id", "post")}
_posted = {}


def _install_mocks():
    b2b.load_token = lambda: "tok"
    b2b.load_access = lambda: ACCESS
    b2b.get_self_id = lambda token: MEMBER_SELF  # this bot is a channel member
    # `**kw` absorbs optional keyword args the real `post()` grows (e.g. the
    # `overhead` hint the length guard passes). A fixed-arity mock turns any
    # such addition into a TypeError in the TEST while production is fine —
    # this exact stub broke that way when `overhead=` was added.
    #
    # It CAPTURES the kwargs rather than discarding them, so the handoff from
    # main() can be asserted. Absorbing-and-dropping would keep the suite green
    # while main() forwarded a wrong (or hardcoded) value.
    b2b.post = lambda ch, txt, tok, **kw: (
        _posted.update(channel=ch, text=txt, kwargs=kw) or {"id": "1"}
    )


def _restore():
    for k, v in _orig.items():
        setattr(b2b, k, v)


_install_mocks()
try:
    # guard REFUSES a recipient who isn't in the bot2bot channel
    sys.argv = ["post.py", "--to", OUTSIDER_A, "ping", "hi"]
    raised = False
    try:
        b2b.main()
    except SystemExit as e:
        raised = True
        msg = str(e)
    check("main: --to non-member → SystemExit (refused, not posted)", raised and "posted" not in _posted)
    check("main: refusal states non-membership + points at access.json",
          raised and "not a member of the bot2bot" in msg and "access.json" in msg)

    # guard ALLOWS a channel member
    _posted.clear()
    sys.argv = ["post.py", "--to", MEMBER_B, "done", "shipped"]
    b2b.main()
    check("main: --to member → posts to bot2bot channel", _posted.get("channel") == BOT2BOT)
    check("main: member post carries the mention", _posted.get("text", "").startswith(f"<@{MEMBER_B}> "))

    # --- main() forwards the REAL overhead to the length guard ---------------
    # The focused guard suite checks `check_length` arithmetic in isolation; it
    # cannot see whether main() hands it the right number. Derive the expected
    # value from the observed message and the body main() was given, so a
    # hardcoded constant in main() (today's prefix happens to be 24 chars) fails
    # here instead of shipping a guard that measures the wrong string.
    _body = "shipped"
    _sent = _posted.get("text", "")
    _seen = _posted.get("kwargs", {})
    check("main: forwards overhead= to post()", "overhead" in _seen)
    check("main: overhead equals len(message) - len(body)",
          _seen.get("overhead") == len(_sent) - len(_body))
    check("main: overhead accounts for BOTH the mention and the kind tag",
          _seen.get("overhead") == len(f"<@{MEMBER_B}> done: "))

    # --- --body-file: prose never crosses a shell quoting boundary ----------
    import pathlib
    import tempfile
    _hazard = ("He approved `qingyun-wu`'s #2909 — an apostrophe closes a "
               "single-quoted shell arg and re-arms the backticks.")
    with tempfile.TemporaryDirectory() as td:
        f = pathlib.Path(td) / "body.txt"
        f.write_text(_hazard + "\n", encoding="utf-8")

        _posted.clear()
        sys.argv = ["post.py", "--to", MEMBER_B, "ping", "--body-file", str(f)]
        b2b.main()
        _sent2 = _posted.get("text", "")
        # The whole point: every character survives, backticks and apostrophe included.
        check("--body-file: body delivered VERBATIM", _hazard in _sent2)
        check("--body-file: trailing newline stripped, not doubled",
              not _sent2.endswith("\n"))
        check("--body-file: mention + kind prefix still applied",
              _sent2.startswith(f"<@{MEMBER_B}> ping: "))
        check("--body-file: overhead still measured against the real body",
              _posted.get("kwargs", {}).get("overhead") == len(_sent2) - len(_hazard))

        # COMPATIBILITY: the flag is only recognised immediately after <kind>.
        # A later literal occurrence is ordinary prose and must still send.
        _posted.clear()
        sys.argv = ["post.py", "--to", MEMBER_B, "ping", "please document", "--body-file", "usage"]
        b2b.main()
        check("literal --body-file LATER in a body stays prose",
              _posted.get("text", "").endswith("please document --body-file usage"))

        # a trailing argument after the path is ambiguous -> refuse
        _posted.clear()
        sys.argv = ["post.py", "--to", MEMBER_B, "ping", "--body-file", str(f), "extra"]
        try:
            b2b.main(); raised2 = False
        except SystemExit:
            raised2 = True
        check("--body-file + trailing arg → refused, nothing posted",
              raised2 and "posted" not in _posted)

        # an empty file must not post a blank message
        blank = pathlib.Path(td) / "blank.txt"; blank.write_text("   \n", encoding="utf-8")
        _posted.clear()
        sys.argv = ["post.py", "--to", MEMBER_B, "ping", "--body-file", str(blank)]
        try:
            b2b.main(); raised3 = False
        except SystemExit:
            raised3 = True
        check("--body-file: empty file refused, nothing posted",
              raised3 and "posted" not in _posted)

    # a missing path fails loudly rather than posting an empty body
    _posted.clear()
    sys.argv = ["post.py", "--to", MEMBER_B, "ping", "--body-file", "/nonexistent/nope.txt"]
    try:
        b2b.main(); raised4 = False
    except SystemExit:
        raised4 = True
    check("--body-file: unreadable path refused, nothing posted",
          raised4 and "posted" not in _posted)

    # --- multi-peer NO-GUESS (2026-07-29 double misfire regression) ---
    # With 2+ peer bots allowlisted and no --to, the old code picked
    # bot_candidates[0] arbitrarily (Pro pinged Air meaning Mini; Mini pinged
    # Air meaning Pro; each stray ping triggered the target's team-tier
    # auto-refusal). New contract: post WITHOUT any mention.
    _posted.clear()
    sys.argv = ["post.py", "ping", "who is around"]
    b2b.main()
    check("main: no --to with 2 peers → posts WITHOUT a mention",
          _posted.get("text", "").startswith("ping: ") and "<@" not in _posted.get("text", ""))

    # single-peer fleets keep the convenient auto-mention
    single_access = {
        "allowFrom": ["1"],
        "groups": {"chan_bot2bot": {"role": "bot2bot",
                                    "allowFrom": [MEMBER_A, MEMBER_SELF]}},
    }
    b2b.load_access = lambda: single_access
    _posted.clear()
    sys.argv = ["post.py", "ping", "you there?"]
    b2b.main()
    check("main: no --to with exactly 1 peer → auto-mentions that peer",
          _posted.get("text", "").startswith(f"<@{MEMBER_A}> "))
    b2b.load_access = lambda: ACCESS

    # resolve_other_bot unit view: multi-peer → None, single-peer → the peer
    check("resolve_other_bot: 2 peers → None (no guess)",
          b2b.resolve_other_bot(ACCESS, MEMBER_SELF, BOT2BOT) is None)
    check("resolve_other_bot: 1 peer → that peer",
          b2b.resolve_other_bot(single_access, MEMBER_SELF, BOT2BOT) == MEMBER_A)

    # legacy configs: owner+bot share the top-level allowFrom, so the
    # not-in-global heuristic yields no bot_candidates. Same no-guess rule.
    legacy_single = {
        "allowFrom": [MEMBER_A, MEMBER_SELF],
        "groups": {"chan_bot2bot": {"role": "bot2bot",
                                    "allowFrom": [MEMBER_A, MEMBER_SELF]}},
    }
    check("resolve_other_bot: legacy 1 non-self id → that id",
          b2b.resolve_other_bot(legacy_single, MEMBER_SELF, BOT2BOT) == MEMBER_A)
    legacy_multi = {
        "allowFrom": [MEMBER_A, MEMBER_B, MEMBER_SELF],
        "groups": {"chan_bot2bot": {"role": "bot2bot",
                                    "allowFrom": [MEMBER_A, MEMBER_B, MEMBER_SELF]}},
    }
    check("resolve_other_bot: legacy 2 non-self ids → None (no guess)",
          b2b.resolve_other_bot(legacy_multi, MEMBER_SELF, BOT2BOT) is None)
finally:
    _restore()

# --- kind vocabulary: every tag the docs tell agents to use must be accepted ---
# Regression for the 2026-08-01 drift: proactive-loop/SKILL.md documents `nack:`
# ("vetoing another bot's pending claim") but VALID_KINDS omitted it, so post.py
# exited 2 on a documented primitive. That is not a cosmetic mismatch — a rejected
# coordination tag degrades to "the retraction lands AFTER the claim it retracts",
# which is exactly how it was found: a nack post was refused, went unnoticed, and
# the correction arrived after the message it corrected.
check("nack is a valid kind", "nack" in b2b.VALID_KINDS)

_DOC = Path(__file__).resolve().parents[1] / "skills" / "proactive-loop" / "SKILL.md"
_doc_kinds = set(re.findall(r"`([a-z-]+):`", _DOC.read_text())) if _DOC.exists() else set()
# FLOOR, and it is the load-bearing half. Without it this check is disableable by
# the very event it exists to catch: if SKILL.md is moved/renamed, or the tags stop
# matching the backtick form, `_doc_kinds` degrades to set(), the gap below is empty,
# and the drift assertion PASSES — reporting green on an unmonitored vocabulary.
# An assertion that a mechanism exists is only meaningful if it cannot pass in the
# broken state, and "source of truth unreadable" is one of the broken states.
check(f"documented-kind extraction is non-degenerate ({len(_doc_kinds)} found, floor 5)",
      len(_doc_kinds) >= 5)
# `opinion-requested:` is the prose name for the `opinion` kind; map it.
_doc_kinds = {"opinion" if k == "opinion-requested" else k for k in _doc_kinds}
_undocumented_gap = _doc_kinds - b2b.VALID_KINDS
check(f"every kind documented in proactive-loop SKILL.md is accepted (gap: {sorted(_undocumented_gap)})",
      not _undocumented_gap)

# The SAME drift, on the surface an agent reads FIRST. The check above only covers
# SKILL.md; post.py's own `Kinds:` help line is a second, independent copy of the
# vocabulary, and the original fix updated VALID_KINDS while leaving it at five —
# so `--help` still told the caller `nack` did not exist even once the code accepted
# it. Widening to the AXIS (every copy of the vocabulary) rather than patching the
# one instance found: a tool whose help contradicts its enforcement misinforms the
# reader in whichever direction the two disagree.
_help_kinds = set()
for _line in (b2b.__doc__ or "").splitlines():
    if _line.strip().startswith("Kinds:"):
        _help_kinds = {k.strip() for k in _line.split(":", 1)[1].split("|") if k.strip()}
        break
# Same floor discipline as above: if the `Kinds:` line is reworded or removed,
# _help_kinds degrades to set() and the equality below would pass vacuously.
check(f"--help kind list is non-degenerate ({len(_help_kinds)} found, floor 5)",
      len(_help_kinds) >= 5)
check(f"--help `Kinds:` line matches VALID_KINDS exactly "
      f"(help-only: {sorted(_help_kinds - b2b.VALID_KINDS)}, "
      f"code-only: {sorted(b2b.VALID_KINDS - _help_kinds)})",
      _help_kinds == b2b.VALID_KINDS)

# --- contract-drift guard: the shipped agent-facing docs must describe the
# no-guess contract this suite pins. If someone reverts the behavior (or the
# docs) without the other, these assertions catch the divergence.
_SKILL_DIR = _POST.parent
_skill_md = (_SKILL_DIR / "SKILL.md").read_text()
_manifest = (_SKILL_DIR / "manifest.json").read_text()
check("SKILL.md documents --to targeting", "--to <peer|id>" in _skill_md)
check("SKILL.md documents multi-peer no-guess (no mention + NOTE)",
      "without any mention" in _skill_md and "never guesses" in _skill_md)
check("SKILL.md documents single-peer auto-mention",
      "exactly ONE peer" in _skill_md and "auto-mentions that peer" in _skill_md)
check("SKILL.md documents the member-guard refusal", "REFUSES" in _skill_md)
check("SKILL.md documents the peers.json roster", "peers.json" in _skill_md)
check("manifest description matches the no-guess contract",
      "--to" in _manifest and "never guess" in _manifest)
check("stale auto-mention contract is gone from the docs",
      "the other Sutando node" not in _skill_md.split("\n---")[0]
      and "@-mentioning the other Sutando node" not in _manifest)

# main() accepts nack end-to-end, and an unknown kind is still refused (guard not disabled)
_install_mocks()
try:
    _posted.clear()
    sys.argv = ["post.py", "--to", MEMBER_B, "nack", "vetoing that claim"]
    b2b.main()
    check("main: nack posts to bot2bot channel", _posted.get("channel") == BOT2BOT)
    check("main: nack body carries the tag", "nack:" in _posted.get("text", ""))

    _posted.clear()
    sys.argv = ["post.py", "--to", MEMBER_B, "definitely-not-a-kind", "x"]
    refused = False
    try:
        b2b.main()
    except SystemExit:
        refused = True
    check("main: unknown kind STILL refused (positive control — guard not disabled)",
          refused and not _posted)
finally:
    _restore()

print()
if _fails:
    print(f"{len(_fails)} test(s) FAILED: {_fails}")
    sys.exit(1)
print("all tests passed — bot2bot-channel scope guard + kind vocabulary")
