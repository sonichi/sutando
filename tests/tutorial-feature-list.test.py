#!/usr/bin/env python3
"""The tutorial's feature list (skills/tutorial/feature-list.json): the page renders from it with
the same HTML the hard-coded sections gave (fallback mode: every screenshot inlined), a malformed
list is refused by field, a screenshot is an asset reference that points at the room's media when
the room has it, the page records the list's revision and `--if-newer` rebuilds only a page whose
list is older, `--list` swaps the list without any network call by default, and the size caps that
keep the page publishable hold."""
from __future__ import annotations

import contextlib
import email.message
import importlib.util
import io
import json
import os
import shutil
import sys
import itertools
import tempfile
import threading
import types
import unittest
import urllib.request
import urllib.response
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
base_spec = importlib.util.spec_from_file_location("tutorial_base", HERE / "tutorial-getting-started.test.py")
base = importlib.util.module_from_spec(base_spec)
base_spec.loader.exec_module(base)  # isolates the environment and loads getting_started once
gs, tp = base.gs, base.gs.tour_page
Section = tp.Section

# --- fixture: the sections as tour_page.py hard-coded them before the list existed ---------------
OLD_SECTIONS = [
    Section("dm", "Your DM",
            "Your DM with {name} is where you ask for things for yourself. What you ask for "
            "yourself (research, mail, calendar) comes back in your DM, never in a room.",
            "Ask {name} in your DM:",
            "What can you do for me today?", "dm"),
    Section("rooms", "Rooms",
            "Humans and agents talk in the same chat.",
            "Press + at the top of the room list and choose Room. Then open the room menu "
            "(&#8942;), choose Invite, and add {name}.",
            images=(("room-list-add.jpg", "The + button at the top of the room list"),
                    ("new-room-dialog.jpg", "The New Chat Room dialog"),
                    ("room-menu-invite.jpg", "The room menu, with Invite"))),
    Section("commons", "Commons",
            "Each room's shared Docs, HTML pages, PDFs, whiteboards, sheets and databases. Open them "
            "with the Commons icon in the room header.",
            "Ask {name} in a room:",
            "Make a plan for our team offsite in Commons.", "room",
            (("room-header-commons.jpg", "The room header; Commons is the second icon"),
             ("commons-surfaces.jpg", "Commons: Doc, Whiteboard, HTML, PDF, Sheet and Database"))),
    Section("agents", "Connect other agents",
            "Bring agents from the apps you already use into your rooms.",
            "Press + at the top of the room list, choose Agent, then Use an agent from another app, "
            "and pick the app you already use: Dot (by OpenAI, in ChatGPT), Grok Bot (in Grok) or "
            "Muse (by Meta)."),
    Section("teamwork", "Teamwork",
            "@-mention any agent in a room to hand it work; it replies in the thread.",
            "In a room with another agent, type @ and ask {name}:",
            "Research the three best note-taking apps for a small team, then ask another agent "
            "in this room to review your answer.", "room",
            (("composer-mention.jpg", "The @ button in the message composer"),)),
    Section("calls", "Calls &amp; voice",
            "Talk live with {name} in your DM, or with people in any room.",
            "Press the headphone icon in the room header (Start call). In your DM, {name} answers; "
            "in a room, start the call and pick your agent under &ldquo;Bring an agent?&rdquo;.",
            images=(("room-header-call.jpg", "The headphone icon in the room header"),)),
    Section("apps", "Connect your apps",
            "Gmail, Calendar, Drive, Slack and more, so {name} can work with your real data.",
            "Press the plug icon in your user card (bottom left) to open Settings &rarr; "
            "Integrations, or ask {name} and a Connect button appears:",
            tp.CONNECT_PROMPT, "dm",
            (("user-card-plug.jpg", "The plug icon in your user card"),
             ("settings-integrations.jpg", "Settings → Apps → Integrations"))),
    Section("activity", "Activity &amp; notices",
            "The bell at the top of the room list and each room's Activity tab show what needs "
            "you and what your agents are working on. The unread badge follows each room's "
            "notification setting.",
            "Try it: open a room's Activity tab while an agent works on something you asked for, "
            "then open the room menu (&#8942;), choose Notifications, and pick Mention &amp; Keywords.",
            images=(("room-list-top.jpg", "The bell at the top of the room list"),)),
    Section("mobile", "Mobile",
            "The same rooms and agents on your phone.",
            "Pair it in Settings &rarr; Mobile pairing.",
            images=(("mobile-pairing.jpg", "Settings: Mobile pairing"),)),
]
OLD_IMPORT_SECTION = Section(
    "import", "Import your Claude Code history",
    "If you use Claude Code, {name} can read your local Claude Code history and bring what you "
    "worked on into its memory. You see a digest first; nothing is kept until you say so.",
    "Ask {name}:",
    tp.IMPORT_PROMPT, "dm",
)


def as_shots(sec):
    """The old (file, alt) pairs as asset references: the asset name is the file's stem, the file
    its fallback. This adapter is the only difference between the fixture and the list."""
    return sec._replace(images=tuple(tp.Shot(f[:-4], f, alt) for f, alt in sec.images))


# The old module appended the import section on a Claude core; the list says so with `runtime`.
OLD_LIST = tp.FeatureList(tp.FEATURES.revision,
                          tuple(as_shots(s) for s in OLD_SECTIONS) + (OLD_IMPORT_SECTION._replace(runtime="claude"),))
MXC_OF = {name: f"mxc://ag2.example/{i:02d}{name.replace('-', '')}" for i, name in enumerate(tp.asset_names(tp.FEATURES))}

STEP_IDS = ["dm", "rooms", "commons", "agents", "teamwork", "calls", "apps", "activity", "mobile", "import"]
ASSET_CAP = 60_000
PAGE_CAP = 300_000


def good_list(**over):
    data = json.loads(tp.LIST_PATH.read_text(encoding="utf-8"))
    data.update(over)
    return data


def with_step(**fields):
    """A list whose first step takes `fields` (a None value removes the key)."""
    data = good_list()
    step = data["steps"][0]
    for k, v in fields.items():
        if v is None:
            step.pop(k, None)
        else:
            step[k] = v
    return data


def many_steps(n, shots=0):
    """A list of `n` text-only steps; with `shots`, each step inlines the biggest fallback file that
    many times under distinct asset names (the duplicate-asset rule is per name, not per file)."""
    biggest = max(tp.ASSETS.glob("*.jpg"), key=lambda p: p.stat().st_size).name
    data = good_list()
    data["steps"] = [{"id": f"step-{i}", "title": f"Step {i}", "surface": "dm", "since_release": "2026-10-23",
                      "body": [f"What step {i} is for {{name}}.", "Ask {name}:"],
                      "screenshot": [{"asset": f"shot-{i}-{j}", "fallback_file": f"assets/{biggest}", "alt": "a"}
                                     for j in range(shots)]}
                     for i in range(n)]
    return data


def load_media_of(argv):
    """The --media map an argv names, as render_page takes it; {} without one."""
    argv = list(argv)
    return json.loads(Path(argv[argv.index("--media") + 1]).read_text()) if "--media" in argv else {}


def served_by(opened):
    """fetch_list's opener replaced by one whose open() is `opened`."""
    return mock.patch.object(gs, "list_opener", return_value=types.SimpleNamespace(open=opened))


class FakeTransport(urllib.request.BaseHandler):
    """Answers https, http and ftp requests from `routes`: {url: (status, body or Location)}."""
    handler_order = 100

    def __init__(self, routes):
        self.routes, self.seen = routes, []

    def _serve(self, req):
        self.seen.append(req.full_url)
        status, payload = self.routes[req.full_url]
        headers = email.message.Message()
        if status == 302:
            headers["Location"] = payload
        resp = urllib.response.addinfourl(io.BytesIO(b"" if status == 302 else payload), headers, req.full_url, status)
        resp.msg = "Found" if status == 302 else "OK"
        return resp

    https_open = http_open = ftp_open = _serve


