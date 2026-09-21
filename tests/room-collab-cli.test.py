#!/usr/bin/env python3
"""Credential and URL resolution for skills/room-collab's CLI.

Precedence is the part that bites: a flag silently losing to an inherited env
var sends an agent to the wrong homeserver with the wrong identity, and both
look like a working command.
"""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

import json  # noqa: E402

import room_collab  # noqa: E402
from room_collab_protocol import RoomDocError  # noqa: E402

FAILS = []


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


def clear_env():
    for v in room_collab.TOKEN_VARS + room_collab.URL_VARS:
        os.environ.pop(v, None)


def test_an_explicit_flag_beats_every_env_var():
    clear_env()
    for v in room_collab.TOKEN_VARS:
        os.environ[v] = f"env-{v}"
    try:
        assert room_collab.resolve_token("flag-wins") == "flag-wins"
    finally:
        clear_env()


def test_env_vars_are_tried_in_declared_order():
    clear_env()
    try:
        # Set only the LAST one: it must still be found.
        os.environ[room_collab.TOKEN_VARS[-1]] = "last"
        assert room_collab.resolve_token(None) == "last"
        # Now set the first too: precedence is the declared order, not chance.
        os.environ[room_collab.TOKEN_VARS[0]] = "first"
        assert room_collab.resolve_token(None) == "first"
    finally:
        clear_env()


def test_a_missing_credential_says_what_to_set_and_does_not_warn_off_the_relay_token():
    """The old error said a relay token "is refused (403) by design". Three
    agents read that and skipped the one credential they held that works."""
    clear_env()
    try:
        room_collab.resolve_token(None)
        raise AssertionError("expected RoomDocError")
    except RoomDocError as e:
        text = str(e)
        for var in room_collab.TOKEN_VARS:
            assert var in text, f"the error must name {var}"
        assert "refused" not in text and "by design" not in text, text


def test_the_relay_token_is_read_in_either_shape():
    """Same secret ships bare on one install and as url|secret on another,
    under the SAME variable name. The name must not be trusted to imply the
    format; the value decides."""
    clear_env()
    try:
        os.environ["REMOTE_TASK_TOKEN"] = "s3cret"
        assert room_collab.resolve_token(None) == "s3cret"
        os.environ["REMOTE_TASK_TOKEN"] = "https://chat.example/relay|s3cret"
        assert room_collab.resolve_token(None) == "s3cret", "the compound form must be split"
    finally:
        clear_env()


def test_an_explicit_compound_token_is_split_too():
    """--token copied straight out of a .env line is the realistic input."""
    assert room_collab.resolve_token("https://h/relay|abc") == "abc"
    assert room_collab.resolve_token("plain|not-a-url") == "plain|not-a-url", \
        "only a URL prefix marks the compound form"


def test_the_relay_variables_come_after_the_document_specific_ones():
    clear_env()
    try:
        os.environ["REMOTE_TASK_TOKEN"] = "relay"
        os.environ["ROOM_DOC_TOKEN"] = "specific"
        assert room_collab.resolve_token(None) == "specific"
    finally:
        clear_env()


def test_the_url_falls_back_to_the_relay_origin():
    """Every agent has REMOTE_TASK_URL; none had AG2_API_ROOT. One guessed the
    host from it by hand and happened to be right."""
    clear_env()
    try:
        os.environ["REMOTE_TASK_URL"] = "https://chat.example/relay"
        assert room_collab.resolve_url(None) == "https://chat.example", "path stripped, origin kept"
        os.environ["AG2_API_ROOT"] = "https://api.example/v"
        assert room_collab.resolve_url(None) == "https://api.example/v", \
            "an explicit api root wins and keeps its path"
    finally:
        clear_env()


def test_a_compound_token_alone_is_enough_to_find_the_service():
    clear_env()
    try:
        os.environ["AG2_REMOTE_TOKEN"] = "https://chat.example/relay|s3cret"
        assert room_collab.resolve_url(None) == "https://chat.example"
        assert room_collab.resolve_token(None) == "s3cret"
    finally:
        clear_env()


