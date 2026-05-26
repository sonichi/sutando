#!/usr/bin/env python3
"""
Structural cross-check that every bridge routes its result-marker decisions
through a unified parser. This is the no-leak invariant guard: as long as
each bridge calls the parser, the per-bridge implementations can't drift
back to hand-rolled startswith checks that miss markers and ship them as
literal text.

Why structural and not behavioral: behavioral cross-bridge testing would
require importing each bridge module, which has side effects (Bolt App
init, env-var reads, dlopen of slack_bolt etc.). The structural check
catches the regression we actually care about — "did someone replace the
parse_markers call with a hand-rolled regex again?" — at near-zero cost
and zero import surface.

Guards:
  1. src/result_markers.py exposes public surface parse_markers + Action
  2. src/slack-bridge.py imports and calls parse_markers
  3. src/telegram-bridge.py imports and calls parse_markers
  4. src/task-bridge.ts has detectSkipMarker() covering all three skip
     markers ([no-send], [REPLIED], [deduped:]) — TS analog of the skip
     phase in result_markers.py (#896)
  5. Behavior smoke test of the Python parser itself

When discord-bridge.py is wired through parse_markers (PR #1174), add a
guard here matching the pattern in guards 2 and 3.

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

    # 4. Task-bridge (TypeScript) has detectSkipMarker() covering all skip
    #    markers (#896). The TS port doesn't share the Python module but must
    #    handle the same three skip markers so no marker leaks through voice.
    tb_ts = REPO / "src" / "task-bridge.ts"
    tb_ts_src = tb_ts.read_text()
    if "detectSkipMarker" not in tb_ts_src:
        return fail(
            "src/task-bridge.ts missing detectSkipMarker() function — "
            "skip markers ([no-send],[REPLIED],[deduped:]) not handled (#896)"
        )
    for marker in ("no-send", "REPLIED", "deduped"):
        if marker not in tb_ts_src:
            return fail(
                f"src/task-bridge.ts detectSkipMarker does not cover [{marker}] — "
                "incomplete skip-marker support (#896)"
            )
    # Must call detectSkipMarker in the result-processing loop, not just define it.
    if tb_ts_src.count("detectSkipMarker(") < 2:
        return fail(
            "src/task-bridge.ts defines detectSkipMarker but appears not to call it "
            "in the result-processing loop (#896)"
        )

    # 5. Behavior smoke test of the Python parser itself
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

    print("PASS: bridges route marker decisions through unified parser; task-bridge detectSkipMarker covers all skip markers; parser strips all markers from body.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
