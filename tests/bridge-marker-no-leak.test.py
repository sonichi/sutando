#!/usr/bin/env python3
"""
Structural cross-check that every bridge routes its result-marker decisions
through `src/result_markers.py:parse_markers` (#873). This is the
no-leak invariant guard: as long as each bridge calls the unified parser,
the per-bridge implementations can't drift back to hand-rolled startswith
checks that miss markers and ship them as literal text.

Why structural and not behavioral: behavioral cross-bridge testing would
require importing each bridge module, which has side effects (Bolt App
init, env-var reads, dlopen of slack_bolt etc.). The structural check
catches the regression we actually care about — "did someone replace the
parse_markers call with a hand-rolled regex again?" — at near-zero cost
and zero import surface.

Guards:
  1. src/slack-bridge.py imports parse_markers from result_markers
  2. src/telegram-bridge.py imports parse_markers from result_markers
  3. src/discord-bridge.py imports parse_markers from result_markers (#896)
  4. Each bridge's marker-handling block calls parse_markers(...)
  5. result_markers.py exposes the public surface parse_markers + Action

Run: python3 tests/bridge-marker-no-leak.test.py
Exit: 0 on pass, 1 on fail.
"""

from pathlib import Path
import sys

REPO = Path(__file__).resolve().parent.parent


def fail(msg: str, ctx: str = "") -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    if ctx:
        print("---context---", file=sys.stderr)
        print(ctx[:1500], file=sys.stderr)
    return 1


def main() -> int:
    # 1. Module exists with the expected public surface
    rm = REPO / "src" / "result_markers.py"
    if not rm.exists():
        return fail(f"{rm} not found — #873 module missing")
    rm_src = rm.read_text()
    for name in ("def parse_markers", "class Action", "class ParseResult"):
        if name not in rm_src:
            return fail(f"src/result_markers.py missing public surface: {name}")

    # 2. Slack bridge wires the parser
    sb = REPO / "src" / "slack-bridge.py"
    sb_src = sb.read_text()
    if "from result_markers import parse_markers" not in sb_src:
        return fail("src/slack-bridge.py must import parse_markers from result_markers")
    if "parse_markers(" not in sb_src:
        return fail("src/slack-bridge.py must call parse_markers(...) somewhere")
    # Specifically, the result-watcher's skip-detection block must call it,
    # not a hand-rolled startswith trio.
    if 'startswith("[no-send]")' in sb_src and "parse_markers" not in sb_src:
        return fail(
            "src/slack-bridge.py still has hand-rolled startswith — must route "
            "through parse_markers() per #873"
        )

    # 3. Telegram bridge wires the parser
    tb = REPO / "src" / "telegram-bridge.py"
    tb_src = tb.read_text()
    if "from result_markers import parse_markers" not in tb_src:
        return fail("src/telegram-bridge.py must import parse_markers from result_markers")
    if "parse_markers(" not in tb_src:
        return fail("src/telegram-bridge.py must call parse_markers(...) somewhere")
    # Telegram-specific: the [deduped:] bug fix from #873 — the marker
    # MUST be detected. If only [no-send] / [REPLIED] are checked (the
    # pre-#873 state), this PR is incomplete.
    if "deduped" not in tb_src:
        return fail(
            "src/telegram-bridge.py does not reference 'deduped' anywhere — "
            "the unified-parser wire-through likely got dropped"
        )

    # 3b. Discord bridge wires the parser (#896)
    db = REPO / "src" / "discord-bridge.py"
    db_src = db.read_text()
    if "from result_markers import parse_markers" not in db_src:
        return fail("src/discord-bridge.py must import parse_markers from result_markers (#896)")
    if "parse_markers(" not in db_src:
        return fail("src/discord-bridge.py must call parse_markers(...) somewhere (#896)")
    # No hand-rolled startswith skip-detection should remain in the send paths
    for hand_rolled in (".startswith('[no-send]')", ".startswith('[REPLIED]')", ".startswith('[deduped:')"):
        if hand_rolled in db_src:
            return fail(
                f"src/discord-bridge.py still has hand-rolled skip check {hand_rolled!r} — "
                "must route through parse_markers() per #896"
            )

    # 4. Behavior smoke test of the parser itself
    sys.path.insert(0, str(REPO / "src"))
    from result_markers import parse_markers

    # Skip terminal: no body, only skip action
    r = parse_markers("[deduped: task-1]\nsecret body")
    if r.body:
        return fail(f"parse_markers leaked body content past a skip marker: {r.body!r}")
    if not any(a.kind == "skip" for a in r.actions):
        return fail("parse_markers did not emit a skip action for [deduped:]")

    # Redirect: body stripped, action present
    r = parse_markers("[channel: C0B4N6DSY90]\nhello")
    if "[channel:" in r.body:
        return fail(f"parse_markers leaked [channel:] marker into body: {r.body!r}")
    if not any(a.kind == "redirect" for a in r.actions):
        return fail("parse_markers did not emit a redirect action")

    # Attach: paths extracted, marker stripped
    r = parse_markers("body [file: /tmp/sutando-x.png] tail")
    if "[file:" in r.body:
        return fail(f"parse_markers leaked [file:] marker into body: {r.body!r}")
    if not any(a.kind == "attach" for a in r.actions):
        return fail("parse_markers did not emit an attach action")

    print("PASS: bridges route marker decisions through parse_markers + parser strips all markers from body.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
