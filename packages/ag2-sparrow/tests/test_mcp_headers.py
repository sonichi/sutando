"""`ag2-sparrow-mcp`: the agent's own AG2 Space MCP credential, for the agent's MCP client.

A Sparrow-run agent holds its relay token and nothing else. Core exchanges that runtime
bearer for a 15-minute MCP access token as the agent itself (the same exchange the
desktop's and the cloud pod's MCP proxy make), so the agent's Claude Code can call Room
Commons Actions with `room.action.execute`. `headers` prints that token as the JSON object
Claude Code's `headersHelper` reads, minting fresh on every run; `setup` prints the one
`claude mcp add-json` line that wires it in.

The bearer goes only to the relay's discovery route and to the mint URL discovery names;
it never reaches stdout, stderr or the MCP server, and a mint URL that is not https (or
plain http to loopback) is refused before the bearer is sent.

Run: python3 packages/ag2-sparrow/tests/test_mcp_headers.py
"""
import http.server
import io
import json
import os
import shlex
import pathlib
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout

sys.path.append(str(pathlib.Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import hermetic_env, isolate_os_environ  # noqa: E402

isolate_os_environ()
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-mcp-headers-")
_FAKE_VI = types.ModuleType("vault_intercept")

_FAKE_VI.get_vault_key = {}.__getitem__  # an empty vault: KeyError, never the Keychain
sys.modules["vault_intercept"] = _FAKE_VI

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT))

from ag2_sparrow import mcp_auth  # noqa: E402

SECRET = "relay-secret-xyz"
ACCESS = "access-token-abc"


class Stub:
    """The relay's discovery route and core-api's mint route on one loopback port."""

    def __init__(self):
        self.seen = []
        self.discovery = None
        self.mint = (200, {"access_token": ACCESS, "expires_in": 900})
        self.redirect_to = None
        stub = self

        # Runs on the server's thread, which this repo's coverage config does not trace.
        class H(http.server.BaseHTTPRequestHandler):  # pragma: no cover
            def log_message(self, *a):
                pass

            def _send(self, status, body):
                if stub.redirect_to:
                    self.send_response(302)
                    self.send_header("location", stub.redirect_to)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                raw = b"<html>bad gateway</html>" if body is None else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                stub.seen.append(("GET", self.path, self.headers.get("authorization"),
                                  self.headers.get("user-agent")))
                if self.path == "/relay/v1/mcp/discovery":
                    self._send(*stub.discovery_reply())
                else:
                    self._send(404, {"error": "nope"})

            def do_POST(self):
                stub.seen.append(("POST", self.path, self.headers.get("authorization"),
                                  self.headers.get("user-agent")))
                self._send(*stub.mint)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def discovery_reply(self):
        if self.discovery is not None:
            return self.discovery
        return 200, {"mint_url": f"{self.base}/api/v1/mcp/agent-access-tokens",
                     "mcp_url": f"{self.base}/mcp"}

    @property
    def relay(self):
        return f"{self.base}/relay"

    def close(self):
        self.srv.shutdown()


def _run_main(argv, gateway):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = mcp_auth.main(argv, resolve=lambda: gateway)
    return rc, out.getvalue(), err.getvalue()


