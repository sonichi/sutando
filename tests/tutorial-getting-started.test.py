#!/usr/bin/env python3
"""skills/tutorial/scripts/getting_started.py: the page it renders, its one-page-per-room marker,
and the room-commons calls publish makes, against a stub room_commons.py that keeps a fake room."""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ, isolated_child_env

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import contextlib
import hashlib
import io
import html
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

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
    sized = cmd in ("append", "replace")
    db["calls"].append([cmd, kind] + ([len(rest[0].encode()), len(rest[1].encode()) if cmd == "replace" else 0]
                                      if sized else rest[:1]))
    def save():
        state.write_text(json.dumps(db))
    if cmd in os.environ.get("STUB_FAIL", "").split(","):
        save(); print("boom", file=sys.stderr); sys.exit(1)
    if cmd == "pages":
        listed = [{"id": None, "kind": "html", "title": "Main"}]
        listed += [{"id": k[5:], "kind": k, "title": db.get("titles", {}).get(k, "Getting started"),
                    "by": db.get("by", {}).get(k, ""), "copy_of": None} for k in db["pages"]]
        print(json.dumps(listed))
    elif cmd == "read":
        print(json.dumps({"chars": 0, "peers": [], "text": db["pages"][kind]}))
    elif cmd == "append":
        db["pages"][kind] += rest[0]
    elif cmd == "replace":
        cur = db["pages"][kind]
        assert rest[0] in cur, "old text absent"
        db["pages"][kind] = cur.replace(rest[0], rest[1], 1)
    elif cmd == "summon":
        pass
    else:
        sys.exit(9)
    save()
