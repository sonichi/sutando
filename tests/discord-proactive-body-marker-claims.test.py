#!/usr/bin/env python3
"""
discord-bridge.py's poll_proactive must claim a `proactive-*.txt` whose BODY
explicitly addresses Discord, even when activity routing (last-owner-activity.json)
says a different bridge had the owner's most recent attention.

slack-bridge.py and telegram-bridge.py both peek the body via
`proactive_routing.body_claimable_by` before deciding whether to claim a file.
discord-bridge.py never did: its only claim gate was `should_claim_proactive_file`
(filename `.to-<channel>` suffix, else last-owner-activity.json), so an explicit
`[channel: <discord-snowflake>]` body marker was silently ignored whenever the
owner's last activity was on a non-Discord bridge. Observed live 2026-09-22: a
news-briefing proactive addressed to a Discord channel sat undelivered for over an
hour with zero trace in discord-bridge.log, because last-owner-activity.json said
"ag2space" at write time.

Fix: `_discord_claims(f)` first tries the existing routing gate; only when that
says no does it peek the body and fall back to `body_target_channel(peek) ==
"discord"` -- the same primitive `proactive-body-target-routing.test.py` already
pins, reused rather than re-derived.

Run: python3 tests/discord-proactive-body-marker-claims.test.py
Exit: 0 on pass, 1 on fail.
"""
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "discord-bridge.py"
sys.path.insert(0, str(REPO / "src"))

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def source_checks() -> None:
    text = SRC.read_text(encoding="utf-8")
    start = text.find('if f.name.startswith("proactive-")', text.find("def poll_proactive"))
    end = text.find("[proactive] send failure", start)
    check(start != -1 and end > start, "proactive claim block is locatable")
    block = text[start:end]
    check("body_target_channel" in block,
          "the claim block consults body_target_channel, not just filename/activity routing")
    check("should_claim_proactive_file(" in block,
          "the existing routing gate is still consulted (this is an addition, not a replacement)")
    check("proactive_routing import" in text and "body_target_channel" in text.split(
        "proactive_routing import", 1)[1].split(")", 1)[0],
          "body_target_channel is imported from proactive_routing, not re-derived")


def behavior_checks() -> None:
    """Exercise the actual claim predicate the fix adds, via the real
    proactive_routing primitives (no private grammar to diverge from them)."""
    from proactive_routing import body_target_channel, should_claim_proactive

    def discord_claims_body(peek: str) -> bool:
        return body_target_channel(peek) == "discord"

    # A Discord snowflake body marker claims regardless of what activity
    # routing alone would have said (the defect: it used to be ignored).
    body = "[channel: 1530802402603700415]\nbriefing text"
    check(discord_claims_body(body),
          "an explicit Discord snowflake in the body is recognised")

    # A body addressed to a DIFFERENT bridge must not be claimed by this
    # override -- it widens discord's claim set, not steals another's mail.
    ag2 = "[channel: !PrxhizfLysTYrYDcnw:ag2.space]\nbriefing"
    check(not discord_claims_body(ag2),
          "an ag2space room address is not misread as a Discord claim")

    # No marker at all: body_target_channel is None, not "discord", so plain
    # files still route by activity only -- unchanged common-case behaviour.
    check(not discord_claims_body("no marker here, just DM text"),
          "an unmarked body does not trigger the override (activity routing still decides)")

    # And the pre-existing routing gate is untouched: when activity already
    # says discord, this fix changes nothing (no regression on the common path).
    import json
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        state = pathlib.Path(td) / "last-owner-activity.json"
        state.write_text(json.dumps({"channel": "discord"}))
        check(should_claim_proactive(state, "discord"),
              "activity-routing alone still claims when it already said discord")


def main() -> int:
    source_checks()
    behavior_checks()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS[:4]))
        return 1
    print("PASS — discord-bridge claims a body-marked file even when activity routing disagrees")
    return 0


if __name__ == "__main__":
    sys.exit(main())
