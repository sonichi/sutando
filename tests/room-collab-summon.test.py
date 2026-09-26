#!/usr/bin/env python3
"""The summon an agent posts must be the SAME event the client's @-picker writes.

If it is not, nothing renders: the web client's timeline reads one key and one
shape, and a message it cannot read degrades to prose — which is the state this
verb exists to leave behind. So this pins the wire shape, not the wording.

Verified against the client's own reader once, outside this suite (its
`mentionSummonCardOf` accepted all three kinds and refused a marker naming
nobody); these assertions are what keeps the shape from drifting after that.
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

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


ROOM = "!r:ag2.space"
WHO = "@qingyun:ag2.space"


def test_the_marker_is_the_key_the_client_reads():
    _body, extra = room_collab.summon_content(ROOM, WHO, "markdown")
    assert "space.ag2.collab.doc.summon" in extra, sorted(extra)
    assert room_collab.SUMMON_KEY == "space.ag2.collab.doc.summon"


def test_v3_carries_who_and_where_so_the_card_needs_no_prose():
    _body, extra = room_collab.summon_content(ROOM, WHO, "markdown", "the passage")
    assert extra[room_collab.SUMMON_KEY] == {
        "room_id": ROOM, "kind": "markdown", "invitee": WHO,
        "v": 3, "context": "the passage",
    }, extra[room_collab.SUMMON_KEY]


def test_a_summon_without_context_omits_the_field_rather_than_sending_empty():
    _body, extra = room_collab.summon_content(ROOM, WHO, "markdown")
    assert "context" not in extra[room_collab.SUMMON_KEY], extra
    for blank in (None, "", "   ", "\n\t "):
        _b, e = room_collab.summon_content(ROOM, WHO, "markdown", blank)
        assert "context" not in e[room_collab.SUMMON_KEY], blank


def test_the_mention_is_what_reaches_the_person():
    # m.mentions is what the gateway turns into a task for whoever is called; a
    # summon without it renders a card nobody was told about.
    body, extra = room_collab.summon_content(ROOM, WHO, "markdown")
    assert extra["m.mentions"] == {"user_ids": [WHO]}, extra.get("m.mentions")
    assert WHO in body, body


def test_every_surface_the_client_knows_is_summonable_and_nothing_else():
    for kind in ("markdown", "board", "kanban"):
        _b, e = room_collab.summon_content(ROOM, WHO, kind)
        assert e[room_collab.SUMMON_KEY]["kind"] == kind
    for bad in ("presentation", "doc", "whiteboard", "", "MARKDOWN"):
        try:
            room_collab.summon_content(ROOM, WHO, bad)
        except RoomDocError:
            continue
        raise AssertionError(f"{bad!r} was accepted as a surface")


def test_a_name_without_a_server_is_refused_not_posted():
    # The client's reader requires @local:server; anything else renders as prose.
    for bad in ("qingyun", "@qingyun", "@:ag2.space", "@a b:hs", "", "   "):
        try:
            room_collab.summon_content(ROOM, bad, "markdown")
        except RoomDocError:
            continue
        raise AssertionError(f"{bad!r} was accepted as an invitee")
    _b, e = room_collab.summon_content(ROOM, f"  {WHO}  ", "markdown")
    assert e[room_collab.SUMMON_KEY]["invitee"] == WHO, "the mxid is trimmed"


def test_context_is_one_line_and_bounded():
    _b, e = room_collab.summon_content(ROOM, WHO, "markdown", "a\nb\t c   d")
    assert e[room_collab.SUMMON_KEY]["context"] == "a b c d"
    _b2, e2 = room_collab.summon_content(ROOM, WHO, "markdown", "x" * 5000)
    ctx = e2[room_collab.SUMMON_KEY]["context"]
    assert len(ctx) == room_collab.SUMMON_CONTEXT_MAX, len(ctx)


def test_it_posts_through_room_ops_say_with_the_marker_attached():
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv

        class R:
            returncode = 0
            stdout = json.dumps({"ok": True, "event_id": "$e"})
            stderr = ""
        return R()

    body, extra = room_collab.summon_content(ROOM, WHO, "board", "why")
    receipt = room_collab.post_summon(ROOM, body, extra, runner=fake_run,
                                      script=Path("/tmp/room_ops.py"))
    assert receipt.get("event_id") == "$e", receipt
    argv = seen["argv"]
    assert argv[2:4] == ["say", ROOM], argv
    assert "--extra-content" in argv, argv
    sent = json.loads(argv[argv.index("--extra-content") + 1])
    assert sent[room_collab.SUMMON_KEY]["kind"] == "board", sent
    assert sent["m.mentions"] == {"user_ids": [WHO]}, sent


def test_a_refused_post_raises_instead_of_reporting_success():
    def fake_run(argv, **kw):
        class R:
            returncode = 0
            stdout = json.dumps({"ok": False, "reason": "not in room"})
            stderr = ""
        return R()

    body, extra = room_collab.summon_content(ROOM, WHO, "markdown")
    try:
        room_collab.post_summon(ROOM, body, extra, runner=fake_run,
                                script=Path("/tmp/room_ops.py"))
    except RoomDocError as e:
        assert "not in room" in str(e), e
        return
    raise AssertionError("a refused post reported success")


def test_the_verb_takes_the_arguments_the_skill_documents():
    a = room_collab.build_parser().parse_args(
        ["summon", ROOM, WHO, "--context", "why", "--dry-run"])
    assert (a.command, a.room, a.invitee, a.context, a.dry_run) == \
        ("summon", ROOM, WHO, "why", True)
    b = room_collab.build_parser().parse_args(["summon", ROOM, WHO])
    assert b.context is None and b.dry_run is False
    # --kind is global, so a board summon reads the same flag every verb does
    c = room_collab.build_parser().parse_args(["--kind", "board", "summon", ROOM, WHO])
    assert c.kind == "board"


def test_a_page_summon_reads_the_page_title_and_survives_when_it_cannot():
    import asyncio
    import contextlib
    import room_collab_client

    class Main:
        pages = [{"id": "ut9pkft9", "title": "Product-roadmap"}]

    opened = []

    @contextlib.asynccontextmanager
    async def fake_open(url, room, token, *, kind="markdown", insecure=False):
        opened.append(kind)
        yield Main()

    real = room_collab_client.open_room_collab
    args = room_collab.build_parser().parse_args(
        ["--url", "https://h", "--token", "t", "--kind", "markdown-ut9pkft9", "summon", ROOM, WHO])
    try:
        room_collab_client.open_room_collab = fake_open
        assert asyncio.run(room_collab.summon_page_title(args)) == "Product-roadmap"
        assert opened == ["markdown"], "the title is read from the Doc's own page list"

        @contextlib.asynccontextmanager
        async def broken(*_a, **_k):
            raise ConnectionError("offline")
            yield  # pragma: no cover
        room_collab_client.open_room_collab = broken
        assert asyncio.run(room_collab.summon_page_title(args)) is None
    finally:
        room_collab_client.open_room_collab = real
    assert room_collab.build_parser().parse_args(
        ["summon", ROOM, WHO, "--page-title", "Plan"]).page_title == "Plan"


def test_the_summon_command_names_the_page_from_its_list_or_from_page_title():
    import contextlib
    import io
    import room_collab_client

    class Main:
        pages = [{"id": "ut9pkft9", "title": "Product-roadmap"}]

    opened = []

    @contextlib.asynccontextmanager
    async def fake_open(url, room, token, *, kind="markdown", insecure=False):
        opened.append(kind)
        yield Main()

    base = ["--url", "https://h", "--token", "t"]
    real = room_collab_client.open_room_collab

    def run(argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert room_collab.main(base + argv) == 0, argv
        return json.loads(out.getvalue())

    try:
        room_collab_client.open_room_collab = fake_open
        got = run(["--kind", "markdown-ut9pkft9", "summon", ROOM, WHO, "--dry-run"])
        assert got["extra_content"][room_collab.SUMMON_KEY]["page_title"] == "Product-roadmap", got
        assert opened == ["markdown"], opened
        got = run(["--kind", "html-zz99zz99", "summon", ROOM, WHO, "--page-title", "Poll", "--dry-run"])
        assert '"Poll" in this room\'s HTML page' in got["body"] and opened == ["markdown"], "a given title opens nothing"
        got = run(["summon", ROOM, WHO, "--dry-run"])
        assert "page_title" not in got["extra_content"][room_collab.SUMMON_KEY] and opened == ["markdown"]
    finally:
        room_collab_client.open_room_collab = real


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("room-collab summon: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-collab summon: ok")