def test_doctor_names_the_source_and_shape_but_never_the_secret():
    """A new agent's first failure is discovery: WHICH variable, WHICH shape,
    WHICH host. The report answers those and must not echo the token."""
    env = {"REMOTE_TASK_TOKEN": "https://chat.example/relay|s3cretvalue"}
    rows = {step: (ok, detail) for step, ok, detail in room_collab.credential_report(None, None, env)}
    assert rows["token"][0] and "REMOTE_TASK_TOKEN" in rows["token"][1]
    assert "compound" in rows["token"][1] and "11 chars" in rows["token"][1]
    assert "s3cretvalue" not in rows["token"][1], "the secret leaked into the report"
    assert rows["url"][0] and "compound token" in rows["url"][1], \
        "with no URL variable the compound token's origin is the source"


def test_doctor_reports_each_missing_piece_on_its_own_row():
    rows = {step: ok for step, ok, _ in room_collab.credential_report(None, None, {})}
    assert rows == {"token": False, "url": False}
    rows = {step: ok for step, ok, _ in room_collab.credential_report("t", None, {"AG2_API_ROOT": "x"})}
    assert rows == {"token": True, "url": True}


def _run_doctor(env, opener=None, kind="markdown"):
    """doctor() with a scripted opener, stdout captured, env isolated."""
    import asyncio
    import contextlib
    import io
    import types

    import room_collab_client
    saved = {k: os.environ.pop(k) for k in list(os.environ)
             if k in room_collab.TOKEN_VARS + room_collab.URL_VARS}
    os.environ.update(env)
    real = room_collab_client.open_room_collab
    if opener is not None:
        room_collab_client.open_room_collab = opener
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rc = asyncio.run(room_collab.doctor(types.SimpleNamespace(
                room="!r:x", kind=kind, token=None, url=None, insecure=False)))
    finally:
        room_collab_client.open_room_collab = real
        for k in room_collab.TOKEN_VARS + room_collab.URL_VARS:
            os.environ.pop(k, None)
        os.environ.update(saved)
    return rc, out.getvalue()


def _opener(doc=None, refuse=None):
    import contextlib

    @contextlib.asynccontextmanager
    async def open_room_collab(url, room, token, kind=None, insecure=False):
        if refuse:
            raise RoomDocError(refuse)
        yield doc

    return open_room_collab


class _Doc:
    text = "hello"
    peers = [{"name": "q"}]
    elements = [1, 2, 3]


def test_doctor_reports_every_step_ok_when_all_is_well():
    rc, out = _run_doctor({"REMOTE_TASK_TOKEN": "s3cret", "REMOTE_TASK_URL": "https://h/relay"},
                          _opener(_Doc()))
    assert rc == 0, out
    for step in ("deps", "token", "url", "connect", "read", "peers"):
        assert f"ok    {step}" in out, out
    assert "5 chars" in out and "1 present" in out and "s3cret" not in out


def test_doctor_reads_elements_on_the_board():
    rc, out = _run_doctor({"REMOTE_TASK_TOKEN": "t", "AG2_API_ROOT": "https://h"},
                          _opener(_Doc()), kind="board")
    assert rc == 0 and "3 elements" in out, out


def test_doctor_stops_at_the_missing_credential():
    rc, out = _run_doctor({"REMOTE_TASK_URL": "https://h/relay"}, _opener(_Doc()))
    assert rc == 2 and "FAIL  token" in out and "connect" not in out, out


def test_doctor_names_the_refusal_as_the_connect_step():
    rc, out = _run_doctor({"REMOTE_TASK_TOKEN": "t", "REMOTE_TASK_URL": "https://h/relay"},
                          _opener(refuse="refused (403) by h: no"))
    assert rc == 2 and "FAIL  connect  refused (403)" in out, out


