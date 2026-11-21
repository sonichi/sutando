#!/usr/bin/env python3
"""The antigravity adapter: Antigravity CLI (agy) woken over ACP per task, and its install/verify/run.

The wake is exercised against tests/agy_stub_acp.py, whose initialize and signed-out session/new answer as
agy 1.5.9 did on a real host (sign-in methods listed at every initialize, -32000 from session/new when not
signed in); its permission requests are assumed, since no signed-in agy turn has been measured.

Run:
    python3 packages/ag2-sparrow/tests/test_antigravity_adapter.py
"""
import argparse
import atexit
import contextlib
import io
import json
import os
import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ, isolated_child_env  # noqa: E402

isolate_os_environ()

PKG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT))
from ag2_sparrow import acp_host, connect_common as cm, holder_lock, task_io  # noqa: E402

# The work-root lock lives under the passwd home, not HOME: keep it in a temp dir for this suite.
LOCK_HOME = tempfile.mkdtemp(prefix="agy-lock-home-")
holder_lock._home = lambda: LOCK_HOME
atexit.register(shutil.rmtree, LOCK_HOME, True)
from ag2_sparrow.adapters.antigravity import cli, antigravity_connector as gc  # noqa: E402
from ag2_sparrow.result_markers import parse_markers, render_skill_prelude  # noqa: E402

STUB = str(Path(__file__).resolve().parent / "agy_stub_acp.py")


def task_text(tid, body, tier="owner", channel="!room:s.space", event_type=None, collaborator=False):
    lines = ["id: " + tid, "timestamp: 2026-11-14T00:00:00Z", "source: ag2space", "channel_id: " + channel,
             "source_room_id: " + channel, "source_message_id: $m-" + tid, "user_id: @alice:s.space",
             "sender_name: Alice", "access_tier: " + tier]
    if collaborator:
        lines.append("collaborator: true")
    if event_type:
        lines.append("event_type: " + event_type)
    return "\n".join(lines + ["task: " + body]) + "\n"


def gateway_owner_task(tid, body, channel="!room:s.space", addressed_to=""):
    """An owner's task as the gateway writes it: the message on one line, then its SKILL INSTRUCTIONS prelude."""
    lines = ["id: " + tid, "timestamp: 2026-11-14T00:00:00Z", "source: ag2space", "channel_id: " + channel,
             "task_layout: mid", "task: " + body, "source_message_id: $m-" + tid, "user_id: @alice:s.space",
             "access_tier: owner"]
    return "\n".join(lines + render_skill_prelude(channel, "ag2space", tid, addressed_to)) + "\n"


class Work:
    """A work root, a Session directory and the stub's HOME (its sign-in, sessions and log)."""

    def __init__(self, d, signed_in=True):
        self.root = Path(d) / "work"
        for sub in ("tasks", "results", "state"):
            (self.root / sub).mkdir(parents=True)
        self.cwd = Path(d) / "cwd"
        self.cwd.mkdir()
        self.home = Path(d) / "home"
        self.home.mkdir()
        if signed_in:
            (self.home / "signed-in").write_text("")

    def task(self, tid, body, **kw):
        (self.root / "tasks" / (tid + ".txt")).write_text(task_text(tid, body, **kw), encoding="utf-8")

    def result(self, tid):
        p = self.root / "results" / (tid + ".txt")
        return p.read_text(encoding="utf-8") if p.exists() else None

    def records(self):
        log = self.home / "stub.log"
        return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()] if log.exists() else []

    def methods(self):
        return [r.get("method") for r in self.records() if r.get("method")]

    def args(self, *extra, env=None):
        a = gc.build_parser().parse_args(["--work", str(self.root), "--cwd", str(self.cwd), "--acp-command",
                                          shlex.join([sys.executable, STUB]), "--once", "1", "--poll", "0.05", *extra])
        gc._prepare(a)
        gc._paths(a, str(self.root))
        a.child_env = isolated_child_env(HOME=str(self.home)) if env is None else env
        return a

    def serve(self, n=1, *extra, env=None):
        a = self.args(*extra, env=env)
        a.once = n
        with contextlib.redirect_stderr(io.StringIO()):
            gc.Connector(a).serve()
        return a

    def metrics(self):
        p = self.root / "state" / "metrics.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()]


class ConnectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.w = Work(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_each_task_wakes_agy_once_and_its_final_text_is_the_reply(self):
        self.w.task("task-1", "STUB:TEXT hi")
        self.w.serve()
        self.assertEqual(self.w.result("task-1"), "hello from agy-stub\n")
        methods = self.w.methods()
        self.assertEqual([m for m in methods if m in ("initialize", "session/new", "session/prompt")],
                         ["initialize", "session/new", "session/prompt"])
        init = next(r for r in self.w.records() if r.get("method") == "initialize")
        self.assertEqual(init["params"]["clientInfo"]["name"], "ag2-sparrow-antigravity")

    def test_signed_in_while_sign_in_methods_are_listed_needs_no_authenticate(self):
        self.w.task("task-1", "STUB:TEXT hi")
        self.w.serve()
        self.assertNotIn("authenticate", self.w.methods())
        self.assertEqual(self.w.metrics()[0]["status"], "completed")

    def test_not_signed_in_is_said_for_a_message_and_posts_nothing_for_an_event(self):
        (self.w.home / "signed-in").unlink()
        self.w.task("task-1", "STUB:TEXT hi")
        self.w.task("task-2", "say hi", event_type="room.joined")
        os.utime(self.w.root / "tasks" / "task-2.txt", (2e9, 2e9))
        self.w.serve(2)
        said = self.w.result("task-1")
        self.assertIn("Antigravity is not signed in on this computer", said)
        self.assertIn("run `agy` once in a terminal and sign in", said)
        self.assertNotIn("session/prompt", self.w.methods())
        self.assertTrue(self.w.result("task-2").startswith("[no-send] Antigravity handled the event without posting"))

    def test_no_reply_posts_nothing(self):
        self.w.task("task-1", "STUB:NOREPLY thanks!")
        self.w.serve()
        self.assertEqual(self.w.result("task-1"), "[no-send] Antigravity chose not to post\n")
        self.assertEqual(parse_markers(self.w.result("task-1")).actions[0].kind, "skip")

    def test_delivery_markers_in_the_final_text_never_act(self):
        self.w.task("task-1", "STUB:MARKER hi")
        self.w.serve()
        said = self.w.result("task-1")
        self.assertIn("hello", said)
        self.assertEqual(parse_markers(said).actions, [])

    def test_the_owner_may_edit_only_under_the_working_directory_and_no_command_is_granted(self):
        self.w.task("task-1", "STUB:EDIT go")
        self.w.task("task-2", "STUB:SHELL go", channel="!b:s.space")
        os.utime(self.w.root / "tasks" / "task-2.txt", (2e9, 2e9))
        self.w.serve(2)
        perms = [r for r in self.w.records() if "permission" in r]
        self.assertEqual(perms, [{"permission": "WriteFile notes.txt", "allowed": True},
                                 {"permission": "WriteFile /etc/stub-outside", "allowed": False},
                                 {"permission": "rm -rf ~ [current working directory]", "allowed": False}])
        reasons = [d["reason"] for m in self.w.metrics() for d in m["permissions"]]
        self.assertEqual(reasons, ["under the working directory", "outside the working directory",
                                   "no command is granted"])

    def test_a_collaborator_may_not_touch_files(self):
        self.w.task("task-1", "STUB:EDIT go", tier="team", collaborator=True)
        self.w.serve(1, "--allow-non-owner")
        perms = [r for r in self.w.records() if "permission" in r]
        self.assertEqual([p["allowed"] for p in perms], [False, False])
        prompt = next(r["text"] for r in self.w.records() if r.get("method") == "session/prompt")
        self.assertIn("You may not read or change files.", prompt)

    def boots(self):
        return [r for r in self.w.records() if "agy_home" in r]

    def owner_home_with_context(self):
        home = self.w.home / ".gemini"
        (home / "antigravity-cli").mkdir(parents=True)
        (home / "GEMINI.md").write_text("owner's saved context")
        (home / "antigravity-cli" / "settings.json").write_text('{"permissions": {"allow": ["command(*)"]}}')
        return home

    def test_a_non_owner_turn_never_loads_the_owners_agy_home_or_working_directory(self):
        self.owner_home_with_context()
        self.w.task("task-1", "STUB:TEXT hi", tier="team", collaborator=True)
        self.w.task("task-2", "STUB:TEXT hi", tier="team", channel="!b:s.space")
        os.utime(self.w.root / "tasks" / "task-2.txt", (2e9, 2e9))
        self.w.serve(2, "--allow-non-owner")
        self.assertEqual(self.w.result("task-1"), "hello from agy-stub\n")
        boots = self.boots()
        self.assertEqual(len(boots), 2)
        news = [r["params"]["cwd"] for r in self.w.records() if r.get("method") == "session/new"]
        for boot, cwd in zip(boots, news):
            home = boot["agy_home"]
            self.assertEqual(boot["argv"][-2:], ["--gemini-dir", home])
            self.assertTrue(os.path.basename(os.path.dirname(home)).startswith(gc.GUEST_PREFIX), home)
            self.assertFalse(home.startswith(str(self.w.home)), "never under the owner's home")
            self.assertEqual(boot["global_context"], [])
            self.assertEqual(json.loads(boot["settings"]), gc.GUEST_SETTINGS)
            self.assertIn("command(*)", json.loads(boot["settings"])["permissions"]["deny"])
            self.assertEqual(boot["process_cwd"], cwd)
            self.assertEqual(os.path.dirname(cwd), os.path.dirname(home))
            self.assertFalse(cwd.startswith(str(self.w.cwd)), "never the owner's working directory")
            self.assertFalse(os.path.exists(os.path.dirname(home)), "the temporary directory is removed")
        self.assertNotEqual(boots[0]["agy_home"], boots[1]["agy_home"])
        self.assertTrue(all(m["isolated"] for m in self.w.metrics()))

    def test_each_non_owner_task_gets_a_throwaway_session_in_read_only_mode(self):
        self.w.task("task-1", "STUB:TEXT one", tier="team", collaborator=True)
        self.w.serve(1, "--allow-non-owner")
        self.w.task("task-2", "STUB:TEXT two", tier="team", collaborator=True)
        self.w.serve(1, "--allow-non-owner")
        methods = self.w.methods()
        self.assertNotIn("session/load", methods)
        self.assertEqual([m for m in methods if m.startswith("session/")],
                         ["session/new", "session/set_mode", "session/prompt"] * 2)
        modes = [r["params"] for r in self.w.records() if r.get("method") == "session/set_mode"]
        self.assertEqual(modes, [{"sessionId": "s-1", "modeId": "plan"}, {"sessionId": "s-2", "modeId": "plan"}])
        store = acp_host.SessionStore(self.w.root / "state" / gc.SESSIONS_NAME)
        self.assertIsNone(store.get(("!room:s.space", "collaborator:@alice:s.space")))
        self.assertEqual(self.w.result("task-2"), "hello from agy-stub\n")

    def test_a_non_owner_turn_is_refused_when_no_read_only_mode_is_offered_or_set(self):
        for marker in ("no-modes", "set-mode-fails"):
            with self.subTest(marker):
                for p in (self.w.home / "stub.log", self.w.root / "results" / "task-1.txt",
                          self.w.root / "state" / "done"):
                    p.unlink(missing_ok=True)
                (self.w.home / marker).write_text("")
                self.w.task("task-1", "STUB:TEXT hi", tier="team", collaborator=True)
                self.w.serve(1, "--allow-non-owner")
                (self.w.home / marker).unlink()
                said = self.w.result("task-1")
                self.assertIn("Antigravity did not take this: it answers non-owners only in a read-only session mode", said)
                self.assertIn("no read-only session mode is offered" if marker == "no-modes"
                              else "setting session mode plan failed", said)
                self.assertNotIn("session/prompt", self.w.methods())
                self.assertEqual(self.w.metrics()[-1]["status"], "refused")
        self.w.task("task-2", "say hi", tier="team", collaborator=True, event_type="room.joined")
        (self.w.home / "no-modes").write_text("")
        self.w.serve(1, "--allow-non-owner")
        self.assertTrue(self.w.result("task-2").startswith("[no-send]"))

    def test_the_owner_turn_keeps_its_home_directory_and_room_session(self):
        home = self.owner_home_with_context()
        self.w.task("task-1", "STUB:TEXT hi")
        self.w.serve()
        boot = self.boots()[0]
        self.assertEqual((boot["agy_home"], boot["process_cwd"]), (str(home), os.path.realpath(self.w.cwd)))
        self.assertNotIn("--gemini-dir", boot["argv"])
        self.assertNotIn("session/set_mode", self.w.methods())
        new = next(r for r in self.w.records() if r.get("method") == "session/new")
        self.assertEqual(new["params"]["cwd"], str(self.w.cwd))
        store = acp_host.SessionStore(self.w.root / "state" / gc.SESSIONS_NAME)
        self.assertEqual(store.get(("!room:s.space", "owner"))["session_id"], "s-1")
        self.assertFalse(self.w.metrics()[0]["isolated"])

    def test_the_read_only_mode_is_picked_by_id_or_name(self):
        pick = lambda modes: acp_host.pick_mode(modes, gc.READ_ONLY_MODES)  # noqa: E731
        self.assertEqual(pick({"availableModes": [{"id": "default"}, {"id": "plan"}]}), "plan")
        self.assertEqual(pick({"availableModes": [{"id": "m2", "name": "Read-only"}]}), "m2")
        for modes in (None, {}, {"availableModes": [{"id": "default"}, {"id": "auto"}]}, {"availableModes": "plan"},
                      {"availableModes": [{"name": "plan"}]}):
            self.assertIsNone(pick(modes), modes)

    def test_only_the_owner_is_served_unless_non_owners_are_opted_in(self):
        self.w.task("task-1", "STUB:TEXT hi", tier="team", collaborator=True)
        self.w.serve()
        self.assertEqual(self.w.result("task-1"), task_io.NON_OWNER_REFUSED)
        self.assertEqual(self.w.records(), [])
        self.w.task("task-2", "STUB:TEXT hi")
        self.w.serve()
        self.assertEqual(self.w.result("task-2"), "hello from agy-stub\n")
        self.w.task("task-3", "STUB:TEXT hi", tier="team", collaborator=True, channel="!b:s.space")
        self.w.task("task-4", "STUB:TEXT hi", channel="!c:s.space")
        os.utime(self.w.root / "tasks" / "task-4.txt", (2e9, 2e9))
        self.w.serve(2, "--allow-non-owner")
        self.assertEqual((self.w.result("task-3"), self.w.result("task-4")), ("hello from agy-stub\n",) * 2)
        self.assertEqual([m["status"] for m in self.w.metrics()], ["refused", "completed", "completed", "completed"])

    def test_a_guest_is_refused_without_waking_agy(self):
        self.w.task("task-1", "STUB:TEXT hi", tier="guest")
        self.w.serve()
        self.assertEqual(self.w.result("task-1"), task_io.NON_OWNER_REFUSED)
        self.assertEqual(self.w.records(), [])

    def test_a_room_keeps_its_session_and_replayed_history_is_not_the_answer(self):
        self.w.task("task-1", "STUB:TEXT one")
        self.w.serve()
        self.w.task("task-2", "STUB:TEXT two")
        self.w.serve()
        self.assertIn("session/load", self.w.methods())
        self.assertEqual(self.w.result("task-2"), "hello from agy-stub\n")
        rec = acp_host.SessionStore(self.w.root / "state" / gc.SESSIONS_NAME).get(("!room:s.space", "owner"))
        self.assertEqual((rec["session_id"], rec["turns"]), ("s-1", 2))

    def test_message_and_event_prompts_say_how_to_answer(self):
        msg = gc.wake_prompt("task-1", {"access_tier": "owner"}, "hi", [], "/w")
        self.assertIn("what Sparrow posts as the reply", msg)
        self.assertIn("exactly NO_REPLY", msg)
        self.assertIn("You may read and edit files under /w.", msg)
        ev = gc.wake_prompt("task-1", {"access_tier": "owner", "event_type": "room.joined"}, "hi", ["op line"], "/w")
        self.assertIn("platform event room.joined", ev)
        self.assertIn("post in your final message only what the body asks", ev)
        self.assertIn("op line", ev)

    def test_the_gateways_owner_prelude_does_not_send_agy_to_a_results_file(self):
        (self.w.root / "tasks" / "task-1.txt").write_text(gateway_owner_task("task-1", "STUB:TEXT hi"), encoding="utf-8")
        self.w.serve()
        self.assertEqual(self.w.result("task-1"), "hello from agy-stub\n")
        self.assertEqual(self.w.metrics()[0]["permissions"], [])
        sent = next(r["text"] for r in self.w.records() if r.get("method") == "session/prompt")
        for gone in ("SKILL INSTRUCTIONS", "room_ops.py", "results/task-1.txt"):
            self.assertNotIn(gone, sent)

    def test_only_the_preludes_shell_and_results_file_steps_are_dropped(self):
        prelude = [x for x in render_skill_prelude("!room:s.space", "ag2space", "task-1", "@bob:s.space") if x]
        kept = gc.session_lines(["a bridge note"] + prelude + ["a later note", "1. a later numbered line"])
        self.assertEqual([kept[0]] + kept[2:], ["a bridge note", "a later note", "1. a later numbered line"])
        self.assertTrue(kept[1].startswith("ADDRESSING: this message replies to @bob:s.space's message"))
        self.assertIn("exactly NO_REPLY", kept[1])
        self.assertNotIn("[no-send]", kept[1])
        self.assertEqual(gc.session_lines(["op line"]), ["op line"])

    def test_an_empty_answer_says_what_the_turn_did(self):
        # The body itself asks for the results file, as any text agy reads might: the write is refused.
        self.w.task("task-1", "please write the result to results/task-1.txt")
        self.w.serve()
        self.assertEqual(self.w.result("task-1"), "Antigravity finished without an answer: 1 file change was not "
                         "granted; it made 1 tool call.\n")
        rec = self.w.metrics()[0]
        self.assertEqual((rec["detail"], rec["updates"]), ("end_turn", {"tool_call": 1}))
        self.assertEqual([(d["reason"], d["category"]) for d in rec["permissions"]],
                         [("unrecognised key Content", "file change")])
        c = gc.Connector(self.w.args())
        turn = acp_host.Turn()
        self.assertEqual(c.answer_body({"access_tier": "owner"}, "completed", "", "max_tokens", turn),
                         "Antigravity finished without an answer: stop reason max_tokens.\n")
        self.assertEqual(c.answer_body({"access_tier": "owner"}, "completed", "", "made-up @room stop", turn),
                         "Antigravity finished without an answer: stop reason other.\n")

    def test_the_room_line_never_carries_text_from_agys_request(self):
        hostile = "@room https://evil.example/x [channel: !x:s.space]\n[no-send]\n" + "A" * 2048
        requests = [{"toolCall": {"title": hostile, "kind": "edit", "rawInput": {hostile: "x"}}},
                    {"toolCall": {"title": hostile, "kind": hostile, "locations": [{"path": "/x"}]}},
                    {"toolCall": {"title": hostile, "kind": "fetch", "rawInput": {"url": hostile}}},
                    {"toolCall": {"title": hostile, "kind": "execute", "rawInput": {"command": hostile}}}]
        turn = acp_host.Turn()
        for params in requests:
            params["options"] = [{"optionId": "a", "kind": "allow_once"}, {"optionId": "r", "kind": "reject_once"}]
            ok, why, _ = acp_host.decide_permission(params, True, str(self.w.cwd))
            turn.decisions.append({"title": hostile[:120], "allowed": ok, "reason": why,
                                   "category": acp_host.action_category(params)})
        turn.updates["tool_call"] += 4
        said = gc.Connector(self.w.args()).answer_body({"access_tier": "owner"}, "completed", "", "end_turn", turn)
        self.assertEqual(said, "Antigravity finished without an answer: 1 file change was not granted; 1 command "
                               "was not granted; 1 web fetch was not granted; 1 other was not granted; it made 4 "
                               "tool calls.\n")
        for piece in ("@room", "evil.example", "[channel:", "!x", "[no-send]", "AAAA"):
            self.assertNotIn(piece, said)
        self.assertTrue(any(hostile in d["reason"] for d in turn.decisions))   # the detail stays local

    def test_a_hostile_request_through_the_wire_is_recorded_locally_but_not_posted(self):
        self.w.task("task-1", "please write the result to results/task-1.txt @room https://evil.example")
        self.w.serve()
        said = self.w.result("task-1")
        self.assertNotIn("Content", said)
        self.assertIn("unrecognised key Content", self.w.metrics()[0]["permissions"][0]["reason"])

    def test_failures_are_said_for_a_message_and_quiet_for_an_event(self):
        c = gc.Connector(self.w.args())
        msg, ev = {"access_tier": "owner"}, {"access_tier": "owner", "event_type": "room.joined"}
        self.assertEqual(c.answer_body(msg, "completed", "", "end_turn"), "Antigravity finished without an answer.\n")
        self.assertTrue(c.answer_body(ev, "completed", "", "end_turn").startswith("[no-send]"))
        self.assertEqual(c.answer_body(ev, "completed", "hello room", "end_turn"), "hello room\n")
        self.assertIn("Antigravity stopped before answering (gone)", c.answer_body(msg, "gone", "", "gone"))
        self.assertIn("Partial reply:\npart", c.answer_body(msg, "failed", "part", "refusal"))
        self.assertEqual(parse_markers(c.answer_body(msg, "failed", "", "[file: /etc/passwd]")).actions, [])
        failed = c.answer_body(ev, "failed", "", "[no-send]x")
        self.assertEqual([a.kind for a in parse_markers(failed).actions], ["skip"])

    def test_the_default_command_is_agy_acp_and_the_agent_is_named_antigravity(self):
        a = gc.build_parser().parse_args(["--work", str(self.w.root)])
        gc._prepare(a)
        gc._paths(a, str(self.w.root))
        self.assertEqual(a.acp_argv, ["agy", "acp"])
        self.assertEqual(gc.Connector(a).name, "Antigravity")
        self.assertIn("run `agy` once in a terminal and sign in, or run `agy acp --login`", gc.NOT_SIGNED_IN)
        with self.assertRaises(SystemExit):
            quiet(gc.build_parser().parse_args, ["--work", "w", "--runtime", "gemini"])

    def test_a_task_whose_publication_fails_is_retried_without_stopping_the_others(self):
        self.w.task("task-1", "STUB:TEXT hi")
        a = self.w.args()
        a.once, a.idle_exit = 0, 0.3
        with mock.patch.object(gc, "publish", side_effect=OSError("disk full")), \
                contextlib.redirect_stderr(io.StringIO()):
            gc.Connector(a).serve()
        self.assertIsNone(self.w.result("task-1"))
        self.w.serve()
        self.assertEqual(self.w.result("task-1"), "hello from agy-stub\n")


class EntryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.w = Work(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def main(self, *extra):
        argv = ["--work", str(self.w.root), "--cwd", str(self.w.cwd), "--acp-command",
                shlex.join([sys.executable, STUB]), "--poll", "0.01", *extra]
        with contextlib.redirect_stderr(io.StringIO()):
            return gc.main(argv)

    def test_main_takes_the_lock_and_serves(self):
        self.w.task("task-1", "STUB:TEXT hi")
        with mock.patch.dict(os.environ, {"HOME": str(self.w.home)}):
            self.assertEqual(self.main("--once", "1"), 0)
        self.assertEqual(self.w.result("task-1"), "hello from agy-stub\n")

    def test_main_refuses_a_held_root_and_an_empty_command(self):
        owner = task_io.acquire_lock(str(self.w.root), "other")
        try:
            self.assertNotEqual(self.main("--once", "1"), 0)
        finally:
            task_io.release_lock(owner)
        with self.assertRaises(SystemExit):
            gc._prepare(gc.build_parser().parse_args(["--work", "w", "--acp-command", " "]))

    def test_held_starts_only_when_the_holder_names_this_process(self):
        me = {"pid": os.getpid(), "live": True, "start": task_io.start_time(os.getpid())}
        for consumer, want in ((me, 0), (None, 2)):
            fd = os.open(str(self.w.root), os.O_RDONLY)
            here = os.getcwd()
            try:
                with mock.patch.object(gc, "holder_consumer", lambda w: consumer), \
                        mock.patch.object(gc, "HELD_WAIT_S", 0.1), mock.patch.object(gc, "serve_locked", lambda a: 0):
                    self.assertEqual(self.main("--held", "--root-fd", str(fd), "--once", "1"), want)
            finally:
                os.chdir(here)
                os.close(fd)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            gc.main(["--work", "w", "--held"])


def quiet(fn, *a):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        return fn(*a), out.getvalue()


class Patches(contextlib.ExitStack):
    def set(self, obj, **attrs):
        for k, v in attrs.items():
            self.enter_context(mock.patch.object(obj, k, v))
        return self


class InstallVerifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.home = t / "home"
        self.home.mkdir()
        (self.home / "signed-in").write_text("")
        self.bin = t / "bin"
        self.bin.mkdir()
        self.agy = self.bin / "agy"
        self.agy.write_text("#!/bin/sh\nexec %s %s \"$@\"\n" % (shlex.quote(sys.executable), shlex.quote(STUB)))
        self.agy.chmod(0o755)
        self.work = t / "work"
        (self.work / "tasks").mkdir(parents=True)
        (self.work / "results").mkdir()
        self.cwd = t / "cwd"
        self.cwd.mkdir()
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home), "PATH": "%s:/usr/bin:/bin" % self.bin})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def install(self, *extra):
        seen = {}

        def fake_install(shape, values, root, **kw):
            seen.update(shape=shape, values=values, root=root)
            return 0
        out = io.StringIO()
        with mock.patch.object(cm, "install", fake_install), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            rc = cli.main(["install", "--work", str(self.work), *extra])
        return rc, seen, out.getvalue()

    def test_install_checks_the_sign_in_and_records_the_command(self):
        rc, seen, out = self.install()
        self.assertEqual(rc, 0, out)
        v = seen["values"]
        self.assertEqual(shlex.split(v["ACP_COMMAND"]), [str(self.agy), "acp"])
        self.assertNotIn("RUNTIME", v)
        self.assertEqual(v["CWD"], str(Path(seen["root"]) / "workspace"))
        self.assertEqual(v["INBOX_DIR"], str(Path(seen["root"]) / "tasks"))
        self.assertEqual(seen["shape"].name, "antigravity-local")
        self.assertIn("session/new", [json.loads(x).get("method") for x in (self.home / "stub.log").read_text().splitlines()])
        rc, seen, out = self.install("--cwd", str(self.cwd), "--session-store", str(self.cwd / "s.json"))
        self.assertEqual((seen["values"]["CWD"], seen["values"]["SESSION_STORE"]),
                         (str(self.cwd.resolve()), str((self.cwd / "s.json").resolve())))

    def test_install_refuses_when_agy_is_not_signed_in(self):
        (self.home / "signed-in").unlink()
        rc, seen, out = self.install()
        self.assertEqual((rc, seen), (cm.EXIT_UNUSABLE, {}))
        self.assertIn("Antigravity is not signed in on this computer (session/new: Authentication required", out)

    def test_install_refuses_without_agy_on_path_or_with_a_bad_cwd(self):
        rc, seen, out = self.install("--acp-command", "no-such-agy acp")
        self.assertEqual((rc, seen), (cm.EXIT_UNUSABLE, {}))
        self.assertIn("no-such-agy is not on PATH; install Antigravity CLI", out)
        self.agy.unlink()
        rc, seen, out = self.install()
        self.assertIn("agy is not on PATH; install Antigravity CLI (curl -fsSL https://antigravity.google/cli/install.sh", out)
        rc, seen, out = self.install("--cwd", str(self.cwd / "missing"))
        self.assertEqual((rc, seen), (cm.EXIT_UNUSABLE, {}))

    def test_a_pasted_token_install_checks_the_sign_in_once_before_the_token_is_written(self):
        seen = {}
        args = argparse.Namespace(work=None, token_env_file=None, token_stdin=True, agent="@a:x", key=None,
                                  single=False, acp_command=None, cwd=None, session_store=None,
                                  allow_non_owner=False, argv=[], parser=None)
        with Patches() as p:
            p.set(cli.cb, install_token_stdin=lambda a, shape, pre, inst, cmd, **k: (seen.update(pre=pre()), 0)[1],
                  host_problem=lambda c: "")
            self.assertEqual(cli.install(args), 0)
        self.assertEqual(seen["pre"], "")
        self.assertTrue(args.signed_in, "the install that follows does not start agy again")
        with mock.patch.object(cli, "probe", side_effect=AssertionError("probed twice")):
            self.assertEqual(cli.signed_in_problem(args, "x"), "")
        (self.home / "signed-in").unlink()
        args.signed_in = False
        with Patches() as p:
            p.set(cli.cb, install_token_stdin=lambda a, shape, pre, inst, cmd, **k: (seen.update(pre=pre()), 1)[1],
                  host_problem=lambda c: "")
            cli.install(args)
        self.assertIn("not signed in", seen["pre"])

    def test_the_connector_runs_held_with_the_recorded_settings_and_a_narrow_env(self):
        cfg = {"ACP_COMMAND": "%s acp" % self.agy, "CWD": str(self.cwd),
               "SESSION_STORE": "/s.json", "ALLOW_NON_OWNER": "1"}
        argv = " ".join(cli.connector_argv(cfg, str(self.work), 7))
        for part in ("-m %s" % gc.__name__, "--held --root-fd 7",
                     "--acp-command %s acp" % self.agy, "--session-store /s.json", "--allow-non-owner"):
            self.assertIn(part, argv)
        env = cli.child_env(cfg, {"PATH": "/usr/bin", "HOME": "/h", "GEMINI_API_KEY": "x", "SECRET_TOKEN": "y"})
        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(self.bin))
        self.assertNotIn("SECRET_TOKEN", env)
        self.assertNotIn("GEMINI_API_KEY", env, "the unit passes no API key; agy uses its own sign-in")
        self.assertNotIn("--runtime", argv)

    def write_config(self):
        values = {"ACP_COMMAND": shlex.join([str(self.agy), "acp"]), "CWD": str(self.cwd),
                  "INBOX_DIR": str(self.work / "tasks"), "RESULTS_DIR": str(self.work / "results"),
                  "ROOT": str(self.work)}
        path = cli.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cm.render_config(cli.LOCAL, values))
        path.chmod(0o600)

    def verify(self, *extra):
        return quiet(cli.main, ["verify", *extra])

    def test_verify_reports_the_sign_in_and_each_part(self):
        self.write_config()
        rc, out = self.verify()
        self.assertIn("PASS acp command:", out)
        self.assertIn("PASS sign-in: Antigravity ACP Server 1.5.9, signed in, protocol 1, session resume yes", out)
        self.assertIn("PASS session cwd:", out)
        self.assertEqual(rc, 1, "no unit is installed in this test, so the tail fails")
        (self.home / "signed-in").unlink()
        rc, out = self.verify()
        self.assertIn("FAIL sign-in: Antigravity is not signed in on this computer", out)
        rc, out = self.verify("--no-probe")
        self.assertNotIn("sign-in", out)

    def test_verify_fails_when_agy_does_not_start_or_answer(self):
        self.write_config()
        self.agy.write_text("#!/bin/sh\nexit 3\n")
        rc, out = self.verify()
        self.assertIn("FAIL sign-in:", out)
        self.assertIn("did not answer", out)
        said, why = cli.probe({"ACP_COMMAND": "/x acp"}, factory=lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
        self.assertIn("did not start", why)
        self.agy.unlink()
        rc, out = self.verify()
        self.assertIn("FAIL acp command:", out)

    def test_run_is_the_managers_and_hands_the_connector_to_the_holder_sequence(self):
        self.assertEqual(quiet(cli.main, ["run"])[0], cm.EXIT_UNUSABLE, "refused without the unit's marker")
        got = {}
        with Patches() as p:
            p.set(cm, job_from_env=lambda shape: (shape, ""), managed_problem=lambda shape: "",
                  run_launchd=lambda argv, popen: (got.update(launchd=argv), 0)[1])
            self.assertEqual(cli.main(["run", "--launchd"]), 0)
        self.assertIn(cli.__name__, " ".join(map(str, got["launchd"])))
        cfg = {"ACP_COMMAND": "%s acp" % self.agy, "CWD": str(self.cwd),
               "ROOT": str(self.work)}

        class Op:
            def __enter__(self):
                return self

            def release(self):
                got["released"] = True

        def hold(shape, root, state, prepare, spawn, supervise, kill, *a, **k):
            ctx, _ = prepare(argparse.Namespace(root_fd=5), root)
            got["child"] = spawn(ctx)
            return supervise("child")
        with Patches() as p:
            p.set(cli, selected=lambda a: (cli.LOCAL, ""), load_config=lambda local=None: (cfg, ""))
            p.set(cli.task_io, make_dirs=lambda owner, dirs: got.update(dirs=dirs), root_path=lambda owner, root: root)
            p.set(cm, operation_lock=lambda local: Op(), hold_and_supervise=hold,
                  supervise_child=lambda child, state, *a: cm.EXIT_HELD, retired=lambda state, rc: rc)
            rc = quiet(cli.run, argparse.Namespace(debug=True, launchd=False),
                       lambda argv, **kw: (argv, kw["pass_fds"]))[0]
        self.assertEqual(rc, cm.EXIT_CHILD_HELD, "the connector's own 75 is reported as 1")
        self.assertTrue(got["released"])
        self.assertIn("workspace", got["dirs"], "the default Session directory is made under the held root")
        argv, fds = got["child"]
        self.assertEqual((argv[argv.index("--root-fd") + 1], fds), ("5", (5,)))

    def test_verify_and_uninstall_delegate_and_refuse_without_a_job(self):
        with Patches() as p:
            p.set(cli, selected=lambda a: (None, "no job"))
            self.assertEqual(quiet(cli.verify, argparse.Namespace())[0], cm.EXIT_UNUSABLE)
            self.assertEqual(quiet(cli.uninstall, argparse.Namespace())[0], cm.EXIT_UNUSABLE)
        seen = {}
        with Patches() as p:
            p.set(cli.cb, unwire_bridge=lambda cfg, cmd, bridge: seen.setdefault("unwired", cmd))
            p.set(cm, uninstall=lambda local, before_free, token_file: before_free({}) and 0)
            self.assertEqual(quiet(cli.main, ["uninstall"])[0], 0)
        self.assertEqual(seen["unwired"], cli.COMMAND)


if __name__ == "__main__":
    unittest.main(verbosity=2)
