#!/usr/bin/env python3
"""`mcp_name_shim`: AG2 Space's MCP tools under names Antigravity CLI keeps.

Live, 2026-11-21: agy, given the desktop's ag2-mcp-proxy over stdio, offered only `ui_describe`, `ui_request`
and `ui_result`: the three AG2 Space tools whose names have no dot (`ag2.whoami`, whose schema is empty, was
dropped too, so the name is what decides). `AGY_KEEPS` models that rule; the shim's names all pass it and a call
by alias reaches the server under its own name.

Run: python3 packages/ag2-sparrow/tests/test_mcp_name_shim.py
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import hermetic_env, isolate_os_environ  # noqa: E402

isolate_os_environ()

PKG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT))
from ag2_sparrow import mcp_auth, mcp_name_shim as shim  # noqa: E402
from ag2_sparrow.adapters.antigravity import antigravity_connector as gc  # noqa: E402

AGY_KEEPS = re.compile(r"[A-Za-z0-9_-]{1,64}")
# AG2 Space's tool names as the MCP server lists them (the proxy forwards them unchanged).
SERVER_TOOLS = ["ag2.whoami", "room.list", "room.inspect", "room.actions.search", "room.actions.describe",
                "room.action.read", "room.action.execute", "room.action.execute_safe", "room.task.list",
                "room.task.create", "room.task.update", "operation.inspect", "approval.inspect", "space.action.read",
                "agent.link", "agent.unlink", "relay.token.issue", "space.action.execute", "ui_describe", "ui_request",
                "ui_result"]
NEVER = ["relay.token.issue", "agent.link", "agent.unlink", "space.action.execute"]
OWNER_GRANT = {shim.TOOLS_ENV: ",".join(sorted(mcp_auth.OWNER_TOOLS))}

FAKE_SERVER = textwrap.dedent("""
    import json, sys
    names = json.loads(sys.argv[1])
    seen = open(sys.argv[2], "a") if len(sys.argv) > 2 else None
    for line in sys.stdin:
        m = json.loads(line)
        if seen:
            seen.write(line)
            seen.flush()
        if m.get("method") == "tools/list":
            tools = [{"name": n, "inputSchema": {"type": "object"}} for n in names]
            out = {"jsonrpc": "2.0", "id": m["id"], "result": {"tools": tools}}
        elif m.get("method") == "tools/call":
            out = {"jsonrpc": "2.0", "id": m["id"], "result": {"content": [{"type": "text", "text": m["params"]["name"]}]}}
        elif "id" in m:
            out = {"jsonrpc": "2.0", "id": m["id"], "result": {}}
        else:
            continue
        print(json.dumps(out), flush=True)