def test_doctor_stops_at_missing_deps_and_says_how_to_install():
    """A None entry in sys.modules makes `import websockets` raise ImportError,
    which is what a bare interpreter without the requirements does."""
    import sys
    saved = sys.modules.get("websockets")
    sys.modules["websockets"] = None  # type: ignore[assignment]
    try:
        rc, out = _run_doctor({"REMOTE_TASK_TOKEN": "t", "REMOTE_TASK_URL": "https://h/relay"})
    finally:
        if saved is None:
            sys.modules.pop("websockets", None)
        else:
            sys.modules["websockets"] = saved
    assert rc == 2 and "FAIL  deps" in out and "requirements.txt" in out, out
    assert "token" not in out, "stops at the first failing step"


def test_run_dispatches_doctor_before_opening_any_socket():
    """`run()` must answer doctor without a connection: that is the command
    an agent runs BEFORE it knows whether a connection is possible."""
    import asyncio
    import contextlib
    import io
    import types

    import room_collab_client
    real = room_collab_client.open_room_collab
    calls = []

    @contextlib.asynccontextmanager
    async def opener(url, room, token, kind=None, insecure=False):
        calls.append(room)
        yield _Doc()

    room_collab_client.open_room_collab = opener
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k in room_collab.TOKEN_VARS + room_collab.URL_VARS}
    os.environ["REMOTE_TASK_TOKEN"] = "t"
    os.environ["REMOTE_TASK_URL"] = "https://h/relay"
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rc = asyncio.run(room_collab.run(types.SimpleNamespace(
                command="doctor", room="!r:x", kind="markdown", token=None, url=None,
                insecure=False, name=None, user_id=None, json=False, settle=0,
                with_authors=False)))
    finally:
        room_collab_client.open_room_collab = real
        for k in room_collab.TOKEN_VARS + room_collab.URL_VARS:
            os.environ.pop(k, None)
        os.environ.update(saved)
    assert rc == 0 and calls == ["!r:x"], (rc, calls, out.getvalue())


def test_the_collab_names_lead_and_the_doc_names_are_still_read():
    """The rename must not strand an install that set the ROOM_DOC_* names:
    they are read after the collab names, for one release."""
    clear_env()
    try:
        os.environ["ROOM_DOC_TOKEN"] = "old"
        assert room_collab.resolve_token(None) == "old"
        os.environ["ROOM_COLLAB_TOKEN"] = "new"
        assert room_collab.resolve_token(None) == "new", "the collab name wins when both are set"
        os.environ["AG2_ROOM_DOC_URL"] = "https://old.example"
        assert room_collab.resolve_url(None) == "https://old.example"
        os.environ["AG2_ROOM_COLLAB_URL"] = "https://new.example"
        assert room_collab.resolve_url(None) == "https://new.example"
    finally:
        clear_env()


def test_a_missing_url_names_its_variables():
    clear_env()
    try:
        room_collab.resolve_url(None)
        raise AssertionError("expected RoomDocError")
    except RoomDocError as e:
        for var in room_collab.URL_VARS:
            assert var in str(e), f"the error must name {var}"


def test_every_subcommand_takes_a_room_and_replace_takes_both_texts():
    p = room_collab.build_parser()
    for cmd in ("read", "peers", "append", "replace"):
        argv = {"read": ["read", "!r:s"], "peers": ["peers", "!r:s"],
                "append": ["append", "!r:s", "text"],
                "replace": ["replace", "!r:s", "old", "new"]}[cmd]
        a = p.parse_args(argv)
        assert a.command == cmd and a.room == "!r:s", f"{cmd} did not parse"
    a = p.parse_args(["replace", "!r:s", "old", "new"])
    assert (a.old, a.new) == ("old", "new")


def test_insecure_is_off_unless_asked():
    p = room_collab.build_parser()
    assert p.parse_args(["read", "!r:s"]).insecure is False, "TLS verification must be the default"
    assert p.parse_args(["--insecure", "read", "!r:s"]).insecure is True


def test_read_prints_the_document_plainly_and_the_json_mode_parses():
    assert room_collab.render("read", text="hello") == "hello"
    payload = json.loads(room_collab.render("read", text="hello", as_json=True))
    assert payload["text"] == "hello" and payload["chars"] == 5, payload
    assert payload["peers"] == []


