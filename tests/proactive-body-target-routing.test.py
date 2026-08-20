#!/usr/bin/env python3
"""A `[channel:]` body implies a BRIDGE, and three adapters each re-derived it.

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
        check(f'body_claimable_by(peek, "{channel}")' in src,
              f"5) {name} delegates its peek to proactive_routing")
        check("from proactive_routing import" in src and "body_claimable_by" in src,
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
    # 1) The classifier.
    for target, kind in (
        ("1530802402603700415", "discord"),
        ("1022910063620390932", "discord"),
        (ROOM, "ag2space"),
        ("#general:ag2.space", "ag2space"),
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
    check(redirect_target_is_foreign("garbage", "discord"),
          "3) UNRECOGNISED is foreign to the default (discord releases it)")

    # 3b) [dm-only] is a PRIVACY guard, not an address: it suppresses a redirect
    #     at delivery and must not decide which bridge may claim.
    check(body_target_channel(f"[dm-only]\n[channel: {ROOM}]\nx") is None,
          "3b) a body led by [dm-only] addresses no bridge")
    for channel in ("discord", "slack", "telegram"):
        check(body_claimable_by(f"[dm-only]\n[channel: {ROOM}]\nx", channel),
              f"3b) so {channel} may still claim it — the guard routes nothing")
    # Address FIRST: routing applies, and [dm-only] still stops the redirect, so
    # it lands in an owner DM either way — which is why routing it is safe.
    check(not body_claimable_by(f"[channel: {ROOM}]\n[dm-only]\nx", "telegram"),
          "3b) an addressed body carrying [dm-only] still routes by its address")

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
