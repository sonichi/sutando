#!/usr/bin/env python3
"""The Getting started refresh after an engine update: `publish --all-rooms --if-newer` rebuilds the
page in every room the marker has an entry for, each room on its own, in the audience it was
written for, and never creates a page."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
base_spec = importlib.util.spec_from_file_location("tutorial_base", HERE / "tutorial-getting-started.test.py")
base = importlib.util.module_from_spec(base_spec)
base_spec.loader.exec_module(base)  # isolates the environment and loads getting_started once
gs, tp = base.gs, base.gs.tour_page

ME = "@kai.agent:example.test"
ROOMS = ("!a:example.test", "!b:example.test", "!c:example.test")
OLD = "tutorial.2026-10-01.1"
# One store per room, picked by the room argument; importing it (as the script does for credential
# names) refuses, as the real room_commons.py does nothing at import.
ROOMED_STUB = (
    "if __name__ != '__main__':\n    raise ImportError('a CLI stub, not importable')\n"
    "import os, re, sys, time\n"
    "while os.environ.get('STUB_STALL') and os.path.exists(os.environ['STUB_STALL']):\n    time.sleep(0.05)\n"
    "_a = [a for a in sys.argv[1:] if a != '--json']\n"
    "_a = _a[2:] if _a[0] == '--kind' else _a\n"
    "os.environ['STUB_STATE'] = os.path.join(os.environ['STUB_ROOMS'], re.sub(r'\\W', '_', _a[1]) + '.json')\n"
) + base.STUB


def store(rooms_dir: Path, room: str) -> Path:
    return rooms_dir / (re.sub(r"\W", "_", room) + ".json")


class RoomedActions(base.FakeActions):
    def __init__(self, rooms_dir: Path):
        super().__init__(rooms_dir / "unused.json", by=ME)
        self.rooms_dir = rooms_dir

    def execute(self, room, action, arguments, operation_id=None):
        self.state = store(self.rooms_dir, room)
        return super().execute(room, action, arguments, operation_id)


class _Rooms(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tutorial-refresh-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ws, self.rooms_dir = self.tmp / "ws", self.tmp / "rooms"
        self.rooms_dir.mkdir()
        self.stub = self.tmp / "room_commons.py"
        self.stub.write_text(ROOMED_STUB)
        self.old_list = self.tmp / "old.json"
        self.old_list.write_text(json.dumps({**json.loads(tp.LIST_PATH.read_text(encoding="utf-8")),
                                             "revision": OLD}))
        env = {k: v for k, v in base.isolated_child_env().items()
               if not k.startswith(("SUTANDO_", "AG2", "REMOTE_TASK", "AGENT_", "ROOM_C", "MATRIX_"))
               and k not in ("CLAUDE_CONFIG_DIR", "CLAUDE_HOME")}
        env.update({"CLAUDE_CONFIG_DIR": str(self.tmp / "claude"), "STUB_ROOMS": str(self.rooms_dir)})
        self.env = env
        for patcher in (mock.patch.dict(os.environ, env, clear=True),
                        mock.patch.object(gs, "_workspace", return_value=self.ws),
                        mock.patch.object(gs, "connect_actions", return_value=RoomedActions(self.rooms_dir))):
            patcher.start()
            self.addCleanup(patcher.stop)
        base.linux_exec_limit(self)

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = gs.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def seed(self, rooms=ROOMS, *extra):
        for room in rooms:
            code, out, err = self.main("publish", "--room", room, "--name", "Blue", "--core", "codex",
                                       "--room-commons", str(self.stub), "--agent-mxid", ME,
                                       "--list", str(self.old_list), *extra)
            self.assertEqual((code, json.loads(out)["action"]), (0, "created"), err)
            self.assertEqual(self.revision(room), OLD)

    def page(self, room):
        return json.loads(store(self.rooms_dir, room).read_text())["pages"]["html-p1"]

    def revision(self, room):
        return tp.read_marker(self.page(room))[1]

    def entries(self):
        return gs.load_marker(gs.marker_path(self.ws))


class AllRoomsTest(_Rooms):
    def refresh(self, *extra):
        return self.main("publish", "--all-rooms", "--if-newer", "--name", "Blue", "--core", "codex",
                         "--room-commons", str(self.stub), "--agent-mxid", ME, *extra)

    def test_every_room_with_an_entry_is_rebuilt_and_an_edited_one_is_kept(self):
        self.seed()
        db = json.loads(store(self.rooms_dir, ROOMS[1]).read_text())
        db["pages"]["html-p1"] += "<p>OWNER NOTES</p>"
        store(self.rooms_dir, ROOMS[1]).write_text(json.dumps(db))
        code, out, err = self.refresh()
        got = json.loads(out)
        self.assertEqual((code, got["ok"], got["action"], sorted(got["rooms"])), (0, True, "refreshed", sorted(ROOMS)), out + err)
        self.assertEqual({r: got["rooms"][r]["action"] for r in ROOMS},
                         {ROOMS[0]: "updated", ROOMS[1]: "kept", ROOMS[2]: "updated"}, err)
        self.assertEqual([self.revision(r) for r in ROOMS], [tp.FEATURES.revision, OLD, tp.FEATURES.revision])
        code, out, _ = self.refresh()
        self.assertEqual({r: v["action"] for r, v in json.loads(out)["rooms"].items()},
                         {ROOMS[0]: "unchanged", ROOMS[1]: "kept", ROOMS[2]: "unchanged"})

    def test_a_failing_room_leaves_the_others_refreshed_and_sets_the_exit_code(self):
        self.seed()
        store(self.rooms_dir, ROOMS[0]).write_text("{")  # its stub store is unreadable: that room's run fails
        code, out, _ = self.refresh()
        got = json.loads(out)
        self.assertNotEqual(code, 0)
        self.assertEqual((got["ok"], got["rooms"][ROOMS[0]]["code"]), (False, code))
        self.assertEqual([got["rooms"][r]["action"] for r in ROOMS[1:]], ["updated", "updated"])

    def test_no_rooms_is_a_clean_no_op_and_the_flags_are_checked(self):
        code, out, _ = self.refresh()
        self.assertEqual((code, json.loads(out)), (0, {"ok": True, "action": "refreshed", "rooms": {}}))
        self.assertEqual(self.main("publish", "--all-rooms", "--agent-mxid", ME)[0], 2)
        self.assertEqual(self.refresh("--room", ROOMS[0])[0], 2)
        self.assertEqual(self.refresh("--audience", "dm")[0], 2)

    def test_a_deleted_page_is_skipped_never_recreated(self):
        self.seed()
        db = json.loads(store(self.rooms_dir, ROOMS[1]).read_text())
        db["pages"] = {}
        store(self.rooms_dir, ROOMS[1]).write_text(json.dumps(db))
        before = self.entries()[ROOMS[1]]
        code, out, err = self.refresh()
        got = json.loads(out)["rooms"]
        self.assertEqual((code, got[ROOMS[1]]["action"], got[ROOMS[1]]["reason"]), (0, "skipped", "page deleted"))
        self.assertIn("page deleted", err)
        self.assertEqual(json.loads(store(self.rooms_dir, ROOMS[1]).read_text())["pages"], {})
        self.assertEqual(self.entries()[ROOMS[1]], before)
        self.assertEqual([got[r]["action"] for r in (ROOMS[0], ROOMS[2])], ["updated", "updated"])

    def test_a_page_deleted_while_its_creation_was_pending_is_not_created_again(self):
        self.seed(ROOMS[:1])
        inputs = gs.inputs_path(self.ws)
        inputs.parent.mkdir(parents=True, exist_ok=True)
        inputs.write_text(json.dumps({"room": ROOMS[0], "owner": "@owner:example.test"}))
        marker = gs.marker_path(self.ws)
        rooms = gs.load_marker(marker)
        page = self.page(ROOMS[0])
        rooms[ROOMS[0]] = {**rooms[ROOMS[0]], "state": "pending", "operation_id": "op-1", "target": page,
                           "before": "", "source": gs.content_hash(""), "done": 1, "mode": "fill"}
        gs.save_marker(marker, rooms)
        db = json.loads(store(self.rooms_dir, ROOMS[0]).read_text())
        db["pages"], db["calls"] = {}, []
        store(self.rooms_dir, ROOMS[0]).write_text(json.dumps(db))
        code, out, err = self.refresh()
        got = json.loads(out)["rooms"][ROOMS[0]]
        self.assertEqual((code, got["action"], got["reason"], got["summoned"]), (0, "skipped", "page deleted", False))
        db = json.loads(store(self.rooms_dir, ROOMS[0]).read_text())
        self.assertEqual(db["pages"], {})
        self.assertNotIn("summon", [c[0] for c in db["calls"]])
        # A creation whose create never answered is the explicit publish's to finish, not a refresh's.
        rooms = gs.load_marker(marker)
        rooms[ROOMS[0]] = {"state": "creating", "operation_id": "op-2", "target": page, "before": "",
                           "source": gs.content_hash(""), "hash": gs.content_hash(page), "done": 0, "mode": "fill",
                           "kind": None}
        gs.save_marker(marker, rooms)
        code, out, _ = self.refresh()
        got = json.loads(out)["rooms"][ROOMS[0]]
        self.assertEqual((code, got["action"], got["reason"]), (0, "skipped", "creation pending"))
        self.assertEqual(json.loads(store(self.rooms_dir, ROOMS[0]).read_text())["pages"], {})
        code, out, _ = self.main("publish", "--room", "!new:example.test", "--if-newer", "--name", "Blue", "--core",
                                 "codex", "--room-commons", str(self.stub), "--agent-mxid", ME)
        self.assertEqual((code, json.loads(out)["action"], json.loads(out)["reason"]), (0, "skipped", "no page"))

    def test_an_unexpected_error_in_one_room_leaves_the_others_refreshed(self):
        self.seed()
        real = gs.Commons.read

        def read(commons, kind):
            if commons.room == ROOMS[0]:
                raise RuntimeError("token=secret-value")
            return real(commons, kind)
        with mock.patch.object(gs.Commons, "read", read):
            code, out, err = self.refresh()
        got = json.loads(out)
        self.assertEqual((code, got["ok"]), (4, False))
        self.assertEqual(got["rooms"][ROOMS[0]], {"code": 4, "ok": False, "reason": "unexpected RuntimeError"})
        self.assertEqual([got["rooms"][r]["action"] for r in ROOMS[1:]], ["updated", "updated"])
        self.assertNotIn("secret-value", out + err)

    def test_a_room_current_by_its_record_makes_no_room_call(self):
        self.seed()
        self.refresh()
        for r in ROOMS:
            db = json.loads(store(self.rooms_dir, r).read_text())
            db["calls"] = []
            store(self.rooms_dir, r).write_text(json.dumps(db))
        code, out, _ = self.refresh()
        got = json.loads(out)["rooms"]
        self.assertEqual({r: (v["action"], v.get("checked")) for r, v in got.items()},
                         {r: ("unchanged", "marker") for r in ROOMS})
        self.assertEqual([json.loads(store(self.rooms_dir, r).read_text())["calls"] for r in ROOMS], [[], [], []])

    def test_each_page_is_rebuilt_in_the_audience_it_was_written_for(self):
        self.seed(ROOMS[:1], "--audience", "room")
        self.seed(ROOMS[1:2])
        self.assertEqual([self.entries()[r]["audience"] for r in ROOMS[:2]], ["room", "dm"])
        code, out, _ = self.refresh()
        self.assertEqual({r: v["action"] for r, v in json.loads(out)["rooms"].items()},
                         {ROOMS[0]: "updated", ROOMS[1]: "updated"})
        self.assertEqual(self.page(ROOMS[0]), tp.render_page("Blue", "codex", "room", ME))
        self.assertIn("your agent", self.page(ROOMS[0]))
        self.assertEqual(self.page(ROOMS[1]), tp.render_page("Blue", "codex", "dm", ME))
        self.assertEqual([self.entries()[r]["audience"] for r in ROOMS[:2]], ["room", "dm"])

    def test_a_legacy_entry_without_an_audience_is_rewritten_only_in_the_setup_dm(self):
        self.seed(ROOMS[:1], "--audience", "room")
        self.seed(ROOMS[1:2])
        marker = gs.marker_path(self.ws)
        rooms = gs.load_marker(marker)
        for r in ROOMS[:2]:
            del rooms[r]["audience"]  # written before entries recorded it
        gs.save_marker(marker, rooms)
        inputs = gs.inputs_path(self.ws)
        inputs.parent.mkdir(parents=True, exist_ok=True)
        inputs.write_text(json.dumps({"room": ROOMS[1]}))
        kept = self.page(ROOMS[0])
        code, out, err = self.refresh()
        got = json.loads(out)["rooms"]
        self.assertEqual((code, got[ROOMS[0]]["action"], got[ROOMS[0]]["reason"]), (0, "skipped", "audience unknown"))
        self.assertEqual(self.page(ROOMS[0]), kept)
        self.assertNotIn("audience", self.entries()[ROOMS[0]])
        self.assertEqual(got[ROOMS[1]]["action"], "updated")
        self.assertEqual((self.page(ROOMS[1]), self.entries()[ROOMS[1]]["audience"]),
                         (tp.render_page("Blue", "codex", "dm", ME), "dm"))


if __name__ == "__main__":
    unittest.main()