def test_every_machine_mode_emits_valid_json():
    """A caller parsing stdout must never meet prose where JSON was promised."""
    for out in (room_collab.render("peers", peers=[{"name": "mars"}]),
                room_collab.render("append", text="abcd", before=1),
                room_collab.render("replace", text="abcd"),
                room_collab.render("read", text="x", as_json=True)):
        json.loads(out)


def test_append_reports_the_growth_it_caused():
    payload = json.loads(room_collab.render("append", text="abcdef", before=2))
    assert payload == {"ok": True, "before": 2, "after": 6}, payload


def test_delta_is_the_new_lines_and_a_first_read_is_all_new():
    assert room_collab.delta_since(None, "a\n\nb\n") == ["a", "b"], "no earlier read: everything"
    assert room_collab.delta_since("a\nb", "a\nb\nc") == ["c"]
    assert room_collab.delta_since("a\nb", "a\nb") == []
    assert room_collab.delta_since("todo: x", "todo: x\ntodo: x") == ["todo: x"], "written again is new again"


def test_a_read_is_remembered_per_room_and_surface_and_recalled_with_its_time():
    import tempfile
    ws = Path(tempfile.mkdtemp())
    p = room_collab.snapshot_path(ws, "!r:x", "markdown")
    assert p.parent == ws / "state" / "room-collab" and "!" not in p.name and ":" not in p.name
    assert p != room_collab.snapshot_path(ws, "!r:x", "board"), "a surface has its own memory"
    assert p != room_collab.snapshot_path(ws, "!other:x", "markdown")
    # Seats share a workspace: two readers of one surface keep two memories.
    mine = room_collab.snapshot_path(ws, "!r:x", "markdown", "@mars:x")
    theirs = room_collab.snapshot_path(ws, "!r:x", "markdown", "@sudoo:x")
    assert mine != theirs and mine != p
    import types
    assert room_collab.reader_identity(types.SimpleNamespace(user_id="@m:x", name="Mars")) == "@m:x"
    assert room_collab.reader_identity(types.SimpleNamespace(user_id=None, name="Mars")) in ("Mars",) + tuple(
        os.environ.get(v) for v in room_collab.IDENTITY_VARS if os.environ.get(v))
    assert room_collab.recall(p) == (None, None), "nothing yet"
    room_collab.remember(p, "first\nsecond")
    text, at = room_collab.recall(p)
    assert text == "first\nsecond" and isinstance(at, float) and at > 0
    assert not p.with_suffix(".tmp").exists(), "written atomically, no temp file left"


def test_presence_asks_the_service_without_a_socket_and_reads_the_counts():
    import io
    seen = {}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout=0):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        return _Resp(json.dumps({"room": "!r:x", "surfaces": {
            "markdown": {"peers": 2, "agents": 1}, "board": {"peers": 0, "agents": 0},
            "kanban": {"peers": "junk"}, "weird": "not a dict"}}).encode())

    got = room_collab.presence_summary("https://h", "!r:x", "tok", opener=opener)
    assert seen["url"] == "https://h/api/v1/room-collab/%21r%3Ax/presence", seen
    assert seen["auth"] == "Bearer tok"
    assert got == {"markdown": {"peers": 2, "agents": 1}, "board": {"peers": 0, "agents": 0},
                   "kanban": {"peers": 0, "agents": 0}}, got
    # An origin that already names the path is not doubled.
    room_collab.presence_summary("https://h/api/v1/room-collab", "!r:x", "tok", opener=opener)
    assert seen["url"].count("/api/v1/room-collab") == 1
    text = room_collab.render_presence("!r:x", got, as_json=False)
    assert "markdown     2 present  (1 agent(s), 1 person(s))" in text, text
    assert json.loads(room_collab.render_presence("!r:x", got, as_json=True))["surfaces"] == got
    assert "nobody is in any surface" in room_collab.render_presence("!r:x", {"markdown": {"peers": 0, "agents": 0}}, False)


