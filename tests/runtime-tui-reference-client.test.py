#!/usr/bin/env python3
"""Tests for the dumb reference-client TUI (architecture probe).

Contract (owner spec, taxonomy part 9): the client composes an instance view
from ONLY registry + manifest + a protocol probe over the instance's own
endpoint. The five states stay separate; a stopped instance shows
Registered/Stopped without any socket; a stale-status manifest is never
trusted as running; identity is verified over the socket.

This drives instance_view()/render_view() against a REAL daemon booted on a
tmp socket + registry (the same shape the E2E harness uses) so the probe path
is exercised end to end, not mocked.

Run: python3 tests/runtime-tui-reference-client.test.py
Exit: 0 on pass, 1 on fail.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "runtime-cli"))
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

import tui  # noqa: E402


def _wait_socket(path: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if Path(path).exists() and tui._socket_reachable(path):
            return True
        time.sleep(0.1)
    return False


class TuiReferenceClientTests(unittest.TestCase):
    def test_stopped_instance_view_needs_no_socket(self):
        m = {"identity": {"agent_id": "research-001"},
             "endpoint": {"type": "unix", "path": "/nonexistent/x.sock"},
             "status": "stopped"}
        v = tui.instance_view(m)
        self.assertEqual(v["existence"], "registered")
        self.assertEqual(v["server"], "stopped")
        self.assertEqual(v["core"], "unknown")
        self.assertIsNone(v["identityVerified"])
        self.assertEqual(v["desiredState"], "stopped")
        # render is string-only and shows the separate states
        r = tui.render_view(v)
        self.assertIn("Server:     stopped", r)
        self.assertIn("research-001", r)

    def test_stale_status_manifest_not_trusted_as_running(self):
        # manifest claims running but the socket is dead → view says stopped
        m = {"identity": {"agent_id": "ghost-001"},
             "endpoint": {"type": "unix", "path": "/nonexistent/y.sock"},
             "status": "running"}
        self.assertEqual(tui.instance_view(m)["server"], "stopped")

    def test_live_instance_view_verifies_identity_over_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            state = Path(tmp) / "state"
            state.mkdir(parents=True)
            sock = run / "rt.sock"
            env = {**os.environ,
                   "SUTANDO_RUN_DIR": str(run),  # hermetic: never the host's live instance.lock
                   "SUTANDO_RUNTIME_SOCKET": str(sock),
                   "SUTANDO_RUNTIME_DB": str(Path(tmp) / "rt.sqlite"),
                   "SUTANDO_HA_DIR": str(Path(tmp) / "ha"),
                   "SUTANDO_RUNTIME_STATE": str(state),
                   "SUTANDO_HOST_LABEL": "tui-host",
                   "SUTANDO_INSTANCE_REGISTRY": str(Path(tmp) / "instances"),
                   "SUTANDO_AGENT_ID": "@tui-agent:example.org"}
            (state / "cores").mkdir()
            (state / "cores" / "tui-host.alive").write_text(
                json.dumps({"host": "tui-host", "pid": 1}))
            (state / "core-status.json").write_text(
                json.dumps({"status": "running", "step": "tui-e2e"}))
            daemon = subprocess.Popen(
                [sys.executable, str(REPO / "src" / "runtime-api" / "server.py")],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                self.assertTrue(_wait_socket(str(sock)), "daemon socket")
                # discover through the registry the daemon wrote at boot
                os.environ["SUTANDO_INSTANCE_REGISTRY"] = str(Path(tmp) / "instances")
                import instance_registry
                # boot registration lands just after the socket — poll briefly
                mans = []
                deadline = time.time() + 10
                while not mans and time.time() < deadline:
                    mans = [m for m in instance_registry.list_instances()
                            if m.get("identity", {}).get("agent_id")
                            == "@tui-agent:example.org"]
                    if not mans:
                        time.sleep(0.1)
                self.assertEqual(len(mans), 1)
                v = tui.instance_view(mans[0])
                self.assertEqual(v["server"], "running")
                self.assertTrue(v["identityVerified"])
                self.assertEqual(v["core"], "running")
                self.assertEqual(v["health"], "healthy")
                self.assertEqual(v.get("activity"), "tui-e2e")
                self.assertIn("Identity:   verified", tui.render_view(v))
            finally:
                daemon.terminate()
                try:
                    daemon.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                os.environ.pop("SUTANDO_INSTANCE_REGISTRY", None)




class TuiBranchTests(unittest.TestCase):
    """Error-shape branches of the pure probe/render core."""

    def _serve_once(self, payload_fn):
        import socket as _s
        import tempfile as _tf
        import threading
        sock_path = _tf.mktemp(prefix="tuib", dir="/tmp")
        srv = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
        srv.bind(sock_path)
        srv.listen(1)

        def serve():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                data = conn.recv(65536)
                resp = payload_fn(data)
                if resp is not None:
                    conn.sendall(resp)
                conn.close()

        threading.Thread(target=serve, daemon=True).start()
        return sock_path, srv

    def test_rpc_error_response_raises(self):
        sock, srv = self._serve_once(lambda d: json.dumps(
            {"jsonrpc": "2.0", "id": "x",
             "error": {"message": "boom"}}).encode() + b"\n")
        try:
            with self.assertRaises(RuntimeError):
                tui._rpc_at(sock, "sutando.info", {})
        finally:
            srv.close()

    def test_early_close_yields_empty_buffer_error(self):
        sock, srv = self._serve_once(lambda d: None)  # close with no bytes
        try:
            with self.assertRaises((RuntimeError, ValueError)):
                tui._rpc_at(sock, "sutando.info", {})
        finally:
            srv.close()

    def test_identity_mismatch_marks_unverified(self):
        def payload(d):
            req = json.loads(d.decode().splitlines()[0])
            m = req.get("method")
            if m == "sutando.info":
                r = {"agentId": "@someone-else:x"}
            else:
                r = {"state": "online"}
            return (json.dumps({"jsonrpc": "2.0", "id": req["id"],
                                "result": r}).encode() + b"\n")
        sock, srv = self._serve_once(payload)
        try:
            v = tui.instance_view({
                "identity": {"agent_id": "@me:x"},
                "endpoint": {"path": sock}})
            self.assertFalse(v["identityVerified"])
        finally:
            srv.close()

    def test_sibling_instance_same_agent_marks_unverified(self):
        # same Stand, different installation: agentId matches but instanceId
        # does not — must fail closed (the action gate then refuses work)
        def payload(d):
            req = json.loads(d.decode().splitlines()[0])
            r = ({"agentId": "@me:x", "instanceId": "other-install"}
                 if req.get("method") == "sutando.info" else {"state": "online"})
            return (json.dumps({"jsonrpc": "2.0", "id": req["id"],
                                "result": r}).encode() + b"\n")
        sock, srv = self._serve_once(payload)
        try:
            v = tui.instance_view({
                "identity": {"agent_id": "@me:x"},
                "instance_id": "mine",
                "endpoint": {"path": sock}})
            self.assertEqual(v["instanceId"], "mine")
            self.assertFalse(v["identityVerified"])
        finally:
            srv.close()

    def test_views_lists_registry_manifests(self):
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            os.environ["SUTANDO_INSTANCE_REGISTRY"] = td
            try:
                tui.instance_registry.write_manifest(
                    "@vw:x", endpoint=str(Path(td) / "none.sock"))
                rows = tui._views()
                self.assertEqual(len(rows), 1)
                self.assertIn("_manifest", rows[0])
            finally:
                os.environ.pop("SUTANDO_INSTANCE_REGISTRY", None)




class TuiActionIdentityGate(unittest.TestCase):
    """The ACTION path (not just the rendered flag): a socket that answered as
    a different instance must never receive task/request/attach/open work."""

    def _drive(self, verified, keys):
        import tui as t
        view = {"agentId": "@x:1", "instanceId": "default", "server": "running",
                "core": "running", "health": "healthy", "endpoint": "/tmp/x.sock",
                "identityVerified": verified, "_manifest": {}}
        calls = []
        feed = iter(keys + ["q"])
        with mock.patch.object(t, "_views", lambda: [view]), \
             mock.patch.object(t, "_rpc_at",
                               lambda ep, m, p, **kw:
                               calls.append((ep, m, p)) or {}), \
             mock.patch.object(t.instance_registry, "attach",
                               lambda a, instance=None:
                               calls.append(("attach", a)) or
                               {"ok": False, "error": "nope"}), \
             mock.patch("builtins.input", lambda *_a: next(feed)):
            t.main([])
        return calls

    def test_mismatched_identity_blocks_all_four_actions(self):
        calls = self._drive(False, ["t @x:1 private-payload", "h @x:1",
                                    "a @x:1", "o @x:1"])
        self.assertEqual(calls, [], "work reached a socket that answered "
                                    "as a DIFFERENT instance")

    def test_unknown_identity_blocks_too(self):
        calls = self._drive(None, ["t @x:1 private-payload"])
        self.assertEqual(calls, [])

    def test_verified_identity_still_submits(self):
        calls = self._drive(True, ["t @x:1 hello"])
        self.assertIn(("/tmp/x.sock", "task.submit", {"task": "hello"}),
                      calls)


class _FakeInstance:
    """A minimal JSON-RPC peer that answers as ONE (agent, instance) identity
    and records the work it is asked to do."""

    def __init__(self, path: str, agent: str, instance: str):
        self.path, self.agent, self.instance = path, agent, instance
        self.received = []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(8)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,),
                             daemon=True).start()

    def _serve(self, conn):
        buf = b""
        while True:
            try:
                chunk = conn.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    req = json.loads(line)
                except ValueError:
                    continue
                conn.sendall((json.dumps(
                    {"jsonrpc": "2.0", "id": req.get("id"),
                     "result": self._answer(req)}) + "\n").encode())
        conn.close()

    def _answer(self, req):
        method = req.get("method")
        if method == "sutando.info":
            return {"agentId": self.agent, "instanceId": self.instance}
        if method == "runtime.health":
            return {"state": "online"}
        self.received.append((method, req.get("params")))
        return {"ok": True}

    def close(self):
        self.srv.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


class TuiSocketReplacementTests(unittest.TestCase):
    """The endpoint that answered while the view was built is not necessarily
    the endpoint that receives the command: the TUI blocks on input in
    between. A sibling runtime that rebinds the path must not inherit the
    earlier verification."""

    AGENT = "@same:x"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sock = str(Path(self.tmp.name) / "rt.sock")
        self.peers = []
        os.environ["SUTANDO_INSTANCE_REGISTRY"] = str(
            Path(self.tmp.name) / "instances")
        import instance_registry as reg
        reg.write_manifest(self.AGENT, instance="blue", endpoint=self.sock)
        self.blue = self._peer("blue")

    def tearDown(self):
        for p in self.peers:
            p.close()
        os.environ.pop("SUTANDO_INSTANCE_REGISTRY", None)
        self.tmp.cleanup()

    def _peer(self, instance):
        p = _FakeInstance(self.sock, self.AGENT, instance)
        self.peers.append(p)
        return p

    def _replace_with_red(self):
        self.blue.close()
        self.red = self._peer("red")

    def _drive(self, command, on_input=None):
        state = {"n": 0}

        def _input(*_a):
            state["n"] += 1
            if state["n"] == 1:
                if on_input:
                    on_input()
                return command
            return "q"

        with mock.patch("builtins.input", _input):
            tui.main([])

    def test_task_submit_does_not_reach_a_replaced_socket(self):
        self.red = None
        self._drive(f"t {self.AGENT}/blue private-for-blue",
                    on_input=self._replace_with_red)
        self.assertEqual(self.red.received, [],
                         "private task reached the sibling runtime that "
                         "rebound the endpoint after verification")
        self.assertEqual(self.blue.received, [])

    def test_request_list_does_not_read_from_a_replaced_socket(self):
        self.red = None
        self._drive(f"h {self.AGENT}/blue", on_input=self._replace_with_red)
        self.assertEqual(self.red.received, [],
                         "request history was read from the sibling runtime "
                         "that rebound the endpoint after verification")

    def test_unreplaced_socket_still_receives_the_task(self):
        self._drive(f"t {self.AGENT}/blue hello")
        self.assertEqual(self.blue.received,
                         [("task.submit", {"task": "hello"})])

    def test_unreplaced_socket_still_serves_request_list(self):
        self._drive(f"h {self.AGENT}/blue")
        self.assertEqual(self.blue.received, [("request.list", {})])


if __name__ == "__main__":
    unittest.main(verbosity=2)
