#!/usr/bin/env python3
"""The pre-send length guard in skills/bot2bot-post/post.py.

Hermetic BY CONSTRUCTION: exercises `check_length` on strings only. It never
loads config, never resolves a channel, and never opens a socket — the guard is
pure arithmetic over the composed content, which is the whole reason it can run
before the network call it exists to avoid.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "bot2bot_post", REPO / "skills" / "bot2bot-post" / "post.py"
)
post_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(post_mod)

LIMIT = post_mod.DISCORD_MAX_CONTENT
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name} {detail}")


def main() -> int:
    print("bot2bot-post length guard:")

    check("the limit is Discord's documented 2000", LIMIT == 2000, f"got {LIMIT}")

    # --- under / at / over the boundary ------------------------------------
    check("a short message passes", post_mod.check_length("hi") is None)
    check("EXACTLY at the limit passes",
          post_mod.check_length("x" * LIMIT) is None,
          "2000 is allowed; only >2000 is rejected")
    check("one over the limit is refused",
          post_mod.check_length("x" * (LIMIT + 1)) is not None)

    # --- the refusal has to be ACTIONABLE ----------------------------------
    # An error that says only "too long" reproduces the API's own uselessness:
    # the caller still cannot tell how much to cut.
    # `or ""` so a guard that wrongly returns None REPORTS each missing element
    # instead of raising TypeError on the first one. A crash names one symptom;
    # this names every property that regressed, which is what a failure is for.
    msg = post_mod.check_length("x" * (LIMIT + 135)) or ""
    check("names the actual length", "2135" in msg, f"got: {msg!r}")
    check("names the overage", "135" in msg, f"got: {msg!r}")
    check("names the limit", str(LIMIT) in msg, f"got: {msg!r}")
    check("states that nothing was sent",
          "NOTHING WAS SENT" in msg.upper(), f"got: {msg!r}")

    # --- the prefix is the part callers forget -----------------------------
    # `<@1509329143110565888> done: ` is 29 chars, so a body sized to exactly
    # 2000 is already over. The guard must report the body budget, not just the
    # total, or the caller trims to the wrong number and fails twice.
    overhead = len("<@1509329143110565888> done: ")
    over_msg = post_mod.check_length("x" * (LIMIT + 50), overhead=overhead) or ""
    check("with overhead, reports the BODY budget",
          str(LIMIT - overhead) in over_msg, f"overhead={overhead} got: {over_msg}")
    check("without overhead, omits the body-budget clause",
          "routing prefix" not in (post_mod.check_length("x" * (LIMIT + 50)) or ""))

    # --- POSITIVE CONTROL --------------------------------------------------
    # Every assertion above still passes against a `check_length` that returns a
    # constant string. This one fails then, because it requires a None.
    check("POSITIVE CONTROL — the guard can also say YES",
          post_mod.check_length("x" * (LIMIT - 1)) is None,
          "if this fails while the refusal cases pass, the guard refuses everything")

    # --- the composed length is what gets measured -------------------------
    # Mirrors main(): overhead is derived as len(message) - len(body), so a
    # future change to the prefix format cannot silently desync the two.
    body = "y" * 1990
    composed = f"<@123> done: {body}"
    derived = len(composed) - len(body)
    check("derived overhead matches the real prefix",
          derived == len("<@123> done: "), f"got {derived}")
    check("a body under the limit can still be refused once composed",
          post_mod.check_length(composed, overhead=derived) is not None,
          "1990-char body + 13-char prefix = 2003 > 2000")

    # --- post() ENFORCES it, and does so BEFORE the network -----------------
    # Everything above tests `check_length` in isolation, which is not the same
    # as testing the call site: the guard could be correct and simply not wired
    # into post(). Same gap @john-the-dev flagged for the main()->post() handoff.
    #
    # The network is stubbed with a RECORDER rather than a raiser, so "did not
    # send" is asserted from an observation (`calls == []`) instead of from the
    # absence of an exception — which would also hold if post() returned early
    # for some unrelated reason.
    calls: list[str] = []
    _orig_client = post_mod._client

    def _recording_transport(req, timeout):
        calls.append(getattr(req, "full_url", "?"))
        return 200, {"id": "STUB"}

    # The PRODUCTION DiscordRestClient stays in the loop; only its transport
    # is scripted, so "did not send" is observed at the real chokepoint.
    from channels.discord.client import DiscordRestClient
    post_mod._client = lambda token: DiscordRestClient(
        token, transport=_recording_transport)
    try:
        raised = ""
        try:
            post_mod.post("chan", "x" * (LIMIT + 200), "tok", overhead=29)
        except SystemExit as e:
            raised = str(e)
        check("post(): over-length raises SystemExit", bool(raised))
        check("post(): the refusal carries the measured numbers",
              "2200" in raised and "200" in raised, f"got: {raised!r}")
        check("post(): NOTHING was sent — the network was never touched", calls == [])

        # POSITIVE CONTROL for the stub itself: a normal message must reach it,
        # otherwise `calls == []` above would pass even with a mis-wired recorder.
        #
        # Wrapped, because `post()` exits the PROCESS on refusal. Against an
        # over-broad guard (one that refuses everything) this call would abort
        # the run before the summary — reporting one symptom and hiding the
        # rest. Catching it turns "the suite died" into "this assertion failed".
        try:
            post_mod.post("chan", "short and fine", "tok", overhead=9)
        except SystemExit as e:
            check("POSITIVE CONTROL — a normal message is NOT refused",
                  False, f"guard refused a legitimate message: {str(e)[:80]}")
        check("POSITIVE CONTROL — a normal message DOES reach the network",
              len(calls) == 1, f"calls={calls}")
    finally:
        post_mod._client = _orig_client

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("bot2bot-post length guard: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