class HeadersTest(unittest.TestCase):
    def setUp(self):
        self.stub = Stub()
        self.addCleanup(self.stub.close)

    def test_prints_the_agents_access_token_as_an_authorization_header(self):
        rc, out, err = _run_main(["headers"], (self.stub.relay, SECRET, ""))
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), {"Authorization": f"Bearer {ACCESS}"})
        self.assertEqual([(m, p) for m, p, _, _ in self.stub.seen],
                         [("GET", "/relay/v1/mcp/discovery"),
                          ("POST", "/api/v1/mcp/agent-access-tokens")])
        self.assertTrue(all(a == f"Bearer {SECRET}" for _, _, a, _ in self.stub.seen))
        # Cloudflare answers a request with no User-Agent with a 403 that reads as an auth failure.
        self.assertTrue(all(ua for _, _, _, ua in self.stub.seen))
        self.assertNotIn(SECRET, out + err)

    def test_mint_url_that_is_not_tls_is_refused_before_the_bearer_is_sent(self):
        self.stub.discovery = (200, {"mint_url": "http://mint.example/api/v1/mcp/agent-access-tokens",
                                     "mcp_url": "https://mcp.example/mcp"})
        rc, out, err = _run_main(["headers"], (self.stub.relay, SECRET, ""))
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("mint_url", err)
        self.assertEqual([m for m, *_ in self.stub.seen], ["GET"])

    def test_unconfigured_discovery_fails_with_nothing_on_stdout(self):
        self.stub.discovery = (404, {"error": "mcp discovery not configured"})
        rc, out, err = _run_main(["headers"], (self.stub.relay, SECRET, ""))
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("404", err)

    def test_a_refused_bearer_names_the_code(self):
        self.stub.mint = (401, {"code": "AGENT_CREDENTIAL_REVOKED"})
        rc, out, err = _run_main(["headers"], (self.stub.relay, SECRET, ""))
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("AGENT_CREDENTIAL_REVOKED", err)
        self.assertNotIn(SECRET, err)

    def test_no_token_fails_without_a_request(self):
        rc, out, err = _run_main(["headers"], ("", "", ""))
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("REMOTE_TASK_TOKEN", err)
        self.assertEqual(self.stub.seen, [])


class EdgeTest(unittest.TestCase):
    def setUp(self):
        self.stub = Stub()
        self.addCleanup(self.stub.close)

    def test_discovery_without_mcp_url_is_refused(self):
        self.stub.discovery = (200, {"mint_url": f"{self.stub.base}/mint"})
        rc, out, err = _run_main(["headers"], (self.stub.relay, SECRET, ""))
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("without mcp_url", err)

    def test_https_urls_pass_the_check(self):
        url = "https://mcp.example/mcp"
        self.assertEqual(mcp_auth._safe_url("mcp_url", url), url)

    def test_a_mint_answer_that_is_not_json_names_the_status(self):
        self.stub.mint = (502, None)
        rc, out, err = _run_main(["headers"], (self.stub.relay, SECRET, ""))
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("502", err)

    def test_an_unreachable_relay_fails_cleanly(self):
        dead = Stub()
        dead.close()
        dead.srv.server_close()
        rc, out, err = _run_main(["headers"], (dead.relay, SECRET, ""))
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("/v1/mcp/discovery", err)
        self.assertNotIn(SECRET, err)