class LosslessTest(unittest.TestCase):
    def test_the_list_renders_the_same_html_as_the_hard_coded_sections(self):
        cases = 0
        for name in ("Blue", "<script>x</script>", "  Kai  "):
            for core in tp.CORES:
                for audience in tp.AUDIENCES:
                    for agent in (None, "@kai.agent:example.test"):
                        for prompts in (None, {"dm": "Draft my update.", "finale": "Plan my week.", "bad": "x"}):
                            for roles in (None, "I picked: Founder, Sales."):
                                old = tp.render_page(name, core, audience, agent, prompts, roles, OLD_LIST)
                                new = tp.render_page(name, core, audience, agent, prompts, roles)
                                self.assertEqual(old, new, (name, core, audience, agent, prompts, roles))
                                cases += 1
        self.assertEqual(cases, 3 * 3 * 2 * 2 * 2 * 2)

    def test_the_bundled_list_carries_todays_steps(self):
        self.assertEqual([s.id for s in tp.FEATURES.steps], STEP_IDS)
        self.assertEqual([s.id for s in tp.SECTIONS], STEP_IDS[:-1])
        by = {s.id: s for s in tp.FEATURES.steps}
        self.assertEqual(by["import"].runtime, "claude")
        self.assertTrue(all(s.runtime is None for s in tp.FEATURES.steps if s.id != "import"))
        self.assertEqual({s.since_release for s in tp.FEATURES.steps}, {"2026-10-15"})
        self.assertEqual(tp.PROMPT_SLOTS, ("dm", "commons", "teamwork", "apps", "import", "finale"))
        self.assertEqual([s.id for s in tp.sections_for("codex")], STEP_IDS[:-1])
        self.assertEqual([s.id for s in tp.sections_for("claude")], STEP_IDS)
        raw = json.loads(tp.LIST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(raw["schema_version"], tp.SCHEMA_VERSION)
        self.assertEqual(raw["revision"], tp.FEATURES.revision)
        self.assertEqual(set(raw["steps"][0]), {"id", "title", "body", "surface", "screenshot",
                                                 "since_release", "paste"})
        shots = [shot for s in raw["steps"] if s["screenshot"] for shot in s["screenshot"]]
        self.assertEqual(len(shots), 11)
        for shot in shots:
            self.assertEqual(set(shot), {"asset", "fallback_file", "alt"})
            self.assertEqual(shot["fallback_file"], f"assets/{shot['asset']}.jpg")  # today's names: the stems

    def test_sections_match_field_for_field(self):
        for old, new in zip(OLD_LIST.steps, tp.FEATURES.steps):
            self.assertEqual((old.id, old.title, old.what, old.how, old.prompt, old.images),
                             (new.id, new.title, new.what, new.how, new.prompt, new.images))
            if old.prompt:
                self.assertEqual(old.where, new.where)  # the paste hint's surface
        for sec in OLD_SECTIONS:
            self.assertEqual([(f, alt) for f, alt in sec.images], [(s.file, s.alt) for s in as_shots(sec).images])


class ListTest(unittest.TestCase):
    def refuse(self, data, *words):
        with self.assertRaises(ValueError) as caught:
            tp.parse_list(data)
        for w in words:
            self.assertIn(w, str(caught.exception))

    def test_the_bundled_list_validates_and_extra_envelope_keys_are_allowed(self):
        served = good_list(kind="tutorial", vendor=None, etag="x")
        self.assertEqual(tp.parse_list(served), tp.FEATURES)
        self.assertEqual(tp.load_list(), tp.FEATURES)

    def test_each_malformed_shape_is_refused_by_name(self):
        self.refuse([], "JSON object")
        self.refuse(good_list(schema_version=2), "schema_version")
        self.refuse(good_list(revision="v2"), "revision")
        self.refuse(good_list(revision="tutorial.2026-10-23"), "revision")
        self.refuse(good_list(steps=[]), "steps")
        self.refuse(with_step(id="Marketplace!"), "steps[0]", "id")
        self.refuse(with_step(id="rooms"), "duplicate", "rooms")
        self.refuse(with_step(id="finale"), "finale")
        self.refuse(with_step(title=""), "step dm", "title")
        self.refuse(with_step(body=["only one part"]), "body", "2 parts")
        self.refuse(with_step(body=["what", "how", "a third part"]), "body", "2 parts")
        self.refuse(with_step(body=[]), "body", "2 parts")
        self.refuse(with_step(body="what, how"), "body", "2 parts")
        self.assertEqual(tp.BODY_PARTS, 2)
        self.refuse(good_list(revision="other.2026-10-24.1"), "revision", "tutorial.YYYY-MM-DD.N")
        self.refuse(with_step(body=["a", {"text": "b", "href": "https://x.test"}]), "link")
        self.refuse(with_step(body=["a", {"label": "b"}]), "body part")
        self.refuse(with_step(paste=""), "paste")
        self.refuse(with_step(surface="mobile"), "surface", "dm, room")  # a paste needs a chat surface
        self.refuse(with_step(paste=None, surface="desk"), "surface", "dm, room, mobile")
        shot = {"asset": "x", "fallback_file": "assets/room-list-add.jpg", "alt": "a"}
        self.refuse(with_step(screenshot=[{**shot, "fallback_file": "../x.jpg"}]), "screenshot[0]", "fallback_file")
        self.refuse(with_step(screenshot=[{**shot, "fallback_file": "room-list-add.jpg"}]), "assets/<name>.jpg")
        self.refuse(with_step(screenshot=[{**shot, "fallback_file": "assets/nope.jpg"}]), "nope.jpg")
        self.refuse(with_step(screenshot=[{"asset": "x", "fallback_file": "assets/room-list-add.jpg"}]), "alt")
        self.refuse(with_step(screenshot=[{**shot, "asset": "Room List"}]), "asset must be")
        self.refuse(with_step(screenshot=[{**shot, "asset": "room-list-add"}]), "duplicate asset", "room-list-add")
        self.refuse(with_step(screenshot=["room-list-add.jpg"]), "object with asset")
        self.refuse(with_step(screenshot="room-list-add.jpg"), "object with asset")
        self.refuse(with_step(since_release="Oct 2026"), "since_release")
        self.refuse(with_step(runtime="gemini"), "runtime")
        data = good_list()
        for s in data["steps"]:
            s["runtime"] = "claude"
        self.refuse(data, "every core")
        self.assertLessEqual(len(good_list()["steps"]), tp.STEPS_MAX // 2)
        self.refuse(many_steps(40), "steps", "at most 24", "got 40")
        self.assertEqual(len(tp.parse_list(many_steps(tp.STEPS_MAX)).steps), tp.STEPS_MAX)

    def test_markup_and_stray_placeholders_are_refused_in_the_title_and_both_body_parts(self):
        tag = "<script>fetch('https://x.test')</script>"
        self.refuse(with_step(title=f"Your DM {tag}"), "step dm title", "'<'")
        self.refuse(with_step(body=[tag, "Ask {name}:"]), "body[0]", "'<'")
        self.refuse(with_step(body=["What.", {"text": "a > b"}]), "body[1]", "'>'")
        self.refuse(with_step(body=["Costs {0} dollars", "How."]), "body[0]", "{name}", "{0}")
        self.refuse(with_step(body=["What.", "Ask {x}:"]), "body[1]", "{name}", "{x}")
        self.refuse(with_step(body=["What {}.", "How."]), "body[0]", "{name}")
        self.refuse(with_step(body=["{name!r}", "How."]), "body[0]", "{name}")
        self.refuse(with_step(body=["Unbalanced {", "How."]), "body[0]", "literal brace")
        self.refuse(with_step(title="Step {0}"), "step dm title", "{name}", "{0}")
        self.refuse(with_step(title="Hello {other}"), "step dm title", "{name}", "{other}")
        self.refuse(with_step(title="Un{matched"), "step dm title", "literal brace")
        page = tp.render_page("Blue", "codex", features=tp.parse_list(with_step(title="{{Your}} DM with {name}")))
        self.assertEqual(page.count("{Your} DM with Blue"), 2)  # the step heading and the tour's step title
        self.assertNotIn("{name}", page)
        data = with_step(body=["Braces {{like these}} stay literal for {name}.", "Ask {name}:"])
        page = tp.render_page("Blue", "codex", features=tp.parse_list(data))
        self.assertIn("Braces {like these} stay literal for Blue.", page)
        self.assertNotIn("{name}", page)

    def test_body_parts_may_be_text_objects_and_the_revision_orders_by_date_then_number(self):
        data = with_step(body=[{"text": "What."}, {"text": "How."}])
        self.assertEqual(tp.parse_list(data).steps[0][2:4], ("What.", "How."))
        self.assertEqual(tp.revision_key("tutorial.2026-10-23.10"), ("2026-10-23", 10))
        self.assertIsNone(tp.revision_key("tutorial.2026-10-23"))
        self.assertIsNone(tp.revision_key(None))
        self.assertTrue(tp.newer("tutorial.2026-10-23.10", "tutorial.2026-10-23.9"))
        self.assertTrue(tp.newer("tutorial.2026-11-01.1", "tutorial.2026-10-23.9"))
        self.assertTrue(tp.newer("tutorial.2026-10-23.1", None))  # a page from before lists had one
        self.assertFalse(tp.newer("tutorial.2026-10-23.1", "tutorial.2026-10-23.1"))
        self.assertFalse(tp.newer("tutorial.2026-10-23.1", "tutorial.2026-10-23.2"))
        self.assertFalse(tp.newer("junk", None))  # an unreadable list revision never forces a rebuild
        self.assertIsNone(tp.revision_key("other.2026-10-24.1"))  # the namespace is part of the format
        self.assertFalse(tp.newer("other.2026-10-24.1", "tutorial.2026-10-23.9"))

    def test_a_new_step_renders_with_the_default_art_and_opens_its_prompt_slot(self):
        data = good_list()
        data["steps"].insert(1, {"id": "marketplace", "title": "Marketplace", "surface": "dm",
                                 "body": ["Cloud tools for {name}.", "Ask {name}:"], "screenshot": None,
                                 "since_release": "2026-10-23", "paste": "Install the Notion tool."})
        features = tp.parse_list(data)
        self.assertEqual(tp.prompt_slots(features)[:2], ("dm", "marketplace"))
        page = tp.render_page("Blue", "codex", prompts={"marketplace": "Show me the Marketplace."},
                              features=features)
        self.assertIn('id="s-marketplace"', page)
        self.assertIn(tp.DEFAULT_ICON, page)
        self.assertIn(f"--a-marketplace:{tp.DEFAULT_ACCENT[0]};", page)
        self.assertIn(f"--a-marketplace:{tp.DEFAULT_ACCENT[1]};", page)
        self.assertIn("Show me the Marketplace.", page)
        self.assertEqual(base.steps(page)[1], "Marketplace")
        self.assertGreaterEqual(tp.contrast(tp.DEFAULT_ACCENT[0], tp.CARD[0]), 4.5)
        self.assertGreaterEqual(tp.contrast(tp.DEFAULT_ACCENT[1], tp.CARD[1]), 4.5)
        self.assertNotIn("--a-marketplace", tp.render_page("Blue", "codex"))

    def test_an_unreadable_file_is_a_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "list.json"
            bad.write_text("{nope")
            with self.assertRaisesRegex(ValueError, "not JSON"):
                tp.load_list(bad)
            with self.assertRaisesRegex(ValueError, "cannot read"):
                tp.load_list(Path(tmp) / "missing.json")
            big = Path(tmp) / "big.json"
            big.write_bytes(json.dumps(good_list(pad="x" * tp.LIST_MAX_BYTES)).encode())
            with self.assertRaisesRegex(ValueError, f"over the {tp.LIST_MAX_BYTES}-byte cap"):
                tp.load_list(big)
            self.assertEqual(gs.LIST_MAX_BYTES, tp.LIST_MAX_BYTES)  # one cap for a file and a URL
            fifo = Path(tmp) / "list.fifo"  # no writer: a plain open would block for ever
            os.mkfifo(fifo)
            done = []

            def attempt():
                with self.assertRaisesRegex(ValueError, "not a regular file"):
                    tp.load_list(fifo)
                done.append(True)
            reader = threading.Thread(target=attempt, daemon=True)
            reader.start()
            reader.join(10)
            self.assertEqual(done, [True])
            with self.assertRaisesRegex(ValueError, "not a regular file"):
                tp.load_list(Path(tmp))


class MediaTest(unittest.TestCase):
    """A screenshot is an asset reference: the room's media when the room has it, the fallback
    file inlined otherwise, which is today's page byte for byte."""

    def test_clean_media_keeps_only_the_lists_assets_with_well_formed_mxc_uris(self):
        raw = {**MXC_OF, "unknown": "mxc://ag2.example/zz", "room-list-top": "https://ag2.example/x.jpg",
               "mobile-pairing": "mxc://no-id", "commons-surfaces": 7}
        kept = tp.clean_media(raw)
        self.assertEqual(set(kept), set(tp.asset_names(tp.FEATURES)) - {"room-list-top", "mobile-pairing",
                                                                         "commons-surfaces"})
        self.assertEqual(tp.clean_media(["x"]), {})
        self.assertEqual(tp.clean_media(None), {})
        self.assertEqual(len(tp.asset_names(tp.FEATURES)), 11)

    def test_a_mapped_asset_points_at_the_rooms_media_and_the_rest_stay_inlined(self):
        page = tp.render_page("Blue", "codex", media={"room-list-add": MXC_OF["room-list-add"]})
        self.assertIn(f'<img src="{MXC_OF["room-list-add"]}" alt="The + button at the top of the room list" '
                      'loading="lazy">', page)
        self.assertEqual(page.count('<img src="data:image/jpeg;base64,'), 10)
        self.assertEqual(page.count('<img src="mxc://'), 1)

    def test_a_fully_mapped_page_shrinks_to_text(self):
        for core in tp.CORES:
            page = tp.render_page("Blue", core, "dm", "@kai.agent:example.test", media=MXC_OF)
            self.assertNotIn("data:image", page)
            self.assertEqual(page.count('<img src="mxc://'), 11)
            self.assertLess(len(page.encode("utf-8")), 40_000, core)

    def test_without_media_the_page_is_the_fallback_page(self):
        self.assertEqual(tp.render_page("Blue", "claude", media={}), tp.render_page("Blue", "claude"))
        self.assertEqual(tp.render_page("Blue", "claude", media={"junk": "mxc://ag2.example/x"}),
                         tp.render_page("Blue", "claude"))


class MarkerTest(unittest.TestCase):
    ME = "@kai.agent:example.test"

    def test_the_marker_records_the_revision_and_reads_back_from_old_and_new_pages(self):
        new = tp.owner_marker(self.ME, tp.FEATURES.revision)
        self.assertEqual(new, f'<meta name="sutando-tutorial" content="{self.ME}" '
                              f'data-revision="{tp.FEATURES.revision}">')
        self.assertEqual(tp.read_marker("<html>" + new), (self.ME, tp.FEATURES.revision))
        self.assertEqual(tp.read_marker(tp.owner_marker(self.ME)), (self.ME, None))  # a pre-list page
        self.assertEqual(tp.read_marker(tp.owner_marker('@a"b:x', 'r"1')), ('@a"b:x', 'r"1'))
        self.assertIsNone(tp.read_marker("<html>no tag</html>"))
        self.assertIsNone(tp.read_marker(""))

    def test_the_page_carries_the_list_revision_only_with_an_agent(self):
        page = tp.render_page("Blue", "codex", "dm", self.ME)
        self.assertEqual(tp.read_marker(page), (self.ME, tp.FEATURES.revision))
        self.assertIsNone(tp.read_marker(tp.render_page("Blue", "codex")))
        other = tp.FeatureList("tutorial.2026-11-01.1", tp.FEATURES.steps)
        self.assertEqual(tp.read_marker(tp.render_page("Blue", "codex", "dm", self.ME, features=other))[1],
                         "tutorial.2026-11-01.1")

    def test_ownership_reads_both_marker_forms(self):
        page = {"kind": "html-g", "title": "Getting started", "by": self.ME}
        self.assertTrue(gs.owned(page, tp.owner_marker(self.ME), self.ME))
        self.assertTrue(gs.owned(page, tp.owner_marker(self.ME, "tutorial.2026-10-23.1"), self.ME))
        self.assertFalse(gs.owned(page, tp.owner_marker("@other:x", "tutorial.2026-10-23.1"), self.ME))
        self.assertFalse(gs.owned(page, 'x <meta name="sutando-tutorial" content="@kai.agent:example.test', self.ME))


class SizeTest(unittest.TestCase):
    def test_every_fallback_file_is_under_the_per_asset_cap(self):
        files = {p.name: p.stat().st_size for p in tp.ASSETS.glob("*.jpg")}
        self.assertTrue(files)
        over = {f: n for f, n in files.items() if n > ASSET_CAP}
        self.assertEqual(over, {})
        used = {shot.file for s in tp.FEATURES.steps for shot in s.images}
        self.assertEqual(used, set(files))

    def test_every_page_variant_is_under_the_page_cap(self):
        long_prompts = {slot: "x" * tp.PROMPT_MAX for slot in tp.PROMPT_SLOTS}
        roles = ", ".join("r" * 40 for _ in range(tp.ROLES_MAX))
        for core in tp.CORES:
            for audience in tp.AUDIENCES:
                page = tp.render_page("x" * tp.NAME_MAX, core, audience, "@" + "a" * 200 + ":example.test",
                                      long_prompts, roles)
                self.assertLessEqual(len(page.encode("utf-8")), PAGE_CAP, (core, audience))


    def test_render_refuses_a_page_over_the_cap_however_the_list_reaches_it(self):
        self.assertEqual(tp.PAGE_MAX_BYTES, PAGE_CAP)
        heavy = tp.parse_list(many_steps(tp.STEPS_MAX, shots=3))  # within the step bound, over the cap
        with self.assertRaisesRegex(ValueError, f"over the {PAGE_CAP}-byte cap") as caught:
            tp.render_page("Blue", "codex", features=heavy)
        self.assertIn("room media", str(caught.exception))
        light = tp.parse_list(many_steps(tp.STEPS_MAX))
        self.assertLess(len(tp.render_page("Blue", "codex", features=light).encode("utf-8")), PAGE_CAP)


class _Cli(unittest.TestCase):
    """main() in-process against a stub room_commons.py, like the base MainTest."""

    ROOM = "!dm:example.test"
    ME = "@kai.agent:example.test"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tutorial-list-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.stub = self.tmp / "room_commons.py"
        self.stub.write_text(base.STUB)
        env = {k: v for k, v in base.isolated_child_env().items()
               if not k.startswith(("SUTANDO_", "AG2", "REMOTE_TASK", "AGENT_", "ROOM_C", "MATRIX_"))
               and k not in ("CLAUDE_CONFIG_DIR", "CLAUDE_HOME")}
        env.update({"CLAUDE_CONFIG_DIR": str(self.tmp / "claude"), "STUB_STATE": str(self.tmp / "room.json"),
                    "SUTANDO_WORKSPACE_DIR": str(self.tmp / "ws")})
        for patcher in (mock.patch.dict(os.environ, env, clear=True),
                        mock.patch.object(gs, "_workspace", return_value=self.tmp / "ws")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.door = base.FakeActions(self.tmp / "room.json", by=self.ME)
        door = mock.patch.object(gs, "connect_actions", return_value=self.door)
        door.start()
        self.addCleanup(door.stop)
        base.linux_exec_limit(self)

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = gs.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def write_list(self, revision, name="list.json", **over):
        path = self.tmp / name
        path.write_text(json.dumps(good_list(revision=revision, **over)))
        return path

    def page_text(self, kind="html-p1"):
        return json.loads((self.tmp / "room.json").read_text())["pages"][kind]

    def record(self, **fields):
        marker = gs.marker_path(self.tmp / "ws")
        rooms = gs.load_marker(marker)
        rooms[self.ROOM] = {**rooms[self.ROOM], **fields}
        gs.save_marker(marker, rooms)

    def chunked(self):
        """The rewrite above the one-call budget: the whole page replaced by the target's first
        piece, the rest appended. Real pages fit one call; the tests pin both paths."""
        return mock.patch.object(gs, "one_call", return_value=False)

    def publish(self, *extra):
        return self.main("publish", "--room", self.ROOM, "--name", "Blue", "--core", "codex",
                         "--room-commons", str(self.stub), "--agent-mxid", self.ME, *extra)


class ListOverrideTest(_Cli):
    def test_render_from_a_list_file(self):
        other = self.write_list("tutorial.2026-11-01.1")
        code, out, err = self.main("render", "--name", "Blue", "--core", "codex", "--list", str(other))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, tp.render_page("Blue", "codex", features=tp.load_list(other)))

    def test_a_bad_list_falls_back_to_the_file_and_says_so(self):
        broken = self.tmp / "broken.json"
        broken.write_text(json.dumps(good_list(schema_version=9)))
        code, out, err = self.main("render", "--name", "Blue", "--core", "codex", "--list", str(broken))
        self.assertEqual(code, 0)
        self.assertEqual(out, tp.render_page("Blue", "codex"))
        self.assertIn("--list", err)
        self.assertIn("schema_version", err)
        self.assertIn("skill's own list", err)
        code, out, err = self.main("render", "--name", "Blue", "--list", str(self.tmp / "missing.json"))
        self.assertEqual((code, out), (0, tp.render_page("Blue")))
        self.assertIn("cannot read", err)

    def test_a_list_with_markup_or_a_stray_placeholder_falls_back_to_the_file_and_says_so(self):
        for field, bad, said in (("title", "Your DM <script>x</script>", "title"),
                                 ("body", ["<a href='https://x.test'>x</a>", "How."], "body[0]"),
                                 ("body", ["Costs {0} dollars", "How."], "{0}"),
                                 ("body", ["What.", "Ask {x}:"], "{x}")):
            broken = self.tmp / "broken.json"
            broken.write_text(json.dumps(with_step(**{field: bad})))
            code, out, err = self.main("render", "--name", "Blue", "--core", "codex", "--list", str(broken))
            self.assertEqual(code, 0, (bad, err))
            self.assertEqual(out, tp.render_page("Blue", "codex"))
            self.assertNotIn("x.test", out)
            for w in ("--list", said, "skill's own list"):
                self.assertIn(w, err, bad)

    def test_a_page_over_the_cap_is_refused_by_render_and_publish(self):
        heavy = self.tmp / "heavy.json"
        heavy.write_text(json.dumps(many_steps(tp.STEPS_MAX, shots=3)))
        code, out, err = self.main("render", "--name", "Blue", "--core", "codex", "--list", str(heavy))
        self.assertEqual((code, out), (2, ""))
        self.assertIn(f"over the {PAGE_CAP}-byte cap", err)
        code, out, _ = self.publish("--list", str(heavy))
        self.assertEqual((code, json.loads(out)["ok"]), (4, False))
        self.assertIn(f"over the {PAGE_CAP}-byte cap", json.loads(out)["reason"])
        state = self.tmp / "room.json"
        self.assertFalse(state.exists() and json.loads(state.read_text()).get("pages"))  # nothing was written

    def test_no_network_call_without_a_list_url(self):
        with mock.patch.object(gs.urllib.request, "urlopen", side_effect=AssertionError("network")) as net, \
                mock.patch.object(gs, "list_opener", side_effect=AssertionError("network")) as opener:
            code, out, _ = self.main("render", "--name", "Blue", "--core", "codex")
            self.assertEqual((code, out), (0, tp.render_page("Blue", "codex")))
            other = self.write_list("tutorial.2026-11-01.1")
            code, _, _ = self.main("render", "--name", "Blue", "--list", str(other))
            self.assertEqual(code, 0)
            code, _, _ = self.publish()
            self.assertEqual(code, 0)
        net.assert_not_called()
        opener.assert_not_called()

    def test_a_served_list_replaces_the_file(self):
        served = json.dumps(good_list(revision="tutorial.2026-11-01.1", kind="tutorial", etag="x")).encode()
        seen = {}

        @contextlib.contextmanager
        def opened(req, timeout=None):
            seen["url"], seen["ua"], seen["timeout"] = req.full_url, req.get_header("User-agent"), timeout
            yield io.BytesIO(served)

        with served_by(opened):
            code, out, err = self.main("render", "--name", "Blue", "--core", "codex",
                                       "--list", "https://api.example.test/setup-copy?scope=tutorial")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(seen, {"url": "https://api.example.test/setup-copy?scope=tutorial",
                                "ua": gs.USER_AGENT, "timeout": gs.LIST_TIMEOUT})
        self.assertEqual(out, tp.render_page("Blue", "codex", features=tp.parse_list(json.loads(served))))
        with served_by(opened):
            code, out, _ = self.publish("--list", "https://u:secret@api.example.test:8443/setup-copy?token=t#x")
        self.assertEqual((code, json.loads(out)["list"]), (0, "https://api.example.test:8443/setup-copy"))

    def test_a_redirect_is_followed_only_to_https(self):
        served = json.dumps(good_list(revision="tutorial.2026-11-01.1")).encode()
        routes = {"https://trusted.example/list": (302, "http://127.0.0.1:8080/cleartext-list"),
                  "http://127.0.0.1:8080/cleartext-list": (200, served),
                  "https://trusted.example/ftp": (302, "ftp://files.example/list"),
                  "ftp://files.example/list": (200, served),
                  "https://trusted.example/moved": (302, "https://cdn.example/list?sig=s"),
                  "https://cdn.example/list?sig=s": (200, served)}
        for url, target in (("https://trusted.example/list", "http://127.0.0.1:8080/cleartext-list"),
                            ("https://trusted.example/ftp", "ftp://files.example/list")):
            transport = FakeTransport(routes)
            with self.assertRaises(ValueError) as caught:
                gs.fetch_list(url, gs.list_opener(transport))
            self.assertIn("only https", str(caught.exception))
            self.assertEqual(transport.seen, [url])  # the cleartext target is never requested
            self.assertNotIn("secret", str(caught.exception))
        transport = FakeTransport(routes)
        self.assertEqual(gs.fetch_list("https://trusted.example/moved", gs.list_opener(transport)).revision,
                         "tutorial.2026-11-01.1")
        self.assertEqual(transport.seen, ["https://trusted.example/moved", "https://cdn.example/list?sig=s"])
        # Through the CLI the refusal falls back to the file; the note shows no userinfo or query.
        with mock.patch.object(gs, "list_opener", lambda *h: urllib.request.build_opener(
                gs._HttpsRedirects(), FakeTransport({"https://me:secret@trusted.example/list?token=abc": (302, "http://x.test/?k=v")}))):
            code, out, err = self.main("render", "--name", "Blue", "--core", "codex",
                                       "--list", "https://me:secret@trusted.example/list?token=abc")
        self.assertEqual((code, out), (0, tp.render_page("Blue", "codex")))
        self.assertIn("--list https://trusted.example/list not used", err)
        self.assertIn("redirect to http://x.test/ refused", err)
        for leak in ("secret", "token=abc", "k=v", "me:"):
            self.assertNotIn(leak, err)
        self.assertEqual(gs.safe_source("https://u:p@h.test:8443/x/y?token=1#f"), "https://h.test:8443/x/y")
        self.assertEqual(gs.safe_source("/tmp/list.json"), "/tmp/list.json")
        self.assertEqual(gs.safe_source("http://user:password@example.test:bad/list?token=x"),
                         "http://example.test:bad/list")
        self.assertEqual(gs.safe_source("https://h.test?token=x"), "https://h.test")
        code, out, err = self.main("render", "--name", "Blue", "--core", "codex",
                                   "--list", "http://user:password@example.test:bad/list?token=x")
        self.assertEqual((code, out), (0, tp.render_page("Blue", "codex")))  # the fallback, not exit 2
        self.assertIn("--list http://example.test:bad/list not used", err)
        for leak in ("password", "token=x", "user:"):
            self.assertNotIn(leak, err)

    def test_a_served_list_that_fails_falls_back(self):
        for url, payload, word in (("http://api.example.test/x", b"{}", "https"),
                                   ("https://api.example.test/x", b"not json", "not JSON"),
                                   ("https://api.example.test/x", b"x" * (gs.LIST_MAX_BYTES + 1), "over"),
                                   ("https://api.example.test/x", json.dumps(good_list(steps=[])).encode(), "steps")):
            @contextlib.contextmanager
            def opened(req, timeout=None, payload=payload):
                yield io.BytesIO(payload)

            with served_by(opened):
                code, out, err = self.main("render", "--name", "Blue", "--core", "codex", "--list", url)
            self.assertEqual((code, out), (0, tp.render_page("Blue", "codex")), url)
            self.assertIn(word, err, url)
        with served_by(mock.Mock(side_effect=OSError("refused"))):
            code, out, err = self.main("render", "--name", "Blue", "--list", "https://api.example.test/x")
        self.assertEqual((code, out), (0, tp.render_page("Blue")))
        self.assertIn("cannot fetch", err)
        self.assertNotIn("refused", err)  # the transport's own words stay out of the note

    def test_media_points_the_page_at_room_media_and_is_remembered_for_the_room(self):
        media = self.tmp / "media.json"
        media.write_text(json.dumps({"room-list-add": MXC_OF["room-list-add"], "junk": "mxc://ag2.example/j",
                                     "mobile-pairing": "https://ag2.example/x.jpg"}))
        code, out, _ = self.publish("--media", str(media))
        got = json.loads(out)
        self.assertEqual((code, got["action"], got["media"]), (0, "created", ["room-list-add"]))
        self.assertIn(f'<img src="{MXC_OF["room-list-add"]}"', self.page_text())
        self.assertEqual(self.page_text().count("data:image/jpeg"), 10)
        entry = gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]
        self.assertEqual(entry["media"], {"room-list-add": MXC_OF["room-list-add"]})
        code, out, _ = self.publish()  # the room keeps its media map
        self.assertEqual((code, json.loads(out)["action"], json.loads(out)["media"]),
                         (0, "unchanged", ["room-list-add"]))
        more = self.tmp / "more.json"
        more.write_text(json.dumps({"new-room-dialog": MXC_OF["new-room-dialog"]}))
        code, out, _ = self.publish("--media", str(more))
        self.assertEqual((code, json.loads(out)["action"], json.loads(out)["media"]),
                         (0, "updated", ["new-room-dialog", "room-list-add"]))
        self.assertEqual(self.page_text().count('<img src="mxc://'), 2)
        broken = self.tmp / "broken.json"
        broken.write_text("{nope")
        code, out, _ = self.main("render", "--name", "Blue", "--media", str(broken))
        self.assertEqual((code, out), (0, tp.render_page("Blue")))

    def test_prompts_for_a_slot_only_the_served_list_has_are_used_and_kept(self):
        data = good_list(revision="tutorial.2026-11-01.1")
        data["steps"].insert(1, {"id": "marketplace", "title": "Marketplace", "surface": "dm",
                                 "body": ["Cloud tools for {name}.", "Ask {name}:"], "screenshot": None,
                                 "since_release": "2026-11-01", "paste": "Install the Notion tool."})
        other = self.tmp / "m.json"
        other.write_text(json.dumps(data))
        prompts = self.tmp / "p.json"
        prompts.write_text(json.dumps({"marketplace": "Show me the Marketplace."}))
        code, out, _ = self.publish("--list", str(other), "--prompts", str(prompts))
        self.assertEqual((code, json.loads(out)["action"]), (0, "created"))
        self.assertIn("Show me the Marketplace.", self.page_text())
        self.assertNotIn("Install the Notion tool.", self.page_text())
        entry = gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]
        self.assertEqual(entry["prompts"], {"marketplace": "Show me the Marketplace."})
        data["revision"] = "tutorial.2026-11-02.1"
        other.write_text(json.dumps(data))
        code, out, _ = self.publish("--if-newer", "--list", str(other))
        self.assertEqual((code, json.loads(out)["action"]), (0, "updated"))
        self.assertIn("Show me the Marketplace.", self.page_text())

    def test_publish_reports_which_list_it_used(self):
        other = self.write_list("tutorial.2026-11-01.1")
        code, out, _ = self.publish("--list", str(other))
        got = json.loads(out)
        self.assertEqual((code, got["action"], got["list"], got["revision"]),
                         (0, "created", str(other), "tutorial.2026-11-01.1"))
        self.assertEqual(tp.read_marker(self.page_text()), (self.ME, "tutorial.2026-11-01.1"))
        entry = gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]
        self.assertEqual((entry["state"], entry["revision"]), ("complete", "tutorial.2026-11-01.1"))