def test_presence_refusals_and_bad_bodies_are_named_not_swallowed():
    import io
    import urllib.error

    def refuse(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, io.BytesIO(b""))

    def garbage(req, timeout=0):
        class _R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return _R(b'{"room": "!r:x"}')

    for opener, needle in ((refuse, "presence refused (403)"), (garbage, "without surfaces")):
        try:
            room_collab.presence_summary("https://h", "!r:x", "tok", opener=opener)
        except RoomDocError as e:
            assert needle in str(e), str(e)
        else:
            raise AssertionError(f"{needle}: should have raised")


def test_read_delta_prints_only_what_is_new_since_the_last_read():
    import asyncio
    import contextlib
    import io
    import tempfile
    import room_collab_client
    ws = tempfile.mkdtemp()

    class _D:
        text = "line one\nline two"
        peers = []
        authors = {}

    def run(*argv):
        args = room_collab.build_parser().parse_args(["--workspace", ws, "--url", "https://h", "--token", "t", *argv])
        real = room_collab_client.open_room_collab
        room_collab_client.open_room_collab = _opener(_D())
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = asyncio.run(room_collab.run(args))
        finally:
            room_collab_client.open_room_collab = real
        return rc, out.getvalue()

    rc, out = run("read", "--delta", "!r:x")
    assert rc == 0 and "no earlier read" in out and "line one" in out and "line two" in out, out
    _D.text = "line one\nline two\nline three"
    rc, out = run("read", "--delta", "!r:x")
    assert rc == 0 and "1 new line(s) since your last read at" in out, out
    assert "line three" in out and "line one" not in out, out
    rc, out = run("--json", "read", "--delta", "!r:x")
    body = json.loads(out)
    assert body["delta"] == [] and body["since"] and body["chars"] == len(_D.text), body
    rc, out = run("read", "!r:x")
    assert rc == 0 and out.strip() == _D.text, "a plain read is unchanged, and it remembers too"


def test_an_unknown_command_refuses_rather_than_printing_nothing():
    try:
        room_collab.render("nope")
        raise AssertionError("expected RoomDocError")
    except RoomDocError:
        pass


# --- comment: where the quote is, what the message is, how it is posted ----

DOC = "Plan\n\noption A is cheap\noption B is fast\noption A is cheap too\n"


def _refuses(fn, *words):
    try:
        fn()
    except RoomDocError as e:
        for w in words:
            assert w in str(e), (w, str(e))
        return
    raise AssertionError("expected RoomDocError")


def test_a_unique_quote_is_found_and_is_the_first_occurrence():
    assert room_collab.locate_quote(DOC, "option B is fast") == (DOC.index("option B"), 0)


def test_an_ambiguous_quote_refuses_unless_nth_picks():
    _refuses(lambda: room_collab.locate_quote(DOC, "option A is cheap"), "occurs 2 times", "--nth 0..1")
    second = DOC.index("option A is cheap too")
    assert room_collab.locate_quote(DOC, "option A is cheap", 1) == (second, 1)
    _refuses(lambda: room_collab.locate_quote(DOC, "option A is cheap", 2), "--nth 2", "2 time(s)")


def test_an_absent_empty_or_oversized_quote_refuses():
    _refuses(lambda: room_collab.locate_quote(DOC, "option C"), "not in the document")
    _refuses(lambda: room_collab.locate_quote(DOC, ""), "empty")
    _refuses(lambda: room_collab.locate_quote("x" * 3000, "x" * 2001), "at most 2000")


def test_the_comment_is_a_message_a_plain_client_reads_and_an_anchor_the_collab_client_pins():
    anchor = {"start": "AAA=", "end": "BBB="}
    body, extra = room_collab.comment_content(anchor, "option B is fast", 0, "  is it? ")
    # The quote leads, then a blank line, then the words: what commentText() strips.
    assert body == "> option B is fast\n\nis it?", body
    inner = extra[room_collab.COMMENT_KEY]
    assert inner == {"anchor": {"start": "AAA=", "end": "BBB=", "quote": "option B is fast", "nth": 0}, "v": 1}
    assert isinstance(inner["v"], int) and isinstance(inner["anchor"]["nth"], int)
    _refuses(lambda: room_collab.comment_content(anchor, "q", 0, "   "), "something to say")


