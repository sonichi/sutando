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
import hashlib
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

    def test_acp_prints_the_agents_http_mcp_server_for_an_acp_session(self):
        rc, out, err = _run_main(["acp"], (self.stub.relay, SECRET, ""))
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), {"type": "http", "name": "ag2-space", "url": f"{self.stub.base}/mcp",
                                           "headers": [{"name": "Authorization", "value": f"Bearer {ACCESS}"}]})
        self.assertNotIn(SECRET, out + err)
        self.assertEqual([p for _, p, _, _ in self.stub.seen],
                         ["/relay/v1/mcp/discovery", "/api/v1/mcp/agent-access-tokens"])

    def _env(self, text, name=".env"):
        path = os.path.join(tempfile.mkdtemp(prefix="mcp-desc-"), name)
        with open(path, "w") as fh:
            fh.write(text)
        os.chmod(path, 0o600)
        return path

    def _descriptor(self, env_file):
        return {"version": 1, "lane": "primary", "env_file": env_file, "env_key": "REMOTE_TASK_TOKEN",
                "mint_url": f"{self.stub.base}/api/v1/mcp/agent-access-tokens", "mcp_url": f"{self.stub.base}/mcp",
                "relay": self.stub.relay, "bearer_digest": hashlib.sha256(SECRET.encode()).hexdigest()}

    def test_descriptor_names_the_agents_token_file_for_the_stdio_proxy_and_no_secret(self):
        env_file = self._env(f"AGENT_MXID=@a:s.space\nREMOTE_TASK_TOKEN='{self.stub.relay}|{SECRET}'\n")
        rc, out, err = _run_main(["descriptor", "--env-file", env_file], None)
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), self._descriptor(env_file))
        self.assertNotIn(SECRET, out + err)
        self.assertEqual([(p, a) for _, p, a, _ in self.stub.seen], [("/relay/v1/mcp/discovery", f"Bearer {SECRET}")],
                         "nothing is minted; discovery runs with the file's own token")

    def test_descriptor_uses_the_named_file_even_when_the_environment_carries_another_token(self):
        # The live failure (2026-11-21): the bridge's resolution won from elsewhere and the file was never named.
        env_file = self._env(f"REMOTE_TASK_TOKEN={self.stub.relay}|{SECRET}\n")
        r = subprocess.run([sys.executable, "-m", "ag2_sparrow.mcp_auth", "descriptor", "--env-file", env_file],
                           capture_output=True, text=True, timeout=60,
                           env=hermetic_env(PYTHONPATH=str(PKG_ROOT),
                                            REMOTE_TASK_TOKEN=f"{self.stub.relay}|env-secret-other"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), self._descriptor(env_file))
        self.assertTrue(all(a == f"Bearer {SECRET}" for _, _, a, _ in self.stub.seen), "never the env's token")
        self.assertNotIn("env-secret-other", r.stdout + r.stderr)

    def _seat(self, token_line, extra=""):
        d = tempfile.mkdtemp(prefix="mcp-seat-")
        chan = os.path.join(d, "channels", "ag2space")
        os.makedirs(chan)
        token = os.path.join(chan, "token")
        with open(token, "w") as fh:
            fh.write(token_line)
        os.chmod(token, 0o600)
        env = os.path.join(chan, ".env")
        with open(env, "w") as fh:
            fh.write(f"AGENT_MXID=@a:s.space\nREMOTE_TASK_TOKEN_FILE={token}\nREMOTE_TASK_TIER=owner\n{extra}")
        os.chmod(env, 0o600)
        return env, os.path.realpath(token)

    def test_a_seat_env_gives_the_raw_descriptor_of_its_token_file(self):
        seat_env, token = self._seat(f"\n{self.stub.relay}/|{SECRET}\n",
                                     extra="REMOTE_TASK_URL=https://override.example/relay\n")
        rc, out, err = _run_main(["descriptor", "--env-file", seat_env], None)
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), {"version": 1, "lane": "primary", "token_format": "raw", "env_file": token,
                                           "mint_url": f"{self.stub.base}/api/v1/mcp/agent-access-tokens",
                                           "mcp_url": f"{self.stub.base}/mcp", "relay": self.stub.relay,
                                           "bearer_digest": hashlib.sha256(SECRET.encode()).hexdigest()})
        self.assertNotIn("env_key", json.loads(out), "an older proxy fails loudly on a raw descriptor")
        self.assertNotIn(SECRET, out + err)
        self.assertEqual([(p, a) for _, p, a, _ in self.stub.seen], [("/relay/v1/mcp/discovery", f"Bearer {SECRET}")],
                         "the token file's own relay, never the REMOTE_TASK_URL override; nothing minted")

    def test_a_seat_descriptor_ignores_a_token_in_the_environment(self):
        seat_env, token = self._seat(f"{self.stub.relay}|{SECRET}\n")
        r = subprocess.run([sys.executable, "-m", "ag2_sparrow.mcp_auth", "descriptor", "--env-file", seat_env],
                           capture_output=True, text=True, timeout=60,
                           env=hermetic_env(PYTHONPATH=str(PKG_ROOT),
                                            REMOTE_TASK_TOKEN=f"{self.stub.relay}|env-secret-other"))
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout)
        self.assertEqual((got["env_file"], got["bearer_digest"]), (token, hashlib.sha256(SECRET.encode()).hexdigest()))
        self.assertTrue(all(a == f"Bearer {SECRET}" for _, _, a, _ in self.stub.seen))

    def test_a_seat_token_file_without_a_relay_or_secret_is_refused(self):
        for line in ("", "bare-secret-only\n", f"{self.stub.relay}|  \n", "REMOTE_TASK_TOKEN=x|y\n"):
            seat_env, _ = self._seat(line)
            rc, out, err = _run_main(["descriptor", "--env-file", seat_env], None)
            self.assertEqual((rc, out), (1, ""), line)
        self.assertEqual(self.stub.seen, [])

    def test_descriptor_refuses_a_plain_token_with_no_relay_and_a_missing_seat_is_no_seat(self):
        bare = self._env("REMOTE_TASK_TOKEN=bare-secret\n")
        rc, out, err = _run_main(["descriptor", "--env-file", bare], None)
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("names no relay URL", err)
        self.assertEqual(mcp_auth.seat_token_file("/no/such/.env"), "")
        self.assertEqual(self.stub.seen, [])

    def test_descriptor_refuses_a_file_the_proxy_cannot_read(self):
        seat_env = self._env("AGENT_MXID=@a:s.space\nREMOTE_TASK_TOKEN_FILE=/seat/channels/ag2space/token\n")
        raw = self._env(f"{self.stub.relay}|{SECRET}\n", "token")   # a seat's raw token file
        exported = self._env(f"export REMOTE_TASK_TOKEN={self.stub.relay}|{SECRET}\n")
        for path in (seat_env, raw, exported, "", "/no/such/.env"):
            rc, out, err = _run_main(["descriptor", "--env-file", path], (self.stub.relay, SECRET, path))
            self.assertEqual((rc, out), (1, ""), path)
            self.assertNotIn(SECRET, err)
        self.assertIn("is unreadable", _run_main(["descriptor", "--env-file", seat_env], None)[2])
        self.assertEqual(self.stub.seen, [], "the bearer is not sent for a descriptor that cannot work")

    def test_owner_tools_are_the_ag2_space_servers_room_tools_only(self):
        ok = mcp_auth.owner_tool_ok
        for tool in ("room.action.execute", "room_action_execute", "room.action.execute_safe", "ag2.whoami"):
            self.assertTrue(ok("ag2-space", tool), tool)
        for server, tool in (("ag2-space", "agent.link"), ("ag2-space", "agent.unlink"),
                             ("ag2-space", "relay.token.issue"), ("ag2-space", "space.action.execute"),
                             ("ag2-space", "ui.request"), ("other", "room.action.execute")):
            self.assertFalse(ok(server, tool), (server, tool))

    def test_mint_url_that_is_not_tls_is_refused_before_the_bearer_is_sent(self):
        self.stub.discovery = (200, {"mint_url": "http://mint.example/api/v1/mcp/agent-access-tokens",
                                     "mcp_url": "https://mcp.example/mcp"})
        rc, out, err = _run_main(["headers"], (self.stub.relay, SECRET, ""))
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("mint_url", err)
        self.assertEqual([m for m, *_ in self.stub.seen], ["GET"])

    def test_a_plaintext_relay_is_refused_before_the_bearer_is_sent(self):
        # The bearer's first stop is discovery, so the relay URL itself must be TLS (or loopback).
        rc, out, err = _run_main(["headers"], ("http://relay.example/", SECRET, ""))
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("the relay URL", err)
        self.assertNotIn(SECRET, err)
        self.assertEqual(self.stub.seen, [])

    def test_a_foreign_origin_mint_url_is_refused_with_nothing_sent(self):
        # A discovery that names another https host must not receive the relay secret.
        for mint_url in ("https://evil.example/mint", f"{self.stub.base}x/mint",
                         self.stub.base.replace("127.0.0.1", "localhost") + "/mint"):
            self.stub.seen.clear()
            self.stub.discovery = (200, {"mint_url": mint_url, "mcp_url": "https://mcp.example/mcp"})
            rc, out, err = _run_main(["headers"], (self.stub.relay, SECRET, ""))
            self.assertEqual((rc, out), (1, ""), mint_url)
            self.assertIn("relay's origin", err)
            self.assertEqual([m for m, *_ in self.stub.seen], ["GET"], mint_url)
            self.assertNotIn(SECRET, err)

    def test_a_mint_url_off_the_mint_path_is_refused_with_nothing_sent(self):
        # Same origin is not enough: the token goes to the bridge's own mint path and nowhere else.
        for path in ("/mint", "/api/v1/mcp/agent-access-tokens/../../admin", "/v1/other"):
            self.stub.seen.clear()
            self.stub.discovery = (200, {"mint_url": f"{self.stub.base}{path}",
                                         "mcp_url": "https://mcp.example/mcp"})
            rc, out, err = _run_main(["headers"], (self.stub.relay, SECRET, ""))
            self.assertEqual((rc, out), (1, ""), path)
            self.assertIn("/api/v1/mcp/agent-access-tokens", err)
            self.assertEqual([m for m, *_ in self.stub.seen], ["GET"], path)
            self.assertNotIn(SECRET, err)

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
        self.stub.discovery = (200, {"mint_url": f"{self.stub.base}/api/v1/mcp/agent-access-tokens"})
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

    def test_an_installed_package_launches_the_console_script_not_python_m(self):
        bindir = tempfile.mkdtemp(prefix="mcp-bin-")
        script = os.path.join(bindir, "ag2-sparrow-mcp")
        open(script, "w").close()
        with mock.patch.object(mcp_auth.sys, "executable", os.path.join(bindir, "python3")):
            cmd = mcp_auth._helper_command("")
        self.assertEqual(cmd, f"{script} headers")
        self.assertNotIn(" -m ", cmd)

    def test_a_source_checkout_runs_isolated_with_no_pythonpath(self):
        root = str(pathlib.Path(mcp_auth.__file__).resolve().parents[1])
        cmd = mcp_auth._helper_command("/e/.env", root)
        self.assertTrue(cmd.startswith("AG2_DEVICE_ENV=/e/.env "), cmd)
        self.assertIn(" -I -c ", cmd)
        self.assertNotIn("PYTHONPATH", cmd)
        self.assertNotIn(" -m ", cmd)

    def test_the_source_checkout_helper_never_imports_a_planted_package_from_its_cwd(self):
        # A client's cwd holding an ag2_sparrow of its own must not be what runs.
        root = str(pathlib.Path(mcp_auth.__file__).resolve().parents[1])
        cwd = tempfile.mkdtemp(prefix="mcp-cwd-")
        os.makedirs(os.path.join(cwd, "ag2_sparrow"))
        with open(os.path.join(cwd, "ag2_sparrow", "__init__.py"), "w") as f:
            f.write("raise SystemExit('planted package imported')\n")
        argv = shlex.split(mcp_auth._helper_command("", root))
        argv[-1] = argv[-1].replace("main(['headers'])", "print(__import__('ag2_sparrow').__file__) or 0")
        done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=hermetic_env(), timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.strip().startswith(root), done.stdout)

    def test_setup_entry_is_the_http_entry_with_the_helper(self):
        stub = Stub(); self.addCleanup(stub.close)
        entry = mcp_auth.setup_entry((stub.relay, SECRET, "/e/.env"), "/src/pkg")
        self.assertEqual(entry["type"], "http")
        self.assertTrue(entry["url"].endswith("/mcp"), entry)
        self.assertIn("AG2_DEVICE_ENV=/e/.env", entry["headersHelper"])
        self.assertIn("/src/pkg", entry["headersHelper"])
        self.assertNotIn(SECRET, json.dumps(entry))

    def test_an_installed_package_needs_no_pythonpath(self):
        self.assertNotIn("PYTHONPATH", mcp_auth._helper_command("/e/.env"))