class IfNewerTest(_Cli):
    def test_a_newer_list_rebuilds_the_page_in_place(self):
        code, out, _ = self.publish()
        self.assertEqual((code, json.loads(out)["action"]), (0, "created"))
        newer = self.write_list("tutorial.2026-11-01.1")
        code, out, _ = self.publish("--if-newer", "--list", str(newer))
        got = json.loads(out)
        self.assertEqual((code, got["action"], got["kind"], got["summoned"]), (0, "updated", "html-p1", False))
        self.assertEqual(tp.read_marker(self.page_text()), (self.ME, "tutorial.2026-11-01.1"))
        self.assertEqual(gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]["revision"],
                         "tutorial.2026-11-01.1")

    def test_a_current_page_is_left_alone_even_when_the_text_would_differ(self):
        self.publish()
        before = self.page_text()
        code, out, _ = self.main("publish", "--room", self.ROOM, "--name", "Kai", "--core", "claude",
                                 "--room-commons", str(self.stub), "--agent-mxid", self.ME, "--if-newer")
        self.assertEqual((code, json.loads(out)["action"]), (0, "unchanged"))
        self.assertEqual(self.page_text(), before)
        older = self.write_list("tutorial.2026-10-01.1")
        code, out, _ = self.publish("--if-newer", "--list", str(older))
        self.assertEqual((code, json.loads(out)["action"]), (0, "unchanged"))
        self.assertEqual(self.page_text(), before)
        # Without --if-newer, a differing text still rewrites, as before.
        code, out, _ = self.main("publish", "--room", self.ROOM, "--name", "Kai", "--core", "codex",
                                 "--room-commons", str(self.stub), "--agent-mxid", self.ME)
        self.assertEqual((code, json.loads(out)["action"]), (0, "updated"))
        self.assertIn("Getting started with Kai", self.page_text())

    def test_a_page_from_before_lists_had_a_revision_is_rebuilt(self):
        self.publish()
        db = json.loads((self.tmp / "room.json").read_text())
        db["pages"]["html-p1"] = tp.MARKER.sub(tp.owner_marker(self.ME), db["pages"]["html-p1"])
        (self.tmp / "room.json").write_text(json.dumps(db))
        self.record(hash=gs.content_hash(db["pages"]["html-p1"]))  # the old script wrote that page
        self.assertEqual(tp.read_marker(self.page_text()), (self.ME, None))
        code, out, _ = self.publish("--if-newer")
        self.assertEqual((code, json.loads(out)["action"]), (0, "updated"))
        self.assertEqual(tp.read_marker(self.page_text()), (self.ME, tp.FEATURES.revision))

    def test_the_refresh_keeps_the_prompts_the_page_was_built_with(self):
        prompts = self.tmp / "p.json"
        prompts.write_text(json.dumps({"dm": "Draft my weekly update.", "finale": "Plan my sprint."}))
        self.publish("--prompts", str(prompts), "--roles", "Founder")
        entry = gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]
        self.assertEqual(entry["prompts"], {"dm": "Draft my weekly update.", "finale": "Plan my sprint."})
        newer = self.write_list("tutorial.2026-11-01.1")
        code, out, _ = self.publish("--if-newer", "--list", str(newer), "--roles", "Founder")
        self.assertEqual((code, json.loads(out)["action"]), (0, "updated"))
        page = self.page_text()
        self.assertIn("Draft my weekly update.", page)
        self.assertIn("Plan my sprint.", page)
        self.assertIn("Picked for: Founder", page)
        # A plain publish without --prompts resets to the generic prompts, as before.
        code, out, _ = self.publish("--list", str(newer))
        self.assertEqual((code, json.loads(out)["action"]), (0, "updated"))
        self.assertNotIn("Draft my weekly update.", self.page_text())
        self.assertNotIn("prompts", gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM])

    def test_the_refresh_keeps_the_roles_line_without_being_told_again(self):
        self.publish("--roles", "I picked: Founder, Sales.")
        self.assertEqual(gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]["roles"], "Founder, Sales")
        newer = self.write_list("tutorial.2026-11-01.1")
        code, out, _ = self.publish("--if-newer", "--list", str(newer))
        self.assertEqual((code, json.loads(out)["action"]), (0, "updated"))
        self.assertIn('<p class="roles">Picked for: Founder, Sales</p>', self.page_text())
        self.assertEqual(tp.read_marker(self.page_text()), (self.ME, "tutorial.2026-11-01.1"))
        code, out, _ = self.publish("--if-newer", "--list", str(self.write_list("tutorial.2026-11-02.1")),
                                    "--roles", "Engineer")  # roles given again win
        self.assertEqual((code, json.loads(out)["action"]), (0, "updated"))
        self.assertIn("Picked for: Engineer</p>", self.page_text())
        code, out, _ = self.publish("--list", str(newer))  # a plain publish resets, like the prompts
        self.assertEqual((code, json.loads(out)["action"]), (0, "updated"))
        self.assertNotIn('class="roles"', self.page_text())
        self.assertNotIn("roles", gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM])

    def test_a_page_someone_edited_is_kept_until_it_hashes_to_the_entry_again(self):
        self.publish()
        written = gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]["hash"]
        db = json.loads((self.tmp / "room.json").read_text())
        mine, theirs = db["pages"]["html-p1"], db["pages"]["html-p1"].replace("</main>", "<p>Our notes.</p></main>")
        db["pages"]["html-p1"] = theirs
        (self.tmp / "room.json").write_text(json.dumps(db))
        newer = self.write_list("tutorial.2026-11-01.1")
        for extra in (("--if-newer", "--list", str(newer)), ("--name", "Kai")):
            code, out, err = self.main("publish", "--room", self.ROOM, "--name", "Blue", "--core", "codex",
                                       "--room-commons", str(self.stub), "--agent-mxid", self.ME, *extra)
            got = json.loads(out)
            self.assertEqual((code, got["action"], got.get("edited")), (0, "kept", True), extra)
            self.assertEqual(self.page_text(), theirs)
            self.assertIn("html-p1", err)
            self.assertIn("edited", err)
            entry = gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]
            self.assertEqual((entry["hash"], entry["revision"]), (written, tp.FEATURES.revision))
        db["pages"]["html-p1"] = mine  # put back as written: the next newer list rebuilds it
        (self.tmp / "room.json").write_text(json.dumps(db))
        code, out, err = self.publish("--if-newer", "--list", str(newer))
        self.assertEqual((code, json.loads(out)["action"], err), (0, "updated", ""))
        self.assertEqual(tp.read_marker(self.page_text()), (self.ME, "tutorial.2026-11-01.1"))

    def test_a_same_or_older_revision_pass_never_blesses_an_owner_edit(self):
        self.publish()
        written = gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]["hash"]
        db = json.loads((self.tmp / "room.json").read_text())
        theirs = db["pages"]["html-p1"].replace("</main>", "<p>Our notes.</p></main>")
        db["pages"]["html-p1"] = theirs
        (self.tmp / "room.json").write_text(json.dumps(db))
        older = self.write_list("tutorial.2026-10-01.1")
        for extra in (("--if-newer",), ("--if-newer", "--list", str(older))):
            code, out, err = self.publish(*extra)
            self.assertEqual((code, json.loads(out)["action"]), (0, "kept"), extra)
            self.assertIn("edited", err)
            self.assertEqual(gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]["hash"], written)
        code, out, _ = self.publish("--if-newer", "--list", str(self.write_list("tutorial.2026-11-01.1")))
        self.assertEqual((code, json.loads(out)["action"]), (0, "kept"))
        self.assertEqual(self.page_text(), theirs)  # the edit survives the next revision

    def test_an_unchanged_or_kept_run_leaves_the_entry_as_written(self):
        prompts, media = self.tmp / "p.json", self.tmp / "m.json"
        prompts.write_text(json.dumps({"dm": "Draft my update."}))
        media.write_text(json.dumps({"room-list-add": MXC_OF["room-list-add"]}))
        self.publish("--prompts", str(prompts), "--roles", "Founder", "--media", str(media))
        before = gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]
        self.assertEqual((before["prompts"], before["roles"], before["media"]),
                         ({"dm": "Draft my update."}, "Founder", {"room-list-add": MXC_OF["room-list-add"]}))
        more = self.tmp / "more.json"
        more.write_text(json.dumps({"new-room-dialog": MXC_OF["new-room-dialog"]}))
        older = self.write_list("tutorial.2026-10-01.1")
        code, out, _ = self.publish("--if-newer", "--list", str(older), "--media", str(more))
        self.assertEqual((code, json.loads(out)["action"]), (0, "unchanged"))
        self.assertEqual(gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM], before)
        db = json.loads((self.tmp / "room.json").read_text())
        db["pages"]["html-p1"] += "<!-- theirs -->"
        (self.tmp / "room.json").write_text(json.dumps(db))
        code, out, _ = self.publish("--media", str(more), "--roles", "Sales")
        self.assertEqual((code, json.loads(out)["action"]), (0, "kept"))
        self.assertEqual(gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM], before)

    BOUNDARIES = {"replace:1": ["replace"], "append:1": ["replace", "append"]}

    def fail_at(self, at, newer, prompts):
        """A created page, then an --if-newer rewrite to `newer` with prompts and roles that the
        stub fails at call `at`. Returns (the page before, the target, what the page holds now)."""
        (self.tmp / "room.json").unlink(missing_ok=True)
        shutil.rmtree(self.tmp / "ws", ignore_errors=True)
        code, out, _ = self.publish()
        self.assertEqual((code, json.loads(out)["action"]), (0, "created"))
        db = json.loads((self.tmp / "room.json").read_text())
        mine, db["calls"] = db["pages"]["html-p1"], []  # STUB_FAIL_AT counts from here
        (self.tmp / "room.json").write_text(json.dumps(db))
        os.environ["STUB_FAIL_AT"] = at
        with self.chunked():
            code, out, _ = self.publish("--if-newer", "--list", str(newer), "--prompts", str(prompts), "--roles", "Founder")
        os.environ.pop("STUB_FAIL_AT")
        self.assertEqual((code, json.loads(out)["ok"]), (4, False))
        db = json.loads((self.tmp / "room.json").read_text())
        self.assertEqual([c[0] for c in db["calls"] if c[0] in ("replace", "append")], self.BOUNDARIES[at])
        journal = self.entry()
        self.assertEqual((journal["state"], gs.content_hash(journal["target"]), journal["source"]),
                         ("updating", journal["hash"], gs.content_hash(mine)))
        self.assertEqual((journal["name"], journal["core"], journal["agent"], journal["previous"]["hash"]),
                         ("Blue", "codex", self.ME, gs.content_hash(mine)))
        self.assertEqual((journal["done"], journal["before"]), (len(self.BOUNDARIES[at]) - 1, mine))
        return mine, journal["target"], self.page_text()

    def entry(self):
        return gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]

    def writes_since(self, n):
        return [c for c in json.loads((self.tmp / "room.json").read_text())["calls"][n:]
                if c[0] not in ("pages", "read")]

    def calls(self):
        return len(json.loads((self.tmp / "room.json").read_text())["calls"])

    def test_a_rewrite_cut_short_is_finished_by_the_next_run_at_the_exact_target(self):
        prompts = self.tmp / "p.json"
        prompts.write_text(json.dumps({"dm": "Draft my update."}))
        newer = self.write_list("tutorial.2026-11-01.1")
        target = tp.render_page("Blue", "codex", "dm", self.ME, {"dm": "Draft my update."}, "Founder",
                                tp.load_list(newer))
        self.assertEqual(len(gs.chunks(target)), 2)  # the default-size page: one replace, one append
        for at in self.BOUNDARIES:
            with self.subTest(at=at):
                mine, journaled, left = self.fail_at(at, newer, prompts)
                self.assertEqual(journaled, target)
                journal = self.entry()
                if at == "replace:1":
                    self.assertEqual(left, mine)  # the rewrite never started
                else:
                    self.assertNotEqual(left, target)
                    self.assertIn(gs.content_hash(left), journal["steps"])  # a state the rewrite itself leaves
                if at == "append:1":
                    self.assertEqual(left, gs.chunks(target)[0])  # the target's first piece, the old page gone
                self.assertEqual(json.loads(self.main("plan", "--room", self.ROOM)[1])["pending"], True)
                # The same request again: finished from the journal, or run afresh when nothing ran.
                code, out, err = self.publish("--if-newer", "--list", str(newer), "--prompts", str(prompts),
                                              "--roles", "Founder", "--owner", "@owner:example.test", "--summon")
                got = json.loads(out)
                if at == "replace:1":
                    self.assertEqual((code, got["action"], got.get("resumed"), got["summoned"], err),
                                     (0, "updated", None, True, ""))  # --summon honoured: nothing was resumed
                else:
                    self.assertEqual((code, got["action"], got["resumed"], got["summoned"], got["name"],
                                      got["revision"], got["list"]),
                                     (0, "resumed", True, False, "Blue", "tutorial.2026-11-01.1", "journal"))
                    self.assertIn("were not applied", err)
                self.assertEqual(self.page_text(), target)
                entry = self.entry()
                self.assertEqual((entry["state"], entry["hash"], entry["prompts"], entry["roles"]),
                                 ("complete", gs.content_hash(target), {"dm": "Draft my update."}, "Founder"))
                self.assertFalse(set(entry) & set(gs.JOURNAL_KEYS))
                code, out, _ = self.publish("--if-newer", "--list", str(newer))
                self.assertEqual((code, json.loads(out)["action"]), (0, "unchanged"))

    def test_an_edit_made_while_a_rewrite_was_cut_short_is_kept_at_every_boundary(self):
        prompts = self.tmp / "p.json"
        prompts.write_text(json.dumps({"dm": "Draft my update."}))
        newer = self.write_list("tutorial.2026-11-01.1")
        for at in self.BOUNDARIES:
            with self.subTest(at=at):
                mine, target, left = self.fail_at(at, newer, prompts)
                previous = {k: v for k, v in self.entry()["previous"].items() if k != "updated_at"}
                db = json.loads((self.tmp / "room.json").read_text())
                theirs = left + "<p>OWNER NOTES</p>"
                db["pages"]["html-p1"] = theirs
                (self.tmp / "room.json").write_text(json.dumps(db))
                n = self.calls()
                for extra in (("--if-newer", "--list", str(newer), "--prompts", str(prompts), "--roles", "Founder"),
                              ("--name", "Kai")):  # the next ordinary run still sees an owner's page
                    code, out, err = self.publish(*extra)
                    got = json.loads(out)
                    self.assertEqual((code, got["action"], got["edited"], got.get("resumed")), (0, "kept", True, None), extra)
                    self.assertIn("edited", err)
                    self.assertEqual(self.page_text(), theirs)
                    entry = {k: v for k, v in self.entry().items() if k != "updated_at"}
                    self.assertEqual(entry.pop("interrupted")["target"], gs.content_hash(target))
                    self.assertEqual(entry, previous)  # the entry from before the rewrite, whole
                    self.assertEqual(entry["hash"], gs.content_hash(mine))
                    self.assertEqual(self.writes_since(n), [])

    def edited_run(self, hook, newer, prompts, chunked=True, extra=(), budget=None, patch=None):
        """A created page, then an --if-newer rewrite during which the stub's owner edits the page
        right after call `hook`. Returns (the page before, the target, the owner's text, the stub
        calls, the run's exit code and output)."""
        (self.tmp / "room.json").unlink(missing_ok=True)
        shutil.rmtree(self.tmp / "ws", ignore_errors=True)
        code, out, _ = self.publish(*extra)
        self.assertEqual((code, json.loads(out)["action"]), (0, "created"))
        db = json.loads((self.tmp / "room.json").read_text())
        mine, db["calls"] = db["pages"]["html-p1"], []
        (self.tmp / "room.json").write_text(json.dumps(db))
        target = tp.render_page("Blue", "codex", "dm", self.ME, {"dm": "Draft my update."}, "Founder",
                                tp.load_list(newer), load_media_of(extra))
        os.environ["STUB_EDIT_AFTER"] = hook
        with self.chunked() if chunked else contextlib.nullcontext(), \
                mock.patch.object(gs, "call_budget", **budget) if budget else contextlib.nullcontext(), \
                patch or contextlib.nullcontext():
            code, out, err = self.publish("--if-newer", "--list", str(newer), "--prompts", str(prompts),
                                          "--roles", "Founder", *extra)
        os.environ.pop("STUB_EDIT_AFTER")
        db = json.loads((self.tmp / "room.json").read_text())
        return mine, target, (db.get("edits") or [None])[-1], db["calls"], code, out, err

    def expect_edit(self, mine, target, theirs, calls, code, out, after, steps):
        """What a run must have done when the owner's edit landed right after write `after` (0:
        before the first write). A needle the edit broke is refused and nothing is written; a
        write that landed on a changed page is taken back when both texts fit a call on this host,
        else the run stops by name. Either way the owner's text is never cut down further."""
        got = json.loads(out)
        writes = [c[0] for c in calls if c[0] in ("replace", "append")]
        if after == 0:
            pre = mine
            stale = steps[0][1][0] in theirs if steps[0][0] == "replace" else True
            left = steps[0][2]  # what the first write leaves when it lands on the owner's page
            landed = theirs.replace(steps[0][1][0], steps[0][1][1], 1) if steps[0][0] == "replace" else theirs + steps[0][1][0]
            i = 0
        else:
            pre = steps[after - 2][2] if after > 1 else mine
            stale, left, landed, i = True, steps[after - 1][2], theirs, after - 1
            pre_calls = [s[0] for s in steps[:after]]
            self.assertEqual(writes[:after], pre_calls)
        if not stale:  # the needle itself was edited: Commons refuses, nothing recorded as written
            self.assertEqual((code, got["action"], got["edited"]), (0, "kept", True))
            self.assertEqual(self.page_text(), theirs)
            self.assertEqual(writes[after:], [])
            done, undo = i, None
        else:
            # The write landed on a changed page and is taken back in place: an appended piece
            # removed, the replaced page replaced back.
            cmd, args, _ = steps[i]
            needle, back = (args[0], "") if cmd == "append" else (args[1], args[0])
            undone = landed.replace(needle, back, 1) if needle in landed else None
            undo_writes = ["replace"] if undone is not None else []
            self.assertEqual((code, got["action"], got["edited"]), (0, "kept", True))
            self.assertEqual(writes[after:], ([cmd] if after == 0 else []) + undo_writes)  # the write, then its undo
            if undone is None:  # the edit broke the piece itself: nothing can be taken back
                self.assertEqual((self.page_text(), got["landed"]), (landed, gs.content_hash(landed)))
                done, undo = i, "failed"
            else:
                self.assertEqual(self.page_text(), undone)
                self.assertNotIn("landed", got)
                done, undo = i, "undone"
        self.assertEqual(self.entry()["interrupted"].get("undo"), undo)
        self.assertEqual(self.entry()["interrupted"]["page"], gs.content_hash(self.page_text()))
        entry = self.entry()
        self.assertEqual((entry["state"], entry["hash"], entry["interrupted"]["done"]),
                         ("complete", gs.content_hash(mine), done))
        self.assertNotEqual(entry["hash"], gs.content_hash(target))
        self.assertFalse(set(entry) & set(gs.JOURNAL_KEYS))
        page = self.page_text()
        code, out, _ = self.publish("--if-newer", "--list", str(self.newer))  # still someone's page
        self.assertEqual((code, json.loads(out)["action"], self.page_text()), (0, "kept", page))

    def test_an_edit_between_any_two_calls_stops_the_rewrite_before_its_next_write(self):
        """Above the one-call budget. read:1 is the read before the journal, read:2 the check before
        the first write; the owner's edit lands right after each. An edit inside the piece being
        replaced makes Commons refuse the needle; an appended tail is caught by the read-back after
        that one write, which is the window the piecewise path cannot close."""
        prompts = self.tmp / "p.json"
        prompts.write_text(json.dumps({"dm": "Draft my update."}))
        self.newer = self.write_list("tutorial.2026-11-01.1")
        for hook, mode in (("read:1", "append"), ("read:1", "inplace"), ("read:2", "inplace"), ("read:2", "append")):
            with self.subTest(hook=hook, mode=mode):
                mine, target, theirs, calls, code, out, err = self.edited_run(f"{hook}:{mode}", self.newer, prompts)
                steps = gs.rewrite_steps(mine, target, whole=False)
                if hook == "read:1":  # before the journal: the check read sees the edit, nothing is written
                    self.assertEqual((code, json.loads(out)["action"], self.page_text()), (0, "kept", theirs))
                    self.assertEqual([c[0] for c in calls if c[0] in ("replace", "append")], [])
                    self.assertEqual(self.entry()["interrupted"]["done"], 0)
                else:
                    self.expect_edit(mine, target, theirs, calls, code, out, 0, steps)

    def test_a_page_within_one_call_is_replaced_whole_or_not_at_all(self):
        """Within the one-call budget (every media-mapped page, and every page on a host whose
        ARG_MAX allows it): one replace whose needle is the whole page. An edit landing between the
        check read and the write makes Commons refuse the needle; the page is the owner's, whole."""
        prompts, media = self.tmp / "p.json", self.tmp / "all.json"
        prompts.write_text(json.dumps({"dm": "Draft my update."}))
        media.write_text(json.dumps(MXC_OF))  # every screenshot mapped: the page is text, under 40 KB
        newer = self.newer = self.write_list("tutorial.2026-11-01.1")
        for hook, mode in (("read:1", "append"), ("read:2", "append"), ("read:2", "inplace")):
            with self.subTest(hook=hook, mode=mode):
                mine, target, theirs, calls, code, out, err = self.edited_run(f"{hook}:{mode}", newer, prompts,
                                                                             chunked=False, extra=("--media", str(media)))
                self.assertTrue(gs.one_call(mine, target, os.environ))
                steps = gs.rewrite_steps(mine, target, whole=True)
                self.assertEqual([s[0] for s in steps], ["replace"])
                got = json.loads(out)
                self.assertEqual((code, got["action"], got["edited"]), (0, "kept", True), (hook, mode))
                self.assertEqual(self.page_text(), theirs)  # the owner's page, whole: never a fragment
                self.assertTrue(theirs.startswith(mine) if mode == "append" else theirs.endswith(mine[12:]))
                writes = [c for c in calls if c[0] in ("replace", "append")]
                # inplace: the whole-page needle is refused (and a refused call is not recorded);
                # append: the needle still matched, the write landed and was taken back.
                self.assertEqual([c[0] for c in writes],
                                 ["replace", "replace"] if (hook, mode) == ("read:2", "append") else [])
                if writes:
                    self.assertEqual(writes[0][2], len(mine.encode()))  # the needle was the whole page
                entry = self.entry()
                self.assertEqual((entry["state"], entry["hash"], entry["interrupted"]["done"]),
                                 ("complete", gs.content_hash(mine), 0))
        # No edit: the one replace lands and reads back as the target.
        mine, target, _, calls, code, out, _ = self.edited_run("read:9:append", newer, prompts, chunked=False,
                                                              extra=("--media", str(media)))
        self.assertEqual((code, json.loads(out)["action"]), (0, "updated"))
        self.assertEqual(self.page_text(), target)
        self.assertEqual([c[0] for c in calls if c[0] in ("replace", "append")], ["replace"])

    def test_an_edit_after_any_write_is_taken_back_or_stops_the_run_by_hash(self):
        prompts = self.tmp / "p.json"
        prompts.write_text(json.dumps({"dm": "Draft my update."}))
        self.newer = self.write_list("tutorial.2026-11-01.1")
        for hook, call in (("replace:1", 1), ("append:1", 2)):
            for mode in ("append", "inplace"):
                with self.subTest(hook=hook, mode=mode):
                    mine, target, theirs, calls, code, out, _ = self.edited_run(f"{hook}:{mode}", self.newer, prompts)
                    self.expect_edit(mine, target, theirs, calls, code, out, call, gs.rewrite_steps(mine, target, whole=False))

    def test_a_second_failure_mid_resume_on_a_three_piece_page_still_ends_at_the_target(self):
        data = good_list(revision="tutorial.2026-11-01.1")
        biggest = max(tp.ASSETS.glob("*.jpg"), key=lambda p: p.stat().st_size).name
        data["steps"].append({"id": "extra", "title": "Extra", "surface": "dm", "since_release": "2026-11-01",
                              "body": ["One more step for {name}.", "Ask {name}:"],
                              "screenshot": [{"asset": "extra-shot", "fallback_file": f"assets/{biggest}", "alt": "a"}]})
        three = self.tmp / "three.json"
        three.write_text(json.dumps(data))
        target = tp.render_page("Blue", "codex", "dm", self.ME, None, None, tp.load_list(three))
        self.assertEqual(len(gs.chunks(target)), 3)  # one replace of the old page, two appends
        code, out, _ = self.publish()
        self.assertEqual((code, json.loads(out)["action"]), (0, "created"))
        for at, done_after, left_pieces in (("append:1", 1, 1), ("append:2", 2, 2)):
            db = json.loads((self.tmp / "room.json").read_text())
            db["calls"] = []
            (self.tmp / "room.json").write_text(json.dumps(db))
            os.environ["STUB_FAIL_AT"] = at
            with self.chunked():
                code, out, _ = self.publish("--if-newer", "--list", str(three))
            os.environ.pop("STUB_FAIL_AT")
            self.assertEqual((code, json.loads(out)["ok"]), (4, False), at)
            journal = self.entry()
            self.assertEqual((journal["state"], journal["done"]), ("updating", done_after), at)
            self.assertEqual(len(gs.chunks(self.page_text())), left_pieces, at)
        code, out, _ = self.publish("--if-newer", "--list", str(three))
        got = json.loads(out)
        self.assertEqual((code, got["action"], got["resumed"]), (0, "resumed", True))
        self.assertEqual(self.page_text(), target)
        self.assertEqual((self.entry()["state"], self.entry()["hash"]), ("complete", gs.content_hash(target)))

    def cut_short(self, newer, prompts):
        """A created page, then an --if-newer rewrite to `newer` with prompts and roles that fails
        after the replace (the target's first piece). Returns the recorded target."""
        (self.tmp / "room.json").unlink(missing_ok=True)
        shutil.rmtree(self.tmp / "ws", ignore_errors=True)
        code, out, _ = self.publish()
        self.assertEqual((code, json.loads(out)["action"]), (0, "created"))
        db = json.loads((self.tmp / "room.json").read_text())
        db["calls"] = []  # STUB_FAIL_AT counts from here
        (self.tmp / "room.json").write_text(json.dumps(db))
        os.environ["STUB_FAIL_AT"] = "append:1"
        with self.chunked():
            code, _, _ = self.publish("--if-newer", "--list", str(newer), "--prompts", str(prompts), "--roles", "Founder")
        os.environ.pop("STUB_FAIL_AT")
        entry = gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]
        self.assertEqual((code, self.page_text()), (4, gs.chunks(entry["target"])[0]))
        self.assertEqual((entry["state"], gs.content_hash(entry["target"])), ("updating", entry["hash"]))
        return entry["target"]

    def test_a_retry_finishes_the_recorded_target_whatever_this_run_asks_for(self):
        prompts = self.tmp / "p.json"
        prompts.write_text(json.dumps({"dm": "Draft my update."}))
        newer = self.write_list("tutorial.2026-11-01.1")
        other = self.write_list("tutorial.2026-12-01.1", name="other.json")
        media = self.tmp / "media.json"
        media.write_text(json.dumps({"room-list-add": MXC_OF["room-list-add"]}))
        base = ["--name", "Blue", "--core", "codex", "--agent-mxid", self.ME]
        retries = {
            "plain, bundled list": base,
            "plain, same list": base + ["--list", str(newer)],
            "list changed": base + ["--list", str(other), "--if-newer"],
            "list unavailable": base + ["--list", str(self.tmp / "gone.json"), "--if-newer"],
            "name changed, summon asked": ["--name", "Kai", "--core", "codex", "--agent-mxid", self.ME, "--list",
                                           str(newer), "--if-newer", "--owner", "@other:example.test", "--summon"],
            "core changed": ["--name", "Blue", "--core", "claude", "--agent-mxid", self.ME, "--list", str(newer),
                             "--if-newer"],
            "audience changed": base + ["--audience", "room", "--list", str(newer), "--if-newer"],
            "agent changed": ["--name", "Blue", "--core", "codex", "--agent-mxid", "@other.agent:example.test",
                              "--list", str(newer), "--if-newer"],
            "new prompts and media": base + ["--list", str(newer), "--prompts", str(prompts), "--media", str(media)],
        }
        for case, argv in retries.items():
            with self.subTest(case=case):
                target = self.cut_short(newer, prompts)
                n = self.calls()
                code, out, err = self.main("publish", "--room", self.ROOM, "--room-commons", str(self.stub), *argv)
                got = json.loads(out)
                self.assertEqual((code, got["action"], got["resumed"], got["summoned"], got["name"], got["core"],
                                  got["agent"], got["revision"], got["media"], got["list"]),
                                 (0, "resumed", True, False, "Blue", "codex", self.ME, "tutorial.2026-11-01.1", [],
                                  "journal"))
                self.assertIn("were not applied", err)
                self.assertEqual(self.page_text(), target)
                self.assertNotIn("summon", [c[0] for c in self.writes_since(n)])
                entry = self.entry()
                self.assertEqual((entry["state"], entry["hash"], entry["prompts"], entry["roles"], entry["revision"],
                                  entry.get("media")),
                                 ("complete", gs.content_hash(target), {"dm": "Draft my update."}, "Founder",
                                  "tutorial.2026-11-01.1", None))
                self.assertFalse(set(entry) & set(gs.JOURNAL_KEYS))
                code, out, err = self.publish("--if-newer", "--list", str(newer))  # the target is current
                self.assertEqual((code, json.loads(out)["action"], err), (0, "unchanged", ""))
                self.assertEqual(self.page_text(), target)
        # A second deliberate run applies different inputs under the usual rules.
        argv = retries["name changed, summon asked"]
        code, out, _ = self.main("publish", "--room", self.ROOM, "--room-commons", str(self.stub), *argv)
        self.assertEqual((code, json.loads(out)["action"]), (0, "unchanged"))  # --if-newer: same revision
        code, out, _ = self.main("publish", "--room", self.ROOM, "--room-commons", str(self.stub),
                                 *[a for a in argv if a != "--if-newer"])
        self.assertEqual((code, json.loads(out)["action"], json.loads(out)["summoned"]), (0, "updated", True))
        self.assertIn("Getting started with Kai", self.page_text())

    def test_recovery_runs_before_this_runs_inputs_are_rendered(self):
        prompts = self.tmp / "p.json"
        prompts.write_text(json.dumps({"dm": "Draft my update."}))
        newer = self.write_list("tutorial.2026-11-01.1")
        heavy = self.tmp / "heavy.json"
        heavy.write_text(json.dumps(many_steps(tp.STEPS_MAX, shots=3)))  # valid, over the page cap once rendered
        target = self.cut_short(newer, prompts)
        code, out, _ = self.publish("--list", str(heavy))
        self.assertEqual((code, json.loads(out)["action"]), (0, "resumed"))
        self.assertEqual(self.page_text(), target)  # recovered before the oversized render could fail
        code, out, _ = self.publish("--list", str(heavy))
        self.assertEqual((code, json.loads(out)["ok"]), (4, False))
        self.assertIn(f"over the {PAGE_CAP}-byte cap", json.loads(out)["reason"])
        self.assertEqual(self.page_text(), target)

    def test_an_updating_entry_whose_target_does_not_hash_is_refused(self):
        prompts = self.tmp / "p.json"
        prompts.write_text(json.dumps({"dm": "Draft my update."}))
        newer = self.write_list("tutorial.2026-11-01.1")
        target = self.cut_short(newer, prompts)
        self.record(target=target.replace("Draft my update.", "Something else."))
        code, out, _ = self.publish("--if-newer", "--list", str(newer))
        self.assertEqual((code, json.loads(out)["ok"]), (4, False))
        self.assertIn("does not hash", json.loads(out)["reason"])
        self.assertEqual(self.page_text(), gs.chunks(target)[0])  # nothing written
        self.assertEqual(gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]["state"], "updating")

    def test_a_revision_only_bump_rewrites_once_then_is_a_no_op(self):
        self.publish()
        bumped = self.write_list("tutorial.2026-11-01.1")  # the same steps, a newer revision
        code, out, _ = self.publish("--if-newer", "--list", str(bumped))
        self.assertEqual((code, json.loads(out)["action"]), (0, "updated"))
        self.assertEqual(tp.read_marker(self.page_text()), (self.ME, "tutorial.2026-11-01.1"))
        self.assertEqual(gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]["revision"], "tutorial.2026-11-01.1")
        page = self.page_text()
        calls = len(json.loads((self.tmp / "room.json").read_text())["calls"])
        code, out, _ = self.publish("--if-newer", "--list", str(bumped))
        self.assertEqual((code, json.loads(out)["action"], self.page_text()), (0, "unchanged", page))
        writes = [c for c in json.loads((self.tmp / "room.json").read_text())["calls"][calls:] if c[0] != "read"]
        self.assertEqual(writes, [["pages", None]])
        # An entry whose revision lags the page it describes catches up without a write.
        self.record(revision="tutorial.2026-10-01.1")
        calls = len(json.loads((self.tmp / "room.json").read_text())["calls"])
        code, out, _ = self.publish("--if-newer", "--list", str(bumped))
        self.assertEqual((code, json.loads(out)["action"]), (0, "unchanged"))
        self.assertEqual(gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]["revision"], "tutorial.2026-11-01.1")
        writes = [c for c in json.loads((self.tmp / "room.json").read_text())["calls"][calls:] if c[0] not in ("read", "pages")]
        self.assertEqual(writes, [])

    def test_a_create_whose_complete_record_failed_never_replaces_an_edit_made_since(self):
        real = gs.save_marker

        def flaky(path, rooms):
            if rooms.get(self.ROOM, {}).get("state") == "complete" and not flaky.done:
                flaky.done = True
                raise OSError("disk full")
            real(path, rooms)
        flaky.done = False
        with mock.patch.object(gs, "save_marker", flaky):
            code, out, _ = self.publish()
        self.assertEqual((code, json.loads(out)["ok"]), (4, False))
        self.assertIn("stays recorded as pending", json.loads(out)["reason"])
        whole = self.page_text()
        self.assertEqual((self.entry()["state"], gs.content_hash(whole)), ("pending", self.entry()["hash"]))
        db = json.loads((self.tmp / "room.json").read_text())
        theirs = whole + "<p>OWNER NOTES</p>"
        db["pages"]["html-p1"] = theirs
        (self.tmp / "room.json").write_text(json.dumps(db))
        n = self.calls()
        code, out, err = self.publish()
        got = json.loads(out)
        self.assertEqual((code, got["action"], got["edited"], got.get("summoned")), (0, "kept", True, False))
        self.assertEqual(self.page_text(), theirs)
        self.assertEqual(self.writes_since(n), [])
        entry = self.entry()
        self.assertEqual((entry["state"], entry["foreign"], entry["hash"]), ("complete", True, gs.content_hash(theirs)))
        code, out, _ = self.publish("--name", "Kai")  # still not a page this script may replace
        self.assertEqual((code, json.loads(out)["action"], self.page_text()), (0, "kept", theirs))

    def test_a_page_found_after_marker_loss_is_adopted_only_as_its_digest_says(self):
        self.publish()
        page = self.page_text()
        self.assertTrue(tp.page_digest_ok(page))
        gs.marker_path(self.tmp / "ws").unlink()
        code, out, _ = self.publish()  # untouched: ours again, and rewrites go through the journal
        got = json.loads(out)
        self.assertEqual((code, got["action"], got.get("adopted"), got["summoned"]), (0, "unchanged", True, False))
        newer = self.write_list("tutorial.2026-11-01.1")
        code, out, _ = self.publish("--if-newer", "--list", str(newer))
        self.assertEqual((code, json.loads(out)["action"]), (0, "updated"))
        self.assertEqual(tp.read_marker(self.page_text()), (self.ME, "tutorial.2026-11-01.1"))
        # Edited after the marker was lost: adopted as found, never rewritten.
        db = json.loads((self.tmp / "room.json").read_text())
        theirs = db["pages"]["html-p1"].replace("</main>", "<p>OWNER NOTES</p></main>")
        db["pages"]["html-p1"] = theirs
        (self.tmp / "room.json").write_text(json.dumps(db))
        self.assertFalse(tp.page_digest_ok(theirs))
        gs.marker_path(self.tmp / "ws").unlink()
        n = self.calls()
        for extra in ((), ("--name", "Kai"), ("--if-newer", "--list", str(self.write_list("tutorial.2026-12-01.1")))):
            code, out, err = self.publish(*extra)
            got = json.loads(out)
            self.assertEqual((code, got["action"], got["edited"]), (0, "kept", True), extra)
            self.assertEqual(self.page_text(), theirs)
            self.assertEqual(self.writes_since(n), [])
        entry = self.entry()
        self.assertEqual((entry["foreign"], entry["hash"], entry["revision"]),
                         (True, gs.content_hash(theirs), "tutorial.2026-11-01.1"))

    def test_recovery_needs_nothing_of_this_runs_request(self):
        prompts = self.tmp / "p.json"
        prompts.write_text(json.dumps({"dm": "Draft my update."}))
        newer = self.write_list("tutorial.2026-11-01.1")
        # An invalid name: recovered first; the name is refused on the run that would use it.
        target = self.cut_short(newer, prompts)
        code, out, err = self.publish("--name", " ")
        self.assertEqual((code, json.loads(out)["action"], self.page_text()), (0, "resumed", target))
        code, out, err = self.publish("--name", " ")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("1-64 characters", err)
        # A writer-less FIFO as --prompts: never opened before recovery, refused (ignored) after.
        fifo = self.tmp / "prompts.fifo"
        os.mkfifo(fifo)
        target = self.cut_short(newer, prompts)
        done = []

        def run(*extra):
            done.append(self.publish(*extra))
        for expect in ("resumed", "unchanged"):
            worker = threading.Thread(target=run, args=("--prompts", str(fifo), "--if-newer", "--list", str(newer)),
                                      daemon=True)
            worker.start()
            worker.join(15)
            self.assertFalse(worker.is_alive(), "blocked on the FIFO")
            code, out, _ = done[-1]
            self.assertEqual((code, json.loads(out)["action"], self.page_text()), (0, expect, target))
        # A list URL: recovered before any fetch.
        target = self.cut_short(newer, prompts)
        with mock.patch.object(gs, "list_opener", side_effect=AssertionError("fetched")) as opener:
            code, out, _ = self.publish("--list", "https://api.example.test/list")
        self.assertEqual((code, json.loads(out)["action"], self.page_text()), (0, "resumed", target))
        opener.assert_not_called()

    SMALL = (131_071, 2_000_000)  # a call budget that holds no 214 KB text
    SPLIT = (250_000, 400_000)  # the page fits an argument, but not beside the whole new page

    def test_a_default_size_page_refreshes_under_linuxs_exec_limit(self):
        code, out, _ = self.publish()
        self.assertEqual((code, json.loads(out)["action"]), (0, "created"))
        mine = self.page_text()
        self.assertGreater(len(mine.encode()), 131_072)  # over Linux's cap on one exec argument
        n = self.calls()
        newer = self.write_list("tutorial.2026-11-01.1")
        code, out, err = self.publish("--if-newer", "--list", str(newer))  # setUp holds every exec to that cap
        self.assertEqual((code, json.loads(out)["action"], err), (0, "updated", ""))
        self.assertEqual(tp.read_marker(self.page_text()), (self.ME, "tutorial.2026-11-01.1"))
        writes = self.writes_since(n)
        self.assertEqual([w[0] for w in writes], ["replace"])
        self.assertEqual(writes[0][2], len(mine.encode()))  # one replace whose needle is the whole page

    def test_a_page_no_call_can_hold_is_refused_and_left_as_is(self):
        prompts = self.tmp / "p.json"
        prompts.write_text(json.dumps({"dm": "Draft my update."}))
        self.newer = self.write_list("tutorial.2026-11-01.1")
        for budget in (self.SMALL, (70_000, 140_000)):
            with self.subTest(budget=budget):
                mine, target, theirs, calls, code, out, err = self.edited_run(
                    "read:2:prepend", self.newer, prompts, chunked=False, budget={"return_value": budget})
                got = json.loads(out)
                self.assertEqual((code, got["ok"], theirs), (4, False, None))  # no second read: nothing ran
                self.assertIn("left as is, not rewritten in pieces", got["reason"])
                self.assertEqual(self.page_text(), mine)
                self.assertEqual([c for c in calls if c[0] in ("replace", "append")], [])
                entry = self.entry()
                self.assertEqual((entry["state"], entry["hash"]), ("complete", gs.content_hash(mine)))
                self.assertFalse(set(entry) & set(gs.JOURNAL_KEYS))

    def test_an_owner_prepend_after_the_check_or_a_read_back_stays_at_the_front(self):
        """A write that lands on a page the owner just prepended to is taken back where it landed:
        the owner's text stays first and the page is theirs, whole. read:2 is the check before the
        first write, read:3 the read-back after it."""
        prompts = self.tmp / "p.json"
        prompts.write_text(json.dumps({"dm": "Draft my update."}))
        self.newer = self.write_list("tutorial.2026-11-01.1")
        for budget in (self.SMALL, self.SPLIT):
            for hook in ("read:2", "read:3"):
                with self.subTest(budget=budget, hook=hook):
                    mine, target, theirs, calls, code, out, err = self.edited_run(
                        f"{hook}:prepend", self.newer, prompts, chunked=False, budget={"return_value": budget})
                    got = json.loads(out)
                    if budget == self.SMALL:  # refused before any write: no read for the owner to follow
                        self.assertEqual((code, got["ok"], theirs, self.page_text()), (4, False, None, mine))
                        continue
                    self.assertEqual((code, got["action"], got["edited"]), (0, "kept", True))
                    self.assertTrue(theirs.startswith("<p>OWNER FIRST</p>"))
                    self.assertEqual(self.page_text(), theirs)
                    self.assertNotIn("landed", got)
                    entry = self.entry()
                    self.assertEqual((entry["state"], entry["hash"], entry["interrupted"]["undo"],
                                      entry["interrupted"]["page"]),
                                     ("complete", gs.content_hash(mine), "undone", gs.content_hash(theirs)))

    def test_an_owner_prepend_while_a_cut_short_rewrite_resumes_stays_at_the_front(self):
        prompts = self.tmp / "p.json"
        prompts.write_text(json.dumps({"dm": "Draft my update."}))
        newer = self.write_list("tutorial.2026-11-01.1")
        for hook in ("read:1", "read:2"):
            with self.subTest(hook=hook):
                (self.tmp / "room.json").unlink(missing_ok=True)
                shutil.rmtree(self.tmp / "ws", ignore_errors=True)
                code, out, _ = self.publish()
                self.assertEqual((code, json.loads(out)["action"]), (0, "created"))
                mine = self.page_text()
                db = json.loads((self.tmp / "room.json").read_text())
                db["calls"] = []
                (self.tmp / "room.json").write_text(json.dumps(db))
                with mock.patch.object(gs, "call_budget", return_value=self.SPLIT):
                    os.environ["STUB_FAIL_AT"] = "write:2"  # cut short after its first write
                    code, out, _ = self.publish("--if-newer", "--list", str(newer), "--prompts", str(prompts))
                    os.environ.pop("STUB_FAIL_AT")
                    self.assertEqual((code, self.entry()["state"], self.entry()["done"]), (4, "updating", 1))
                    db = json.loads((self.tmp / "room.json").read_text())
                    db["calls"] = []
                    (self.tmp / "room.json").write_text(json.dumps(db))
                    os.environ["STUB_EDIT_AFTER"] = f"{hook}:prepend"
                    code, out, err = self.publish()
                    os.environ.pop("STUB_EDIT_AFTER")
                got = json.loads(out)
                theirs = json.loads((self.tmp / "room.json").read_text())["edits"][-1]
                self.assertEqual((code, got["action"], got["edited"]), (0, "kept", True))
                self.assertTrue(theirs.startswith("<p>OWNER FIRST</p>"))
                self.assertEqual(self.page_text(), theirs)
                self.assertNotIn("landed", got)
                entry = self.entry()
                self.assertEqual((entry["state"], entry["hash"]), ("complete", gs.content_hash(mine)))

    def test_a_replace_that_cannot_be_taken_back_is_reported_as_landed(self):
        prompts = self.tmp / "p.json"
        prompts.write_text(json.dumps({"dm": "Draft my update."}))
        self.newer = self.write_list("tutorial.2026-11-01.1")
        real = gs.fits_call
        # The undo's call (the shorter first piece back to the whole old page) does not fit this host.
        fits = mock.patch.object(gs, "fits_call", lambda a, b, env=None: real(a, b, env) and len(a) >= len(b))
        mine, target, theirs, calls, code, out, err = self.edited_run(
            "replace:1:append", self.newer, prompts, chunked=False, budget={"return_value": self.SPLIT}, patch=fits)
        got = json.loads(out)
        self.assertEqual((code, got["action"], got["edited"]), (0, "kept", True))
        self.assertEqual([c[0] for c in calls if c[0] in ("replace", "append")], ["replace"])  # no undo write
        self.assertEqual(self.page_text(), theirs)  # the page as the replace left it, note included
        self.assertEqual(got["landed"], gs.content_hash(theirs))
        self.assertIn("could not be taken back", err)
        self.assertIn(got["landed"], err)
        entry = self.entry()
        self.assertEqual((entry["state"], entry["hash"], entry["interrupted"]["undo"], entry["interrupted"]["page"]),
                         ("complete", gs.content_hash(mine), "unfit", gs.content_hash(theirs)))
        n = self.calls()
        code, out, _ = self.publish("--if-newer", "--list", str(self.newer))
        self.assertEqual((code, json.loads(out)["action"], self.page_text()), (0, "kept", theirs))
        self.assertEqual(self.writes_since(n), [])

    def legacy_page(self, **kw):
        """A page as a head before the digest wrote it: this head's render, the digest stripped."""
        page = tp.render_page(kw.pop("name", "Blue"), kw.pop("core", "codex"), "dm", kw.pop("agent", self.ME), **kw)
        self.assertIsNone(tp.page_digest_ok(tp.strip_digest(page)))
        return tp.strip_digest(page)

    def seed(self, page, by=None):
        db = {"pages": {"html-p1": page}, "calls": [], "next": 2, "by": {"html-p1": by or self.ME}}
        (self.tmp / "room.json").write_text(json.dumps(db))

    def test_a_pre_digest_page_with_our_tag_is_adopted_and_upgraded_once(self):
        self.seed(self.legacy_page())  # equal to this run's render, less the digest
        code, out, _ = self.publish()
        got = json.loads(out)
        self.assertEqual((code, got["action"], got.get("adopted"), got["summoned"]), (0, "updated", True, False))
        page = self.page_text()
        self.assertTrue(tp.page_digest_ok(page))
        self.assertEqual(page, tp.render_page("Blue", "codex", "dm", self.ME))
        entry = self.entry()
        self.assertEqual((entry["state"], entry["hash"], entry.get("foreign")), ("complete", gs.content_hash(page), None))
        code, out, _ = self.publish("--if-newer")
        self.assertEqual((code, json.loads(out)["action"]), (0, "unchanged"))
        self.assertEqual(json.loads(self.main("publish", "--room", self.ROOM, "--name", "Blue", "--core", "codex",
                                              "--room-commons", str(self.stub), "--agent-mxid", self.ME)[1])["kind"],
                         "html-p1")
        self.assertEqual(len([k for k in json.loads((self.tmp / "room.json").read_text())["pages"]]), 1)
        # Not equal to the render (another name then), but the only tutorial page and shaped as one
        # is written: adopted and upgraded, even under --if-newer at the same revision.
        gs.marker_path(self.tmp / "ws").unlink()
        self.seed(self.legacy_page(name="Old"))
        code, out, _ = self.publish("--if-newer")
        got = json.loads(out)
        self.assertEqual((code, got["action"], got.get("adopted")), (0, "updated", True))
        self.assertTrue(tp.page_digest_ok(self.page_text()))
        self.assertIn("Getting started with Blue", self.page_text())
        self.assertIsNone(self.entry().get("foreign"))
        code, out, _ = self.publish("--if-newer")
        self.assertEqual((code, json.loads(out)["action"]), (0, "unchanged"))

    def test_a_pre_digest_page_with_a_foreign_tag_or_an_edit_is_kept(self):
        other = "@other.agent:example.test"
        self.seed(self.legacy_page(agent=other), by=other)
        theirs = self.page_text()
        code, out, _ = self.publish()
        got = json.loads(out)
        self.assertEqual((code, got["action"], got["kind"]), (0, "created", "html-p2"))  # theirs is never touched
        db = json.loads((self.tmp / "room.json").read_text())
        self.assertEqual(db["pages"]["html-p1"], theirs)
        self.assertFalse([c for c in db["calls"] if c[0] in ("replace", "append") and c[1] == "html-p1"])
        # Our tag, visibly edited (text outside the page, a second tag): kept as found, foreign. An
        # edit inside the body of a pre-digest page is not told apart from an older render.
        shutil.rmtree(self.tmp / "ws", ignore_errors=True)
        for theirs in (self.legacy_page() + "<p>OWNER NOTES</p>",
                       "<!-- mine -->" + self.legacy_page(),
                       self.legacy_page().replace("<title>", tp.owner_marker(self.ME, "tutorial.2026-01-01.1") + "<title>")):
            with self.subTest(theirs=theirs[:30] + theirs[-30:]):
                self.seed(theirs)
                gs.marker_path(self.tmp / "ws").unlink(missing_ok=True)
                for extra in ((), ("--if-newer",), ("--name", "Kai")):
                    n = self.calls()
                    code, out, _ = self.publish(*extra)
                    got = json.loads(out)
                    self.assertEqual((code, got["action"], got["edited"]), (0, "kept", True), extra)
                    self.assertEqual(self.page_text(), theirs)
                    self.assertEqual(self.writes_since(n), [])
                self.assertEqual((self.entry()["foreign"], self.entry()["hash"]), (True, gs.content_hash(theirs)))
        # Two tutorial pages in the room: the shape rule alone does not adopt either.
        gs.marker_path(self.tmp / "ws").unlink()
        legacy = self.legacy_page(name="Old")
        db = {"pages": {"html-p1": legacy, "html-p2": legacy.replace("Old", "Older")}, "calls": [], "next": 3,
              "by": {"html-p1": self.ME, "html-p2": self.ME}}
        (self.tmp / "room.json").write_text(json.dumps(db))
        code, out, _ = self.publish()
        self.assertEqual((code, json.loads(out)["ok"]), (4, False))  # two marked pages: refused, as before

    def test_a_pending_page_is_finished_whatever_the_revision(self):
        self.door.fail = "after"
        code, out, _ = self.publish()
        self.assertEqual(code, 4)
        code, out, _ = self.publish("--if-newer")
        self.assertEqual((code, json.loads(out)["action"]), (0, "resumed"))
        self.assertEqual(self.page_text(), tp.render_page("Blue", "codex", "dm", self.ME))
        self.assertEqual(self.entry()["state"], "complete")
        self.assertNotIn("operation_id", self.entry())  # the create is over: nothing of its journal stays

    def test_publish_api_takes_the_list_and_the_rule(self):
        marker = gs.marker_path(self.tmp / "ws")
        commons = gs.Commons(self.stub, self.ROOM, actions=lambda: self.door)
        out = gs.publish(commons, marker, "Blue", core="codex", agent=self.ME)
        self.assertEqual(out["revision"], tp.FEATURES.revision)
        other = tp.FeatureList("tutorial.2026-11-01.1", tp.FEATURES.steps)
        out = gs.publish(commons, marker, "Blue", core="codex", agent=self.ME, features=other, if_newer=True)
        self.assertEqual((out["action"], out["revision"]), ("updated", "tutorial.2026-11-01.1"))
        out = gs.publish(commons, marker, "Kai", core="codex", agent=self.ME, features=other, if_newer=True)
        self.assertEqual(out["action"], "unchanged")


