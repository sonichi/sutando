#!/usr/bin/env python3
"""skills/tutorial/scripts/getting_started.py: the page it renders, its one-page-per-room marker,
and the room-commons calls publish makes, against a stub room_commons.py that keeps a fake room."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "tutorial" / "scripts" / "getting_started.py"

spec = importlib.util.spec_from_file_location("getting_started", SCRIPT)
gs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gs)

STUB = textwrap.dedent('''
    import json, os, sys
    from pathlib import Path
    state = Path(os.environ["STUB_STATE"])
    db = json.loads(state.read_text()) if state.exists() else {"pages": {}, "calls": [], "next": 1}
    argv = sys.argv[1:]
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    kind = None
    if argv[0] == "--kind":
        kind, argv = argv[1], argv[2:]
    cmd, room, rest = argv[0], argv[1], argv[2:]
    db["calls"].append([cmd, kind] + rest[:1])
    def save():
        state.write_text(json.dumps(db))
    if cmd in os.environ.get("STUB_FAIL", "").split(","):
        save(); print("boom", file=sys.stderr); sys.exit(1)
    if cmd == "pages":
        listed = [{"id": None, "kind": "html", "title": "Main"}]
        listed += [{"id": k[5:], "kind": k, "title": "Getting started"} for k in db["pages"]]
        print(json.dumps(listed))
    elif cmd == "page-add":
        k = "html-p%d" % db["next"]; db["next"] += 1; db["pages"][k] = ""
        print(json.dumps({"ok": True, "id": k[5:], "kind": k, "title": rest[0]}))
    elif cmd == "read":
        print(json.dumps({"chars": 0, "peers": [], "text": db["pages"][kind]}))
    elif cmd == "append":
        db["pages"][kind] += rest[0]
    elif cmd == "replace":
        cur = db["pages"][kind]
        assert rest[0] in cur, "old text absent"
        db["pages"][kind] = cur.replace(rest[0], rest[1], 1)
    elif cmd == "page-remove":
        db["pages"].pop(rest[0], None)
    elif cmd == "summon":
        pass
    else:
        sys.exit(9)
    save()
''')


TOUR = ["Rooms", "Commons", "Teamwork", "Calls & voice", "Connect other agents",
        "Connect your apps", "Activity & notices", "Mobile"]
IMPORT = "Import your Claude Code history"


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.headings, self.buttons, self._in_h2 = [], [], False

    def handle_starttag(self, tag, attrs):
        if tag == "button":
            self.buttons.append(dict(attrs).get("data-prompt"))
        self._in_h2 = tag == "h2"

    def handle_endtag(self, tag):
        if tag == "h2":
            self._in_h2 = False

    def handle_data(self, data):
        if self._in_h2:
            self.headings.append(data)


def parse(page):
    p = Page()
    p.feed(page)
    return p


class RenderTest(unittest.TestCase):
    def test_tour_has_all_eight_sections_in_order(self):
        for core in gs.CORES:
            self.assertEqual(parse(gs.render_page("Blue", core)).headings[:8], TOUR)

    def test_import_section_only_on_a_claude_core(self):
        claude = parse(gs.render_page("Blue", "claude"))
        self.assertEqual(claude.headings, TOUR + [IMPORT])
        self.assertIn(gs.IMPORT_PROMPT, claude.buttons)
        for core in ("codex", "unknown"):
            page = parse(gs.render_page("Blue", core))
            self.assertEqual(page.headings, TOUR)
            self.assertNotIn(gs.IMPORT_PROMPT, page.buttons)
            self.assertNotIn("Claude Code", gs.render_page("Blue", core))

    def test_unknown_core_value_is_refused(self):
        with self.assertRaises(ValueError):
            gs.render_page("Blue", "gemini")

    def test_copy_buttons_and_kept_connect_prompt(self):
        page = gs.render_page("Blue", "codex")
        self.assertEqual(parse(page).buttons,
                         [p for _, _, _, p in gs.SECTIONS if p])
        self.assertIn(gs.CONNECT_PROMPT, parse(page).buttons)
        self.assertIn("Settings &rarr; Integrations", page)
        self.assertIn("navigator.clipboard", page)
        self.assertIn("prefers-color-scheme: dark", page)

    def test_codex_and_claude_code_are_not_offered_as_addable_agents(self):
        agents = [s for s in gs.SECTIONS if s[0] == "Connect other agents"][0]
        self.assertNotIn("Codex", " ".join(agents[1:3]))
        self.assertNotIn("Claude", " ".join(agents[1:3]))

    def test_render_is_deterministic_and_depends_only_on_name_and_core(self):
        self.assertEqual(gs.render_page("Kai"), gs.render_page("  Kai "))
        self.assertNotEqual(gs.render_page("Kai"), gs.render_page("Mo"))
        self.assertNotEqual(gs.render_page("Kai", "claude"), gs.render_page("Kai", "codex"))

    def test_name_is_escaped_everywhere(self):
        page = gs.render_page('<img src=x onerror="alert(1)">', "claude")
        self.assertNotIn("<img src=x", page)
        self.assertGreaterEqual(page.count("&lt;img"), 5)

    def test_empty_or_long_name_is_refused(self):
        for bad in ("", "   ", "x" * (gs.NAME_MAX + 1)):
            with self.assertRaises(ValueError):
                gs.render_page(bad)


class MarkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = gs.marker_path(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_or_corrupt_marker_reads_empty(self):
        self.assertEqual(gs.load_marker(self.path), {})
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json")
        self.assertEqual(gs.load_marker(self.path), {})
        self.path.write_text(json.dumps({"rooms": {"!r": {"kind": "markdown-x"}}}))
        self.assertEqual(gs.load_marker(self.path), {})

    def test_round_trip(self):
        gs.save_marker(self.path, {"!r:x": {"kind": "html-a", "hash": "h"}})
        self.assertEqual(gs.load_marker(self.path), {"!r:x": {"kind": "html-a", "hash": "h"}})
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["getting-started.json"])

    def test_plan(self):
        rooms = {"!r:x": {"kind": "html-a"}}
        self.assertEqual(gs.plan({}, "!r:x"), {"action": "create", "kind": None})
        self.assertEqual(gs.plan(rooms, "!r:x"), {"action": "update", "kind": "html-a"})
        self.assertEqual(gs.plan(rooms, "!r:x", ["html-a"]), {"action": "update", "kind": "html-a"})
        self.assertEqual(gs.plan(rooms, "!r:x", ["html-b"]), {"action": "create", "kind": None})
        self.assertEqual(gs.plan(rooms, "!other:x"), {"action": "create", "kind": None})


class PublishTest(unittest.TestCase):
    ROOM = "!dm:ag2.space"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.stub = base / "room_commons.py"
        self.stub.write_text(STUB)
        self.state = base / "room.json"
        self.marker = gs.marker_path(base / "ws")
        os.environ["STUB_STATE"] = str(self.state)
        os.environ.pop("STUB_FAIL", None)

    def tearDown(self):
        os.environ.pop("STUB_STATE", None)
        os.environ.pop("STUB_FAIL", None)
        self.tmp.cleanup()

    def room(self):
        return json.loads(self.state.read_text())

    def publish(self, name="Sutando", **kw):
        kw.setdefault("core", "codex")
        return gs.publish(gs.Commons(self.stub, self.ROOM), self.marker, name, **kw)

    def test_first_run_creates_writes_and_summons(self):
        out = self.publish(owner="@chi:ag2.space")
        self.assertEqual(out, {"ok": True, "action": "created", "kind": "html-p1", "summoned": True})
        room = self.room()
        self.assertEqual(room["pages"], {"html-p1": gs.render_page("Sutando", "codex")})
        self.assertEqual([c[0] for c in room["calls"]], ["page-add", "append", "summon"])
        self.assertEqual(room["calls"][-1], ["summon", "html-p1", "@chi:ag2.space"])
        self.assertEqual(gs.load_marker(self.marker)[self.ROOM]["kind"], "html-p1")

    def test_second_run_is_idempotent(self):
        self.publish(owner="@chi:ag2.space")
        out = self.publish(owner="@chi:ag2.space")
        self.assertEqual(out["action"], "unchanged")
        self.assertFalse(out["summoned"])
        room = self.room()
        self.assertEqual(list(room["pages"]), ["html-p1"])
        self.assertEqual([c[0] for c in room["calls"]][3:], ["pages", "read"])

    def test_changed_content_rewrites_the_same_page(self):
        self.publish()
        out = self.publish(name="Kai")
        self.assertEqual(out["action"], "updated")
        self.assertEqual(self.room()["pages"], {"html-p1": gs.render_page("Kai", "codex")})

    def test_core_change_rewrites_the_same_page(self):
        self.publish()
        out = self.publish(core="claude")
        self.assertEqual((out["action"], out["kind"]), ("updated", "html-p1"))
        self.assertIn(gs.IMPORT_PROMPT, self.room()["pages"]["html-p1"])

    def test_removed_page_is_created_again(self):
        self.publish()
        db = self.room()
        db["pages"] = {}
        self.state.write_text(json.dumps(db))
        out = self.publish()
        self.assertEqual((out["action"], out["kind"]), ("created", "html-p2"))
        self.assertEqual(gs.load_marker(self.marker)[self.ROOM]["kind"], "html-p2")

    def test_failed_write_removes_the_empty_page_and_keeps_no_marker(self):
        os.environ["STUB_FAIL"] = "append"
        with self.assertRaises(gs.PublishError):
            self.publish(owner="@chi:ag2.space")
        room = self.room()
        self.assertEqual(room["pages"], {})
        self.assertNotIn("summon", [c[0] for c in room["calls"]])
        self.assertEqual(gs.load_marker(self.marker), {})

    def test_failed_summon_still_reports_the_page(self):
        os.environ["STUB_FAIL"] = "summon"
        out = self.publish(owner="@chi:ag2.space")
        self.assertEqual((out["action"], out["summoned"]), ("created", False))
        self.assertIn("summon", out["summon_error"])
        self.assertEqual(gs.load_marker(self.marker)[self.ROOM]["kind"], "html-p1")


class CliTest(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)

    def test_render(self):
        for core in gs.CORES:
            done = self.run_cli("render", "--name", "Sutando", "--core", core)
            self.assertEqual(done.returncode, 0)
            self.assertEqual(done.stdout, gs.render_page("Sutando", core))

    def test_detect_core_reads_the_engine_resolver(self):
        env = dict(os.environ, SUTANDO_CORE_RUNTIME="codex")
        done = subprocess.run([sys.executable, "-c",
                               "import importlib.util as u;s=u.spec_from_file_location('g',%r);"
                               "m=u.module_from_spec(s);s.loader.exec_module(m);print(m.detect_core())"
                               % str(SCRIPT)], capture_output=True, text=True, env=env)
        self.assertEqual(done.stdout.strip(), "codex")

    def test_missing_room_commons_exits_3(self):
        done = self.run_cli("publish", "--room", "!r:x", "--name", "Sutando",
                            "--room-commons", "/nonexistent/room_commons.py")
        self.assertEqual(done.returncode, 3)
        self.assertEqual(json.loads(done.stdout)["reason"], "room-commons not installed")

    def test_bad_name_exits_2(self):
        done = self.run_cli("publish", "--room", "!r:x", "--name", " ")
        self.assertEqual(done.returncode, 2)


if __name__ == "__main__":
    unittest.main()