def _fresh_bridge():
    # `from . import` reads the package attribute, so popping sys.modules alone reuses the old import.
    sys.modules.pop("ag2_sparrow.remote_gateway_bridge", None)
    sys.modules["ag2_sparrow"].__dict__.pop("remote_gateway_bridge", None)


class ResolveTest(unittest.TestCase):
    def test_reads_the_bridges_own_resolution(self):
        relay = "https://gw.example/relay"
        with mock.patch.dict(os.environ, {"REMOTE_TASK_TOKEN": f"{relay}|{SECRET}"}):
            _fresh_bridge()
            self.assertEqual(mcp_auth.resolve_gateway(), (relay, SECRET, ""))

    def test_names_the_device_env_file_that_supplied_the_token(self):
        relay = "https://gw.example/relay"
        env_file = os.path.join(tempfile.mkdtemp(prefix="mcp-headers-"), ".env")
        with open(env_file, "w") as fh:
            fh.write(f"REMOTE_TASK_TOKEN='{relay}|{SECRET}'\n")
        os.chmod(env_file, 0o600)
        with mock.patch.dict(os.environ, {"AG2_DEVICE_ENV": env_file}):
            _fresh_bridge()
            url, bearer, source = mcp_auth.resolve_gateway()
        self.assertEqual((url, bearer), (relay, SECRET))
        self.assertEqual(os.path.realpath(source), os.path.realpath(env_file))


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
