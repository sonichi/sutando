#!/usr/bin/env python3
"""The database commands, driven through the real CLI and client against an in-memory room.

What is pinned: values set by property name land as the web client stores them,
a refused value writes nothing at all, a board move writes the group value, and a
CSV (the demo-day sheet's shape: a notes row above the headers) becomes rows.

Run: python3 tests/room-collab-database-cli.test.py  (exit 0 pass / 1 fail)
"""
import asyncio
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

try:
    from pycrdt import Awareness, Doc
except ImportError as exc:  # pragma: no cover
    print(f"room-collab database cli: FAIL — dependencies missing ({exc}).")
    sys.exit(1)

import room_collab  # noqa: E402
import room_collab_client  # noqa: E402
from room_collab_client import RoomDoc  # noqa: E402
from room_collab_protocol import RoomDocError  # noqa: E402

FAILS = []
BY = "@sutando-x:ag2.space"
BASE = ["--url", "https://h", "--token", "t", "--kind", "db", "--user-id", BY, "--settle", "0"]


class FakeWS:
    def __init__(self):
        self.sent = 0

    async def send(self, payload):
        self.sent += 1


def room():
    # RoomDoc binds a future to the current loop; each CLI call then runs in its own.
    asyncio.set_event_loop(asyncio.new_event_loop())
    doc = Doc()
    return RoomDoc(FakeWS(), doc, Awareness(doc), "markdown", kind="db")


def cli(argv, page):
    @contextlib.asynccontextmanager
    async def fake_open(url, room_id, token, *, kind="markdown", insecure=False):
        assert kind == "db", kind
        yield page

    real = room_collab_client.open_room_collab
    room_collab_client.open_room_collab = fake_open
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = room_collab.main(BASE + argv)
        return rc, out.getvalue(), err.getvalue()
    finally:
        room_collab_client.open_room_collab = real


def ok(argv, page):
    rc, out, err = cli(argv, page)
    assert rc == 0, (argv, err)
    return out


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except SystemExit as e:
        FAILS.append(f"{name}: the CLI exited {e.code} instead of running")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


def test_create_add_update_move_read():
    page = room()
    assert "no databases" in ok(["dbs", "!r:x"], page)
    db = json.loads(ok(["create", "!r:x", "--template", "tasks", "--name", "Launch"], page))["db"]
    maps = page.database
    assert maps["dbs"][db]["name"] == "Launch" and maps["dbs"][db]["by"] == BY
    row = json.loads(ok(["add", "!r:x", "--db", "launch", "--set", "Name=Write the demo",
                         "--set", "Status=in progress", "--set", "Assignee=@mark:x,@air.agent:x",
                         "--set", "Due=2026-09-25"], page))["row"]
    cells = page.database["cells"]
    assert cells[f"{db}|{row}|status"]["v"] == "o1" and cells[f"{db}|{row}|status"]["by"] == BY
    assert cells[f"{db}|{row}|assignee"]["v"] == ["@mark:x", "@air.agent:x"]
    assert cells[f"{db}|{row}|due"]["v"] == {"start": "2026-09-25"}
    ok(["update", "!r:x", "--row", "write the demo", "--set", "Priority=High", "--set", "Due="], page)
    cells = page.database["cells"]
    assert cells[f"{db}|{row}|priority"]["v"] == "o0" and f"{db}|{row}|due" not in cells, "empty clears"
    out = json.loads(ok(["move", "!r:x", "--row", row, "--to", "Done"], page))
    assert out["view"] == "Board" and page.database["cells"][f"{db}|{row}|status"]["v"] == "o2"
    board = ok(["read", "!r:x", "--view", "Board"], page)
    assert "## Done (1)" in board and "Write the demo" in board, board
    table = json.loads(ok(["read", "!r:x", "--view", "table", "--json"], page))
    assert table["rows"][0]["values"]["Status"] == "Done" and table["rows"][0]["values"]["Priority"] == "High"
    listing = ok(["dbs", "!r:x"], page)
    assert "Launch" in listing and "1 rows" in listing, listing