class DocTest(unittest.TestCase):
    SKILL = tp.LIST_PATH.parent / "SKILL.md"

    def test_skill_md_describes_the_list_workflow_the_revision_rule_and_the_visuals_follow_up(self):
        text = self.SKILL.read_text(encoding="utf-8")
        self.assertIn("feature-list.json", text)
        self.assertIn("--if-newer", text)
        self.assertIn("--list", text)
        self.assertIn("data-revision", text)
        self.assertIn("Visuals (follow-up)", text)
        self.assertIn("Commons asset references", text)
        self.assertIn("room.media.upload", text)
        self.assertIn("mxc://", text)
        self.assertIn("fallback_file", text)
        self.assertIn("--media", text)
        self.assertNotIn("reword them in `tour_page.py`", text)
        self.assertIn(str(ASSET_CAP // 1000) + " KB", text)
        self.assertIn(str(PAGE_CAP // 1000) + " KB", text)
        self.assertIn("`updating`", text)
        self.assertIn("refuses to run without one", text)
        canon = (HERE.parent / "docs" / "tutorial-delivery.md").read_text(encoding="utf-8")
        for word in ("--if-newer", "kept", "updating", "resumed", "journal", "read back", "digest", "0600 file",
                     "mxc://", "1 MiB", "--agent-mxid", "Last verified:"):
            self.assertIn(word, canon)


if __name__ == "__main__":
    unittest.main()