def test_a_mention_is_the_full_mxid_in_the_body():
    body, _ = room_collab.comment_content({}, "q", 0, "which one?", ["@qingyun:hs", ""])
    assert body == "> q\n\n@qingyun:hs which one?", body


def test_the_comment_is_posted_through_room_ops_say_with_the_anchor_as_extra_content(tmp_path=None):
    import tempfile
    calls = []

    class _Proc:
        returncode = 0
        stdout = '{"ok": true, "event_id": "$e1", "state": "confirmed"}'
        stderr = ""

    def runner(argv, **kw):
        calls.append((argv, kw))
        return _Proc()

    with tempfile.TemporaryDirectory() as d:
        script = Path(d) / "room_ops.py"
        script.write_text("")
        extra = {room_collab.COMMENT_KEY: {"anchor": {"quote": "日本"}, "v": 1}}
        receipt = room_collab.post_comment("!r:hs", "> 日本\n\nok", extra, runner=runner, script=script)
    assert receipt["event_id"] == "$e1"
    argv, kw = calls[0]
    assert argv[1:5] == [str(script), "say", "!r:hs", "> 日本\n\nok"], argv
    assert argv[5] == "--extra-content" and json.loads(argv[6]) == extra
    assert kw == {"capture_output": True, "text": True}


def test_a_post_that_did_not_land_is_a_refusal_with_the_reason():
    class _Proc:
        returncode = 0
        stdout = '{"ok": false, "reason": "client gate denies !r:hs"}'
        stderr = ""
    script = Path(__file__)  # any existing file stands in for the script
    _refuses(lambda: room_collab.post_comment("!r:hs", "b", {}, runner=lambda *a, **k: _Proc(), script=script),
             "not posted", "client gate denies")

    class _Crash:
        returncode = 1
        stdout = ""
        stderr = "Traceback ...\nKeyError: 'x'"
    _refuses(lambda: room_collab.post_comment("!r:hs", "b", {}, runner=lambda *a, **k: _Crash(), script=script),
             "not posted", "KeyError")


def test_without_room_ops_installed_the_refusal_names_the_dry_run_route():
    import unittest.mock as mock
    with mock.patch.object(room_collab, "room_ops_script", return_value=None):
        _refuses(lambda: room_collab.post_comment("!r:hs", "b", {}, runner=lambda *a, **k: None),
                 "agent-room-ops", "--dry-run")


def test_room_ops_is_looked_for_beside_this_skill():
    found = room_collab.room_ops_script()
    assert found == REPO / "skills" / "agent-room-ops" / "room_ops.py", found


def test_comment_runs_end_to_end_through_a_fake_surface():
    import contextlib
    import io
    import unittest.mock as mock
    try:
        import room_collab_client
    except (ImportError, SystemExit):
        return  # the client's dependencies are not installed here; the pure parts are tested above

    class _Doc:
        text = DOC
        peers = []

        def anchor(self, start, end):
            return {"start": f"s{start}", "end": f"e{end}"}

    @contextlib.asynccontextmanager
    async def fake_open(*_a, **_k):
        yield _Doc()

    posted = []

    def fake_post(room, body, extra):
        posted.append((room, body, extra))
        return {"ok": True, "event_id": "$e", "state": "confirmed"}

    def run(argv, stream="stdout"):
        buf = io.StringIO()
        with (contextlib.redirect_stdout(buf) if stream == "stdout" else contextlib.redirect_stderr(buf)):
            rc = room_collab.main(argv)
        return rc, buf.getvalue()

    with mock.patch.object(room_collab_client, "open_room_collab", fake_open), \
            mock.patch.object(room_collab, "post_comment", fake_post), \
            mock.patch.dict(os.environ, {"AG2_MATRIX_TOKEN": "t", "AG2_API_ROOT": "https://h"}):
        rc, out = run(["comment", "!r:hs", "option B is fast", "why?", "--dry-run"])
        shown = json.loads(out)
        at = DOC.index("option B is fast")
        assert rc == 0 and posted == [], (rc, posted)
        assert shown["body"] == "> option B is fast\n\nwhy?"
        assert shown["extra_content"][room_collab.COMMENT_KEY]["anchor"] == \
            {"start": f"s{at}", "end": f"e{at + len('option B is fast')}", "quote": "option B is fast", "nth": 0}

        rc, out = run(["comment", "!r:hs", "option A is cheap", "why?", "--nth", "1"])
        assert rc == 0 and "occurrence 1" in out and "$e" in out, out
        assert posted[-1][0] == "!r:hs" and posted[-1][2][room_collab.COMMENT_KEY]["anchor"]["nth"] == 1

        rc, out = run(["--json", "comment", "!r:hs", "option B is fast", "why?"])
        assert rc == 0 and json.loads(out)["event_id"] == "$e"

        # An ambiguous quote is a refusal on stderr, and nothing is posted for it.
        rc, err = run(["comment", "!r:hs", "option A is cheap", "why?"], stream="stderr")
        assert rc == 2 and "occurs 2 times" in err and len(posted) == 2, (rc, err)