""")


class Session:
    """The shim over the fake server, driven as a client does: one request, then its answer."""

    def __init__(self, names=SERVER_TOOLS, grant=OWNER_GRANT, launcher=None):
        self.dir = tempfile.TemporaryDirectory()
        server = os.path.join(self.dir.name, "server.py")
        self.seen = os.path.join(self.dir.name, "seen.jsonl")
        Path(server).write_text(FAKE_SERVER)
        head = [launcher] if launcher else [sys.executable, "-m", "ag2_sparrow.mcp_name_shim", "--"]
        self.p = subprocess.Popen(head + [sys.executable, server, json.dumps(names), self.seen],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                  bufsize=1, cwd=str(PKG_ROOT), env=hermetic_env(**grant))

    def received(self):
        """The methods and tool names the server got."""
        if not os.path.exists(self.seen):
            return []
        msgs = [json.loads(line) for line in Path(self.seen).read_text().splitlines()]
        return [(m.get("method"), (m.get("params") or {}).get("name")) for m in msgs]

    def ask(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def close(self):
        self.p.stdin.close()
        rc = self.p.wait(30)
        self.p.stdout.close()
        self.got = self.received()
        self.dir.cleanup()
        return rc


class ShimTest(unittest.TestCase):
    def test_without_the_shim_agy_keeps_only_the_ui_tools(self):
        self.assertEqual([n for n in SERVER_TOOLS if AGY_KEEPS.fullmatch(n)], ["ui_describe", "ui_request", "ui_result"])

    def test_through_the_shim_agy_keeps_every_granted_tool_and_calls_reach_the_server_by_its_name(self):
        sess = Session()
        self.assertEqual(sess.ask({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})["id"], 1)
        listed = [t["name"] for t in sess.ask({"jsonrpc": "2.0", "id": "l", "method": "tools/list"})["result"]["tools"]]
        called = [sess.ask({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": n}})
                  ["result"]["content"][0]["text"] for i, n in enumerate(["room_action_execute", "ag2.whoami"])]
        self.assertEqual(sess.close(), 0)
        self.assertEqual(listed[:3], ["ag2_whoami", "room_list", "room_inspect"])
        self.assertEqual(listed, [n for n in listed if AGY_KEEPS.fullmatch(n)], "agy keeps them all")
        self.assertEqual(sorted(listed), sorted(shim.alias(n) for n in mcp_auth.OWNER_TOOLS))
        self.assertEqual(called, ["room.action.execute", "ag2.whoami"])

    def test_a_team_turns_shim_never_lists_or_forwards_an_owner_only_tool(self):
        # Regression: agy auto-allowing MCP never asks the policy, so the shim must refuse these itself.
        team = gc.sspace_grant({"access_tier": "team", "collaborator": "true"})
        self.assertEqual(team, gc.sspace_grant({"access_tier": "owner"}), "owner and team share one grant")
        with tempfile.TemporaryDirectory() as state:
            sess = Session(launcher=gc.write_launcher(state, "t", team))
            listed = [t["name"] for t in sess.ask({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
            asks = [shim.alias(n) for n in NEVER] + NEVER + ["ui_request", "room_action_execute_2"]
            answers = [sess.ask({"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": {"name": n}}) for n in asks]
            bad = sess.ask({"jsonrpc": "2.0", "id": "m", "method": "tools/call", "params": ["relay_token_issue"]})
            ok = sess.ask({"jsonrpc": "2.0", "id": "ok", "method": "tools/call", "params": {"name": "room_action_execute"}})
            self.assertEqual(sess.close(), 0)
        for n in NEVER:
            self.assertNotIn(shim.alias(n), listed)
            self.assertNotIn(n, listed)
        self.assertNotIn("ui_request", listed)
        self.assertIn("room_action_execute", listed)
        for ask, answer in zip(asks, answers):
            self.assertEqual((answer["id"], answer["error"]["code"]), (ask, shim.NOT_GRANTED), ask)
        self.assertEqual(bad["error"]["code"], shim.NOT_GRANTED, "a malformed call is refused")
        self.assertEqual(ok["result"]["content"][0]["text"], "room.action.execute")
        self.assertEqual([c for c in sess.got if c[0] == "tools/call"], [("tools/call", "room.action.execute")],
                         "no refused call reached the server")

    def test_without_a_grant_the_shim_lists_and_forwards_nothing(self):
        self.assertEqual(gc.sspace_grant({"access_tier": "guest"}), frozenset())
        sess = Session(grant={})
        listed = sess.ask({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        answer = sess.ask({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "ag2_whoami"}})
        self.assertEqual(sess.close(), 0)
        self.assertEqual((listed, answer["error"]["code"]), ([], shim.NOT_GRANTED))
        self.assertNotIn("tools/call", [m for m, _ in sess.got])

    def test_a_batch_forwards_its_granted_calls_and_answers_the_rest(self):
        names, pending = shim.Names({"room.list"}), set()
        names.rename_tools([{"name": "room.list"}])
        batch = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "room_list"}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "relay_token_issue"}},
                 {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "agent_link"}}]
        forward, reply = shim.to_server(json.dumps(batch) + "\n", names, pending)
        self.assertEqual([m["params"]["name"] for m in json.loads(forward)], ["room.list"])
        self.assertEqual([m["id"] for m in json.loads(reply)], [2], "a refused notification gets no answer")
        self.assertEqual(shim.to_server(json.dumps(batch[2]) + "\n", names, pending), (None, None))

    def test_main_in_process_relays_until_the_client_closes(self):
        with tempfile.TemporaryDirectory() as d:
            server = os.path.join(d, "server.py")
            Path(server).write_text(FAKE_SERVER)
            out = io.StringIO()
            calls = "".join(json.dumps({"jsonrpc": "2.0", "id": i, "method": m, "params": {"name": "agent_link"}}) + "\n"
                            for i, m in ((1, "tools/list"), (2, "tools/call")))
            with unittest.mock.patch.dict(os.environ, {shim.TOOLS_ENV: "room.list"}):
                rc = shim.main(["--", sys.executable, server, json.dumps(["room.list"])], stdin=io.StringIO(calls),
                               stdout=out)
        self.assertEqual(rc, 0)
        lines = sorted((json.loads(line) for line in out.getvalue().splitlines()), key=lambda m: m["id"])
        self.assertEqual(lines[0]["result"]["tools"][0]["name"], "room_list")
        self.assertEqual(lines[1]["error"]["code"], shim.NOT_GRANTED)

    def test_the_policy_grants_the_aliases_it_granted_by_server_name(self):
        for name in SERVER_TOOLS:
            self.assertEqual(mcp_auth.owner_tool_ok("ag2-space", shim.alias(name)), mcp_auth.owner_tool_ok("ag2-space", name),
                             name)

    def test_colliding_and_overlong_names_get_distinct_safe_aliases(self):
        names = shim.Names({"a.b", "a_b", "x" * 70 + ".y"})
        tools = [{"name": "a.b"}, {"name": "a_b"}, {"name": "x" * 70 + ".y"}, {"name": 3}, {}, "z", {"name": "c.d"}]
        names.rename_tools(tools)
        got = [t.get("name") for t in tools]
        self.assertEqual(got[:2], ["a_b_2", "a_b"] if got[0] != "a_b" else ["a_b", "a_b_2"])
        self.assertTrue(AGY_KEEPS.fullmatch(got[2]))
        self.assertEqual(len(got), 3, "a malformed or ungranted entry is dropped")
        self.assertEqual({names.server_name(got[0]), names.server_name(got[1])}, {"a.b", "a_b"})

    def test_an_alias_keeps_its_server_tool_across_pages_and_later_lists(self):
        names = shim.Names({"a.b", "a_b"})
        first = [{"name": "a.b"}]
        names.rename_tools(first)
        later = [{"name": "a_b"}]   # another page, or a refresh, lists a tool with the same alias
        names.rename_tools(later)
        self.assertEqual((first[0]["name"], later[0]["name"]), ("a_b", "a_b_2"))
        self.assertEqual((names.server_name("a_b"), names.server_name("a_b_2")), ("a.b", "a_b"),
                         "the alias the policy granted still forwards to the tool it was granted for")
        again = [{"name": "a_b"}, {"name": "a.b"}]
        names.rename_tools(again)
        self.assertEqual([t["name"] for t in again], ["a_b_2", "a_b"])

    def test_every_alias_fits_agys_prefixed_name_rule(self):
        for name in SERVER_TOOLS + ["x." * 40, "a" * 80]:
            self.assertRegex("mcp_ag2-space_" + shim.alias(name), r"^[a-zA-Z0-9_-]{1,64}$", name)
        self.assertEqual(shim.alias("a" * 50), "a" * 50)

    def test_client_lines_that_are_not_json_rpc_messages_are_answered_and_never_forwarded(self):
        names, pending = shim.Names({"room.list"}), set()
        for line, code in (("not json\n", shim.PARSE_ERROR), ('"x"\n', shim.INVALID_REQUEST), ("[]\n", shim.INVALID_REQUEST),
                           ('{"jsonrpc": "2.0", "id": 4}\n', shim.INVALID_REQUEST)):
            forward, reply = shim.to_server(line, names, pending)
            self.assertIsNone(forward, line)
            self.assertEqual(json.loads(reply)["error"]["code"], code, line)
        forward, reply = shim.to_server("[1, 2]\n", names, pending)
        self.assertEqual((forward, [e["error"]["code"] for e in json.loads(reply)]), (None, [shim.INVALID_REQUEST] * 2))
        for line in ("not json\n", "[1, 2]\n", '"x"\n'):
            self.assertEqual(shim.to_client(line, names, pending), line, "server lines pass through")

    def test_only_the_allowed_methods_reach_the_server(self):
        names, pending = shim.Names({"room.list"}), set()
        for line in ('{"jsonrpc": "2.0", "method": "notifications/initialized"}\n',
                     '{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n',
                     '{"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}}\n',
                     '{"jsonrpc": "2.0", "id": "s1", "result": {}}\n'):
            self.assertEqual(shim.to_server(line, names, pending), (line, None), line)
        for method in ("events/subscribe", "resources/read", "sampling/createMessage", 7):
            forward, reply = shim.to_server(json.dumps({"jsonrpc": "2.0", "id": 3, "method": method}) + "\n", names, pending)
            self.assertEqual((forward, json.loads(reply)["error"]["code"]), (None, shim.NOT_GRANTED), method)
            self.assertEqual(shim.to_server(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n", names, pending),
                             (None, None), "a notification of another method is dropped")

    def test_events_subscribe_and_a_garbage_line_never_reach_the_server_and_allowed_methods_still_work(self):
        sess = Session()
        init = sess.ask({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        sub = sess.ask({"jsonrpc": "2.0", "id": 2, "method": "events/subscribe", "params": {"name": "agent.trigger"}})
        sess.p.stdin.write("{not json\n")
        sess.p.stdin.flush()
        garbage = json.loads(sess.p.stdout.readline())
        sess.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        ping = sess.ask({"jsonrpc": "2.0", "id": 3, "method": "ping"})
        listed = sess.ask({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})["result"]["tools"]
        call = sess.ask({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "room_list"}})
        self.assertEqual(sess.close(), 0)
        self.assertEqual((init["id"], ping["id"], call["result"]["content"][0]["text"]), (1, 3, "room.list"))
        self.assertIn("room_list", [t["name"] for t in listed])
        self.assertEqual((sub["id"], sub["error"]["code"]), (2, shim.NOT_GRANTED))
        self.assertEqual((garbage["id"], garbage["error"]["code"]), (None, shim.PARSE_ERROR))
        self.assertEqual([m for m, _ in sess.got], ["initialize", "notifications/initialized", "ping", "tools/list",
                                                    "tools/call"], "nothing else reached the server")

    def test_a_listed_batch_is_remembered_and_renamed(self):
        names, pending = shim.Names({"room.list"}), set()
        batch, _ = shim.to_server(json.dumps([{"jsonrpc": "2.0", "id": 9, "method": "tools/list"}]) + "\n", names, pending)
        self.assertEqual(pending, {"9"})
        answer = json.dumps([{"jsonrpc": "2.0", "id": 9, "result": {"tools": [{"name": "room.list"}]}}]) + "\n"
        self.assertEqual(json.loads(shim.to_client(answer, names, pending))[0]["result"]["tools"][0]["name"], "room_list")
        self.assertTrue(batch.endswith("\n"))

    def test_a_server_that_exits_ends_the_relay(self):
        many = "".join(json.dumps({"jsonrpc": "2.0", "id": i, "method": "ping"}) + "\n" for i in range(5000))
        rc = shim.main([sys.executable, "-c", "raise SystemExit(3)"], stdin=io.StringIO(many), stdout=io.StringIO())
        self.assertEqual(rc, 3)

    def test_the_status_file_carries_this_turns_id_and_how_far_it_got(self):
        with tempfile.TemporaryDirectory() as d:
            status = os.path.join(d, "status.json")
            env = {shim.STATUS_ENV: status, shim.TURN_ENV: "t1"}
            with unittest.mock.patch.dict(os.environ, dict(env, **{shim.TOOLS_ENV: "room.list,ui_request"})):
                self.assertEqual(shim.main(["/no/such/program"], stdin=io.StringIO(""), stdout=io.StringIO()), 127)
                self.assertEqual(json.loads(Path(status).read_text()), {"state": "exited", "code": 127, "turn": "t1"})
                server = os.path.join(d, "server.py")
                Path(server).write_text(FAKE_SERVER)
                shim.main([sys.executable, server, json.dumps(["room.list", "ui_request"])], stdout=io.StringIO(),
                          stdin=io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"))
                self.assertEqual(json.loads(Path(status).read_text()), {"state": "listed", "tools": 2, "granted": 2, "turn": "t1"})
        shim.Status({}).write(state="started")   # no status path: nothing written, nothing raised

    def test_no_command_is_a_usage_error_and_the_module_runs_as_a_script(self):
        self.assertEqual(shim.main([], stdin=io.StringIO(""), stdout=io.StringIO()), 2)
        r = subprocess.run([sys.executable, "-m", "ag2_sparrow.mcp_name_shim", sys.executable, "-c", "pass"],
                           capture_output=True, text=True, timeout=60, cwd=str(PKG_ROOT), input="",
                           env=hermetic_env())
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
