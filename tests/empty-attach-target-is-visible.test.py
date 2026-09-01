#!/usr/bin/env python3
"""A file marker with NO path must reach the user, not the operator log alone.

`[file:]` and `[file: ]` both parse to `attach("")` with an empty body. Both
sinks then took the "prose quotation" branch — log-only, deliberately not
surfaced — and marked the result delivered. A result whose entire content is a
file marker therefore produced **zero user-visible output and was durably
retired**.

Scope, measured rather than assumed: at merge-base the SPACED form already did
this, so the silent branch predates #3180. What #3180 changes is the BARE form,
which used to leak `[file:]` as literal text — ugly, but visible. Aligning the
two spellings without this fix would have moved the bare form from visible to
silent, so the sink change ships with the parser change rather than after it.

An empty target is not a prose quotation: nothing quotes `[file:]` meaning a
path. It is malformed, and malformed is a thing to say out loud.

  1) the parser: bare and spaced agree, and both yield an EMPTY body
  2) the branch an empty path selects is neither "send" nor "prose quotation"
  3) both sinks route an empty target to a surfacing branch, in source
  4) a real-looking absent path still takes the quiet branch (not widened)

Run: python3 tests/empty-attach-target-is-visible.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from result_markers import parse_markers  # noqa: E402
from policy.egress.attachment import is_path_sendable  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def main() -> int:
    # 1) The parser half — this is what #3180 aligns.
    for body in ("[file:]", "[file: ]", "[send:]", "[attach: ]"):
        parsed = parse_markers(body)
        kinds = [(a.kind, a.value) for a in parsed.actions]
        check(kinds == [("attach", "")],
              f"1) {body!r} -> attach(''), got {kinds}")
        check(parsed.body == "",
              f"1) {body!r} leaves an EMPTY body, got {parsed.body!r}")

    # 2) The primitive the sinks branch on. An empty path is not sendable and
    #    is not a file — which is exactly why it fell into the quiet branch.
    check(is_path_sendable("") is False, "2) an empty path is not sendable")
    check(os.path.isfile("") is False, "2) and is not a file, so the isfile test cannot see it")

    # 3) Both sinks must now select a SURFACING branch for it. Asserted in
    #    source because the send calls live inside async/network handlers.
    discord = (REPO / "src" / "discord-bridge.py").read_text(encoding="utf-8")
    dblock = discord[discord.find("# Send files (allowlist-gated"):][:1400]
    check("elif not fpath:" in dblock,
          "3) discord-bridge branches on an empty target BEFORE the isfile test")
    check("no path" in dblock and "channel.send(" in dblock.split("elif not fpath:")[1][:400],
          "3) and that branch sends to the channel, not just print()")
    check(dblock.find("elif not fpath:") < dblock.find("elif not os.path.isfile(fpath):"),
          "3) ordering: the empty case is decided before the prose-quotation case")

    telegram = (REPO / "src" / "telegram-bridge.py").read_text(encoding="utf-8")
    tblock = telegram[telegram.find("# Send files (allowlist-gated"):][:1500]
    check("elif not fpath:" in tblock,
          "3) telegram-bridge branches on an empty target too")
    check("sendMessage" in tblock.split("elif not fpath:")[1][:400],
          "3) and that branch sends a message, not just print()")

    # 4) The quiet branch still exists for what it was FOR — a prose-quoted
    #    path that looks real and is absent. Widening it would be its own bug.
    ghost = "/tmp/definitely-not-here-9c3f1a2b/report.pdf"
    check(is_path_sendable(ghost) is False and not os.path.isfile(ghost),
          "4) an absent but real-looking path still fails both tests")
    check("likely a prose quotation" in dblock and "prose quotation" in tblock,
          "4) and both sinks keep the quiet branch for it")

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS[:3]))
        return 1
    print("PASS — a pathless file marker is spoken, not swallowed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
