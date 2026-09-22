#!/usr/bin/env python3
"""Tests for skills/agent-room-ops/room_ops.py — `context` is the name of the
Context-document FOLDER, and `doc` survives as an alias that says so.

`doc` said nothing about which store it addressed, and a room's *live*
collaborative document is a different one. An agent asked to write into the
document found this command, wrote where nobody was looking, and reported there
was no write path. The command's own help string had always said "Context
document"; only the name disagreed.

Pure, no network: the gateway is absent, so every call degrades to a structured
refusal — which is all these assertions need.
Lives under tests/ so CI auto-discovers it (find tests -name '*.test.py').
Run: python3 tests/agent-room-ops-context-rename.test.py  (exit 0 pass / 1 fail)
"""
import argparse
import contextlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "skills", "agent-room-ops"))

import room_ops  # noqa: E402

FAILS = []


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


def subcommand_names():
    captured = []
    real = argparse.ArgumentParser.add_subparsers

    def spy(self, *a, **k):
        sub = real(self, *a, **k)
        real_add = sub.add_parser

        def add(name, *aa, **kk):
            captured.append(name)
            return real_add(name, *aa, **kk)

        sub.add_parser = add
        return sub

    argparse.ArgumentParser.add_subparsers = spy
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                room_ops._main(["--help"])
            except SystemExit:
                pass
    finally:
        argparse.ArgumentParser.add_subparsers = real
    return captured


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            room_ops._main(argv)
        except SystemExit:
            pass
    return out.getvalue(), err.getvalue()


def test_context_is_registered_and_doc_still_is():
    names = subcommand_names()
    assert "context" in names, "the store's own help calls it a Context document"
    assert "doc" in names, "the old name must not break callers mid-flight"


def test_the_old_name_points_at_the_new_one_and_at_the_other_store():
    _, err = run(["doc", "get", "!x:y"])
    assert "room_ops context" in err, f"the old name must point at the new one: {err!r}"
    assert "room-collab" in err, "and must name where the LIVE document lives"


def test_the_note_never_lands_on_stdout():
    """Callers parse stdout as JSON; a note there would break every one of them
    while explaining itself."""
    out, _ = run(["doc", "get", "!x:y"])
    assert "note:" not in out, f"the note must stay off stdout: {out[:120]!r}"
    json.loads(out)


def test_the_new_name_says_nothing():
    """Control: the note is about the OLD name, not printed unconditionally."""
    out, err = run(["context", "get", "!x:y"])
    assert "note:" not in err, f"the new name must be silent: {err!r}"
    json.loads(out)


def test_both_names_reach_the_same_store():
    old_out, _ = run(["doc", "get", "!x:y"])
    new_out, _ = run(["context", "get", "!x:y"])
    assert json.loads(old_out) == json.loads(new_out), "the alias must not diverge"


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("agent-room-ops context rename: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("agent-room-ops context rename: ok")