class RedirectTest(unittest.TestCase):
    """A 3xx would carry the bearer to its Location, past the https/loopback check."""

    def setUp(self):
        self.stub, self.elsewhere = Stub(), Stub()
        self.addCleanup(self.stub.close)
        self.addCleanup(self.elsewhere.close)
        self.stub.redirect_to = f"{self.elsewhere.base}/stolen"

    def test_a_redirected_discovery_is_an_error_and_is_not_followed(self):
        rc, out, err = _run_main(["headers"], (self.stub.relay, SECRET, ""))
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("302", err)
        self.assertEqual([m for m, *_ in self.stub.seen], ["GET"])
        self.assertEqual(self.elsewhere.seen, [])

    def test_a_redirected_mint_is_an_error_and_is_not_followed(self):
        mint_url = f"{self.stub.base}/api/v1/mcp/agent-access-tokens"
        with self.assertRaisesRegex(mcp_auth.McpAuthError, "302"):
            mcp_auth.mint(mint_url, SECRET)
        self.assertEqual([m for m, *_ in self.stub.seen], ["POST"])
        self.assertEqual(self.elsewhere.seen, [])


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.stub = Stub()
        self.addCleanup(self.stub.close)

    def test_prints_the_claude_mcp_add_json_line_for_the_discovered_server(self):
        rc, out, err = _run_main(["setup"], (self.stub.relay, SECRET, "/home/a/.ag2/.env"))
        self.assertEqual(rc, 0, err)
        line = [ln for ln in out.splitlines() if ln.startswith("claude mcp add-json")]
        self.assertEqual(len(line), 1, out)
        cfg = json.loads(shlex.split(line[0])[-1])
        self.assertEqual(cfg["type"], "http")
        self.assertEqual(cfg["url"], f"{self.stub.base}/mcp")
        # The helper reads the token from the file Sparrow read, not from Claude Code's env.
        self.assertIn("AG2_DEVICE_ENV=/home/a/.ag2/.env", cfg["headersHelper"])
        self.assertTrue(cfg["headersHelper"].endswith(" headers"))
        self.assertNotIn(SECRET, out + err)
        self.assertEqual([m for m, *_ in self.stub.seen], ["GET"])

    def test_env_only_token_says_the_client_needs_it_too(self):
        rc, out, err = _run_main(["setup"], (self.stub.relay, SECRET, ""))
        self.assertEqual(rc, 0, err)
        self.assertNotIn("AG2_DEVICE_ENV=", out)
        self.assertIn("REMOTE_TASK_TOKEN", out + err)
        self.assertNotIn(SECRET, out + err)


class HelperCommandTest(unittest.TestCase):
    def test_from_the_zipapp_the_helper_puts_the_pyz_on_pythonpath(self):
        pyz = os.path.join(tempfile.mkdtemp(prefix="mcp-pyz-"), "ag2-sparrow.pyz")
        open(pyz, "w").close()
        with mock.patch.object(mcp_auth, "__file__", os.path.join(pyz, "ag2_sparrow", "mcp_auth.py")):
            cmd = mcp_auth._helper_command("")
        self.assertIn(f"PYTHONPATH={os.path.realpath(pyz)} ", cmd)
        self.assertNotIn("AG2_DEVICE_ENV", cmd)

    def test_an_installed_package_needs_no_pythonpath(self):
        self.assertNotIn("PYTHONPATH", mcp_auth._helper_command("/e/.env"))


class ResolveTest(unittest.TestCase):
    def test_reads_the_bridges_own_resolution(self):
        relay = "https://gw.example/relay"
        with mock.patch.dict(os.environ, {"REMOTE_TASK_TOKEN": f"{relay}|{SECRET}"}):
            sys.modules.pop("ag2_sparrow.remote_gateway_bridge", None)
            self.assertEqual(mcp_auth.resolve_gateway(), (relay, SECRET, ""))


class EntryPointTest(unittest.TestCase):
    """The shipped module resolves the token exactly as the bridge does: env, then device .env."""

    def setUp(self):
        self.stub = Stub()
        self.addCleanup(self.stub.close)

    def _child(self, **env):
        return subprocess.run(
            [sys.executable, "-m", "ag2_sparrow.mcp_auth", "headers"],
            capture_output=True, text=True, timeout=60,
            env=hermetic_env(PYTHONPATH=str(PKG_ROOT), **env))

    def test_token_from_the_environment(self):
        r = self._child(REMOTE_TASK_TOKEN=f"{self.stub.relay}|{SECRET}")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"Authorization": f"Bearer {ACCESS}"})
        self.assertNotIn(SECRET, r.stdout + r.stderr)

    def test_token_from_the_device_env_file(self):
        d = tempfile.mkdtemp(prefix="mcp-headers-")
        env_file = os.path.join(d, ".env")
        with open(env_file, "w") as fh:
            fh.write(f"REMOTE_TASK_TOKEN='{self.stub.relay}|{SECRET}'\n")
        os.chmod(env_file, 0o600)
        r = self._child(AG2_DEVICE_ENV=env_file)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"Authorization": f"Bearer {ACCESS}"})
        self.assertNotIn(SECRET, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
