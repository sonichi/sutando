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

AGY_KEEPS = re.compile(r"[A-Za-z0-9_-]{1,64}")
# AG2 Space's tool names as the MCP server lists them (the proxy forwards them unchanged).
SERVER_TOOLS = ["ag2.whoami", "room.list", "room.inspect", "room.actions.search", "room.actions.describe",
                "room.action.read", "room.action.execute", "room.action.execute_safe", "room.task.list",
                "room.task.create", "room.task.update", "operation.inspect", "approval.inspect", "space.action.read",
                "agent.link", "relay.token.issue", "ui_describe", "ui_request", "ui_result"]

FAKE_SERVER = textwrap.dedent("""
    import json, sys
    names = json.loads(sys.argv[1])
    for line in sys.stdin:
        m = json.loads(line)
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

    def __init__(self, names=SERVER_TOOLS):
        self.dir = tempfile.TemporaryDirectory()
        server = os.path.join(self.dir.name, "server.py")
        Path(server).write_text(FAKE_SERVER)
        self.p = subprocess.Popen([sys.executable, "-m", "ag2_sparrow.mcp_name_shim", "--", sys.executable, server,
                                   json.dumps(names)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                  bufsize=1, cwd=str(PKG_ROOT), env=hermetic_env())

    def ask(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def close(self):
        self.p.stdin.close()
        rc = self.p.wait(30)
        self.p.stdout.close()
        self.dir.cleanup()
        return rc


class ShimTest(unittest.TestCase):
    def test_without_the_shim_agy_keeps_only_the_ui_tools(self):
        self.assertEqual([n for n in SERVER_TOOLS if AGY_KEEPS.fullmatch(n)], ["ui_describe", "ui_request", "ui_result"])

    def test_through_the_shim_agy_keeps_every_tool_and_calls_reach_the_server_by_its_name(self):
        sess = Session()
        self.assertEqual(sess.ask({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})["id"], 1)
        listed = [t["name"] for t in sess.ask({"jsonrpc": "2.0", "id": "l", "method": "tools/list"})["result"]["tools"]]
        called = [sess.ask({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": n}})
                  ["result"]["content"][0]["text"] for i, n in enumerate(["room_action_execute", "ui_request", "ag2.whoami"])]
        self.assertEqual(sess.close(), 0)
        self.assertEqual(listed[:3], ["ag2_whoami", "room_list", "room_inspect"])
        self.assertEqual(listed, [n for n in listed if AGY_KEEPS.fullmatch(n)], "agy keeps them all")
        self.assertEqual(len(set(listed)), len(SERVER_TOOLS))
        self.assertEqual(called, ["room.action.execute", "ui_request", "ag2.whoami"])

    def test_main_in_process_relays_until_the_client_closes(self):
        with tempfile.TemporaryDirectory() as d:
            server = os.path.join(d, "server.py")
            Path(server).write_text(FAKE_SERVER)
            out = io.StringIO()
            rc = shim.main(["--", sys.executable, server, json.dumps(["room.list"])],
                           stdin=io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"),
                           stdout=out)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["result"]["tools"][0]["name"], "room_list")

    def test_the_policy_grants_the_aliases_it_granted_by_server_name(self):
        for name in SERVER_TOOLS:
            self.assertEqual(mcp_auth.owner_tool_ok("ag2-space", shim.alias(name)), mcp_auth.owner_tool_ok("ag2-space", name),
                             name)

    def test_colliding_and_overlong_names_get_distinct_safe_aliases(self):
        names = shim.Names()
        tools = [{"name": "a.b"}, {"name": "a_b"}, {"name": "x" * 70 + ".y"}, {"name": 3}, {}]
        names.rename_tools(tools)
        got = [t.get("name") for t in tools]
        self.assertEqual(got[:2], ["a_b_2", "a_b"] if got[0] != "a_b" else ["a_b", "a_b_2"])
        self.assertTrue(AGY_KEEPS.fullmatch(got[2]))
        self.assertEqual(got[3:], [3, None], "a malformed entry is left as it came")
        self.assertEqual({names.server_name(got[0]), names.server_name(got[1])}, {"a.b", "a_b"})

    def test_an_alias_keeps_its_server_tool_across_pages_and_later_lists(self):
        names = shim.Names()
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

    def test_lines_it_does_not_understand_pass_through(self):
        names, pending = shim.Names(), set()
        for line in ("not json\n", "[1, 2]\n", '"x"\n', '{"jsonrpc": "2.0", "method": "notifications/initialized"}\n'):
            self.assertEqual(shim.to_server(line, names, pending), line)
            self.assertEqual(shim.to_client(line, names, pending), line)
        batch = shim.to_server(json.dumps([{"jsonrpc": "2.0", "id": 9, "method": "tools/list"}]) + "\n", names, pending)
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
            with unittest.mock.patch.dict(os.environ, env):
                self.assertEqual(shim.main(["/no/such/program"], stdin=io.StringIO(""), stdout=io.StringIO()), 127)
                self.assertEqual(json.loads(Path(status).read_text()), {"state": "exited", "code": 127, "turn": "t1"})
                server = os.path.join(d, "server.py")
                Path(server).write_text(FAKE_SERVER)
                shim.main([sys.executable, server, json.dumps(["room.list", "ui_request"])], stdout=io.StringIO(),
                          stdin=io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"))
                self.assertEqual(json.loads(Path(status).read_text()), {"state": "listed", "tools": 2, "turn": "t1"})
        shim.Status({}).write(state="started")   # no status path: nothing written, nothing raised

    def test_no_command_is_a_usage_error_and_the_module_runs_as_a_script(self):
        self.assertEqual(shim.main([], stdin=io.StringIO(""), stdout=io.StringIO()), 2)
        r = subprocess.run([sys.executable, "-m", "ag2_sparrow.mcp_name_shim", sys.executable, "-c", "pass"],
                           capture_output=True, text=True, timeout=60, cwd=str(PKG_ROOT), input="",
                           env=hermetic_env())
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
