#!/usr/bin/env python3
r"""A `[channel:]` body implies a BRIDGE, and three adapters each re-derived it.

The filename layer (`.to-<channel>`) already had one owner in proactive_routing.
The BODY marker did not: slack-bridge and telegram-bridge each carried

    re.match(r'\[channel:\s*\d{17,20}\]', peek)

which recognises a Discord snowflake and nothing else. A Matrix room id therefore
read as "addressed to nobody" and the file was claimable — so a proactive aimed at
an ag2.space room could be delivered to the owner's Telegram or Slack DM instead.
Measured on this host: discord-bridge logged 19 releases of one such file, all
`!PrxhizfLysTYrYDcnw:ag2.space`, so the shape is live, not hypothetical.

Two policies, one classifier, because the adapters genuinely differ:

  body_claimable_by         slack/telegram — skip only a RECOGNISED foreign target,
                            so a malformed one still gets delivered rather than
                            stranded (their pre-existing behaviour, kept).
  redirect_target_is_foreign  discord — the default destination, so anything not
                            positively a snowflake is released (also unchanged).

  1) classifier: snowflake / matrix / slack ids, and what is NOT an address
  2) body extraction: leading marker only, whitespace tolerated
  3) the two policies differ exactly on the unrecognised target
  3b) [dm-only] disarms the address, so nothing routes on it
  3c) asking the parser peels a D7 header a text match cannot see past
  4) THE DEFECT: the old Discord-only predicate disagrees on a Matrix room
  5) every adapter delegates — no private copy of the grammar survives

Run: python3 tests/proactive-body-target-routing.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

FAILS: list[str] = []
ROOM = "!PrxhizfLysTYrYDcnw:ag2.space"


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def source_checks() -> None:
    """Run BEFORE the import so a parent-commit run reports the defect, not an
    ImportError — a missing symbol says nothing about what the old code did."""
    private = re.compile(r'channel:\\s\*\\d\{17,20\}')
    for name, channel in (("slack-bridge.py", "slack"),
                          ("telegram-bridge.py", "telegram")):
        src = (REPO / "src" / name).read_text(encoding="utf-8")
        check(f'proactive_body_guard(f.name, peek, "{channel}")' in src,
              f"5) {name} delegates its peek to proactive_routing")
        check("from proactive_routing import" in src and "proactive_body_guard" in src,
              f"5) {name} imports it")
        check(not private.search(src),
              f"5) no private Discord-only body grammar survives in {name}")
    discord = (REPO / "src" / "discord-bridge.py").read_text(encoding="utf-8")
    check("redirect_target_is_foreign(" in discord,
          "5) discord-bridge delegates its redirect fence too")
    check(not private.search(discord),
          "5) no private Discord-only body grammar survives in discord-bridge.py")


def main() -> int:
    source_checks()
    from proactive_routing import (  # noqa: PLC0415 — see source_checks
        body_claimable_by, body_target_channel, redirect_target_is_foreign,
        target_channel_kind)
    from result_markers import parse_markers  # noqa: PLC0415
    # 1) The classifier.
    for target, kind in (
        ("1530802402603700415", "discord"),
        ("1022910063620390932", "discord"),
        (ROOM, "ag2space"),
        # room-ID-only contract: an alias is NOT executable (the backend does
        # no alias resolution), so it stays unrecognised like any other value
        ("#general:ag2.space", None),
        (f"{ROOM}:8448", "ag2space"),
        ("!v6:[2001:db8::1]", "ag2space"),
        ("!v6:[2001:db8::1]:8448", "ag2space"),
        ("C0123ABCD", "slack"),
        ("G01ABCDEF", "slack"),
        ("D01ABCDEF", "slack"),
        # Not addresses: too short for a snowflake, no server part, empty.
        ("12345", None), ("!room", None), ("garbage", None),
        ("", None), (None, None), ("   ", None),
    ):
        got = target_channel_kind(target)
        check(got == kind, f"1) {target!r} -> {kind}, got {got}")

    # 2) Only the LEADING marker routes — the rule result_markers already applies
    #    when deciding whether a redirect is an action or inert prose.
    for body, kind in (
        (f"[channel: {ROOM}]\nbody", "ag2space"),
        (f"  [channel:{ROOM}]\nbody", "ag2space"),
        ("[channel: 1530802402603700415]\nbody", "discord"),
        ("prose first\n[channel: 1530802402603700415]\nbody", None),
        ("see [channel: 1530802402603700415] inline", None),
        ("no marker at all", None),
        ("", None),
    ):
        got = body_target_channel(body)
        check(got == kind, f"2) {body[:34]!r} -> {kind}, got {got}")

    # 3) The two policies agree on every recognised target and differ only on the
    #    unrecognised one. That difference is the deliberate part.
    for target in (ROOM, "C0123ABCD", "1530802402603700415"):
        body = f"[channel: {target}]\nx"
        kind = target_channel_kind(target)
        for channel in ("discord", "slack", "telegram", "ag2space"):
            check(body_claimable_by(body, channel) == (kind == channel),
                  f"3) recognised {target[:18]!r}: claimable by {channel} iff it is {kind}")
            check(redirect_target_is_foreign(target, channel) == (kind != channel),
                  f"3) recognised {target[:18]!r}: foreign to {channel} iff it is not {kind}")
    check(body_claimable_by("[channel: garbage]\nx", "telegram"),
          "3) UNRECOGNISED stays claimable (telegram delivers rather than strands)")

    # 3a) Telegram is NOT a target kind BY DECISION: its bridge drops [channel:]
    #     redirects, so classifying one would route a file to a non-delivery.
    tg_src = (REPO / "src" / "telegram-bridge.py").read_text(encoding="utf-8")
    check("silently drops [channel:] redirects" in tg_src,
          "3a) telegram-bridge still documents that it DROPS a [channel:] redirect")
    check("parse_markers(reply_text)" in tg_src,
          "3a) and the drop happens at its parse_markers call, not a private branch")
    for tg_id in ("12345", "-1001234567890", "987654321"):
        check(target_channel_kind(tg_id) is None,
              f"3a) {tg_id} classifies as NO bridge, so nothing routes to telegram")
        check(body_claimable_by(f"[channel: {tg_id}]\nx", "telegram"),
              f"3a) and {tg_id} stays claimable by every bridge — delivered, not stranded")
    check(redirect_target_is_foreign("garbage", "discord"),
          "3) UNRECOGNISED is foreign to the default (discord releases it)")

    # 3b) [dm-only] DISARMS the redirect in EITHER order, so no address is left
    #     to route on — and matching text instead of asking the parser misses it.
    for order, label in ((f"[dm-only]\n[channel: {ROOM}]\nx", "dm-only first"),
                         (f"[channel: {ROOM}]\n[dm-only]\nx", "address first")):
        check(body_target_channel(order) is None,
              f"3b) {label}: [dm-only] leaves no executable address")
        for channel in ("discord", "slack", "telegram", "ag2space"):
            check(body_claimable_by(order, channel),
                  f"3b) {label}: {channel} may still claim it")
        check(not any(a.kind == "redirect" for a in parse_markers(order).actions),
              f"3b) {label}: and the shared parser issues no redirect")

    # 3c) Asking the PARSER also peels a D7 header for free; a leading-marker
    #     text match cannot see past one.
    check(body_target_channel(f"**[core: 1]**\n[channel: {ROOM}]\nx") == "ag2space",
          "3c) a D7-headed body still routes by its address")

    # 4) THE DEFECT, stated as the disagreement it is. This is the predicate the
    #    two bridges carried; it is right about Discord and blind to everything else.
    old = re.compile(r"\[channel:\s*\d{17,20}\]")

    def old_skips(peek: str) -> bool:
        return bool(peek.startswith("[channel:") and old.match(peek))

    for body, kind in ((f"[channel: {ROOM}]\nbriefing", "ag2space"),
                       ("[channel: C0123ABCD]\nbriefing", "slack")):
        check(not old_skips(body),
              f"4) the OLD predicate does not skip a {kind} target — the defect")
        # Every bridge EXCEPT the one addressed must now decline it.
        for channel in ("telegram", "slack", "discord"):
            if channel == kind:
                check(body_claimable_by(body, channel),
                      f"4) the addressed bridge ({channel}) still claims its own")
                continue
            check(not body_claimable_by(body, channel),
                  f"4) and the new one does not claim a {kind} target on {channel}")
    snow = "[channel: 1530802402603700415]\nbriefing"
    check(old_skips(snow) and not body_claimable_by(snow, "telegram"),
          "4) and both agree on the Discord case, so nothing regresses there")

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS[:4]))
        return 1
    print("PASS — the body marker's bridge implication has one owner")
    return 0


if __name__ == "__main__":
    sys.exit(main())
