#!/usr/bin/env python3
"""The local Antigravity connector's long-lived agy and its one shared Session (acp_host.AcpHost).

The owner's and attested collaborators' turns, from every room, go to one `agy acp` process and one shared
Session, as local Claude Code keeps one session; a restricted sender's turn runs in a short process of its own
with a throwaway Session and no MCP server. Driven against tests/agy_stub_acp.py (its pid on each boot line
counts the spawns), and AcpHost's restart rules against a fake client.

Run:
    python3 packages/ag2-sparrow/tests/test_antigravity_shared_session.py
"""
import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ, isolated_child_env  # noqa: E402

isolate_os_environ()

from _helpers.subprocess_coverage import covered_code  # noqa: E402

PKG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT))
from ag2_sparrow import acp_client, acp_host, connector_cards, task_io  # noqa: E402
from ag2_sparrow.adapters.antigravity import antigravity_connector as gc, gemini_key as gk  # noqa: E402

STUB = str(Path(__file__).resolve().parent / "agy_stub_acp.py")
# The stub as a `python -c` child: measured too when the suite runs under subprocess coverage.
STUB_ARGV = [sys.executable, "-c", covered_code(
    "import runpy, sys; sys.argv = [%r] + sys.argv[1:]; runpy.run_path(%r, run_name='__main__')" % (STUB, STUB))]
ENTRY = {"name": "ag2-space", "command": "/state/ag2-mcp-shim", "args": ["/app/node", "/app/ag2-mcp-proxy.mjs"],
         "env": []}
DM = "!dm:s.space"


def task_text(tid, body, tier="owner", channel="!a:s.space", user="@alice:s.space", collaborator=False, kind=None):
    lines = ["id: " + tid, "timestamp: 2026-11-27T00:00:00Z", "source: ag2space", "channel_id: " + channel,
             "source_room_id: " + channel, "source_message_id: $m-" + tid, "user_id: " + user,
             "access_tier: " + tier] + (["collaborator: true"] if collaborator else []) \
        + (["channel_kind: " + kind] if kind else [])
    return "\n".join(lines + ["task: " + body]) + "\n"


class Work:
    def __init__(self, d):
        self.root = Path(d) / "work"
        for sub in ("tasks", "results", "state"):
            (self.root / sub).mkdir(parents=True)
        (Path(str(self.root) + ".bridge") / "state").mkdir(parents=True)
        self.cwd = Path(d) / "cwd"
        self.cwd.mkdir()
        self.home = Path(d) / "home"      # the stub's own files; the test process's HOME is never changed
        self.home.mkdir()
        (self.home / "signed-in").write_text("")
        self.n = 0
        self.minted = []

    def task(self, tid, body, **kw):
        p = self.root / "tasks" / (tid + ".txt")
        p.write_text(task_text(tid, body, **kw), encoding="utf-8")
        self.n += 1
        os.utime(p, (2e9 + self.n, 2e9 + self.n))   # served in the order written

    def result(self, tid):
        p = self.root / "results" / (tid + ".txt")
        return p.read_text(encoding="utf-8") if p.exists() else None

    def records(self):
        log = self.home / "stub.log"
        return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()] if log.exists() else []

    def boots(self):
        return [r for r in self.records() if "argv" in r]

    def calls(self, method):
        return [r for r in self.records() if r.get("method") == method]

    def prompts(self):
        return self.calls("session/prompt")

    def shared(self):
        return json.loads((self.root / "state" / acp_host.SHARED_NAME).read_text())

    def metrics(self):
        p = self.root / "state" / "metrics.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()]

    def args(self, *extra, tools=False):
        a = gc.build_parser().parse_args(
            ["--work", str(self.root), "--cwd", str(self.cwd), "--acp-command", shlex.join(STUB_ARGV), "--poll", "0.05",
             *(["--token-env-file", str(self.root / "agent.env"), "--agent-id", "@agy:s.space",
                "--mcp-proxy-command", "/app/node /app/ag2-mcp-proxy.mjs"] if tools else []), *extra])
        gc._prepare(a)
        gc._paths(a, str(self.root))
        a.child_env = isolated_child_env(HOME=str(self.home))
        return a

    def mint(self, proxy, token_file, agent_id, state, turn="", tools=frozenset()):
        self.minted.append(tools)
        return dict(ENTRY), ""

    def serve(self, n, *extra, tools=False):
        a = self.args(*extra, tools=tools)
        a.once = n
        with contextlib.redirect_stderr(io.StringIO()) as err:
            conn = gc.Connector(a, proxy_entry=self.mint)
            conn.serve()
        return conn, err.getvalue()


class SharedSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.w = Work(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_owner_turns_from_two_rooms_and_a_collaborator_share_one_process_and_one_session(self):
        self.w.task("task-1", "STUB:ROOM one", channel="!a:s.space")
        self.w.task("task-2", "STUB:ROOM two", channel="!b:s.space")
        self.w.task("task-3", "STUB:ROOM three", channel="!c:s.space", tier="team", user="@bob:s.space",
                    collaborator=True)
        conn, _ = self.w.serve(3, tools=True)
        self.assertEqual(len(self.w.boots()), 1, "one agy process for every turn")
        self.assertEqual(conn.host.spawns, 1)
        self.assertIsNone(conn.host.client, "and it ends with the connector")
        self.assertEqual([r["sessionId"] for r in self.w.prompts()], ["s-1"] * 3)
        self.assertEqual(self.w.shared()["session_id"], "s-1")
        self.assertEqual(self.w.shared()["turns"], 3)
        # Replies are routed by task, each to its own room's result.
        self.assertEqual([self.w.result(t).splitlines()[0] for t in ("task-1", "task-2", "task-3")],
                         ["room !a:s.space for @alice:s.space", "room !b:s.space for @alice:s.space",
                          "room !c:s.space for @bob:s.space"])
        texts = [r["text"] for r in self.w.prompts()]
        for text, room, sender, tier in zip(texts, ("!a:s.space", "!b:s.space", "!c:s.space"),
                                            ("@alice:s.space", "@alice:s.space", "@bob:s.space"),
                                            ("owner", "owner", "team")):
            self.assertTrue(text.startswith("[shared session] This turn is from room %s, sent by %s (access_tier=%s"
                                            % (room, sender, tier)), text[:200])
            self.assertIn("Your reply to this turn goes back to room %s, and only there." % room, text)
        # The collaborator carries the non-owner note claude-local adds; the owner does not.
        self.assertIn("The sender is not your owner. " + task_io.NON_OWNER_NOTE, texts[2])
        self.assertNotIn(task_io.NON_OWNER_NOTE, texts[0])
        # The MCP server is attached once, when the Session opened, with the one grant owner and Team share.
        self.assertEqual([r["params"]["mcpServers"] for r in self.w.calls("session/new")], [[ENTRY]])
        self.assertEqual(self.w.minted, [gc.mcp_auth.OWNER_TOOLS])
        self.assertEqual([m["isolated"] for m in self.w.metrics()], [False] * 3)

    def test_a_guest_turn_gets_a_throwaway_session_in_its_own_process_and_never_touches_the_shared_one(self):
        self.w.task("task-1", "STUB:TEXT hi")
        self.w.task("task-2", "STUB:ROOM hi", channel="!g:s.space", tier="guest", user="@eve:s.space")
        self.w.task("task-3", "STUB:TEXT again", tier="team", user="@mallory:s.space")   # an unattested claim
        self.w.task("task-4", "STUB:TEXT back")
        self.w.serve(4, tools=True)
        boots = self.w.boots()
        self.assertEqual(["--gemini-dir" in b["argv"] for b in boots], [False, True, True],
                         "the long-lived agy, then a short one per restricted turn")
        shared = self.w.shared()["session_id"]
        sids = [r["sessionId"] for r in self.w.prompts()]
        self.assertEqual((sids[0], sids[3]), (shared, shared))
        self.assertNotIn(shared, sids[1:3])
        self.assertEqual(len(set(sids[1:3])), 2, "each restricted turn its own Session")
        news = self.w.calls("session/new")
        self.assertEqual([r["params"]["mcpServers"] for r in news], [[ENTRY], [], []], "no MCP for a restricted turn")
        self.assertEqual(self.w.calls("session/load"), [], "the shared Session never reloaded for them")
        self.assertEqual(self.w.shared()["turns"], 2, "restricted turns are never counted on, or saved to, it")
        self.assertEqual(self.w.result("task-2"), "no room lead\n", "a throwaway prompt has no shared-session lead")
        guest_prompt = self.w.prompts()[1]["text"]
        self.assertIn("room=!g:s.space", guest_prompt)
        self.assertIn("sender=@eve:s.space", guest_prompt)

    def test_an_opted_out_collaborator_is_served_as_a_guest_and_never_reaches_the_shared_session(self):
        self.w.task("task-1", "STUB:TEXT hi")
        self.w.task("task-2", "STUB:ROOM hi", channel="!c:s.space", tier="team", user="@bob:s.space",
                    collaborator=True)
        self.w.task("task-3", "STUB:TEXT back")
        _, err = self.w.serve(3, "--no-allow-collaborator", tools=True)
        boots = self.w.boots()
        self.assertEqual(["--gemini-dir" in b["argv"] for b in boots], [False, True],
                         "the collaborator's turn runs in a short agy of its own, with its own home")
        shared = self.w.shared()["session_id"]
        sids = [r["sessionId"] for r in self.w.prompts()]
        self.assertEqual((sids[0], sids[2]), (shared, shared))
        self.assertNotEqual(sids[1], shared, "never the shared Session id")
        self.assertEqual(self.w.calls("session/load"), [], "the shared Session is never loaded for them")
        news = self.w.calls("session/new")
        self.assertEqual([r["params"]["mcpServers"] for r in news], [[ENTRY], []], "no owner tools")
        self.assertEqual(self.w.minted, [gc.mcp_auth.OWNER_TOOLS], "minted once, for the owner's Session only")
        self.assertEqual(self.w.shared()["turns"], 2)
        self.assertEqual(self.w.result("task-2"), "no room lead\n", "a throwaway prompt, no shared-session lead")
        self.assertEqual([(m["policy"], m["isolated"]) for m in self.w.metrics()],
                         [(task_io.OWNER, False), (task_io.RESTRICTED, True), (task_io.OWNER, False)])
        self.assertEqual(gc.turn_for({"access_tier": "team", "collaborator": "true"}, False),
                         (task_io.RESTRICTED, gc.ISOLATED_TURN))
        self.assertEqual(gc.turn_for({"access_tier": "team", "collaborator": "true"}),
                         (task_io.COLLABORATOR, gc.OWNER_TURN), "the default: shared, as before")
        self.assertEqual(gc.turn_for({"access_tier": "owner"}, False), (task_io.OWNER, gc.OWNER_TURN))

    def test_an_agy_that_died_while_idle_is_restarted_and_the_shared_session_reloaded(self):
        self.w.task("task-1", "STUB:DIE bye")
        self.w.task("task-2", "STUB:ROOM again", channel="!b:s.space")
        conn = gc.Connector(self.w.args())
        with contextlib.redirect_stderr(io.StringIO()) as out:
            conn.handle("task-1.txt")
            self.assertTrue(conn.host.client.gone.wait(10), "the stub exits after its answer")
            conn.handle("task-2.txt")
            conn.host.close()
        err = out.getvalue()
        self.assertEqual(self.w.result("task-1"), "answered, then exiting\n")
        self.assertEqual(self.w.result("task-2"), "room !b:s.space for @alice:s.space\n")
        self.assertEqual(len(self.w.boots()), 2)
        self.assertEqual([r["params"]["sessionId"] for r in self.w.calls("session/load")], ["s-1"])
        self.assertEqual([r["sessionId"] for r in self.w.prompts()], ["s-1", "s-1"])
        self.assertIn("restarting the agent: the agent process exited", err)

    def test_a_refused_load_opens_a_new_shared_session_and_says_so(self):
        self.w.task("task-1", "STUB:TEXT one")
        self.w.serve(1)
        (self.w.home / "refuse-load").write_text("")
        self.w.task("task-2", "STUB:TEXT two")
        _, err = self.w.serve(1)
        self.assertIn("shared session/load refused", err)
        self.assertIn("opening a new shared session", err)
        self.assertEqual(self.w.shared()["session_id"], "s-2")
        self.assertEqual(self.w.shared()["turns"], 1)
        self.assertEqual([r["sessionId"] for r in self.w.prompts()], ["s-1", "s-2"])
        self.assertEqual(self.w.result("task-2"), "hello from agy-stub\n")

    def test_old_per_room_sessions_are_left_alone(self):
        old = acp_host.SessionStore(self.w.root / "state" / gc.SESSIONS_NAME)
        old.remember(("!a:s.space", "owner"), "old-room-session", str(self.w.cwd), 5)
        before = (self.w.root / "state" / gc.SESSIONS_NAME).read_text()
        self.w.task("task-1", "STUB:TEXT one")
        self.w.serve(1)
        self.assertEqual(self.w.calls("session/load"), [], "never resumed")
        self.assertEqual((self.w.root / "state" / gc.SESSIONS_NAME).read_text(), before, "never changed or deleted")

    def test_a_turn_cap_starts_a_new_shared_session(self):
        for i in range(3):
            self.w.task("task-%d" % i, "STUB:TEXT %d" % i)
        _, err = self.w.serve(3, "--shared-session-turns", "2")
        self.assertEqual([r["sessionId"] for r in self.w.prompts()], ["s-1", "s-1", "s-2"])
        self.assertIn("reached its cap of 2 turns", err)
        self.w.task("task-9", "STUB:TEXT later")
        self.w.serve(1, "--shared-session-turns", "1")
        self.assertEqual(self.w.prompts()[-1]["sessionId"], "s-3", "a saved Session at its cap is not resumed")
        self.assertEqual(self.w.calls("session/load"), [])

    # ── the model ────────────────────────────────────────────────────────────

    def model_state(self, **kw):
        p = self.w.root / "state" / "model.json"
        if kw:
            p.write_text(json.dumps(kw))
        return json.loads(p.read_text())

    def test_set_model_is_sent_once_on_the_shared_session_for_an_offered_pick(self):
        (self.w.home / "models").write_text("")
        self.model_state(wanted="claude-opus-4-6-thinking", asked_in=DM)
        self.w.task("task-1", "STUB:TEXT one", channel=DM, kind="dm")
        self.w.task("task-2", "STUB:TEXT two", channel="!b:s.space")
        with mock.patch.object(connector_cards, "queue", return_value=True):
            self.w.serve(2)
        self.assertEqual([r["params"] for r in self.w.calls("session/set_model")],
                         [{"sessionId": "s-1", "modelId": "claude-opus-4-6-thinking"}])
        self.assertEqual([m["model"] for m in self.w.metrics()], ["claude-opus-4-6-thinking"] * 2)

    def test_a_pick_the_session_does_not_offer_is_not_sent_the_owner_is_told_once_and_it_is_kept(self):
        (self.w.home / "models").write_text("")
        self.model_state(wanted="gemini-9-ultra", asked_in=DM)
        self.w.task("task-1", "STUB:TEXT one", channel=DM, kind="dm")
        self.w.task("task-2", "STUB:TEXT two", channel=DM, kind="dm")
        queued = []
        with mock.patch.object(connector_cards, "queue", side_effect=lambda *a, **k: queued.append(a) or True):
            _, err = self.w.serve(2)
        self.assertEqual(self.w.calls("session/set_model"), [], "never asked for: agy would refuse an unknown id")
        self.assertIn("model gemini-9-ultra is not offered by this session", err)
        self.assertEqual(self.model_state()["wanted"], "gemini-9-ultra", "the saved pick is kept")
        self.assertEqual([m.get("model_switch") for m in self.w.metrics()], ["not-switched", None], "told once")
        card = json.dumps(queued)
        self.assertIn("could not switch to gemini-9-ultra (%s)" % gc.MODEL_NOT_OFFERED, card)
        self.assertEqual([m["model"] for m in self.w.metrics()], ["gemini-3.8-flash-medium"] * 2)

    def test_a_quota_error_closes_the_long_lived_agy_for_the_next_turn(self):
        self.w.task("task-1", "STUB:QUOTA")
        self.w.task("task-2", "STUB:TEXT after")
        with mock.patch.object(connector_cards, "queue", return_value=True):
            self.w.serve(2)
        self.assertEqual(len(self.w.boots()), 2, "started again: the owner may have switched account")
        self.assertEqual([r["params"]["sessionId"] for r in self.w.calls("session/load")], ["s-1"])

    def test_a_turn_on_the_open_session_reports_the_tools_its_launch_left_it_with(self):
        self.w.task("task-1", "STUB:TEXT one")
        self.w.task("task-2", "STUB:TEXT two", channel="!b:s.space")
        self.w.serve(2, tools=True)
        # The stub launches no MCP server: the first turn records that, the second reports it from the record.
        self.assertEqual([m["sspace_tools_down"] for m in self.w.metrics()], ["the helper did not start"] * 2)
        self.assertIn(gc.TOOLS_DOWN % "the helper did not start", self.w.result("task-2"))


class KeyChain:
    """`security find-generic-password` over {service: (value, mdat)}: the value with -w, else the dates only."""

    def __init__(self):
        self.items = {}

    def __call__(self, argv, **kw):
        got = self.items.get(argv[argv.index("-s") + 1])
        if got is None:
            return subprocess.CompletedProcess(argv, 44, "", "not found\n")
        if "-w" in argv:
            return subprocess.CompletedProcess(argv, 0, got[0] + "\n", "")
        return subprocess.CompletedProcess(argv, 0, '    "cdat"<timedate>=0x00  "20261127000000Z\\000"\n'
                                                    '    "mdat"<timedate>=0x00  "%s\\000"\n' % got[1], "")


class KeySwapTest(unittest.TestCase):
    """A Gemini API key replaced in the app (A -> B) restarts the long-lived agy, which signs in on B and resumes
    the shared Session; the same key keeps it; another agent's item never touches it."""
    AGENT, KEY_A, KEY_B = "@agy:s.space", "AIzaDUMMY-key-A-0123456789", "AIzaDUMMY-key-B-9876543210abcdef"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.w = Work(self.tmp.name)
        self.chain = KeyChain()
        for name in ("gemini_api_key_from_app", "key_marker"):
            real = getattr(gk, name)
            patcher = mock.patch.object(gk, name, lambda agent_id="", real=real: real(
                agent_id, run=self.chain, platform="darwin", environ={}))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service = gk.agent_service(self.AGENT)

    def turn(self, conn, tid):
        self.w.task(tid, "STUB:TEXT " + tid)
        conn.handle(tid + ".txt")

    def test_a_to_b_restarts_on_b_and_resumes_and_the_same_key_does_not(self):
        self.chain.items[self.service] = (self.KEY_A, "20261127000000Z")
        conn = gc.Connector(self.w.args("--agent-id", self.AGENT))
        with contextlib.redirect_stderr(io.StringIO()) as out:
            self.turn(conn, "task-1")
            self.turn(conn, "task-2")
            self.assertEqual(len(self.w.boots()), 1, "the same key: one process")
            old = conn.host.client
            self.chain.items[gk_service_other(self.AGENT)] = (self.KEY_B, "20261127090000Z")
            self.turn(conn, "task-3")
            self.assertEqual(len(self.w.boots()), 1, "another agent's key: no restart")
            self.chain.items[self.service] = (self.KEY_B, "20261127010203Z")
            self.turn(conn, "task-4")
            self.assertIsNotNone(old.p.poll(), "the old agy exited")
            conn.host.close()
        err = out.getvalue()
        boots = self.w.boots()
        self.assertEqual(len(boots), 2)
        auths = self.w.calls("authenticate")
        self.assertEqual([(a["pid"], a["params"]["key_len"]) for a in auths],
                         [(boots[0]["pid"], len(self.KEY_A)), (boots[1]["pid"], len(self.KEY_B))])
        self.assertEqual([r["params"]["sessionId"] for r in self.w.calls("session/load")], ["s-1"])
        prompts = self.w.prompts()
        self.assertEqual([(r["pid"], r["sessionId"]) for r in prompts],
                         [(boots[0]["pid"], "s-1")] * 3 + [(boots[1]["pid"], "s-1")])
        self.assertEqual(self.w.result("task-4"), self.w.result("task-1").replace("task-1", "task-4"))
        self.assertEqual(err.count("restarting the agent: the authentication changed"), 1)
        stored = (self.w.root / "state" / acp_host.SHARED_NAME).read_text() + (self.w.home / "stub.log").read_text()
        for key in (self.KEY_A, self.KEY_B):
            self.assertNotIn(key, err + stored + str(conn.host.probed))

    def test_a_removed_key_restarts_on_the_sign_in_as_before(self):
        self.chain.items[self.service] = (self.KEY_A, "20261127000000Z")
        conn = gc.Connector(self.w.args("--agent-id", self.AGENT))
        with contextlib.redirect_stderr(io.StringIO()) as out:
            self.turn(conn, "task-1")
            del self.chain.items[self.service]
            self.turn(conn, "task-2")
            conn.host.close()
        self.assertEqual(len(self.w.boots()), 2)
        self.assertEqual(len(self.w.calls("authenticate")), 1, "the new agy is not sent a key")
        self.assertIn("not resuming shared session s-1", out.getvalue())
        self.assertEqual(self.w.shared()["auth"], acp_host.SIGNED_IN_AUTH)


def gk_service_other(agent):
    return gk.agent_service("@other-" + agent.lstrip("@"))


# ── AcpHost's restart rules, on a fake client ────────────────────────────────

class FakeClient:
    """One fake agy process: answers by `script` (method -> result, or an exception to raise)."""

    def __init__(self, script, spawned, on_permission=None, on_update=None):
        self.script, self.calls, self.notes = dict(script), [], []
        self.on_permission, self.on_update = on_permission, on_update
        self.gone = threading.Event()
        self.closed = False
        spawned.append(self)

    def initialize(self, timeout):
        return {"agentCapabilities": {"loadSession": True, "promptCapabilities": {"image": True},
                                      "sessionCapabilities": {"additionalDirectories": {}}}}

    def call(self, method, params, timeout):
        self.calls.append((method, params))
        got = self.script.get(method, {})
        if callable(got):
            got = got(self, params)
        if isinstance(got, Exception):
            raise got
        return got

    def notify(self, method, params):
        self.notes.append(method)

    def close(self, grace=5.0):
        self.closed = True


class HostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.spawned, self.scripts, self.said = [], [], []
        self.args = mock.Mock(acp_argv=["agy", "acp"], cwd=self.tmp.name, child_env={}, state=str(self.state),
                              rpc_timeout=1, turn_timeout=1, interrupt_grace=0)

    def tearDown(self):
        self.tmp.cleanup()

    def host(self, *scripts, **kw):
        self.scripts = list(scripts)

        def factory(argv, **k):
            script = self.scripts.pop(0) if len(self.scripts) > 1 else self.scripts[0]
            if isinstance(script, Exception):
                raise script
            return FakeClient(script, self.spawned, k.get("on_permission"), k.get("on_update"))
        return acp_host.AcpHost(factory, self.args, "err", "not signed in (%s)", lambda *a: self.said.append(
            " ".join(map(str, a))), str(self.state / acp_host.SHARED_NAME), **kw)

    OK = {"session/new": {"sessionId": "s1"}, "session/prompt": {"stopReason": "end_turn"}}

    def run_turn(self, host, decide=None, **kw):
        return host.run_turn("hi", decide or (lambda p: (False, "no", acp_host.REFUSED)), **kw)

    def test_gone_before_the_prompt_is_retried_once_on_a_fresh_process(self):
        host = self.host(dict(self.OK, **{"session/new": acp_client.AcpGone("session/new: exited")}), self.OK)
        status, _, _, turn = self.run_turn(host)
        self.assertEqual((status, len(self.spawned)), ("completed", 2))
        self.assertTrue(self.spawned[0].closed)
        self.assertEqual(turn.session_id, "s1")
        self.assertTrue(any("went away before the prompt" in s for s in self.said))

    def test_a_load_cut_short_is_retried_as_a_load_on_a_fresh_process(self):
        acp_host.SharedSessionFile(self.state / acp_host.SHARED_NAME).write({"session_id": "s0", "cwd": self.tmp.name})
        host = self.host(dict(self.OK, **{"session/load": acp_client.AcpGone("session/load: exited")}), self.OK)
        status, _, _, turn = self.run_turn(host)
        self.assertEqual((status, turn.session_id, turn.resumed), ("completed", "s0", True))
        self.assertEqual([[m for m, _ in c.calls][0] for c in self.spawned], ["session/load"] * 2)

    def test_a_timeout_before_the_prompt_is_retried_once_and_a_second_one_ends_the_turn(self):
        host = self.host(dict(self.OK, **{"session/new": acp_client.AcpTimeout("session/new: no response")}))
        status, _, detail, _ = self.run_turn(host)
        self.assertEqual((status, detail, len(self.spawned)), ("timeout", "session/new: no response", 2))
        self.assertIsNone(host.client)

    def test_gone_after_the_prompt_was_sent_is_never_sent_again(self):
        host = self.host(dict(self.OK, **{"session/prompt": acp_client.AcpGone("session/prompt: exited")}), self.OK)
        status, _, _, _ = self.run_turn(host)
        self.assertEqual((status, len(self.spawned)), ("gone", 1))
        self.assertEqual([m for m, _ in self.spawned[0].calls].count("session/prompt"), 1)
        self.assertEqual(self.run_turn(host)[0], "completed", "the next turn starts a fresh one")
        self.assertEqual(len(self.spawned), 2)
        self.assertEqual([m for m, _ in self.spawned[1].calls][:1], ["session/load"])

    def test_a_prompt_that_times_out_is_cancelled_and_the_process_closed(self):
        host = self.host(dict(self.OK, **{"session/prompt": acp_client.AcpTimeout("x")}))
        status, _, detail, _ = self.run_turn(host)
        self.assertEqual((status, detail), ("timeout", "not finished after 1s"))
        self.assertEqual(self.spawned[0].notes, ["session/cancel"])
        self.assertTrue(self.spawned[0].closed)

    def test_signed_out_closes_the_process_and_another_error_keeps_it(self):
        host = self.host(dict(self.OK, **{"session/new": acp_client.AcpError("Authentication required", acp_host.AUTH_REQUIRED)}))
        status, _, detail, _ = self.run_turn(host)
        self.assertEqual((status, detail), ("failed", "not signed in (Authentication required)"))
        self.assertIsNone(host.client)
        host = self.host(dict(self.OK, **{"session/prompt": acp_client.AcpError("RESOURCE_EXHAUSTED", -32603)}))
        self.assertEqual(self.run_turn(host)[:3:2], ("failed", "RESOURCE_EXHAUSTED"))
        self.assertIsNotNone(host.client, "a failed prompt leaves the process running")
        (self.state / acp_host.SHARED_NAME).unlink()
        host = self.host(dict(self.OK, **{"session/new": {}}))
        self.assertEqual(self.run_turn(host)[:3:2], ("failed", "session/new returned no sessionId"))
        self.assertIsNone(host.client, "a Session that would not open closes it")

    def test_a_command_that_does_not_start_fails_the_turn(self):
        host = self.host(OSError("no such file"))
        status, _, detail, _ = self.run_turn(host)
        self.assertEqual(status, "failed")
        self.assertIn("did not start", detail)

    def test_an_exited_process_and_a_changed_authentication_restart_it(self):
        keyed = [False]
        host = self.host(self.OK, auth_probe=lambda: keyed[0])
        self.run_turn(host)
        self.spawned[0].gone.set()
        self.assertEqual(host.restart_reason(), "the agent process exited")
        self.run_turn(host)
        keyed[0] = True
        self.assertEqual(host.restart_reason(), "the authentication changed")
        self.run_turn(host)
        self.assertEqual(len(self.spawned), 3)

        def broken():
            raise RuntimeError("keychain")
        host.auth_probe = broken
        self.assertEqual(host.restart_reason(), "", "an unreadable probe reads as unchanged")

    def test_an_unknown_marker_neither_restarts_nor_moves_the_baseline(self):
        marker = ["saved v=1"]
        host = self.host(self.OK, auth_probe=lambda: marker[0])
        self.run_turn(host)
        marker[0] = None
        self.assertEqual(host.restart_reason(), "", "unknown is not a removal")
        self.run_turn(host)
        self.assertEqual((len(self.spawned), host.probed), (1, "saved v=1"))
        self.assertIn("authentication not checked: the probe could not tell", self.said)
        host.close()
        self.run_turn(host)      # started while unknown: the last known marker is kept
        self.assertEqual((len(self.spawned), host.probed), (2, "saved v=1"))
        marker[0] = "saved v=2"
        self.assertEqual(host.restart_reason(), "the authentication changed")
        marker[0] = ""
        self.assertEqual(host.restart_reason(), "the authentication changed", "removed is a change")

    def test_a_changed_key_marker_closes_the_old_process_and_an_unchanged_one_keeps_it(self):
        marker = ["saved mdat=1"]
        host = self.host(self.OK, auth_probe=lambda: marker[0])
        self.run_turn(host)
        self.run_turn(host)
        self.assertEqual(len(self.spawned), 1, "the same key: the same process")
        marker[0] = "saved mdat=2"
        self.run_turn(host)
        self.assertEqual(len(self.spawned), 2)
        self.assertTrue(self.spawned[0].closed, "the old process is closed before the new one serves")
        self.assertEqual([m for m, _ in self.spawned[1].calls][-1], "session/prompt")
        self.assertEqual(host.probed, "saved mdat=2")

    def test_only_the_turn_in_progress_and_its_own_session_get_permissions_and_updates(self):
        seen = []

        def prompt(client, params):
            other = client.on_permission({"sessionId": "elsewhere", "toolCall": {"kind": "read"}})
            mine = client.on_permission({"sessionId": "s1", "toolCall": {"kind": "read", "title": "Read x"},
                                         "options": [{"optionId": "y", "kind": "allow_once"}]})
            seen.extend([other, mine])
            for sid in ("elsewhere", "s1"):
                client.on_update({"sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk",
                                                               "content": {"type": "text", "text": sid}}})
            return {"stopReason": "end_turn"}
        host = self.host(dict(self.OK, **{"session/prompt": prompt}))
        status, text, _, turn = self.run_turn(host, decide=lambda p: (True, "ok", {"outcome": {"optionId": "y"}}))
        self.assertEqual((status, text), ("completed", "s1"))
        self.assertEqual(seen, [acp_host.REFUSED, {"outcome": {"optionId": "y"}}])
        self.assertEqual([d["title"] for d in turn.decisions], ["Read x"])
        client = self.spawned[0]
        self.assertEqual(client.on_permission({"sessionId": "s1"}), acp_host.REFUSED, "no turn in progress")
        client.on_update({"sessionId": "s1", "update": {}})   # dropped between turns

    def test_a_listed_model_is_set_once_an_unlisted_one_never_and_an_unlisted_session_still_asks(self):
        models = {"currentModelId": "m1", "availableModels": [{"modelId": "m1"}, {"modelId": "m2"}]}
        host = self.host(dict(self.OK, **{"session/new": {"sessionId": "s1", "models": models}}))
        self.assertEqual(self.run_turn(host, model="m2")[3].model, "m2")
        self.assertEqual(self.run_turn(host, model="m2")[3].model, "m2")
        self.assertEqual([m for m, _ in self.spawned[0].calls].count("session/set_model"), 1)
        turn = self.run_turn(host, model="m9")[3]
        self.assertEqual((turn.model, turn.model_skipped), ("m2", "m9"))
        self.assertEqual([m for m, _ in self.spawned[0].calls].count("session/set_model"), 1)
        host = self.host(dict(self.OK, **{"session/set_model": acp_client.AcpError("Unknown model ID")}))
        turn = self.run_turn(host, model="m9")[3]
        self.assertEqual((turn.model, turn.model_skipped), ("", ""), "no list: asked, refused, logged")
        self.assertTrue(any("session/set_model refused" in s for s in self.said))
        host = self.host(dict(self.OK, **{"session/set_model": acp_client.AcpGone("set_model: exited")}), self.OK)
        self.assertEqual(self.run_turn(host, model="m9")[0], "completed", "retried on a fresh process")

    def test_a_throwaway_session_never_asks_for_a_model_it_does_not_list(self):
        client = FakeClient(self.OK, [])
        models = {"currentModelId": "m1", "availableModels": [{"modelId": "m1"}]}
        self.assertEqual(acp_host.use_model(client, "s9", "m9", models, None, 1,
                                                  lambda *a: self.said.append(" ".join(a))), "m1")
        self.assertEqual(client.calls, [])
        self.assertIn("not offered by this session", " ".join(self.said))

    def test_media_goes_to_the_shared_root_and_images_as_blocks(self):
        root = str(self.state / acp_host.MEDIA_ROOT_NAME)
        host = self.host(self.OK, media_root=root)
        self.assertTrue(os.path.isdir(root))
        media = acp_host.Media(files=(("a.png", "image/png", 3, None),),
                               images=({"type": "image", "mimeType": "image/png", "data": "eA==", "_name": "a.png"},))
        self.run_turn(host, media=media)
        calls = dict(self.spawned[0].calls)
        self.assertEqual(calls["session/new"]["additionalDirectories"], [root])
        self.assertEqual([b["type"] for b in calls["session/prompt"]["prompt"]], ["text", "image", "text"])

    def test_the_shared_record_is_written_0600_and_an_unreadable_one_is_none(self):
        f = acp_host.SharedSessionFile(self.state / "x.json")
        self.assertIsNone(f.read())
        f.write({"session_id": "s1"})
        self.assertEqual(os.stat(self.state / "x.json").st_mode & 0o777, 0o600)
        self.assertEqual(f.read()["session_id"], "s1")
        (self.state / "x.json").write_text("[]")
        self.assertIsNone(f.read())
        self.assertIsNone(acp_host.offered_model_ids({}))
        self.assertEqual(acp_host.offered_model_ids({"availableModels": [{"modelId": "a"}, "junk"]}), ["a"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