def test_a_refused_value_writes_nothing():
    page = room()
    ok(["create", "!r:x", "--template", "tasks"], page)
    before = page.database
    sent = page._ws.sent
    rc, _, err = cli(["add", "!r:x", "--set", "Name=x", "--set", "Status=Blocked"], page)
    assert rc == 2 and "Status" in err and "Not started, In progress, Done" in err, err
    rc, _, err = cli(["add", "!r:x", "--set", "Name=x", "--set", "Assignee=mark"], page)
    assert rc == 2 and "Assignee" in err and "@name:server" in err, err
    rc, _, err = cli(["add", "!r:x", "--set", "Due=Sep 25"], page)
    assert rc == 2 and "YYYY-MM-DD" in err, err
    assert page.database == before and page._ws.sent == sent, "nothing reached the room"
    rc, _, err = cli(["move", "!r:x", "--row", "nope", "--to", "Done"], page)
    assert rc == 2 and "no row" in err, err
    ok(["create", "!r:x", "--template", "wiki"], page)
    rc, _, err = cli(["add", "!r:x", "--set", "Name=x"], page)
    assert rc == 2 and "several databases" in err, err


def test_the_demo_day_sheet_becomes_a_database():
    csv_text = ('"",Note to presenters:\n'
                "Team Demo Date,Persenter,Use case brief descipton ,"
                "Time needed (assuming 5~10m if not otherwise specified),Demo video,Killer Use Case Status (x)\n"
                "Sep 25,John,Cloud Sutando,,,\n"
                'Sep 25,Qingyun,"Room-collab, multi-agent",10,https://v.example/q,Confirmed\n'
                ",Rui,tbd,,,\n"
                "Oct 2,,,,,\n"
                ",,,,,\n")
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
        fh.write(csv_text)
    page = room()
    maps = ["--map", "Team Demo Date=Demo date", "--map", "Persenter=Presenter",
            "--map", "Use case brief=Use case", "--map", "Time needed=Minutes",
            "--map", "Killer Use Case Status=Killer use case"]
    out = json.loads(ok(["create", "!r:x", "--template", "demo_day", "--from-csv", fh.name,
                         "--header-row", "2", "--year", "2026"] + maps, page))
    assert out["rows_added"] == 4 and out["blank_rows_skipped"] == 1 and out["unmatched_headers"] == [], out
    v = json.loads(ok(["read", "!r:x", "--view", "By date", "--json"], page))
    got = [(r["values"]["Use case"], r["values"]["Demo date"], r["values"]["Killer use case"]) for r in v["rows"]]
    assert got[0] == ("tbd", "", ""), "an empty date sorts first, as in the web client"
    assert got[1:3] == [("Cloud Sutando", "2026-09-25", ""), ("Room-collab, multi-agent", "2026-09-25", "Confirmed")], got
    rc, _, err = cli(["create", "!r:x", "--template", "demo_day", "--from-csv", fh.name, "--header-row", "2"]
                     + maps, page)
    assert rc == 2 and "--year" in err, err
    assert len(page.database["dbs"]) == 1, "a refused import creates no database either"
    rep = json.loads(ok(["import", "!r:x", fh.name, "--header-row", "2", "--year", "2026"], page))
    assert "Persenter" in rep["unmatched_headers"] and rep["columns"] == {"Demo video": "Demo video"}, rep


def test_other_kinds_are_told_to_open_the_db():
    asyncio.set_event_loop(asyncio.new_event_loop())
    doc = Doc()
    sheet = RoomDoc(FakeWS(), doc, Awareness(doc), "markdown", kind="sheet")
    try:
        sheet.database
    except RoomDocError as exc:
        assert "kind='db'" in str(exc), exc
    else:
        raise AssertionError("a sheet has no databases")
    try:
        asyncio.run(room().put_database({"nope": {}}))
    except RoomDocError as exc:
        assert "not a database map" in str(exc)
    else:
        raise AssertionError("an unknown map is refused")


def test_kanban_add_and_move_still_need_their_arguments():
    parser = room_collab.build_parser()
    args = parser.parse_args(["--kind", "kanban", "add", "!r:x", "ship it"])
    assert args.text == "ship it" and args.db is None
    args = parser.parse_args(["--kind", "kanban", "move", "!r:x", "c1", "doing"])
    assert (args.card_id, args.column) == ("c1", "doing")
    args = parser.parse_args(["--json", "--kind", "db", "read", "!r:x"])
    assert args.json is True, "a --json before the command survives the command's own --json"


for name, fn in list(globals().items()):
    if name.startswith("test_"):
        check(name, fn)

if FAILS:
    print("room-collab database cli: FAIL")
    for f in FAILS:
        print("  " + f)
    sys.exit(1)
print("room-collab database cli: ok")
