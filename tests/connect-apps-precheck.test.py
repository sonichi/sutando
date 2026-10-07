#!/usr/bin/env python3
"""Tests for skills/connect-apps/hooks/connect-precheck.py: the keyword table, the room kind, the
cache-only connectedness verdict, first-touch gating, fail-open, and the hook's registration.

Nothing here touches the network or the real workspace.

Run: python3 tests/connect-apps-precheck.test.py
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "skills" / "connect-apps" / "hooks" / "connect-precheck.py"
spec = importlib.util.spec_from_file_location("connect_precheck", HOOK)
hook = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(hook)

sys.path.insert(0, str(ROOT / "src"))
from skill_hooks import discover  # noqa: E402

ROOM = "!room:ag2.space"
NOW = 1_789_000_000.0
TABLE = hook.load_table()


def task_text(text="what's on my calendar tomorrow?", channel_kind="dm", member_count=None, source="ag2space",
              tier="owner", collaborator=None, event="$evt1"):
    head = ["id: task-1", f"source: {source}", f"channel_id: {ROOM}", f"task: {text}"]
    tail = {"channel_kind": channel_kind, "room_member_count": member_count, "source_message_id": event,
            "user_id": "@owner:ag2.space", "access_tier": tier, "collaborator": collaborator}
    return "\n".join(head + [f"{k}: {v}" for k, v in tail.items() if v is not None]) + "\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "tasks").mkdir()
        (self.ws / "state").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, task="task-1", **kw):
        p = self.ws / "tasks" / f"{task}.txt"
        p.write_text(task_text(**kw).replace("id: task-1", f"id: {task}"))
        return p

    def warm(self, *active, at=NOW):
        (self.ws / "state" / hook.CACHE_NAME).write_text(json.dumps(
            {"version": 1, "connectors": {"value_ts": at, "connections": [], "active": list(active)}}))

    def payload(self, path=None, sid="s1", event="PreToolUse", tool="Read", **inp):
        inp = inp or {"file_path": str(path or self.ws / "tasks" / "task-1.txt")}
        return {"session_id": sid, "hook_event_name": event, "tool_name": tool, "tool_input": inp}


class TestTable(unittest.TestCase):
    def test_the_shipped_table_covers_the_popular_toolkits(self):
        slugs = [a["slug"] for a in TABLE]
        for slug in ("gmail", "googlecalendar", "googledrive", "googlemeet", "slack", "linear", "notion", "github",
                     "youtube", "jira", "asana", "trello", "hubspot", "salesforce", "zoom", "dropbox", "outlook",
                     "twitter", "discord", "figma"):
            self.assertIn(slug, slugs)
        self.assertTrue(all(a["patterns"] for a in TABLE))

    def test_hits_by_name_and_by_alias(self):
        cases = {
            "what's on my calendar tomorrow": ["googlecalendar"],
            "Any email from Sam?": ["gmail"],
            "anything new in my inbox": ["gmail"],
            "my Linear issues": ["linear"],
            "find the Drive doc about pricing": ["googledrive"],
            "post this to slack": ["slack"],
            "open the figma file": ["figma"],
            "add it to gcal": ["googlecalendar"],
            "list my jira tickets and any pull requests": ["github", "jira"],
            "send a meet link and check my outlook": ["googlemeet", "outlook"],
            "what did I tweet yesterday": ["twitter"],
        }
        for text, want in cases.items():
            with self.subTest(text):
                self.assertEqual([h["slug"] for h in hook.match_apps(text, TABLE)], want)

    def test_a_longer_phrase_beats_the_shorter_one_it_contains(self):
        # "cold email campaign" is Smartlead's phrase; the "email" inside it must not also name Gmail.
        hits = hook.match_apps("set up a cold email campaign to founders", TABLE)
        self.assertEqual([h["slug"] for h in hits], ["smartlead"])
        self.assertEqual(hits[0]["prefer_skill"], "campaign-runner")
        # Gmail is still named by a word of its own ("inbox") even though "email" sits inside "cold email".
        hits = hook.match_apps("reply to the cold email from Bob in my inbox", TABLE)
        self.assertEqual([(h["slug"], h["keyword"]) for h in hits], [("smartlead", "cold email"), ("gmail", "inbox")])
        # Both apps genuinely named: both stay.
        self.assertEqual([h["slug"] for h in hook.match_apps("check gmail, then the campaign stats", TABLE)],
                         ["smartlead", "gmail"])

    def test_suppression_is_by_match_span_not_by_keyword_string(self):
        # Gmail's only word is "email": it is dropped only where it sits inside Smartlead's longer match,
        # never because "email" is a substring of a Smartlead keyword matched somewhere else in the text.
        both = ("reply to the cold email from Bob in my inbox",
                "email me when the email sequence finishes",
                "send an email about the cold email campaign",
                "draft an email to Bob and check my email campaign stats",
                "set up a cold email campaign, then email Bob the results")
        for text in both:
            with self.subTest(text):
                self.assertEqual([h["slug"] for h in hook.match_apps(text, TABLE)], ["smartlead", "gmail"])
        for text in ("set up a Smartlead campaign", "cold email campaign"):
            with self.subTest(text):
                self.assertEqual([h["slug"] for h in hook.match_apps(text, TABLE)], ["smartlead"])
        # The reported keyword is the first one with a match of its own, not the first one that matched at all.
        hits = hook.match_apps("send an email about the cold email campaign", TABLE)
        self.assertEqual([(h["slug"], h["keyword"]) for h in hits], [("smartlead", "cold email"), ("gmail", "email")])

    def test_misses_and_word_boundaries(self):
        for text in ("what's the weather", "linearly interpolate these", "the emailed report", "calendars in general",
                     "", "a zoomed-in view", "slackers"):
            with self.subTest(text):
                self.assertEqual(hook.match_apps(text, TABLE), [], text)

    def test_a_malformed_table_yields_no_apps(self):
        p = Path(tempfile.mkdtemp()) / "t.json"
        p.write_text('{"apps": [{"slug": 3}, "x", {"slug": "ok", "keywords": ["ok", 5]}]}')
        table = hook.load_table(p)
        self.assertEqual([(a["slug"], a["keywords"]) for a in table], [("ok", ["ok"])])
        p.write_text("{not json")
        self.assertEqual(hook.load_table(p), [])


class TestTaskFields(Base):
    def test_fields_and_message_text(self):
        text = task_text(text="check my calendar") + "===SUTANDO SYSTEM INSTRUCTIONS===\nemail everyone\n"
        f = hook.task_fields(text)
        self.assertEqual((f["source"], f["channel_kind"], f["access_tier"], f["source_message_id"]),
                         ("ag2space", "dm", "owner", "$evt1"))
        self.assertEqual(f["text"], "check my calendar", "writer fields and instruction blocks are not the message")
        f = hook.task_fields("id: t\ntask: first line\nsecond line of the ask\nCaller: hi\nuser_id: @o:x\n")
        self.assertEqual(f["text"], "first line second line of the ask Caller: hi")

    def test_room_kind(self):
        self.assertEqual(hook.room_kind({"channel_kind": "dm", "room_member_count": "9"}), "dm")
        self.assertEqual(hook.room_kind({"channel_kind": "room", "room_member_count": "2"}), "room")
        self.assertEqual(hook.room_kind({"room_member_count": "2"}), "dm")
        self.assertEqual(hook.room_kind({"room_member_count": "5"}), "room")
        self.assertEqual(hook.room_kind({"channel_kind": "weird"}), "unknown")
        self.assertEqual(hook.room_kind({}), "unknown")

    def test_task_refs(self):
        blob = json.dumps({"file_path": "/ws/tasks/task-12.txt", "other": "tasks/archive/task-99.txt tasks/task-cron-1.txt"})
        self.assertEqual(hook.task_refs(blob), [("/ws/tasks/task-12.txt", "task-12")])
        self.assertEqual(hook.task_refs(json.dumps({"command": "cat tasks/task-chat-5.txt"})), [("tasks/task-chat-5.txt", "task-chat-5")])
        self.assertEqual(hook.task_refs("results/task-12.txt"), [])


class TestHandle(Base):
    def test_warm_cache_dm(self):
        self.task()
        self.warm("linear")
        line = hook.handle(self.payload(), now=NOW + 1, table=TABLE)
        self.assertTrue(line.startswith("connect-apps precheck: needs_connect=googlecalendar (Google Calendar); connected=linear; room_kind=dm; reply_to=dm; run: "), line)
        self.assertIn(f"card googlecalendar --room '{ROOM}' --reply-to '$evt1' --task task-1 --owner-from-task --request-file -", line)
        self.assertNotIn("--private", line)
        self.assertIn(str(hook.SCRIPT), line)

    def test_room_kind_drives_private(self):
        for kw, want_kind, want_flag in ((dict(channel_kind="room"), "room", " --private "),
                                         (dict(channel_kind=None, member_count=2), "dm", "--owner-from-task --request-file"),
                                         (dict(channel_kind=None, member_count=7), "room", " --private "),
                                         (dict(channel_kind=None), "unknown", "[--private unless the room is your DM]")):
            with self.subTest(kw):
                self.task(**kw)
                self.warm()
                line = hook.handle(self.payload(sid=f"s-{want_kind}-{kw}"), now=NOW, table=TABLE)
                self.assertIn(f"room_kind={want_kind}", line)
                self.assertIn(want_flag, line)

    def test_reply_to_is_the_room_unless_the_room_is_the_owners_dm(self):
        # The answer goes where it was asked: only a confirmed DM makes it a DM answer; an unknown
        # room kind is a room, so a personal-sounding request from a shared room stays in that room.
        self.assertEqual(hook.reply_to({"channel_kind": "dm"}), "dm")
        self.assertEqual(hook.reply_to({"channel_kind": "room"}), "room")
        self.assertEqual(hook.reply_to({"room_member_count": "2"}), "dm")
        self.assertEqual(hook.reply_to({"room_member_count": "7"}), "room")
        self.assertEqual(hook.reply_to({}), "room")
        for kw, want in ((dict(channel_kind="room"), "room_kind=room; reply_to=room"),
                         (dict(channel_kind=None, member_count=2), "room_kind=dm; reply_to=dm"),
                         (dict(channel_kind=None), "room_kind=unknown; reply_to=room")):
            with self.subTest(kw):
                self.task(**kw)
                self.warm()
                line = hook.handle(self.payload(sid=f"s-reply-{kw}"), now=NOW, table=TABLE)
                self.assertIn(want, line)

    def test_cold_stale_or_skewed_cache_claims_nothing_about_connectedness(self):
        self.task()
        for name, prep in (("cold", lambda: None), ("stale", lambda: self.warm("linear", at=NOW - hook.CACHE_TTL_S)),
                           ("future", lambda: self.warm("linear", at=NOW + 2 * hook.CACHE_TTL_S)),
                           ("torn", lambda: (self.ws / "state" / hook.CACHE_NAME).write_text("{oops"))):
            with self.subTest(name):
                prep()
                line = hook.handle(self.payload(sid=f"s-{name}"), now=NOW, table=TABLE)
                self.assertIn("mentions=googlecalendar (Google Calendar); connected=unknown (cache cold", line)
                self.assertNotIn("needs_connect", line)
                self.assertNotIn("connected=linear", line)
                self.assertIn("run: python3", line, "the card hint still helps: card itself reads the cloud")

    def test_a_prefer_skill_app_gets_the_skill_line_and_no_card(self):
        # The bare Smartlead connector cannot attach a mailbox, upload leads or write a sequence;
        # campaign-runner does all of it, so the line must route there and never offer a card.
        self.task(text="set up a cold email campaign in smartlead")
        self.warm("gmail")
        with tempfile.TemporaryDirectory() as ccd, mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": ccd}):
            line = hook.handle(self.payload(sid="s-skill-missing"), now=NOW + 1, table=TABLE)
            self.assertIn("needs_connect=none", line)
            self.assertIn("prefer_skill=campaign-runner for smartlead (not installed: offer to install it (marketplace skill); never the bare smartlead connector)", line)
            self.assertNotIn("card smartlead", line)
            self.assertNotIn("run:", line)
            (Path(ccd) / "skills" / "campaign-runner").mkdir(parents=True)
            (Path(ccd) / "skills" / "campaign-runner" / "SKILL.md").write_text("x")
            line = hook.handle(self.payload(sid="s-skill-installed"), now=NOW + 1, table=TABLE)
            self.assertIn("prefer_skill=campaign-runner for smartlead (installed; never the bare smartlead connector)", line)

    def test_skill_installed_resolves_claude_home_through_the_canonical_helper(self):
        # A setup that only sets $CLAUDE_HOME (no $CLAUDE_CONFIG_DIR) is the helper's second level.
        with tempfile.TemporaryDirectory() as ch, mock.patch.dict("os.environ", {"CLAUDE_HOME": ch}):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            self.assertFalse(hook.skill_installed("campaign-runner"))
            (Path(ch) / "skills" / "campaign-runner").mkdir(parents=True)
            (Path(ch) / "skills" / "campaign-runner" / "SKILL.md").write_text("x")
            self.assertTrue(hook.skill_installed("campaign-runner"))
            with tempfile.TemporaryDirectory() as ccd, mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": ccd}):
                self.assertFalse(hook.skill_installed("campaign-runner"), "$CLAUDE_CONFIG_DIR outranks $CLAUDE_HOME")
        # No core tree (the helper cannot be imported): the stock ~/.claude default, never a crash.
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(sys.modules, {"util_paths": None}), \
                mock.patch.dict("os.environ", {"HOME": home}):
            os.environ.pop("CLAUDE_CONFIG_DIR", None); os.environ.pop("CLAUDE_HOME", None)
            self.assertFalse(hook.skill_installed("campaign-runner"))
            (Path(home) / ".claude" / "skills" / "campaign-runner").mkdir(parents=True)
            (Path(home) / ".claude" / "skills" / "campaign-runner" / "SKILL.md").write_text("x")
            self.assertTrue(hook.skill_installed("campaign-runner"))

    def test_all_matched_apps_connected_means_no_run_hint(self):
        self.task()
        self.warm("googlecalendar", "gmail")
        line = hook.handle(self.payload(), now=NOW, table=TABLE)
        self.assertIn("needs_connect=none; connected=gmail,googlecalendar; room_kind=dm; reply_to=dm", line)
        self.assertNotIn("run:", line)

    def test_non_owner_and_non_ag2space_tasks_get_no_card_hint(self):
        for name, kw in (("team", dict(tier="team")), ("collaborator", dict(collaborator="true")),
                         ("slack", dict(source="slack")), ("no event", dict(event=None))):
            with self.subTest(name):
                self.task(**kw)
                self.warm()
                line = hook.handle(self.payload(sid=f"s-{name}"), now=NOW, table=TABLE)
                self.assertIn("needs_connect=googlecalendar (Google Calendar)", line)
                self.assertIn("run: no card (not the owner's own AG2 Space message)", line)
                self.assertNotIn("connectors.py card", line)

    def test_a_task_naming_no_app_is_silent(self):
        self.task(text="what's the weather like")
        self.warm()
        self.assertIsNone(hook.handle(self.payload(), now=NOW, table=TABLE))

    def test_first_touch_per_session_task_and_event(self):
        self.task()
        self.warm()
        self.assertIsNotNone(hook.handle(self.payload(), now=NOW, table=TABLE))
        self.assertIsNone(hook.handle(self.payload(), now=NOW + 1, table=TABLE), "the second touch says nothing")
        self.assertIsNone(hook.handle(self.payload(tool="Bash", command=f"cat {self.ws}/tasks/task-1.txt"), now=NOW + 2, table=TABLE))
        self.assertIsNotNone(hook.handle(self.payload(event="PostToolUse"), now=NOW + 3, table=TABLE),
                             "PostToolUse speaks once too: it is the event Claude Code documents additionalContext for")
        self.assertIsNone(hook.handle(self.payload(event="PostToolUse"), now=NOW + 4, table=TABLE))
        self.assertIsNotNone(hook.handle(self.payload(sid="s2"), now=NOW + 5, table=TABLE), "another session")
        self.task("task-2")
        self.assertIsNotNone(hook.handle(self.payload(self.ws / "tasks" / "task-2.txt"), now=NOW + 6, table=TABLE), "another task")
        state = json.loads((self.ws / "state" / hook.SESSIONS_NAME).read_text())
        self.assertEqual(state["sessions"]["s1"]["tasks"], {"task-1": ["PreToolUse", "PostToolUse"], "task-2": ["PreToolUse"]})

    def test_the_session_state_is_bounded(self):
        self.task()
        for i in range(hook.MAX_SESSIONS + 7):
            hook.handle(self.payload(sid=f"s{i:03d}"), now=NOW + i, table=TABLE)
        sessions = json.loads((self.ws / "state" / hook.SESSIONS_NAME).read_text())["sessions"]
        self.assertEqual(len(sessions), hook.MAX_SESSIONS)
        self.assertNotIn("s000", sessions, "the oldest sessions go first")
        self.assertIn(f"s{hook.MAX_SESSIONS + 6:03d}", sessions)

    def test_other_events_tools_and_inputs_are_ignored(self):
        self.task()
        self.warm()
        self.assertIsNone(hook.handle(self.payload(event="Stop"), now=NOW, table=TABLE))
        self.assertIsNone(hook.handle({"session_id": "s", "hook_event_name": "PreToolUse", "tool_input": "tasks/task-1.txt"}, now=NOW, table=TABLE))
        self.assertIsNone(hook.handle({"session_id": "", "hook_event_name": "PreToolUse", "tool_input": {}}, now=NOW, table=TABLE))
        self.assertIsNone(hook.handle(self.payload(self.ws / "tasks" / "task-gone.txt"), now=NOW, table=TABLE))

    def test_a_relative_path_uses_the_configured_workspace(self):
        self.task()
        self.warm("linear")
        with mock.patch.object(hook, "workspace_for", return_value=self.ws) as wf:
            line = hook.handle(self.payload(tool="Bash", command="cat tasks/task-1.txt"), now=NOW, table=TABLE)
        self.assertIn("needs_connect=googlecalendar", line)
        wf.assert_called_once_with("tasks/task-1.txt")


class TestMain(Base):
    def run_main(self, payload):
        out = io.StringIO()
        code = hook.main(stdin=io.StringIO(payload if isinstance(payload, str) else json.dumps(payload)), stdout=out)
        return code, out.getvalue()

    def test_output_is_hook_json_for_the_event_that_fired(self):
        self.task()
        self.warm("linear", at=time.time())  # main() reads the wall clock, so the cache must be fresh now
        for event in ("PreToolUse", "PostToolUse"):
            with self.subTest(event):
                code, out = self.run_main(self.payload(event=event))
                data = json.loads(out)
                self.assertEqual(code, 0)
                self.assertEqual(data["hookSpecificOutput"]["hookEventName"], event)
                self.assertTrue(data["hookSpecificOutput"]["additionalContext"].startswith("connect-apps precheck: needs_connect=googlecalendar"))
                self.assertEqual(set(data), {"hookSpecificOutput"})

    def test_garbage_input_always_exits_0_and_prints_nothing(self):
        for payload in ("", "{not json", "[]", "null", '{"session_id": 5}', json.dumps({"session_id": "s", "hook_event_name": "PreToolUse", "tool_input": {"file_path": "/nowhere/tasks/task-1.txt"}})):
            with self.subTest(payload[:20]):
                self.assertEqual(self.run_main(payload), (0, ""))
        with mock.patch.object(hook, "handle", side_effect=RuntimeError("boom")):
            self.assertEqual(self.run_main(self.payload()), (0, ""))

    def test_the_warm_path_is_four_local_reads_and_nothing_else(self):
        # Budget by I/O the code controls, not wall-clock: four local reads, no network, no
        # subprocess, one write.
        self.task()
        self.warm("linear", at=NOW)
        reads = []
        real_read_text = Path.read_text

        def counting_read_text(path, *a, **kw):
            reads.append(Path(path).name)
            return real_read_text(path, *a, **kw)

        with mock.patch.object(Path, "read_text", counting_read_text), \
             mock.patch("socket.socket", side_effect=AssertionError("network from a hook")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("subprocess from a hook")):
            line = hook.handle(self.payload(sid="io-budget"), now=NOW)
        self.assertTrue(line and line.startswith("connect-apps precheck: needs_connect=googlecalendar"), line)
        self.assertEqual(sorted(reads), sorted(["task-1.txt", hook.SESSIONS_NAME, hook.CACHE_NAME, hook.TABLE_PATH.name]))
        # The only write is the first-touch ledger.
        written = sorted(q.name for q in (self.ws / "state").iterdir())
        self.assertEqual(written, sorted([hook.CACHE_NAME, hook.SESSIONS_NAME, hook.SESSIONS_NAME.replace(".json", ".lock")]))
        # A second touch of the same task in the same session reads only the ledger path and stops.
        reads.clear()
        with mock.patch.object(Path, "read_text", counting_read_text):
            self.assertIsNone(hook.handle(self.payload(sid="io-budget"), now=NOW))
        self.assertNotIn(hook.TABLE_PATH.name, reads, "the table is not re-read once the task was handled")
        self.assertNotIn(hook.CACHE_NAME, reads)


class TestRegistration(unittest.TestCase):
    def test_the_manifest_declares_the_hook_for_both_events_and_discovery_finds_it(self):
        manifest = json.loads((ROOT / "skills" / "connect-apps" / "manifest.json").read_text())
        self.assertEqual([(h["event"], h["command"]) for h in manifest["hooks"]],
                         [("PreToolUse", "./hooks/connect-precheck.py"), ("PostToolUse", "./hooks/connect-precheck.py")])
        found = {(event, token) for event, token, _cmd, _prior in discover(ROOT) if token == "connect-precheck.py"}
        self.assertEqual(found, {("PreToolUse", "connect-precheck.py"), ("PostToolUse", "connect-precheck.py")})

    def test_the_hook_is_self_contained_in_the_skill(self):
        src = HOOK.read_text()
        self.assertNotIn("import cloud_auth", src)
        self.assertNotIn("urllib", src, "never a network read from a hook")
        self.assertTrue((ROOT / "skills" / "connect-apps" / "precheck_apps.json").exists())


if __name__ == "__main__":
    unittest.main()
