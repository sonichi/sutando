#!/usr/bin/env python3
"""Drives src/artifact_surface.py: the producer refuses what a renderer would
have to guess about, and the degrade path never raises.

Run: python3 tests/artifact-surface.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import artifact_surface as a  # noqa: E402

FAILS = []


def check(label, cond):
    print(f"  {'ok  ' if cond else 'FAIL'}{label}")
    if not cond:
        FAILS.append(label)


def main() -> int:
    print("artifact-surface:")

    art = a.build(a.X_POST, {"text": "hello"})
    check("build stamps type, version and content",
          art == {"type": "x_post", "version": a.SCHEMA_VERSION, "content": {"text": "hello"}})

    check("extra_content rides the space.ag2.* channel",
          a.as_extra_content(art) == {"space.ag2.artifact": art})

    for bad, label in [
        (lambda: a.build("mastodon_post", {"text": "x"}), "unknown type refused"),
        (lambda: a.build(a.X_POST, {}), "missing required key refused"),
        (lambda: a.build(a.X_POST, {"text": "   "}), "whitespace-only required key refused"),
        (lambda: a.build(a.EMAIL, {"subject": "s"}), "email without body refused"),
        (lambda: a.build(a.X_POST, "not a dict"), "non-dict content refused"),
    ]:
        try:
            bad()
            check(label, False)
        except a.ArtifactError:
            check(label, True)
        except Exception as exc:  # noqa: BLE001 - any other type is the bug
            check(f"{label} (raised {type(exc).__name__})", False)

    check("email with both keys builds",
          a.build(a.EMAIL, {"subject": "s", "body": "b"})["type"] == "email")

    # The degrade path is the one that must never raise: it only runs when the
    # renderer has already met something unfamiliar.
    check("a known artifact does not degrade", a.degrade_to_plain(art) is None)

    newer = {"type": "x_post", "version": a.SCHEMA_VERSION + 1, "content": {"text": "future"}}
    d = a.degrade_to_plain(newer)
    check("a newer version degrades rather than rendering",
          d is not None and d["reason"] == "newer_version" and d["text"] == "future")

    unknown = {"type": "poll", "version": 1, "content": {"text": "pick one"}}
    d = a.degrade_to_plain(unknown)
    check("an unknown type degrades and keeps the writing",
          d is not None and d["reason"] == "unknown_type" and d["text"] == "pick one")

    d = a.degrade_to_plain({"type": "email", "version": 99, "content": {"body": "b"}})
    check("degrade falls back through body when text is absent",
          d is not None and d["text"] == "b")

    for junk, label in [
        (None, "None"), ("string", "a string"), ([], "a list"),
        ({}, "an empty dict"),
        ({"type": "x_post", "version": "nonsense", "content": {}}, "an unparseable version"),
        ({"type": "x_post", "version": 1, "content": "not a dict"}, "non-dict content"),
    ]:
        try:
            out = a.degrade_to_plain(junk)
            check(f"degrade survives {label}", isinstance(out, dict))
        except Exception as exc:  # noqa: BLE001
            check(f"degrade survives {label} (raised {type(exc).__name__})", False)

    check("version 0 degrades rather than passing",
          a.degrade_to_plain({"type": "x_post", "version": 0, "content": {}}) is not None)

    print(f"artifact-surface: {'FAIL (' + str(len(FAILS)) + ')' if FAILS else 'PASS'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
