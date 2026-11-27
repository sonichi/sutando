#!/usr/bin/env python3
"""`room_ops.py action`: a room.commons.* Action as the session's own agent, held to who asked.

A pool worker's session acts with its own agent's credential and is refused when it has none; the
core's is never used in its place. An `execute` that answers a task sends the task's own sender as
`asked_by`, and a server refusal comes back as a refusal. Only room.commons.* names are sent, and
neither the relay bearer nor the minted delegation reaches the output.

The connections record, the vault, the task files and the server are fixtures; no network.

Run: python3 tests/room-ops-action.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import contextlib
import functools
import io
import json
import logging
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "skills" / "agent-room-ops")]

_FAKE_VI = types.ModuleType("vault_intercept")


def _no_vault(var):
    raise KeyError(var)


_FAKE_VI.get_vault_key = _no_vault
sys.modules["vault_intercept"] = _FAKE_VI

import action
import connections as C  # noqa: E402
import room_ops

SERVER = "alpha-fixture.net"
RELAY = f"https://chat.{SERVER}/relay"
MINT = f"https://chat.{SERVER}/api/v1/mcp/agent-access-tokens"
MCP = f"https://mcp.{SERVER}/mcp"
ROOM = f"!commons:{SERVER}"
CORE_AGENT, OWN_AGENT = f"@core:{SERVER}", f"@helper:{SERVER}"
OWNER, MEMBER = f"@owner:{SERVER}", f"@member:{SERVER}"
W, OTHER_W = "7c700e11" + "0" * 24, "9d9d9d9d" + "0" * 24
OWN = C.worker_instance_for(SERVER, W)
CORE_SECRET, OWN_SECRET, MINTED = "core-secret-0001", "own-secret-0002", "minted-delegation-0003"
WRITE = "room.commons.pages.create"


def row(instance, agent, **extra):
    r = {"server": SERVER, "instance": instance, "gateway_url": RELAY, "token_ref": C.token_ref_for(instance),
         "primary": False, "added_by": "owner", "added_at": "2026-10-01T00:00:00Z", "verified": True,
         "server_source": "identity", "agent_mxid": agent}
    r.update(extra)
    return r


class FakeVault:
    def __init__(self):
        self.store = {C.token_ref_for("alpha"): f"{RELAY}|{CORE_SECRET}",
                      C.token_ref_for(OWN): f"{RELAY}|{OWN_SECRET}"}

    def get(self, key):
        return self.store[key]


class FakeServer:
    """The relay, the mint and the MCP endpoint. Like the real one, it holds a write that carries
    `asked_by` to that person's rights: only EDITORS may write."""

    EDITORS = (OWNER,)

    def __init__(self, held=False, leak=False):
        self.calls, self.held, self.leak = [], held, leak

    def __call__(self, method, url, *, bearer="", payload=None, headers=None, **_):
        self.calls.append({"method": method, "url": url, "bearer": bearer, "payload": payload})
        if url == RELAY + "/v1/mcp/discovery":
            return 200, {}, {"mint_url": MINT, "mcp_url": MCP}
        if url == MINT:
            return 200, {}, {"access_token": MINTED}
        if url != MCP or bearer != MINTED:
            return 401, {}, {}
        return 200, {}, {"jsonrpc": "2.0", "id": 1, "result": self.tool(payload["params"])}

    def tool(self, params):
        args = params["arguments"]
        if params["name"] == "room.actions.describe":
            return {"structuredContent": {"action": args["action"], "action_revision": "r1"}}
        if params["name"] == "room.action.read":
            return {"structuredContent": {"status": "completed", "result": {"pages": []}}}
        asked = args.get("asked_by")
        if asked is not None and asked not in self.EDITORS:
            said = "that person may not edit this page"
            if self.leak:
                said += f" (relay {OWN_SECRET}, delegation {MINTED})"
            return {"isError": True, "content": [{"type": "text", "text": json.dumps(
                {"code": "FORBIDDEN", "message": said, "suggested_action": "ask a room editor"})}]}
        if self.held:
            return {"structuredContent": {"status": "approval_required", "approval_id": "ap-1",
                                          "operation_id": args["operation_id"]}}
        return {"structuredContent": {"status": "completed", "result": {"page_id": "p1"}}}

    def sent(self, tool):
        return [c["payload"]["params"]["arguments"] for c in self.calls
                if c["url"] == MCP and c["payload"]["params"]["name"] == tool]


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="room-ops-action-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.ws = self.root / "workspace"
        (self.ws / "state").mkdir(parents=True)
        (self.ws / "tasks").mkdir()
        (self.root / "config" / "channels").mkdir(parents=True)
        self.record([row("alpha", CORE_AGENT, primary=True), row(OWN, OWN_AGENT, worker=W)])
        patcher = mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(self.root / "config")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = FakeServer()

    def record(self, rows):
        C.record_path(self.ws).write_text(json.dumps({"schema_version": C.SCHEMA_VERSION, "connections": rows}))

    def task(self, *headers, body="please update the plan page"):
        path = self.ws / "tasks" / "task-1.txt"
        path.write_text("\n".join(("id: task-1", "source: ag2space", f"channel_id: {ROOM}") + headers
                                  + (f"task: {body}", "")), encoding="utf-8")
        return path

    def run_action(self, verb="execute", name=WRITE, worker=W, **kw):
        if verb == "execute" and "task_file" not in kw:
            kw.setdefault("no_task", True)
        return action.run(verb, ROOM, name, kw.pop("arguments", {"title": "Plan"}), workspace=self.ws,
                          environ={C.SESSION_WORKER_ENV: worker} if worker else {}, vault=FakeVault(),
                          request=self.server, **kw)

    def assert_nothing_sent(self, res):
        self.assertFalse(res["ok"], res)
        self.assertEqual(res["state"], "not_sent", res)
        self.assertEqual(self.server.calls, [])


