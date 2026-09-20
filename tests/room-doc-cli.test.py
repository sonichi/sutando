#!/usr/bin/env python3
"""Credential and URL resolution for skills/room-doc's CLI.

Precedence is the part that bites: a flag silently losing to an inherited env
var sends an agent to the wrong homeserver with the wrong identity, and both
look like a working command.
"""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-doc" / "scripts"))

import json  # noqa: E402

import room_doc  # noqa: E402
from room_doc_protocol import RoomDocError  # noqa: E402

FAILS = []


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


def clear_env():
    for v in room_doc.TOKEN_VARS + room_doc.URL_VARS:
        os.environ.pop(v, None)


def test_an_explicit_flag_beats_every_env_var():
    clear_env()
    for v in room_doc.TOKEN_VARS:
        os.environ[v] = f"env-{v}"
    try:
        assert room_doc.resolve_token("flag-wins") == "flag-wins"
    finally:
        clear_env()


def test_env_vars_are_tried_in_declared_order():
    clear_env()
    try:
        # Set only the LAST one: it must still be found.
        os.environ[room_doc.TOKEN_VARS[-1]] = "last"
        assert room_doc.resolve_token(None) == "last"
        # Now set the first too: precedence is the declared order, not chance.
        os.environ[room_doc.TOKEN_VARS[0]] = "first"
        assert room_doc.resolve_token(None) == "first"
    finally:
        clear_env()


def test_a_missing_credential_says_what_to_set_and_does_not_warn_off_the_relay_token():
    """The old error said a relay token "is refused (403) by design". Three
    agents read that and skipped the one credential they held that works."""
    clear_env()
    try:
        room_doc.resolve_token(None)
        raise AssertionError("expected RoomDocError")
    except RoomDocError as e:
        text = str(e)
        for var in room_doc.TOKEN_VARS:
            assert var in text, f"the error must name {var}"
        assert "refused" not in text and "by design" not in text, text


def test_the_relay_token_is_read_in_either_shape():
    """Same secret ships bare on one install and as url|secret on another,
    under the SAME variable name. The name must not be trusted to imply the
    format; the value decides."""
    clear_env()
    try:
        os.environ["REMOTE_TASK_TOKEN"] = "s3cret"
        assert room_doc.resolve_token(None) == "s3cret"
        os.environ["REMOTE_TASK_TOKEN"] = "https://chat.example/relay|s3cret"
        assert room_doc.resolve_token(None) == "s3cret", "the compound form must be split"
    finally:
        clear_env()


def test_an_explicit_compound_token_is_split_too():
    """--token copied straight out of a .env line is the realistic input."""
    assert room_doc.resolve_token("https://h/relay|abc") == "abc"
    assert room_doc.resolve_token("plain|not-a-url") == "plain|not-a-url", \
        "only a URL prefix marks the compound form"


def test_the_relay_variables_come_after_the_document_specific_ones():
    clear_env()
    try:
        os.environ["REMOTE_TASK_TOKEN"] = "relay"
        os.environ["ROOM_DOC_TOKEN"] = "specific"
        assert room_doc.resolve_token(None) == "specific"
    finally:
        clear_env()


def test_the_url_falls_back_to_the_relay_origin():
    """Every agent has REMOTE_TASK_URL; none had AG2_API_ROOT. One guessed the
    host from it by hand and happened to be right."""
    clear_env()
    try:
        os.environ["REMOTE_TASK_URL"] = "https://chat.example/relay"
        assert room_doc.resolve_url(None) == "https://chat.example", "path stripped, origin kept"
        os.environ["AG2_API_ROOT"] = "https://api.example/v"
        assert room_doc.resolve_url(None) == "https://api.example/v", \
            "an explicit api root wins and keeps its path"
    finally:
        clear_env()


def test_a_compound_token_alone_is_enough_to_find_the_service():
    clear_env()
    try:
        os.environ["AG2_REMOTE_TOKEN"] = "https://chat.example/relay|s3cret"
        assert room_doc.resolve_url(None) == "https://chat.example"
        assert room_doc.resolve_token(None) == "s3cret"
    finally:
        clear_env()


def test_a_missing_url_names_its_variables():
    clear_env()
    try:
        room_doc.resolve_url(None)
        raise AssertionError("expected RoomDocError")
    except RoomDocError as e:
        for var in room_doc.URL_VARS:
            assert var in str(e), f"the error must name {var}"


def test_every_subcommand_takes_a_room_and_replace_takes_both_texts():
    p = room_doc.build_parser()
    for cmd in ("read", "peers", "append", "replace"):
        argv = {"read": ["read", "!r:s"], "peers": ["peers", "!r:s"],
                "append": ["append", "!r:s", "text"],
                "replace": ["replace", "!r:s", "old", "new"]}[cmd]
        a = p.parse_args(argv)
        assert a.command == cmd and a.room == "!r:s", f"{cmd} did not parse"
    a = p.parse_args(["replace", "!r:s", "old", "new"])
    assert (a.old, a.new) == ("old", "new")


def test_insecure_is_off_unless_asked():
    p = room_doc.build_parser()
    assert p.parse_args(["read", "!r:s"]).insecure is False, "TLS verification must be the default"
    assert p.parse_args(["--insecure", "read", "!r:s"]).insecure is True


def test_read_prints_the_document_plainly_and_the_json_mode_parses():
    assert room_doc.render("read", text="hello") == "hello"
    payload = json.loads(room_doc.render("read", text="hello", as_json=True))
    assert payload["text"] == "hello" and payload["chars"] == 5, payload
    assert payload["peers"] == []


def test_every_machine_mode_emits_valid_json():
    """A caller parsing stdout must never meet prose where JSON was promised."""
    for out in (room_doc.render("peers", peers=[{"name": "mars"}]),
                room_doc.render("append", text="abcd", before=1),
                room_doc.render("replace", text="abcd"),
                room_doc.render("read", text="x", as_json=True)):
        json.loads(out)


def test_append_reports_the_growth_it_caused():
    payload = json.loads(room_doc.render("append", text="abcdef", before=2))
    assert payload == {"ok": True, "before": 2, "after": 6}, payload


def test_an_unknown_command_refuses_rather_than_printing_nothing():
    try:
        room_doc.render("nope")
        raise AssertionError("expected RoomDocError")
    except RoomDocError:
        pass


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("room-doc cli: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-doc cli: ok")