''')


TOUR = ["Your DM", "Rooms", "Commons", "Connect other agents", "Teamwork", "Calls & voice",
        "Connect your apps", "Activity & notices", "Mobile"]
FINALE = "You\u2019re set \u2014 ask Blue anything"
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


def steps(page):
    """The step headings, without the closing card's (which comes first in the markup)."""
    heads = parse(page).headings
    assert heads[0] == FINALE, heads[0]
    return heads[1:]


class RenderTest(unittest.TestCase):
    def test_tour_has_all_nine_sections_in_order(self):
        for core in gs.CORES:
            self.assertEqual(steps(gs.render_page("Blue", core))[:9], TOUR)
        self.assertLess(TOUR.index("Connect other agents"), TOUR.index("Teamwork"))

    def test_import_section_only_on_a_claude_core(self):
        claude = parse(gs.render_page("Blue", "claude"))
        self.assertEqual(steps(gs.render_page("Blue", "claude")), TOUR + [IMPORT])
        self.assertIn(gs.IMPORT_PROMPT, claude.buttons)
        for core in ("codex", "unknown"):
            page = parse(gs.render_page("Blue", core))
            self.assertEqual(steps(gs.render_page("Blue", core)), TOUR)
            self.assertNotIn(gs.IMPORT_PROMPT, page.buttons)
            self.assertNotIn("Claude Code", gs.render_page("Blue", core))

    def test_unknown_core_value_is_refused(self):
        with self.assertRaises(ValueError):
            gs.render_page("Blue", "gemini")

    def test_copy_buttons_and_kept_connect_prompt(self):
        page = gs.render_page("Blue", "codex")
        self.assertEqual([b for b in parse(page).buttons if b],
                         [gs.tour_page.FINALE_PROMPT] + [s.prompt for s in gs.SECTIONS if s.prompt])
        self.assertIn(gs.CONNECT_PROMPT, parse(page).buttons)
        self.assertIn("Settings &rarr; Integrations", page)
        self.assertIn("navigator.clipboard", page)
        self.assertIn("prefers-color-scheme: dark", page)

    def test_dm_step_comes_first_and_says_personal_answers_stay_in_the_dm(self):
        dm = gs.SECTIONS[0]
        self.assertEqual((dm.id, dm.title, dm.where), ("dm", "Your DM", "dm"))
        self.assertIn("never in a room", dm.what)
        self.assertEqual(dm.prompt, "What can you do for me today?")

    def test_every_prompt_says_where_to_paste_it(self):
        where = {s.id: s.where for s in gs.sections_for("claude") if s.prompt}
        self.assertEqual(where, {"dm": "dm", "commons": "room", "teamwork": "room", "apps": "dm", "import": "dm"})
        page = gs.render_page("Blue", "claude")
        self.assertEqual(page.count('<p class="paste">Paste in: a room</p>'), 2)
        self.assertEqual(page.count('<p class="paste">Paste in: your DM</p>'), 4)  # dm, apps, import, finale

    def test_closing_card_is_hidden_until_everything_is_done(self):
        page = gs.render_page("Blue", "claude")
        self.assertIn('<section class="card finale" id="finale" aria-labelledby="h-finale" hidden', page)
        self.assertIn("finale.hidden = n !== ids.length", gs.tour_page.SCRIPT)
        self.assertIn(html.escape(gs.tour_page.FINALE_PROMPT), page)

    def test_step_title_replaces_step_numbers(self):
        page = gs.render_page("Blue", "codex")
        self.assertNotRegex(page, r"Step \d+ of \d+")
        self.assertIn('<p class="step-title"><span id="step-title">Your DM</span>'
                      '<span class="pos" id="step-pos" aria-hidden="true">1/9</span></p>', page)
        self.assertNotRegex(page, r'id="step-title"[^>]*aria-live')  # announced once, by heading focus
        self.assertIn("stepPos.textContent = (cur + 1) + '/' + ids.length", gs.tour_page.SCRIPT)
        self.assertIn("stepTitle.textContent = titles[cur]", gs.tour_page.SCRIPT)

    def test_reviewed_wording(self):
        by = {s.id: s for s in gs.sections_for("claude")}
        self.assertIn("Dot (by OpenAI, in ChatGPT), Grok Bot (in Grok) or Muse (by Meta)", by["agents"].how)
        self.assertIn("Mention &amp; Keywords", by["activity"].how)
        self.assertIn("Activity tab", by["activity"].how)
        self.assertTrue(by["import"].what.startswith("If you use Claude Code"))
        self.assertIn("local Claude Code history", by["import"].what)
        self.assertIn("headphone icon in the room header", by["calls"].how)
        self.assertIn("Bring an agent?", by["calls"].how)
        self.assertIn("+ at the top of the room list", by["rooms"].how)

    def test_second_review_wording(self):
        by = {s.id: s for s in gs.sections_for("claude")}
        self.assertIn("Docs, HTML pages, PDFs, whiteboards, sheets and databases", by["commons"].what)
        self.assertIn("start the call and pick your agent under &ldquo;Bring an agent?&rdquo;", by["calls"].how)
        self.assertIn("open the room menu (&#8942;), choose Notifications, and pick Mention &amp; Keywords",
                      by["activity"].how)
        self.assertIn(("settings-integrations.jpg", "Settings \u2192 Apps \u2192 Integrations"), by["apps"].images)

    def test_room_audience_says_your_agent(self):
        room = gs.render_page("Blue", "claude", "room")
        self.assertIn("<h1>Getting started with your agent</h1>", room)
        self.assertIn("ask your agent anything", room)
        self.assertNotIn("Blue", re.sub(r'src="data:[^"]*"', "", room))
        dm = gs.render_page("Blue", "claude", "dm")
        self.assertIn("<h1>Getting started with Blue</h1>", dm)
        self.assertEqual(gs.render_page("Blue", "claude"), dm)
        with self.assertRaises(ValueError):
            gs.render_page("Blue", "claude", "everyone")

    def test_no_microphone_or_user_card_voice_button(self):
        for core in gs.CORES:
            text = re.sub(r"data:image/[^\"]+", "", gs.render_page("Blue", core)).lower()
            self.assertNotRegex(text, r"micro|\bmic\b|push to talk")
        self.assertNotIn("02-microphone", " ".join(f for s in gs.SECTIONS for f, _ in s.images))

    def test_codex_and_claude_code_are_not_offered_as_addable_agents(self):
        agents = [s for s in gs.SECTIONS if s.title == "Connect other agents"][0]
        self.assertNotIn("Codex", agents.what + agents.how)
        self.assertNotIn("Claude", agents.what + agents.how)

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


def _inside_try(script: str, at: int) -> bool:
    """Whether position `at` lies inside the block of the nearest preceding `try {`."""
    start = script.rfind("try {", 0, at)
    if start < 0:
        return False
    depth = 0
    for ch in script[start + len("try "):at]:
        depth += (ch == "{") - (ch == "}")
    return depth > 0


class InteractiveTest(unittest.TestCase):
    def setUp(self):
        self.page = gs.render_page("Blue", "claude")

    def test_tour_controls_and_progress(self):
        for marker in ('id="back"', 'id="next"', 'id="all"', 'id="dots"', 'aria-pressed="false"',
                       'role="progressbar"', 'aria-valuemax="10"', "0 of 10 done", 'aria-label="Tour steps"'):
            self.assertIn(marker, self.page)
        self.assertEqual(self.page.count('type="checkbox" data-done='), 10)
        self.assertEqual(self.page.count("Mark done"), 10)
        self.assertIn("ArrowRight", self.page)

    def test_js_class_is_set_in_head_and_no_js_shows_every_section(self):
        head, body = self.page.split("</head>", 1)
        self.assertIn("document.documentElement.classList.add('js')", head)
        self.assertLess(head.index("classList.add('js')"), head.index("<style>"))
        self.assertIn('main id="tour" data-mode="step"', body)
        self.assertIn('.js main[data-mode="step"] .sec { display:none; }', head)
        self.assertNotRegex(head, r"(?m)^main\[data-mode")  # step hiding only under .js
        self.assertIn(".controls { display:none; }", head)
        self.assertEqual(body.count('class="sec card cur"'), 1)

    def test_progress_and_mark_done_are_hidden_without_js(self):
        head = self.page.split("</head>", 1)[0]
        self.assertIn(".progress, .done { display:none; }", head)
        self.assertIn(".js .progress { display:block; }", head)
        self.assertIn(".js .done { display:inline-flex;", head)

    def test_show_all_pressed_style_and_awaited_set(self):
        self.assertIn('#all[aria-pressed="true"] {', self.page)
        self.assertIn("await art.state.set(store.key, v)", gs.tour_page.SCRIPT)

    def test_checkboxes_start_disabled_until_progress_loads(self):
        self.assertEqual(self.page.count('data-done="'), 10)
        self.assertEqual(len(re.findall(r'type="checkbox" data-done="[a-z]+" disabled>', self.page)), 10)
        self.assertIn("b.disabled = false", gs.tour_page.SCRIPT)

    def test_show_all_toggle_keeps_one_label(self):
        self.assertIn('<button type="button" id="all" aria-pressed="false">Show all</button>', self.page)
        self.assertNotIn("Step by step", self.page)
        self.assertNotIn("all.textContent", gs.tour_page.SCRIPT)

    def test_status_region_announces_copy_and_copy_has_no_static_label(self):
        self.assertIn('role="status" aria-live="polite"', self.page)
        self.assertIn("status.textContent", gs.tour_page.COPY_SCRIPT)
        self.assertNotRegex(self.page, r'class="copy"[^>]*aria-label=')
        self.assertIn('aria-describedby="p-commons"', self.page)

    def test_copy_fallback_names_the_platform_keys(self):
        script = gs.tour_page.COPY_SCRIPT
        for text in ("Press \\u2318C", "Press Ctrl+C", "Press Ctrl/\\u2318+C", "userAgentData"):
            self.assertIn(text, script)

    def test_no_svg_pairs_role_img_with_aria_hidden(self):
        for tag in re.findall(r"<svg\b[^>]*>", self.page):
            self.assertFalse('role="img"' in tag and 'aria-hidden="true"' in tag, tag)
        self.assertNotIn('role="img"', self.page)

    def test_an_svg_per_section_and_a_hero(self):
        self.assertEqual(self.page.count('<div class="icon" aria-hidden="true"><svg'), 10)
        self.assertIn('class="hero-art"', self.page)
        self.assertEqual(set(gs.tour_page.ICONS), {s.id for s in gs.sections_for("claude")})

    def test_no_external_urls(self):
        for core in gs.CORES:
            page = gs.render_page("Blue", core)
            bare = re.sub(r'src="data:image/jpeg;base64,[A-Za-z0-9+/=]+"', 'src=""', page)
            self.assertNotRegex(bare, r"(?i)\b(?:https?:)?//[a-z0-9]")
            self.assertNotRegex(bare, r"(?i)<(?:link|iframe|object|embed)\b|url\(")
            self.assertEqual(re.findall(r'\bsrc="([^"]*)"', bare), [""] * bare.count("<img "))

    def test_dark_mode_and_reduced_motion(self):
        self.assertIn("prefers-color-scheme: dark", self.page)
        self.assertIn(':root:not([data-theme="light"]) {', self.page)  # an explicit light theme wins
        self.assertIn(':root[data-theme="dark"] { color-scheme: dark;', self.page)
        self.assertIn("document.body.getAttribute('data-theme')", gs.tour_page.SCRIPT)
        self.assertIn("prefers-reduced-motion: reduce", self.page)
        self.assertIn("@media (max-width: 520px)", self.page)

    def test_every_storage_access_is_guarded(self):
        script = gs.tour_page.SCRIPT
        pattern = r"localStorage|local\.(?:getItem|setItem)|art\.state\.|window\.artifact"
        hits = [m.start() for m in re.finditer(pattern, script)]
        self.assertGreaterEqual(len(hits), 6)
        for at in hits:
            self.assertTrue(_inside_try(script, at), script[max(0, at - 60):at + 30])

    def test_progress_uses_the_page_runtime_keyed_per_viewer(self):
        script = gs.tour_page.SCRIPT
        self.assertIn("'gs-done-' + id", script)
        self.assertIn("art.state.set(store.key, v)", script)
        self.assertIn("art.state.on(store.key", script)
        self.assertNotIn("art.state.set('gs-done'", script)
        self.assertIn("typeof v.then === 'function'", script)

    def test_accent_palette_meets_aa_on_cards_in_both_themes(self):
        tp = gs.tour_page
        for sid, (light, dark) in tp.ACCENTS.items():
            self.assertGreaterEqual(tp.contrast(light, tp.CARD[0]), 4.5, sid)
            self.assertGreaterEqual(tp.contrast(dark, tp.CARD[1]), 4.5, sid)

    def test_page_scripts_parse(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        bodies = re.findall(r"<script>(.*?)</script>", self.page, re.S)
        self.assertEqual(len(bodies), 3)
        for body in bodies:
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
                fh.write(body)
            done = subprocess.run([node, "--check", fh.name], capture_output=True, text=True)
            os.unlink(fh.name)
            self.assertEqual(done.returncode, 0, done.stderr)


JSDOM_HARNESS = r"""
const { JSDOM } = require('jsdom');
const html = require('fs').readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = { errors: [], sets: [] };
const dom = new JSDOM(html, { url: 'https://page.test/', runScripts: 'dangerously', pretendToBeVisual: true,
  beforeParse(w) {
    w.addEventListener('error', (e) => out.errors.push(String(e.message)));
    w.Element.prototype.scrollIntoView = function (o) { out.scrolled = [this.id, o && o.behavior]; };
    w.matchMedia = () => ({ matches: false });
    const runtime = (me, get) => ({ ready: Promise.resolve(), me,
      state: { get, set: (k, v) => out.sets.push([k, v]), on: () => {} } });
    if (scenario === 'me') w.artifact = runtime({ id: 'u42' }, () => Promise.resolve(['rooms']));
    if (scenario === 'nome') w.artifact = runtime(null, () => ['rooms']);
    if (scenario === 'slow') w.artifact = { ready: new Promise(() => {}), me: null, state: { get: () => undefined, set: () => {}, on: () => {} } };
    if (scenario === 'advance') w.artifact = runtime({ id: 'u7' }, () => []);
    if (scenario === 'finale') w.artifact = runtime({ id: 'u8' }, () => ['dm', 'rooms', 'commons', 'agents', 'teamwork', 'calls', 'apps', 'activity', 'mobile']);
    if (scenario === 'copy-ok') Object.defineProperty(w.navigator, 'clipboard', { value: { writeText: () => Promise.resolve() } });
    if (scenario === 'copy-fail') {
      Object.defineProperty(w.navigator, 'clipboard', { value: { writeText: () => Promise.reject(new Error('denied')) } });
      Object.defineProperty(w.navigator, 'platform', { value: 'Win32' });
      w.document.execCommand = () => false;
    }
  } });
const w = dom.window, d = w.document;
const mark = (i) => { const b = d.querySelectorAll('input[data-done]')[i]; b.checked = true; b.dispatchEvent(new w.Event('change', { bubbles: true })); };
(async () => {
  out.jsClass = d.documentElement.classList.contains('js');
  out.disabledEarly = Array.from(d.querySelectorAll('input[data-done]')).every((b) => b.disabled);
  await sleep(scenario === 'slow' ? 1800 : 60);
  out.enabled = Array.from(d.querySelectorAll('input[data-done]')).every((b) => !b.disabled);
  out.progress = d.getElementById('progress-label').textContent;
  if (scenario === 'me' || scenario === 'nome' || scenario === 'slow') {
    mark(2);
    out.local = w.localStorage.getItem('gs-done');
  }
  if (scenario === 'advance') {
    out.before = d.querySelector('.sec.cur').id;
    mark(0);
    out.after = d.querySelector('.sec.cur').id;
    out.focused = d.activeElement && d.activeElement.id;
    out.title = d.getElementById('step-title').textContent;
    d.getElementById('all').click();
    mark(3);
    out.afterAll = d.querySelector('.sec.cur').id;
  }
  if (scenario === 'finale') {
    out.finaleHiddenBefore = d.getElementById('finale').hidden;
    mark(9);
    out.finaleHiddenAfter = d.getElementById('finale').hidden;
    out.focused = d.activeElement && d.activeElement.id;
  }
  if (scenario === 'showall') {
    const all = d.getElementById('all');
    out.labelBefore = all.textContent;
    all.click();
    out.pressed = all.getAttribute('aria-pressed');
    out.labelAfter = all.textContent;
    d.querySelectorAll('.dot')[4].click();
    out.focused = d.activeElement && d.activeElement.id;
    out.mode = d.getElementById('tour').dataset.mode;
  }
  if (scenario === 'keys') {
    const key = (target) => target.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    key(d.body);
    out.afterBody = d.querySelector('.sec.cur').id;
    key(d.getElementById('next'));
    out.afterButton = d.querySelector('.sec.cur').id;
    key(d.querySelector('.sec.cur h2'));
    out.afterHeading = d.querySelector('.sec.cur').id;
  }
  if (scenario.startsWith('copy')) {
    d.querySelector('button[data-prompt]').click();
    await sleep(100);
    out.first = d.getElementById('status').textContent;
    d.querySelector('button[data-prompt]').click();
    await sleep(10);
    out.between = d.getElementById('status').textContent;
    await sleep(100);
    out.button = d.querySelector('button[data-prompt]').textContent;
    out.status = d.getElementById('status').textContent;
  }
  out.progressAfter = d.getElementById('progress-label').textContent;
  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""


def _jsdom_node():
    """(node, env) when node can require jsdom (NODE_PATH may point at a node_modules), else None."""
    node = shutil.which("node")
    if not node:
        return None
    probe = subprocess.run([node, "-e", "require('jsdom')"], capture_output=True, text=True)
    return (node, isolated_child_env()) if probe.returncode == 0 else None


class PageBehaviourTest(unittest.TestCase):
    """The page's script in jsdom; skipped where node cannot load jsdom (set NODE_PATH to run)."""

    @classmethod
    def setUpClass(cls):
        cls.node = _jsdom_node()
        if cls.node is None:
            raise unittest.SkipTest("node with jsdom not available")
        cls.tmp = Path(tempfile.mkdtemp(prefix="tutorial-jsdom-"))
        cls.page = cls.tmp / "page.html"
        cls.page.write_text(gs.render_page("Blue", "claude"))
        cls.harness = cls.tmp / "harness.cjs"
        cls.harness.write_text(JSDOM_HARNESS)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_page(self, scenario):
        node, env = self.node
        done = subprocess.run([node, str(self.harness), str(self.page), scenario],
                              capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout)
        self.assertEqual(out["errors"], [])
        return out

    def test_js_class_is_set_before_the_body_and_boxes_wait_for_load(self):
        out = self.run_page("slow")
        self.assertTrue(out["jsClass"])
        self.assertTrue(out["disabledEarly"])
        self.assertTrue(out["enabled"])

    def test_viewer_key_is_used_and_a_thenable_get_is_awaited(self):
        out = self.run_page("me")
        self.assertEqual(out["progress"], "1 of 10 done")
        self.assertEqual(out["sets"], [["gs-done-u42", ["rooms", "commons"]]])
        self.assertIsNone(out["local"])

    def test_without_a_viewer_id_the_shared_state_is_never_written(self):
        for scenario in ("nome", "slow"):
            out = self.run_page(scenario)
            self.assertEqual(out["sets"], [], scenario)
            self.assertEqual(json.loads(out["local"]), ["commons"], scenario)

    def test_show_all_keeps_one_label_and_dots_scroll_and_focus(self):
        out = self.run_page("showall")
        self.assertEqual((out["labelBefore"], out["labelAfter"], out["pressed"]), ("Show all", "Show all", "true"))
        self.assertEqual(out["mode"], "all")
        self.assertEqual(out["scrolled"], ["s-teamwork", "smooth"])
        self.assertEqual(out["focused"], "h-teamwork")

    def test_mark_done_advances_in_step_mode_only(self):
        out = self.run_page("advance")
        self.assertEqual((out["before"], out["after"], out["focused"], out["title"]),
                         ("s-dm", "s-rooms", "h-rooms", "Rooms"))
        self.assertEqual(out["afterAll"], "s-rooms")

    def test_closing_card_appears_when_all_are_done(self):
        out = self.run_page("finale")
        self.assertEqual((out["finaleHiddenBefore"], out["finaleHiddenAfter"], out["focused"]),
                         (True, False, "h-finale"))
        self.assertEqual(out["progressAfter"], "10 of 10 done")

    def test_arrow_keys_only_inside_the_tour_and_not_on_controls(self):
        out = self.run_page("keys")
        self.assertEqual((out["afterBody"], out["afterButton"], out["afterHeading"]), ("s-dm", "s-dm", "s-rooms"))

    def test_copy_outcomes_are_announced(self):
        ok = self.run_page("copy-ok")
        self.assertEqual(ok["between"], "")  # cleared first, so a repeat copy is announced again
        self.assertEqual(ok["first"], ok["status"])
        self.assertEqual(ok["button"], "Copied")
        self.assertEqual(ok["status"], "Copied: " + gs.tour_page.FINALE_PROMPT)
        fail = self.run_page("copy-fail")
        self.assertEqual(fail["button"], "Press Ctrl+C")
        self.assertIn("Press Ctrl+C", fail["status"])


class NameTest(unittest.TestCase):
    MXID = "@chi-air-blue.agent:example.test"

    def fetcher(self, profile=None, well_known=None):
        seen = []

        def fetch(url):
            seen.append(url)
            if url.endswith("/.well-known/matrix/client"):
                if well_known is None:
                    raise OSError("no well-known")
                return json.dumps({"m.homeserver": {"base_url": well_known}}).encode()
            if profile is None:
                raise OSError("403")
            return json.dumps(profile).encode()
        return fetch, seen

    def test_explicit_name_is_kept(self):
        self.assertEqual(gs.resolve_name("Blue", self.MXID, fetch=self.fetcher()[0]), "Blue")

    def test_auto_uses_the_profile_displayname_from_the_well_known_homeserver(self):
        fetch, seen = self.fetcher({"displayname": "  Echo  Blue "}, "https://chat.example.test/")
        self.assertEqual(gs.resolve_name("auto", self.MXID, fetch), "Echo Blue")
        self.assertEqual(seen[-1], "https://chat.example.test/_matrix/client/v3/profile/"
                                   "%40chi-air-blue.agent%3Aexample.test/displayname")

    def test_auto_falls_back_to_the_localpart_then_sutando(self):
        self.assertEqual(gs.resolve_name("auto", self.MXID, self.fetcher()[0]), "Chi Air Blue")
        self.assertEqual(gs.resolve_name("auto", self.MXID, self.fetcher({"displayname": None})[0]),
                         "Chi Air Blue")
        self.assertEqual(gs.resolve_name("auto", "", self.fetcher()[0]), "Sutando")
        self.assertEqual(gs.resolve_name("auto", "not-an-mxid", self.fetcher()[0]), "Sutando")

    def test_a_non_https_homeserver_is_not_followed(self):
        fetch, seen = self.fetcher({"displayname": "X"}, "http://plain.example.test")
        gs.resolve_name("auto", self.MXID, fetch)
        self.assertTrue(seen[-1].startswith("https://example.test/"))


class CredentialTest(unittest.TestCase):
    """room_commons.py's credentials come from the channel env file, only when the env has none."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tutorial-cred-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = self.tmp / "claude-config"
        env = self.config / "channels" / "ag2space" / ".env"
        env.parent.mkdir(parents=True)
        env.write_text("AG2_REMOTE_TOKEN=https://relay.test.invalid/relay|not-a-real-secret\n"
                       "AGENT_MXID=@fixture.agent:example.test\nOTHER=x\n")
        env.chmod(0o600)
        self.script = self.tmp / "room_commons.py"
        self.script.write_text('TOKEN_VARS = ("ROOM_COMMONS_TOKEN", "AG2_REMOTE_TOKEN")\n'
                               'URL_VARS = ("AG2_API_ROOT", "REMOTE_TASK_URL")\n')
        self.base = {"CLAUDE_CONFIG_DIR": str(self.config), "PATH": os.environ.get("PATH", "")}

    def test_capability_names_come_from_the_script(self):
        self.assertEqual(gs.capability_names(self.script),
                         ("AG2_API_ROOT", "REMOTE_TASK_URL", "ROOM_COMMONS_TOKEN", "AG2_REMOTE_TOKEN"))
        self.assertEqual(gs.capability_names(self.tmp / "missing.py"), ())

    def test_env_file_fills_the_credentials(self):
        with mock.patch.dict(os.environ, self.base, clear=True):
            env = gs.commons_env(self.script)
            mxid = gs.agent_mxid()
        self.assertEqual(mxid, "@fixture.agent:example.test")
        self.assertTrue(env["AG2_REMOTE_TOKEN"].endswith("|not-a-real-secret"))
        self.assertNotIn("OTHER", env)

    def test_an_env_credential_wins_and_nothing_is_mixed_in(self):
        with mock.patch.dict(os.environ, {**self.base, "AG2_API_ROOT": "https://other.test.invalid"},
                             clear=True):
            env = gs.commons_env(self.script)
        self.assertNotIn("AG2_REMOTE_TOKEN", env)

    def test_the_token_is_never_printed(self):
        stub = self.tmp / "rc_fail.py"
        stub.write_text('import os, sys\nTOKEN_VARS = ("AG2_REMOTE_TOKEN",)\nURL_VARS = ()\n'
                        'if __name__ == "__main__":\n'
                        '    print("boom", os.environ.get("AG2_REMOTE_TOKEN"), file=sys.stderr)\n'
                        '    sys.exit(1)\n')  # a child that leaks its credential into its error
        env = {**self.base, "HOME": str(self.tmp)}
        done = subprocess.run([sys.executable, str(SCRIPT), "publish", "--room", "!r:x", "--name", "Blue",
                               "--core", "codex", "--room-commons", str(stub)],
                              capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 4, done.stdout + done.stderr)
        self.assertIn("boom [redacted]", done.stdout)
        self.assertNotIn("not-a-real-secret", done.stdout + done.stderr)
        self.assertNotIn("relay.test.invalid/relay|", done.stdout + done.stderr)

    def test_redact_hides_credential_values_and_token_halves(self):
        env = {"AG2_REMOTE_TOKEN": "https://relay.test.invalid/r|s3cr3t-value", "ROOM_KEY": "short",
               "PATH": "/usr/bin:/bin"}
        said = gs.redact("token s3cr3t-value and https://relay.test.invalid/r|s3cr3t-value in /usr/bin", env)
        self.assertEqual(said, "token [redacted] and [redacted] in /usr/bin")
        self.assertEqual(gs.redact("nothing", None), "nothing")


class FinderTest(unittest.TestCase):
    def test_installed_skill_is_found_when_none_is_beside_this_one(self):
        tmp = Path(tempfile.mkdtemp(prefix="tutorial-find-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        installed = tmp / "skills" / "room-commons" / "scripts" / "room_commons.py"
        installed.parent.mkdir(parents=True)
        installed.write_text("")
        beside = gs.REPO / "skills" / "room-commons" / "scripts" / "room_commons.py"
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(tmp)}):
            found = gs.find_room_commons()
        self.assertEqual(found, beside if beside.is_file() else installed)


class DocTest(unittest.TestCase):
    def test_skill_md_states_the_real_chunk_size(self):
        text = (REPO / "skills" / "tutorial" / "SKILL.md").read_text()
        self.assertIn(f"{gs.CHUNK_BYTES:,} bytes", text)
        self.assertIn("131,072", text)
        self.assertLess(gs.CHUNK_BYTES, 131_072)
        self.assertNotRegex(text, r"at most (?!" + re.escape(f"{gs.CHUNK_BYTES:,}") + r")[\d,]+ bytes")

    def test_skill_md_documents_the_room_audience(self):
        self.assertIn("--audience room", (REPO / "skills" / "tutorial" / "SKILL.md").read_text())


class ChunkTest(unittest.TestCase):
    def test_chunks_respect_the_byte_limit_and_characters(self):
        text = "a" * 10 + "\u00e9" * 10 + "\U0001f600" * 5
        parts = gs.chunks(text, 7)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(p.encode("utf-8")) <= 7 for p in parts))
        self.assertEqual(gs.chunks(""), [])
        self.assertLess(gs.CHUNK_BYTES, 131_072)  # Linux MAX_ARG_STRLEN, below Commons' 196,608


class AssetTest(unittest.TestCase):
    def test_no_two_steps_share_an_image(self):
        tp = gs.tour_page
        seen = {}
        for s in tp.sections_for("claude"):
            for f, _ in s.images:
                digest = hashlib.sha256((tp.ASSETS / f).read_bytes()).hexdigest()
                self.assertNotIn(digest, seen, f"{s.id} reuses {seen.get(digest)}'s image {f}")
                seen[digest] = s.id


    def test_every_step_image_exists_and_is_small(self):
        tp = gs.tour_page
        used = {f for s in tp.sections_for("claude") for f, _ in s.images}
        files = {p.name: p.stat().st_size for p in tp.ASSETS.glob("*.jpg")}
        self.assertEqual(used, set(files))
        self.assertTrue(all(size <= 60_000 for size in files.values()), files)
        self.assertLessEqual(sum(files.values()), 500_000)
        for s in tp.sections_for("claude"):
            for f, alt in s.images:
                self.assertTrue(alt.strip(), f)

    def test_each_step_with_an_image_renders_it_inline(self):
        page = gs.render_page("Blue", "claude")
        for s in gs.tour_page.sections_for("claude"):
            block = page[page.index(f'id="s-{s.id}"'):]
            block = block[:block.index("</section>")]
            self.assertEqual(block.count('<img src="data:image/jpeg;base64,'), len(s.images), s.id)
        for sid in ("rooms", "commons", "teamwork", "calls", "apps", "activity", "mobile"):
            self.assertTrue([s for s in gs.SECTIONS if s.id == sid][0].images, sid)

    def test_rendered_page_size_is_bounded(self):
        self.assertLess(len(gs.render_page("Blue", "claude").encode()), 400_000)


class EdgeTest(unittest.TestCase):
    """Branches the end-to-end tests do not reach; no network, no live workspace."""

    def test_https_get_sends_a_user_agent_and_caps_the_read(self):
        seen = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n):
                seen["n"] = n
                return b'{"displayname": "X"}'

        def urlopen(req, timeout):
            seen["ua"], seen["timeout"] = req.get_header("User-agent"), timeout
            return Resp()

        with mock.patch.object(gs.urllib.request, "urlopen", urlopen):
            self.assertEqual(gs._https_get("https://example.test/x"), b'{"displayname": "X"}')
        self.assertEqual(seen, {"ua": gs.USER_AGENT, "timeout": gs.PROFILE_TIMEOUT, "n": 65536})

    def test_marker_whose_rooms_is_not_a_map_reads_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json"
            path.write_text(json.dumps({"rooms": ["!r:x"]}))
            self.assertEqual(gs.load_marker(path), {})

    def test_detect_core(self):
        with mock.patch.dict(os.environ, {"SUTANDO_CORE_RUNTIME": "codex"}):
            self.assertEqual(gs.detect_core(), "codex")
        import sutando_config
        with mock.patch.object(sutando_config, "resolve_core_runtime", side_effect=ValueError("bad")):
            self.assertEqual(gs.detect_core(), "unknown")
        with mock.patch.object(sutando_config, "resolve_core_runtime", return_value="gemini"):
            self.assertEqual(gs.detect_core(), "unknown")

    def test_workspace_goes_through_the_resolver(self):
        import workspace_default
        with mock.patch.object(workspace_default, "resolve_workspace", return_value=Path("/ws.test")):
            self.assertEqual(gs._workspace(), Path("/ws.test"))

    def test_a_timeout_is_a_publish_error(self):
        commons = gs.Commons(Path("/nonexistent/rc.py"), "!r:x")
        with mock.patch.object(gs.subprocess, "run", side_effect=subprocess.TimeoutExpired("rc", 120)):
            with self.assertRaisesRegex(gs.PublishError, "timed out"):
                commons.run("pages", "!r:x")

    def test_rewrite_of_an_empty_page_appends(self):
        commons = gs.Commons(Path("/nonexistent/rc.py"), "!r:x")
        calls = []
        with mock.patch.object(gs.Commons, "run", lambda self, *a, **k: calls.append((a, k)) or ""):
            commons.rewrite("html-a", "", "<p>new</p>")
        self.assertEqual(calls, [(("append", "!r:x", "<p>new</p>"), {"kind": "html-a"})])

    def test_channel_value_prefers_the_environment(self):
        import channel_credentials
        self.assertEqual(channel_credentials.channel_value("AGENT_MXID", {"AGENT_MXID": "@env:x"}), "@env:x")


class MainTest(unittest.TestCase):
    """main() in-process: a temp workspace and channel dir, a stub room_commons.py, and no
    identity or credential variable inherited from the host."""

    ROOM = "!dm:example.test"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tutorial-main-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.stub = self.tmp / "room_commons.py"
        self.stub.write_text(STUB)
        env = {k: v for k, v in isolated_child_env().items()
               if not k.startswith(("SUTANDO_", "AG2", "REMOTE_TASK", "AGENT_", "ROOM_C", "MATRIX_"))
               and k not in ("CLAUDE_CONFIG_DIR", "CLAUDE_HOME")}
        env.update({"CLAUDE_CONFIG_DIR": str(self.tmp / "claude"), "STUB_STATE": str(self.tmp / "room.json"),
                    "SUTANDO_WORKSPACE_DIR": str(self.tmp / "ws")})
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        ws = mock.patch.object(gs, "_workspace", return_value=self.tmp / "ws")
        ws.start()
        self.addCleanup(ws.stop)
        self.door = FakeActions(self.tmp / "room.json", by="@kai.agent:example.test")
        door = mock.patch.object(gs, "connect_actions", return_value=self.door)
        door.start()
        self.addCleanup(door.stop)

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = gs.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_render_for_each_audience_and_core(self):
        code, out, _ = self.main("render", "--name", "Blue", "--core", "claude", "--audience", "room")
        self.assertEqual((code, out), (0, gs.render_page("Blue", "claude", "room")))
        with mock.patch.object(gs, "detect_core", return_value="codex"):
            code, out, _ = self.main("render", "--name", "Blue", "--core", "auto")
        self.assertEqual((code, out), (0, gs.render_page("Blue", "codex")))

    def test_render_name_auto_falls_back_without_the_network(self):
        with mock.patch.object(gs, "_https_get", side_effect=OSError("offline")):
            code, out, _ = self.main("render", "--name", "auto", "--agent-mxid", "@kai-blue.agent:example.test")
        self.assertEqual(code, 0)
        self.assertIn("<h1>Getting started with Kai Blue</h1>", out)

    def test_bad_name_exits_2(self):
        code, _, err = self.main("render", "--name", " ")
        self.assertEqual(code, 2)
        self.assertIn("1-64 characters", err)

    def test_plan_reads_the_marker(self):
        code, out, _ = self.main("plan", "--room", self.ROOM)
        self.assertEqual((code, json.loads(out)), (0, {"action": "create", "kind": None, "pending": False}))

    def test_publish_creates_then_reports_unchanged(self):
        code, out, _ = self.main("publish", "--room", self.ROOM, "--name", "Blue", "--core", "codex",
                                 "--owner", "@owner:example.test", "--room-commons", str(self.stub),
                                 "--agent-mxid", "@kai.agent:example.test")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"ok": True, "action": "created", "kind": "html-p1",
                                           "summoned": True, "name": "Blue"})
        with mock.patch.object(gs, "detect_core", return_value="codex"):
            code, out, _ = self.main("publish", "--room", self.ROOM, "--name", "Blue",
                                     "--room-commons", str(self.stub), "--agent-mxid", "@kai.agent:example.test")
        self.assertEqual((code, json.loads(out)["action"]), (0, "unchanged"))

    def test_publish_with_tailored_prompts_and_roles(self):
        f = self.tmp / "p.json"
        f.write_text(json.dumps({"dm": "Draft my weekly update."}))
        code, out, _ = self.main("publish", "--room", self.ROOM, "--name", "Blue", "--core", "codex",
                                 "--room-commons", str(self.stub), "--agent-mxid", "@kai.agent:example.test",
                                 "--prompts", str(f), "--roles", "founder")
        self.assertEqual(code, 0)
        page = json.loads((self.tmp / "room.json").read_text())["pages"][json.loads(out)["kind"]]
        self.assertIn("Draft my weekly update.", page)
        self.assertIn("Picked for: Founder", page)

    def test_publish_reason_never_carries_a_credential(self):
        def leak(*a, **k):
            raise RuntimeError("401 Authorization: Bearer srvtoken-123456 via https://relay.x.test/r|secretvalue1")
        self.door.execute = leak
        code, out, _ = self.main("publish", "--room", self.ROOM, "--name", "Blue", "--core", "codex",
                                 "--room-commons", str(self.stub), "--agent-mxid", "@kai.agent:example.test")
        self.assertEqual(code, 4)
        for secret in ("srvtoken-123456", "secretvalue1", "relay.x.test/r|"):
            self.assertNotIn(secret, out)
        self.assertIn("did not complete", json.loads(out)["reason"])

    def test_publish_without_room_commons_exits_3(self):
        with mock.patch.object(gs, "find_room_commons", return_value=self.tmp / "missing.py"):
            code, out, _ = self.main("publish", "--room", self.ROOM, "--name", "Blue", "--core", "codex")
        self.assertEqual((code, json.loads(out)["reason"]), (3, "room-commons not installed"))

    def test_publish_failure_exits_4(self):
        os.environ["STUB_FAIL"] = "pages"
        code, out, _ = self.main("publish", "--room", self.ROOM, "--name", "Blue", "--core", "codex",
                                 "--room-commons", str(self.stub))
        self.assertEqual(code, 4)
        self.assertIn("pages failed", json.loads(out)["reason"])


    # The setup app's saved inputs: <workspace>/state/onboarding/tutorial-inputs.json.
    def save_inputs(self, data):
        path = gs.inputs_path(self.tmp / "ws")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data))

    def room_state(self):
        return json.loads((self.tmp / "room.json").read_text())

    def test_publish_alone_takes_room_owner_and_roles_from_the_inputs_file(self):
        self.save_inputs({"room": self.ROOM, "owner": "@owner:example.test",
                          "agent_mxid": "@kai.agent:example.test", "roles": "I picked: Founder, Sales."})
        with mock.patch.object(gs, "detect_core", return_value="codex"), \
                mock.patch.object(gs, "_https_get", side_effect=OSError("offline")):
            code, out, _ = self.main("publish", "--room-commons", str(self.stub))
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual((result["action"], result["summoned"], result["name"]), ("created", True, "Kai"))
        self.assertIn(["summon", result["kind"], "@owner:example.test"], self.room_state()["calls"])
        self.assertIn("Picked for: Founder, Sales", self.room_state()["pages"][result["kind"]])

    def test_command_line_overrides_the_inputs_file(self):
        self.save_inputs({"room": self.ROOM, "owner": "@owner:example.test", "roles": "Founder"})
        code, out, _ = self.main("publish", "--name", "Blue", "--core", "codex", "--owner", "@other:example.test",
                                 "--roles", "Sales", "--room-commons", str(self.stub),
                                 "--agent-mxid", "@kai.agent:example.test")
        self.assertEqual(code, 0)
        page = self.room_state()["pages"][json.loads(out)["kind"]]
        self.assertIn("Picked for: Sales", page)
        self.assertNotIn("Founder", page.split("Picked for:", 1)[1][:80])

    def test_owner_and_roles_stay_in_the_inputs_room(self):
        self.save_inputs({"room": self.ROOM, "owner": "@owner:example.test", "roles": "Founder"})
        code, out, _ = self.main("publish", "--room", "!elsewhere:example.test", "--name", "Blue",
                                 "--core", "codex", "--room-commons", str(self.stub),
                                 "--agent-mxid", "@kai.agent:example.test")
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertFalse(result["summoned"])
        self.assertNotIn("Picked for:", self.room_state()["pages"][result["kind"]])

    def test_no_room_anywhere_exits_2(self):
        for data in (None, "{not json", "[]", {"room": "dm"}, {"room": "!x:example.test\n"}):
            if data is not None:
                self.save_inputs(data)
            code, out, err = self.main("publish", "--room-commons", str(self.stub))
            self.assertEqual((code, out), (2, ""), data)
            self.assertIn("no --room", err)

    def test_garbled_inputs_keep_the_flag_only_behaviour(self):
        self.save_inputs("{garbled")
        code, out, _ = self.main("publish", "--room", self.ROOM, "--name", "Blue", "--core", "codex",
                                 "--room-commons", str(self.stub), "--agent-mxid", "@kai.agent:example.test")
        self.assertEqual((code, json.loads(out)["action"], json.loads(out)["summoned"]), (0, "created", False))

    def test_load_inputs_keeps_only_well_formed_fields(self):
        path = self.tmp / "inputs.json"
        path.write_text(json.dumps({"room": self.ROOM, "owner": "owner@example.test",
                                    "agent_mxid": "@kai.agent:example.test", "roles": ["Founder", " Sales "],
                                    "extra": "ignored"}))
        self.assertEqual(gs.load_inputs(path), {"room": self.ROOM, "agent_mxid": "@kai.agent:example.test",
                                                "roles": "Founder, Sales"})
        path.write_text(json.dumps({"room": 7, "owner": "@o:example.test\n", "roles": "a\nb",
                                    "agent_mxid": "@" + "a" * 300 + ":example.test"}))
        self.assertEqual(gs.load_inputs(path), {})
        self.assertEqual(gs.load_inputs(self.tmp / "absent.json"), {})


class PersonalTest(unittest.TestCase):
    """Prompts the agent writes for the owner's work (--prompts) and the roles line (--roles)."""

    def test_valid_prompts_override_their_slots_and_are_escaped(self):
        mine = {"dm": "Summarise the papers I send you & flag open questions",
                "commons": "Make a reading list for my thesis in Commons.",
                "finale": "Help me plan this week's experiments."}
        page = gs.render_page("Blue", "codex", prompts=mine)
        buttons = [b for b in parse(page).buttons if b]
        self.assertEqual(buttons[0], mine["finale"])
        self.assertIn(mine["dm"], buttons)
        self.assertIn(mine["commons"], buttons)
        self.assertIn(html.escape(mine["dm"]), page)
        self.assertNotIn(gs.SECTIONS[0].prompt, buttons)  # the default dm prompt is replaced
        self.assertIn(gs.SECTIONS[4].prompt, buttons)  # teamwork kept its default

    def test_invalid_entries_fall_back_per_slot(self):
        bad = {"dm": "see https://example.test", "commons": "x" * (gs.tour_page.PROMPT_MAX + 1),
               "teamwork": "line one\nline two", "apps": 42, "import": "<b>bold</b>", "finale": "   ",
               "unknown-slot": "ignored", "mobile": "not a prompt slot"}
        self.assertEqual(gs.tour_page.clean_prompts(bad), {})
        self.assertEqual(gs.render_page("Blue", "claude", prompts=bad), gs.render_page("Blue", "claude"))
        self.assertEqual(gs.tour_page.clean_prompts({"dm": " ok ", "apps": "www.example.test",
                                                     "commons": "Read example.com for me"}), {"dm": "ok"})
        self.assertEqual(gs.tour_page.clean_prompts(["dm", "x"]), {})

    def test_the_slot_decides_where_to_paste_not_the_text(self):
        page = gs.render_page("Blue", "codex", prompts={"commons": "Ask in your DM: plan my week."})
        block = page[page.index('id="s-commons"'):]
        block = block[:block.index("</section>")]
        self.assertIn('<p class="paste">Paste in: a room</p>', block)

    def test_prompt_files_never_fail_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            good, broken, wrong = (Path(tmp) / n for n in ("good.json", "broken.json", "list.json"))
            good.write_text(json.dumps({"dm": "Draft my weekly update.", "apps": "http://x.test"}))
            broken.write_text("{not json")
            wrong.write_text("[1, 2]")
            self.assertEqual(gs.load_prompts(good), {"dm": "Draft my weekly update."})
            self.assertEqual(gs.load_prompts(broken), {})
            self.assertEqual(gs.load_prompts(wrong), {})
            self.assertEqual(gs.load_prompts(Path(tmp) / "missing.json"), {})
            self.assertEqual(gs.load_prompts(None), {})

    def test_roles_line(self):
        page = gs.render_page("Blue", "codex", roles=["researcher", "product-manager", "researcher"])
        self.assertIn('<p class="roles">Picked for: Researcher, Product Manager</p>', page)
        self.assertNotIn('class="roles"', gs.render_page("Blue", "codex", roles=[""]))
        self.assertNotIn('class="roles"', gs.render_page("Blue", "codex"))
        self.assertEqual(gs.tour_page.role_labels(["<script>", "ok_role", 7]), ["Ok Role"])
        self.assertEqual(gs.tour_page.role_labels(None), [])

    def test_roles_take_the_first_contact_payload_as_given(self):
        labels = gs.tour_page.role_labels
        self.assertEqual(labels("Founder, Sales."), ["Founder", "Sales"])  # cinny-webclient#2231's line
        self.assertEqual(labels("I picked: Founder, Sales."), ["Founder", "Sales"])  # the whole line
        self.assertEqual(labels("  i PICKED :researcher, founder"), ["Researcher", "Founder"])
        self.assertEqual(labels("Product Manager, Designer."), ["Product Manager", "Designer"])
        self.assertEqual(labels("founder,sales"), ["Founder", "Sales"])  # slugs, when labels were missing
        self.assertEqual(labels("product-manager"), ["Product Manager"])
        self.assertEqual(labels("Founder, founder, FOUNDER"), ["Founder"])  # de-duplicated, first kept
        self.assertEqual(labels("<b>x</b>, " + "a" * 41 + ", ., , Sr. Engineer, R&D, Ingenier\u00eda"),
                         ["Sr. Engineer", "R&D", "Ingenier\u00eda"])
        self.assertEqual(len(labels(",".join(f"role{i}" for i in range(9)))), gs.tour_page.ROLES_MAX)
        page = gs.render_page("Blue", "codex", roles="R&D, Product Manager.")
        self.assertIn('<p class="roles">Picked for: R&amp;D, Product Manager</p>', page)

    def test_cli_passes_prompts_and_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "p.json"
            f.write_text(json.dumps({"dm": "Draft my weekly update."}))
            done = subprocess.run([sys.executable, str(SCRIPT), "render", "--name", "Blue", "--core", "codex",
                                   "--prompts", str(f), "--roles", "Founder, Product Manager."],
                                  capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, gs.render_page("Blue", "codex", prompts={"dm": "Draft my weekly update."},
                                                     roles=["Founder", "Product Manager"]))


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
        rooms = {"!r:x": {"kind": "html-a"}, "!p:x": {"kind": "html-p", "state": "pending"}}
        create = {"action": "create", "kind": None, "pending": False}
        listed = lambda *kinds: [{"kind": k, "title": "Other"} for k in kinds]  # noqa: E731
        self.assertEqual(gs.plan({}, "!r:x"), create)
        self.assertEqual(gs.plan(rooms, "!r:x"), {"action": "update", "kind": "html-a", "pending": False})
        self.assertEqual(gs.plan(rooms, "!r:x", listed("html-a")), {"action": "update", "kind": "html-a", "pending": False})
        self.assertEqual(gs.plan(rooms, "!p:x", listed("html-p")), {"action": "update", "kind": "html-p", "pending": True})
        self.assertEqual(gs.plan(rooms, "!r:x", listed("html-b")), create)
        self.assertEqual(gs.plan(rooms, "!other:x"), create)

    def test_ownership_needs_this_agents_marker(self):
        me, page = "@me:x", lambda by, **kw: {"kind": "html-g", "title": "Getting started", "by": by, **kw}  # noqa: E731
        mark = gs.owner_marker(me)
        self.assertTrue(gs.owned(page(me), "<html>" + mark, me))
        self.assertTrue(gs.owned(page(""), mark, me))  # listing without a writer: the text decides
        self.assertFalse(gs.owned(page(""), "<html>someone's notes</html>", me))
        self.assertFalse(gs.owned(page(me), gs.owner_marker("@other:x"), me))
        self.assertFalse(gs.owned(page("@other:x"), mark, me))
        self.assertFalse(gs.owned(page(me), "", me))  # an empty page is never the tutorial's
        self.assertFalse(gs.owned(page(""), "", me))
        self.assertIsNone(gs.owned(page(me), mark, None))
        self.assertEqual(gs.candidates([page(me), page(me, copy_of={"room": "!a:x"}),
                                        {"kind": "html-n", "title": "Notes"}]), [page(me)])
        self.assertEqual(gs.plan({}, "!r:x", [page(me)]), {"action": "reconcile", "kind": None, "pending": True})
        creating = {"!r:x": {"state": "creating", "operation_id": "op-1"}}
        self.assertEqual(gs.plan(creating, "!r:x", []),
                         {"action": "resume", "kind": None, "pending": True, "operation_id": "op-1"})


class FakeActions:
    """room.commons.pages.create as the server keeps it: one page per operation_id, its first body
    written once. `fail` = "before" refuses without creating; "after" creates, then loses the answer."""

    def __init__(self, state: Path, by: str = "@agent:example.test"):
        self.state, self.by, self.fail, self.ops, self.kind = state, by, None, [], None

    def execute(self, room, action, arguments, operation_id=None):
        assert action == gs.CREATE_ACTION and arguments["kind"] == "html", (action, arguments)
        self.ops.append(operation_id)
        if self.fail == "before":
            self.fail = None
            raise RuntimeError("connection reset")
        db = json.loads(self.state.read_text()) if self.state.exists() else {"pages": {}, "calls": [], "next": 1}
        made = db.setdefault("ops", {})
        if operation_id not in made:
            k = "html-p%d" % db["next"]
            db["next"] += 1
            db["pages"][k] = arguments["body"]
            db.setdefault("by", {})[k] = self.by
            made[operation_id] = k
        db["calls"].append(["create", made[operation_id], operation_id])
        self.state.write_text(json.dumps(db))
        if self.fail == "after":
            self.fail = None
            raise TimeoutError("timed out after the server answered")
        return {"kind": self.kind or made[operation_id], "surface": "html", "operation_id": operation_id}


class PublishTest(unittest.TestCase):
    ROOM = "!dm:ag2.space"
    ME = "@agent:example.test"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.stub = base / "room_commons.py"
        self.stub.write_text(STUB)
        self.state = base / "room.json"
        self.marker = gs.marker_path(base / "ws")
        self.door = FakeActions(self.state)
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
        kw.setdefault("agent", self.ME)
        return gs.publish(gs.Commons(self.stub, self.ROOM, actions=lambda: self.door), self.marker, name, **kw)

    def page(self, name="Sutando", core="codex", audience="dm"):
        return gs.render_page(name, core, audience, self.ME)

    def ops(self):
        return [c for c in self.room()["calls"] if c[0] == "create"]

    def test_first_run_creates_writes_and_summons(self):
        out = self.publish(owner="@chi:ag2.space")
        self.assertEqual(out, {"ok": True, "action": "created", "kind": "html-p1", "summoned": True})
        room = self.room()
        self.assertEqual(room["pages"], {"html-p1": self.page()})
        parts = gs.chunks(self.page())
        self.assertGreater(len(parts), 1)  # the page is over one write's limit
        self.assertIn(gs.owner_marker(self.ME), parts[0])  # the create's own body carries ownership
        self.assertEqual([c[0] for c in room["calls"]],
                         ["pages", "create", "read", "append", "summon"])
        appended = [c for c in room["calls"] if c[0] == "append"]
        self.assertTrue(all(c[2] <= gs.CHUNK_BYTES for c in appended))
        self.assertEqual(room["calls"][-1], ["summon", "html-p1", "@chi:ag2.space"])
        entry = gs.load_marker(self.marker)[self.ROOM]
        self.assertEqual((entry["kind"], entry["state"]), ("html-p1", "complete"))

    def test_second_run_is_idempotent(self):
        self.publish(owner="@chi:ag2.space")
        n = len(self.room()["calls"])
        out = self.publish(owner="@chi:ag2.space")
        self.assertEqual((out["action"], out["summoned"]), ("unchanged", False))
        self.assertEqual(list(self.room()["pages"]), ["html-p1"])
        self.assertEqual([c[0] for c in self.room()["calls"]][n:], ["pages", "read"])

    def test_changed_content_rewrites_the_same_page(self):
        self.publish()
        out = self.publish(name="Kai")
        self.assertEqual(out["action"], "updated")
        self.assertEqual(self.room()["pages"], {"html-p1": self.page("Kai")})

    def test_a_large_change_clears_in_pieces_then_appends_in_pieces(self):
        self.publish()
        before = len(self.room()["calls"])
        old, new = self.page(), self.page(core="claude")
        self.assertGreater(len(old.encode()), gs.CHUNK_BYTES)
        out = self.publish(core="claude")
        self.assertEqual(out["action"], "updated")
        calls = self.room()["calls"][before:]
        self.assertEqual([c[0] for c in calls if c[0] == "replace"], ["replace"] * len(gs.chunks(old)))
        self.assertTrue(all(c[3] == 0 and c[2] <= gs.CHUNK_BYTES for c in calls if c[0] == "replace"))
        self.assertEqual(len([c for c in calls if c[0] == "append"]), len(gs.chunks(new)))
        self.assertEqual(self.room()["pages"], {"html-p1": new})

    def test_a_small_change_is_one_replace(self):
        commons = gs.Commons(self.stub, self.ROOM)
        db = {"pages": {"html-p1": "<p>old</p>"}, "calls": [], "next": 2}
        self.state.write_text(json.dumps(db))
        commons.rewrite("html-p1", "<p>old</p>", "<p>new</p>")
        self.assertEqual([c[0] for c in self.room()["calls"]], ["replace"])
        self.assertEqual(self.room()["pages"]["html-p1"], "<p>new</p>")

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

    # --- create-once recovery -------------------------------------------------------------

    def test_ambiguous_create_is_retried_with_the_same_operation_id(self):
        self.door.fail = "after"
        with self.assertRaisesRegex(gs.PublishError, "did not complete"):
            self.publish(owner="@chi:ag2.space")
        entry = gs.load_marker(self.marker)[self.ROOM]
        self.assertEqual(entry["state"], "creating")
        out = self.publish(owner="@chi:ag2.space")
        # the landed page carries this agent's tag, so the rerun finishes it without a second create
        self.assertEqual((out["action"], out["kind"], out["summoned"]), ("updated", "html-p1", True))
        self.assertEqual(self.door.ops, [entry["operation_id"]])
        self.assertEqual(self.room()["pages"], {"html-p1": self.page()})

    def test_resumed_create_with_changed_content_rewrites_the_first_body(self):
        self.door.fail = "after"
        with self.assertRaises(gs.PublishError):
            self.publish(name="Old")
        out = self.publish(name="New")
        self.assertEqual((out["action"], out["kind"]), ("updated", "html-p1"))
        self.assertEqual(self.room()["pages"], {"html-p1": self.page("New")})
        self.assertEqual(len(self.door.ops), 1)

    def test_resume_without_a_listed_tagged_page_reissues_the_same_operation_id(self):
        self.door.fail = "after"
        with self.assertRaises(gs.PublishError):
            self.publish()
        op = gs.load_marker(self.marker)[self.ROOM]["operation_id"]
        db = self.room()
        db["titles"] = {"html-p1": "Untitled page"}  # not yet listed as "Getting started"
        self.state.write_text(json.dumps(db))
        out = self.publish(name="Other")  # content changed since the first attempt
        self.assertEqual(self.door.ops, [op, op])  # the server answers the same operation id
        self.assertEqual((out["action"], out["kind"]), ("created", "html-p1"))
        self.assertEqual(self.room()["pages"], {"html-p1": self.page("Other")})

    def test_actions_client_errors_are_redacted(self):
        commons = gs.Commons(self.stub, self.ROOM, env={"AG2_REMOTE_TOKEN": "https://relay.x.test/r|abcdefghijkl"},
                             actions=lambda: self)
        self.execute = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("401 Authorization: Bearer abcdefghijkl at https://relay.x.test/r|abcdefghijkl"))
        with self.assertRaises(gs.PublishError) as caught:
            commons.create_page("Getting started", "<p>x</p>", "op-1")
        said = str(caught.exception)
        self.assertNotIn("abcdefghijkl", said)
        self.assertNotIn("relay.x.test/r|", said)
        self.assertIn("Bearer [redacted]", said)
        self.assertEqual(gs.redact("token Bearer srv-issued-123 from https://r.test/x|sec", {}),
                         "token Bearer [redacted] from [redacted]")

    def test_create_refused_before_landing_is_retried_with_the_same_operation_id(self):
        self.door.fail = "before"
        with self.assertRaises(gs.PublishError):
            self.publish()
        self.assertEqual(self.room()["pages"], {})
        self.assertEqual(gs.load_marker(self.marker)[self.ROOM]["state"], "creating")
        out = self.publish()
        self.assertEqual((out["action"], out["kind"]), ("created", "html-p1"))
        self.assertEqual(len(set(self.door.ops)), 1)

    def test_crash_after_create_before_recording_its_kind(self):
        real, n = gs.save_marker, {"i": 0}

        def flaky(path, rooms):
            n["i"] += 1
            if n["i"] == 2:  # the "pending" write, right after the create returned
                raise OSError("killed")
            real(path, rooms)
        with mock.patch.object(gs, "save_marker", flaky):
            with self.assertRaisesRegex(gs.PublishError, "could not record html-p1"):
                self.publish()
        self.assertEqual(gs.load_marker(self.marker)[self.ROOM]["state"], "creating")
        out = self.publish()
        self.assertEqual((out["action"], out["kind"]), ("updated", "html-p1"))
        self.assertEqual(list(self.room()["pages"]), ["html-p1"])
        self.assertEqual(self.room()["pages"]["html-p1"], self.page())
        self.assertEqual(len(self.door.ops), 1)

    def test_marker_write_failure_after_the_page_is_whole(self):
        real, n = gs.save_marker, {"i": 0}

        def flaky(path, rooms):
            n["i"] += 1
            if n["i"] == 3:  # the "complete" write
                raise OSError("disk full")
            real(path, rooms)
        with mock.patch.object(gs, "save_marker", flaky):
            with self.assertRaisesRegex(gs.PublishError, "could not record html-p1"):
                self.publish()
        out = self.publish(owner="@chi:ag2.space")
        self.assertEqual((out["action"], out["kind"], out["summoned"]), ("unchanged", "html-p1", True))
        self.assertEqual(list(self.room()["pages"]), ["html-p1"])

    def test_lost_marker_recovers_the_page_by_its_ownership_marker(self):
        self.publish(owner="@chi:ag2.space")
        self.marker.unlink()
        out = self.publish(owner="@chi:ag2.space")
        self.assertEqual((out["action"], out["kind"], out.get("adopted")), ("unchanged", "html-p1", True))
        self.assertFalse(out["summoned"])  # adopting is not a first creation: no second card
        self.assertEqual([c[0] for c in self.room()["calls"]].count("summon"), 1)
        self.assertEqual(list(self.room()["pages"]), ["html-p1"])
        self.assertEqual(gs.load_marker(self.marker)[self.ROOM]["state"], "complete")

    def test_adopted_page_is_summoned_only_on_request(self):
        self.publish(owner="@chi:ag2.space")
        self.marker.unlink()
        out = self.publish(owner="@chi:ag2.space", summon=True)
        self.assertEqual((out.get("adopted"), out["summoned"]), (True, True))
        self.assertEqual([c[0] for c in self.room()["calls"]].count("summon"), 2)

    def test_failed_append_stays_pending_and_the_next_run_finishes_it(self):
        os.environ["STUB_FAIL"] = "append"
        with self.assertRaises(gs.PublishError) as caught:
            self.publish(owner="@chi:ag2.space")
        self.assertIn("html-p1 stays recorded as pending", str(caught.exception))
        self.assertNotIn("taken off", str(caught.exception))
        self.assertEqual(gs.load_marker(self.marker)[self.ROOM]["state"], "pending")
        os.environ.pop("STUB_FAIL")
        out = self.publish(owner="@chi:ag2.space")
        self.assertEqual((out["action"], out["kind"], out["summoned"]), ("updated", "html-p1", True))
        self.assertEqual(self.room()["pages"], {"html-p1": self.page()})
        self.assertEqual(len(self.door.ops), 1)

    def test_a_create_answer_without_an_html_kind_is_refused(self):
        self.door.kind = "markdown-abcdefgh"
        with self.assertRaisesRegex(gs.PublishError, "returned no html page kind"):
            self.publish()
        self.assertEqual(gs.load_marker(self.marker)[self.ROOM]["state"], "creating")

    def test_unrelated_same_title_pages_are_never_adopted_or_touched(self):
        db = {"pages": {"html-p1": "<p>someone's own getting-started notes</p>", "html-p2": ""},
              "calls": [], "next": 3, "by": {"html-p1": "@someone:example.test", "html-p2": self.ME}}
        self.state.write_text(json.dumps(db))
        out = self.publish()
        self.assertEqual((out["action"], out["kind"]), ("created", "html-p3"))
        pages = self.room()["pages"]
        self.assertEqual((pages["html-p1"], pages["html-p2"]), ("<p>someone's own getting-started notes</p>", ""))
        self.assertFalse([c for c in self.room()["calls"] if c[0] in ("replace", "append") and c[1] != "html-p3"])

    def test_two_tutorial_pages_stop_with_a_reason(self):
        db = {"pages": {"html-p1": self.page(), "html-p2": self.page()}, "calls": [], "next": 3,
              "by": {"html-p1": self.ME, "html-p2": self.ME}}
        self.state.write_text(json.dumps(db))
        with self.assertRaisesRegex(gs.PublishError, r"2 carry its marker: \['html-p1', 'html-p2'\]"):
            self.publish()
        self.assertEqual([c[0] for c in self.room()["calls"]], ["pages", "read", "read"])
        self.assertEqual(gs.load_marker(self.marker), {})

    def test_unknown_agent_id_with_a_candidate_stops(self):
        db = {"pages": {"html-p1": "<p>x</p>"}, "calls": [], "next": 2, "by": {"html-p1": "@someone:example.test"}}
        self.state.write_text(json.dumps(db))
        with self.assertRaisesRegex(gs.PublishError, "pass --agent-mxid"):
            self.publish(agent=None)

    def test_failed_summon_still_reports_the_page(self):
        os.environ["STUB_FAIL"] = "summon"
        out = self.publish(owner="@chi:ag2.space")
        self.assertEqual((out["action"], out["summoned"]), ("created", False))
        self.assertIn("summon", out["summon_error"])
        self.assertEqual(gs.load_marker(self.marker)[self.ROOM]["kind"], "html-p1")

    def test_connect_actions_uses_room_commons_own_client_and_restores_the_env(self):
        fake = Path(self.tmp.name) / "rc" / "room_commons.py"
        fake.parent.mkdir()
        fake.write_text("import os\ndef mcp_door(args):\n"
                        "    return lambda: ('door', os.environ.get('AG2_REMOTE_TOKEN'), args.token)\n")
        os.environ.pop("AG2_REMOTE_TOKEN", None)
        got = gs.connect_actions(fake, {"AG2_REMOTE_TOKEN": "fixture-token"})
        self.assertEqual(got, ("door", "fixture-token", None))
        self.assertNotIn("AG2_REMOTE_TOKEN", os.environ)
        self.assertNotIn(str(fake.parent), sys.path)


class CliTest(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)

    def test_render(self):
        for core in gs.CORES:
            done = self.run_cli("render", "--name", "Sutando", "--core", core)
            self.assertEqual(done.returncode, 0)
            self.assertEqual(done.stdout, gs.render_page("Sutando", core))

    def test_detect_core_reads_the_engine_resolver(self):
        env = isolated_child_env(SUTANDO_CORE_RUNTIME="codex")
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