class WhoAsked(Base):
    def test_a_member_without_edit_rights_gets_the_servers_refusal_not_a_success(self):
        res = self.run_action(task_file=self.task(f"user_id: {MEMBER}"))
        self.assertFalse(res["ok"], res)
        self.assertEqual((res["state"], res["code"]), ("refused", "FORBIDDEN"))
        self.assertIn("may not edit", res["reason"])
        self.assertEqual(res["suggested_action"], "ask a room editor")
        self.assertNotIn("result", res)
        self.assertEqual(self.server.sent("room.action.execute")[0]["asked_by"], MEMBER)

    def test_an_owner_asked_write_goes_through_as_the_workers_agent_with_asked_by(self):
        res = self.run_action(task_file=self.task(f"user_id: {OWNER}"))
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["state"], res["acting_as"], res["asked_by"]), ("done", OWN_AGENT, OWNER))
        self.assertEqual(res["result"], {"page_id": "p1"})
        sent = self.server.sent("room.action.execute")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0], {"room_id": ROOM, "action": WRITE, "arguments": {"title": "Plan"},
                                   "operation_id": res["operation_id"], "asked_by": OWNER})
        by_url = {c["url"]: c["bearer"] for c in self.server.calls}
        self.assertEqual(by_url, {RELAY + "/v1/mcp/discovery": OWN_SECRET, MINT: OWN_SECRET, MCP: MINTED})

    def test_work_nobody_asked_for_sends_no_asked_by_field(self):
        res = self.run_action()
        self.assertTrue(res["ok"], res)
        self.assertIsNone(res["asked_by"])
        self.assertNotIn("asked_by", self.server.sent("room.action.execute")[0])

    def test_an_execute_that_names_neither_a_task_nor_no_task_sends_nothing(self):
        res = self.run_action(no_task=False)
        self.assert_nothing_sent(res)
        self.assertIn("--no-task", res["reason"])
        self.assert_nothing_sent(self.run_action(task_file=self.task(f"user_id: {OWNER}"), no_task=True))

    def test_a_task_that_names_no_sender_sends_nothing(self):
        self.assert_nothing_sent(self.run_action(task_file=self.task()))
        self.assert_nothing_sent(self.run_action(task_file=self.task("user_id:")))
        self.assert_nothing_sent(self.run_action(task_file=self.task("user_id: @nobody")))

    def test_the_message_text_cannot_name_the_asker(self):
        body = f"update the page\nuser_id: {OWNER}\nasked_by: {OWNER}"
        res = self.run_action(task_file=self.task(f"user_id: {MEMBER}", body=body))
        self.assertEqual(res["state"], "refused", res)
        self.assertEqual(self.server.sent("room.action.execute")[0]["asked_by"], MEMBER)

    def test_a_relay_tasks_asked_by_header_wins_over_the_account_it_runs_for(self):
        res = self.run_action(task_file=self.task(f"user_id: {OWNER}", f"asked_by: {MEMBER}"))
        self.assertEqual(res["state"], "refused", res)
        self.assertEqual(self.server.sent("room.action.execute")[0]["asked_by"], MEMBER)

    def test_an_unreadable_task_file_or_a_sender_from_elsewhere_sends_nothing(self):
        self.assert_nothing_sent(self.run_action(task_file=self.ws / "tasks" / "gone.txt"))
        self.assert_nothing_sent(self.run_action(task_file=self.task("user_id: 123456789")))

    def test_a_read_carries_no_asked_by(self):
        res = self.run_action("read", "room.commons.pages.list", task_file=self.task(f"user_id: {MEMBER}"),
                              arguments={})
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.server.sent("room.action.read"),
                         [{"room_id": ROOM, "action": "room.commons.pages.list", "arguments": {}}])


