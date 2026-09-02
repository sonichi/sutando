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
  3c. src/remote-gateway-bridge.py imports parse_markers (file-attach change)
  4. Each bridge's marker-handling block calls parse_markers(...)
  5. result_markers.py exposes the public surface parse_markers + Action

Run: python3 tests/bridge-marker-no-leak.test.py
Exit: 0 on pass, 1 on fail.
"""

from pathlib import Path
import re
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
    if 'startswith("[no-send]")' in sb_src:
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

    # 3c. Remote-gateway bridge wires the parser (outbound file-attach change).
    # The implementation is canonical in the ag2-sparrow package
    # (src/remote-gateway-bridge.py is a thin loader shim post-#2082), so the
    # guard reads the package source; the package imports its bundled copy
    # relatively ("from .result_markers import ...").
    gb = REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "remote_gateway_bridge.py"
    gb_src = gb.read_text()
    if "from .result_markers import parse_markers" not in gb_src:
        return fail("ag2_sparrow/remote_gateway_bridge.py must import parse_markers from .result_markers")
    if "parse_markers(" not in gb_src:
        return fail("ag2_sparrow/remote_gateway_bridge.py must call parse_markers(...) somewhere")
    for hand_rolled in ('startswith("[no-send]")', 'startswith("[deduped:")'):
        if hand_rolled in gb_src:
            return fail(
                f"ag2_sparrow/remote_gateway_bridge.py still has hand-rolled skip check {hand_rolled!r} — "
                "must route through parse_markers() per #873"
            )
    # Name-independent grammar ban (mirrors the src/ consumer loop below): the
    # proactive drain once compiled its own `[channel:...]` regex, which this
    # guard waved through because only the four src/ bridges were scanned.
    # Destination-FORMAT validation (e.g. a Matrix `!room:server` shape) is
    # legitimately local; recognizing the marker grammar itself is not.
    for m in re.finditer(r"re\.compile\((.{0,120}?)\)", gb_src, re.S):
        literal = m.group(1)
        if (
            "channel:" in literal
            or "dm-only" in literal
            or re.search(r"file\s*\|\s*send\s*\|\s*attach", literal)
        ):
            return fail(
                f"ag2_sparrow/remote_gateway_bridge.py compiles a local marker "
                f"regex ({literal.strip()[:60]}...) — the marker grammar belongs "
                "solely to result_markers.py; route through parse_markers()"
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

    # ---- Adoption guards: no consumer may re-define the marker grammar ----
    # Scoped deliberately to the delivery consumers. A repo-wide ban would
    # reject docs, tests, and unrelated protocols, so keep this file-specific.
    #
    # Telegram was previously a KNOWN GAP here: send_reply() compiled its own
    # file|send|attach regex, so it stripped attachment markers but left every
    # other marker in the body — and poll_proactive() passes raw result text,
    # leaking [dm-only]/[channel:] to the owner. send_reply() now derives
    # attachments from parse_markers() actions, so Telegram is in scope.
    #
    # Slack was omitted from this tuple while src/slack-bridge.py still carried
    # a dead module-scope FILE_MARKER_RE. Delivery already went through
    # parse_markers(), so nothing misbehaved — but the guard stayed green over a
    # live drift artifact that a future edit could revive. Regex removed and
    # Slack added here, so all four delivery consumers are enforced.
    consumers = (
        "src/discord-bridge.py",
        "src/dm-result.py",
        "src/telegram-bridge.py",
        "src/slack-bridge.py",
    )
    for rel in consumers:
        src = (REPO / rel).read_text()
        if "_FILE_MARKER_RE" in src:
            return fail(
                f"{rel} defines/uses _FILE_MARKER_RE — the attachment-marker "
                "grammar belongs solely to src/result_markers.py",
                rel,
            )
        if "def _split_file_markers" in src:
            return fail(
                f"{rel} defines _split_file_markers() — derive attachments from "
                'parse_markers() actions with kind == "attach" instead',
                rel,
            )
        # Name-independent check. The two guards above only catch the grammar
        # when it is spelled with those exact identifiers; Telegram's drift was
        # an anonymous `file_pattern = re.compile(r'\[(?:file|send|attach)...')`,
        # which both would have waved through. Match the GRAMMAR itself in any
        # regex literal so a renamed local parser cannot reintroduce the drift.
        for m in re.finditer(r"re\.compile\((.{0,120}?)\)", src, re.S):
            literal = m.group(1)
            if re.search(r"file\s*\|\s*send\s*\|\s*attach", literal) or (
                "attach:" in literal and "file:" in literal
            ):
                return fail(
                    f"{rel} compiles a local attachment-marker regex "
                    f"({literal.strip()[:60]}...) — the marker grammar belongs "
                    "solely to src/result_markers.py; derive attachments from "
                    'parse_markers() actions with kind == "attach"',
                    rel,
                )

    # dm-result.py must actually USE the canonical parser for delivery prep,
    # not merely import it for skip markers.
    dm = (REPO / "src" / "dm-result.py").read_text()
    if "from result_markers import parse_markers" not in dm:
        return fail("dm-result.py does not import parse_markers from result_markers")
    if "parse_markers(text)" not in dm:
        return fail("dm-result.py does not call parse_markers(text) for delivery preparation")
    if 'kind == "attach"' not in dm:
        return fail(
            'dm-result.py does not derive attachments from actions with kind == "attach"'
        )

    # 5. The architecture doc must not describe a CONFORMING consumer as an
    # un-migrated drift instance. Section 1-4 above enforce the consumer set in
    # code; this keeps the prose maintainers actually read from drifting out of
    # sync with it. Regression: PR #2551 migrated telegram-bridge.py to
    # parse_markers and updated CLAUDE.md, but left
    # docs/architecture-boundaries.md asserting telegram-bridge.py "still
    # compiles a local file|send|attach regex" — pointing maintainers at an
    # already-closed gap. Paragraph-scoped rather than sentence-scoped because
    # the claim spans a colon ("telegram-bridge.py does not: ... still
    # compiles ...") and sentence splitting severs subject from predicate.
    consumers = {
        "discord-bridge.py": REPO / "src" / "discord-bridge.py",
        "slack-bridge.py": REPO / "src" / "slack-bridge.py",
        "telegram-bridge.py": REPO / "src" / "telegram-bridge.py",
        "dm-result.py": REPO / "src" / "dm-result.py",
    }
    conforming = {
        name
        for name, path in consumers.items()
        if path.is_file() and "from result_markers import parse_markers" in path.read_text()
    }

    doc_path = REPO / "docs" / "architecture-boundaries.md"
    if not doc_path.is_file():
        return fail("docs/architecture-boundaries.md is missing")

    # Per-consumer non-conformance assertions. Deliberately NOT including
    # paragraph-level hedges like "conformance is partial": those describe the
    # section, not a named consumer, and matching them flags conforming
    # consumers that the same paragraph correctly lists as conforming.
    non_conformance_claims = (
        "does not:",
        "still compiles a",
        "live instance of the drift",
    )
    for para in doc_path.read_text().split("\n\n"):
        flat = " ".join(para.split())
        if not any(claim in flat for claim in non_conformance_claims):
            continue
        named = sorted(n for n in conforming if n in flat)
        if named:
            return fail(
                "docs/architecture-boundaries.md describes a consumer as un-migrated, "
                f"but it imports parse_markers: {', '.join(named)}",
                flat,
            )

    print("PASS: bridges route marker decisions through parse_markers + parser strips all markers from body.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