def test_a_reply_is_the_words_as_they_are_with_any_mention_in_front():
    assert room_collab.reply_content("  yes, final ") == "yes, final"
    assert room_collab.reply_content("which?", ["@q:hs", ""]) == "@q:hs which?"
    _refuses(lambda: room_collab.reply_content("   "), "something to say")


def test_a_reply_is_posted_in_the_comments_thread_through_room_ops():
    calls = []

    class _Proc:
        returncode = 0
        stdout = '{"ok": true, "event_id": "$r1", "state": "confirmed"}'
        stderr = ""

    def runner(argv, **kw):
        calls.append(argv)
        return _Proc()

    script = Path(__file__)
    receipt = room_collab.post_reply("!r:hs", " $root ", "yes", runner=runner, script=script)
    assert receipt["event_id"] == "$r1"
    assert calls[0][2:] == ["say", "!r:hs", "yes", "--thread-root", "$root"], calls[0]
    _refuses(lambda: room_collab.post_reply("!r:hs", "root", "yes", runner=runner, script=script),
             "event id", "$abc")
    assert len(calls) == 1, "a bad id never reaches room-ops"


def test_reply_runs_without_opening_the_document():
    import contextlib
    import io
    import unittest.mock as mock
    posted = []

    def fake_post(room, root, body):
        posted.append((room, root, body))
        return {"ok": True, "event_id": "$r1"}

    def run(argv, stream="stdout"):
        buf = io.StringIO()
        with (contextlib.redirect_stdout(buf) if stream == "stdout" else contextlib.redirect_stderr(buf)):
            rc = room_collab.main(argv)
        return rc, buf.getvalue()

    with mock.patch.object(room_collab, "post_reply", fake_post), \
            mock.patch.object(room_collab, "resolve_token", side_effect=AssertionError("no credential needed")):
        rc, out = run(["reply", "!r:hs", "$root", "yes", "--dry-run"])
        assert rc == 0 and json.loads(out) == {"room": "!r:hs", "thread_root": "$root", "body": "yes"} and posted == []
        rc, out = run(["reply", "!r:hs", "$root", "yes", "--mention", "@q:hs"])
        assert rc == 0 and "$r1" in out and posted == [("!r:hs", "$root", "@q:hs yes")], (out, posted)
        rc, out = run(["--json", "reply", "!r:hs", "$root", "ok"])
        assert rc == 0 and json.loads(out)["event_id"] == "$r1"
        rc, err = run(["reply", "!r:hs", "$root", "   "], stream="stderr")
        assert rc == 2 and "something to say" in err


def test_the_comment_command_parses_its_flags():
    a = room_collab.build_parser().parse_args(
        ["comment", "!r:hs", "the words", "why?", "--nth", "1", "--mention", "@a:hs", "--mention", "@b:hs", "--dry-run"])
    assert (a.command, a.quote, a.text, a.nth, a.mention, a.dry_run) == \
        ("comment", "the words", "why?", 1, ["@a:hs", "@b:hs"], True)
    b = room_collab.build_parser().parse_args(["comment", "!r:hs", "q", "t"])
    assert b.nth is None and b.mention == [] and b.dry_run is False


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("room-collab cli: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-collab cli: ok")