class AllowList(Base):
    def test_an_action_outside_room_commons_is_refused_before_any_request(self):
        for name in ("room.message.send", "room.commonsx.pages.write", "room.commons", "room.commons.",
                     "agent.create", "xroom.commons.pages.create", "room.commons.pages.create\n", ""):
            for verb in ("describe", "read", "execute"):
                res = self.run_action(verb, name)
                self.assert_nothing_sent(res)
                self.assertIn("room.commons.*", res["reason"])

    def test_an_unknown_verb_is_refused(self):
        self.assert_nothing_sent(self.run_action("execute_safe"))

    def test_describe_asks_the_server_for_that_action(self):
        res = self.run_action("describe")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["result"]["action_revision"], "r1")
        self.assertEqual(self.server.sent("room.actions.describe"), [{"room_id": ROOM, "action": WRITE}])


class WhoseAgent(Base):
    """The core's credential is there to be used in every case: a refusal is the worker rule's."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict("os.environ", {"GATEWAY_URL": RELAY, "GATEWAY_TOKEN": CORE_SECRET})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_cores_credential_is_usable_here(self):
        res = self.run_action(worker="")
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.server.calls[0]["bearer"], CORE_SECRET)

    def test_a_malformed_worker_id_is_refused_not_read_as_the_core(self):
        for bad in ("Not A Worker", "-x", "w" * 40):
            res = self.run_action(worker=bad)
            self.assert_refused_without_the_core(res)
            self.assertIn(C.SESSION_WORKER_ENV, res["reason"])

    def test_a_worker_whose_token_is_missing_is_refused(self):
        vault = FakeVault()
        del vault.store[C.token_ref_for(OWN)]
        res = action.run("execute", ROOM, WRITE, None, workspace=self.ws, environ={C.SESSION_WORKER_ENV: W},
                         vault=vault, request=self.server, no_task=True)
        self.assert_refused_without_the_core(res)

    def assert_refused_without_the_core(self, res):
        self.assert_nothing_sent(res)
        self.assertNotIn(CORE_SECRET, json.dumps(res))

    def test_a_worker_with_no_agent_of_its_own_is_refused_never_sent_as_the_core(self):
        self.assert_refused_without_the_core(self.run_action(worker=OTHER_W))

    def test_a_disabled_identity_is_refused(self):
        self.record([row("alpha", CORE_AGENT, primary=True), row(OWN, OWN_AGENT, worker=W, disabled=True)])
        self.assert_refused_without_the_core(self.run_action())

    def test_an_identity_that_names_no_agent_is_refused(self):
        self.record([row("alpha", CORE_AGENT, primary=True), row(OWN, "", worker=W)])
        self.assert_refused_without_the_core(self.run_action())

    def test_a_room_on_a_server_the_worker_has_no_agent_on_is_refused(self):
        res = action.run("execute", "!commons:beta-fixture.net", WRITE, {}, workspace=self.ws, no_task=True,
                         environ={C.SESSION_WORKER_ENV: W}, vault=FakeVault(), request=self.server)
        self.assert_refused_without_the_core(res)

    def test_a_checkout_whose_workspace_holds_no_record_says_so(self):
        """A seat's command run from another checkout: the record is in the install's workspace only."""
        empty = self.root / "other-checkout" / "workspace"
        (empty / "state").mkdir(parents=True)
        with mock.patch("workspace_default.resolve_workspace", return_value=empty):
            res = action.run("describe", ROOM, WRITE, workspace=None, agent_mxid=OWN_AGENT,
                             environ={C.SESSION_WORKER_ENV: W}, vault=FakeVault(), request=self.server)
        self.assert_refused_without_the_core(res)
        self.assertIn("run the command from the install's own checkout", res["reason"])
        res = self.run_action("describe", agent_mxid=OWN_AGENT)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["acting_as"], OWN_AGENT)

    def test_it_resolves_the_row_a_post_in_that_room_resolves(self):
        rows = [row("alpha", CORE_AGENT, primary=True), row(OWN, OWN_AGENT, worker=W, dirs="own"),
                row(C.worker_instance_for(SERVER, OTHER_W), f"@second:{SERVER}", worker=OTHER_W, dirs="own")]
        self.record(rows)
        posts_as = C.connection_for_room(ROOM, self.ws, {C.SESSION_WORKER_ENV: W})
        res = self.run_action()
        self.assertTrue(res["ok"], res)
        self.assertEqual((posts_as["worker"], res["acting_as"]), (W, posts_as["agent_mxid"]))
        self.assertEqual(self.server.calls[0]["bearer"], OWN_SECRET)

    def test_naming_another_agent_is_refused(self):
        self.assert_refused_without_the_core(self.run_action(agent_mxid=CORE_AGENT))

    def test_the_cores_session_acts_with_the_credential_its_posts_use(self):
        env = {"GATEWAY_URL": RELAY, "GATEWAY_TOKEN": CORE_SECRET}
        with mock.patch.dict("os.environ", env):
            res = action.run("execute", ROOM, WRITE, {}, task_file=self.task(f"user_id: {OWNER}"),
                             workspace=self.ws, environ={}, request=self.server, gate=None)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.server.calls[0]["bearer"], CORE_SECRET)
        self.assertEqual(self.server.sent("room.action.execute")[0]["asked_by"], OWNER)


class Outcomes(Base):
    def test_a_write_held_for_approval_is_not_a_success_and_keeps_its_operation_id(self):
        self.server = FakeServer(held=True)
        res = self.run_action(operation_id="op-7")
        self.assertFalse(res["ok"], res)
        self.assertEqual((res["state"], res["operation_id"]), ("held", "op-7"))
        self.assertIn("do NOT run it again", res["reason"])

    def test_an_execute_with_no_answer_is_unknown_not_unsent(self):
        _, pp = action._owners()

        def down(method, url, **kw):
            if url == MCP:
                raise pp.TransportError("TimeoutError")
            return self.server(method, url, **kw)
        res = action.run("execute", ROOM, WRITE, {}, workspace=self.ws, environ={C.SESSION_WORKER_ENV: W},
                         vault=FakeVault(), request=down, no_task=True)
        self.assertEqual((res["ok"], res["state"]), (False, "unknown"))
        self.assertIn(res["operation_id"], json.dumps(res))

    def test_an_execute_whose_reply_was_too_large_to_read_is_unknown(self):
        """`http_json` reads a reply over its cap as an empty object, after the write was sent."""
        def oversized(method, url, **kw):
            answer = self.server(method, url, **kw)
            return (200, {}, {}) if url == MCP else answer
        res = action.run("execute", ROOM, WRITE, {}, workspace=self.ws, environ={C.SESSION_WORKER_ENV: W},
                         vault=FakeVault(), request=oversized, no_task=True, operation_id="op-9")
        self.assertEqual((res["ok"], res["state"], res["operation_id"]), (False, "unknown", "op-9"))
        self.assertEqual(len(self.server.sent("room.action.execute")), 1)
        self.assertIn("--operation-id", res["reason"])


class RefusedHere(Base):
    def core(self, room=ROOM, **kw):
        return action.run("execute", room, WRITE, kw.pop("arguments", {}), workspace=self.ws, environ={},
                          request=self.server, no_task=True, **kw)

    def test_a_missing_room_or_arguments_that_are_no_object_send_nothing(self):
        self.assert_nothing_sent(self.core(room=""))
        self.assert_nothing_sent(self.core(arguments=["a list"]))

    def test_a_worker_whose_token_is_missing_is_refused(self):
        vault = FakeVault()
        del vault.store[C.token_ref_for(OWN)]
        res = action.run("execute", ROOM, WRITE, None, workspace=self.ws, environ={C.SESSION_WORKER_ENV: W},
                         vault=vault, request=self.server, no_task=True)
        self.assert_nothing_sent(res)
        self.assertNotIn(CORE_SECRET, json.dumps(res))

    def test_a_core_with_no_gateway_credential_is_refused(self):
        self.assert_nothing_sent(self.core())

    def test_a_room_whose_server_has_no_connection_is_refused(self):
        with mock.patch.object(action._gateway, "gateway", side_effect=action._gateway.RoomConnectionError("none")):
            self.assert_nothing_sent(self.core())
        with mock.patch.dict("os.environ", {"GATEWAY_URL": RELAY, "GATEWAY_TOKEN": CORE_SECRET}), \
                mock.patch.object(C, "room_agent_mxid", side_effect=C.NoConnectionForRoom("none")):
            self.assert_nothing_sent(self.core())

    def test_the_client_gate_denies_a_room_it_does_not_list(self):
        self.assert_nothing_sent(self.run_action(gate={OWN_AGENT: {"rooms": ["!other:hs"]}}))

    def test_a_checkout_without_the_actions_client_is_refused(self):
        with mock.patch.dict(sys.modules, {"pool_provision": None}):
            res = self.run_action()
        self.assert_nothing_sent(res)
        self.assertIn("pool_provision", res["reason"])

    def test_a_read_with_no_answer_failed(self):
        _, pp = action._owners()

        def down(method, url, **kw):
            raise pp.TransportError("TimeoutError")
        res = action.run("read", ROOM, "room.commons.pages.list", {}, workspace=self.ws,
                         environ={C.SESSION_WORKER_ENV: W}, vault=FakeVault(), request=down)
        self.assertEqual((res["ok"], res["state"]), (False, "failed"))


class NoSecretInOutput(Base):
    def cli(self, *argv):
        seam = functools.partial(action.run, workspace=self.ws, environ={C.SESSION_WORKER_ENV: W},
                                 vault=FakeVault(), request=self.server)
        out, err, logs = io.StringIO(), io.StringIO(), io.StringIO()
        handler = logging.StreamHandler(logs)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        with mock.patch.object(action, "run", seam), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = room_ops._main(list(argv))
        return rc, out.getvalue(), out.getvalue() + err.getvalue() + logs.getvalue()

    def test_neither_token_reaches_stdout_stderr_or_the_log_even_when_the_server_echoes_them(self):
        self.server = FakeServer(leak=True)
        rc, out, everything = self.cli("action", "execute", ROOM, WRITE, "--args", '{"title": "Plan"}',
                                       "--task-file", str(self.task(f"user_id: {MEMBER}")))
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)["state"], "refused")
        for secret in (OWN_SECRET, MINTED, CORE_SECRET):
            self.assertNotIn(secret, everything)
        self.assertIn("[redacted]", out)

    def test_a_completed_write_exits_0_and_prints_no_token(self):
        rc, out, everything = self.cli("action", "execute", ROOM, WRITE, "--args", '{"title": "Plan"}',
                                       "--task-file", str(self.task(f"user_id: {OWNER}")))
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["asked_by"], OWNER)
        for secret in (OWN_SECRET, MINTED, CORE_SECRET):
            self.assertNotIn(secret, everything)

    def test_arguments_come_from_a_file_and_bad_json_sends_nothing(self):
        path = self.root / "args.json"
        path.write_text('{"title": "From a file"}', encoding="utf-8")
        rc, _, _ = self.cli("action", "execute", ROOM, WRITE, "--args-file", str(path), "--no-task")
        self.assertEqual(rc, 0)
        self.assertEqual(self.server.sent("room.action.execute")[0]["arguments"], {"title": "From a file"})
        self.server.calls.clear()
        rc, out, _ = self.cli("action", "execute", ROOM, WRITE, "--args", "{not json")
        self.assertEqual((rc, self.server.calls), (2, []))
        self.assertFalse(json.loads(out)["ok"])

    def test_the_command_line_needs_a_task_or_no_task_and_never_both(self):
        rc, out, _ = self.cli("action", "execute", ROOM, WRITE)
        self.assertEqual((rc, json.loads(out)["state"], self.server.calls), (1, "not_sent", []))
        rc, out, _ = self.cli("action", "execute", ROOM, WRITE, "--no-task")
        self.assertEqual((rc, json.loads(out)["asked_by"]), (0, None))
        self.assertNotIn("asked_by", self.server.sent("room.action.execute")[0])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            room_ops._main(["action", "execute", ROOM, WRITE, "--no-task", "--task-file", "t.txt"])

    def test_capabilities_names_the_command(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            room_ops._main(["capabilities"])
        caps = json.loads(out.getvalue())
        self.assertIn("action", caps["commands"])
        self.assertEqual(caps["action"]["verbs"], ["describe", "execute", "read"])
        self.assertEqual(caps["action"]["execute_needs_one_of"], ["--task-file", "--no-task"])


if __name__ == "__main__":
    unittest.main()
