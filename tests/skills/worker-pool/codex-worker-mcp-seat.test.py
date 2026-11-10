#!/usr/bin/env python3
"""A Codex worker seat's s-space MCP proxy mints as the worker's own agent, or refuses.

Codex hands a stdio MCP server only the variables its entry names, so the launcher must start Codex with
the desktop's s-space entry plus `env_vars` naming the worker and its credential resolver, and export
both into the session. A broken resolver still exports `[]`, which the proxy refuses to start on.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))

import codex_worker_mcp as M  # noqa: E402
import connections  # noqa: E402

try:
    import tomllib
except ModuleNotFoundError:
    import toml_fallback as tomllib

PROXY_ENV = {"AG2_MCP_DESCRIPTOR": "/a s/ag2-mcp.json", "AG2_MCP_LOG": "/a s/proxy.log",
             "AG2_MCP_ROOM_ACTIONS": "/a s/room-actions.jsonl"}
CLAUDE_ENTRY = {"type": "stdio", "command": "/a s/node", "args": ["/a s/ag2-mcp-proxy.mjs"], "env": PROXY_ENV}
CODEX_TOML = ('[mcp_servers.s-space]\ncommand = "/c/node"\nargs = ["/c/ag2-mcp-proxy.mjs"]\n'
              'startup_timeout_sec = 30\n\n[mcp_servers.s-space.env]\nAG2_MCP_DESCRIPTOR = "/c/d.json"\n')


def seat_table(args):
    """The table the `-c mcp_servers.s-space=<value>` override sets, parsed as Codex parses it."""
    assert len(args) == 2 and args[0] == "-c", args
    key, value = args[1].split("=", 1)
    assert key == "mcp_servers.s-space", key
    return tomllib.loads(f"x = {value}")["x"]


class Registration(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.codex, self.claude = self.tmp / "codex", self.tmp / "claude"
        self.codex.mkdir(); self.claude.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def claude_json(self, entry):
        (self.claude / ".claude.json").write_text(json.dumps({"mcpServers": {"s-space": entry, "other": {}}}))

    def test_the_passthrough_names_are_the_ones_the_engine_and_proxy_use(self):
        self.assertEqual(M.PASSTHROUGH, (connections.SESSION_WORKER_ENV, connections.MCP_WORKER_CREDENTIAL_ENV))

    def test_the_codex_homes_entry_is_kept_whole_with_the_worker_passthrough_added(self):
        (self.codex / "config.toml").write_text(CODEX_TOML)
        self.claude_json(CLAUDE_ENTRY)
        t = seat_table(M.overrides(M.registration(self.codex, self.claude)))
        self.assertEqual(t["command"], "/c/node")
        self.assertEqual(t["startup_timeout_sec"], 30)
        self.assertEqual(t["env"], {"AG2_MCP_DESCRIPTOR": "/c/d.json"})
        self.assertEqual(t["env_vars"], ["SUTANDO_WORKER_ID", "AG2_MCP_WORKER_CREDENTIAL"])

    def test_with_no_codex_entry_the_claude_registration_is_carried_over(self):
        (self.codex / "config.toml").write_text('check_for_update_on_startup = false\n')
        self.claude_json(CLAUDE_ENTRY)
        t = seat_table(M.overrides(M.registration(self.codex, self.claude)))
        self.assertEqual(t, {"command": "/a s/node", "args": ["/a s/ag2-mcp-proxy.mjs"], "env": PROXY_ENV,
                             "env_vars": ["SUTANDO_WORKER_ID", "AG2_MCP_WORKER_CREDENTIAL"]})

    def test_existing_env_vars_are_kept_and_never_repeated(self):
        (self.codex / "config.toml").write_text(CODEX_TOML.replace(
            "startup_timeout_sec = 30\n", 'env_vars = ["HTTPS_PROXY", "SUTANDO_WORKER_ID"]\n'))
        t = seat_table(M.overrides(M.registration(self.codex)))
        self.assertEqual(t["env_vars"], ["HTTPS_PROXY", "SUTANDO_WORKER_ID", "AG2_MCP_WORKER_CREDENTIAL"])

    def test_awkward_paths_survive_the_round_trip(self):
        odd = 'C:\\x "q" \u00e9\x7f\n'
        self.claude_json({"command": odd, "args": [odd], "env": {"K": odd}})
        t = seat_table(M.overrides(M.registration(self.codex, self.claude)))
        self.assertEqual((t["command"], t["args"], t["env"]), (odd, [odd], {"K": odd}))

    def test_nothing_is_printed_when_no_config_registers_a_stdio_server(self):
        self.assertEqual(M.overrides(M.registration(self.codex, self.claude)), [])
        (self.codex / "config.toml").write_text("not = [toml")
        self.claude_json({"type": "http", "url": "https://x"})
        self.assertEqual(M.overrides(M.registration(self.codex, self.claude)), [])
        self.assertEqual(M.overrides(M.registration(self.codex, None)), [])


class Launcher(unittest.TestCase):
    """The real launcher in a fake repo: tmux records its argv, the resolver compose is a stub."""

    def launch(self, tmp, *, compose_ok=True, codex_toml=None):
        tmp = Path(tmp)
        root, home, ws, bin_dir = tmp / "repo", tmp / "home", tmp / "ws", tmp / "bin"
        for p in (home, ws, bin_dir, home / "codex", home / "claude"):
            p.mkdir(parents=True)
        for rel in ("skills/worker-pool/scripts/launch-codex-worker-session.sh",
                    "skills/worker-pool/scripts/worker-mcp-credential.sh",
                    "skills/worker-pool/scripts/codex_worker_mcp.py",
                    "scripts/python-binary.sh", "src/agent/launch-path.sh", "src/toml_fallback.py"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, root / rel)
        (root / "scripts/sutando-config.sh").write_text(
            '#!/bin/sh\ncase "$1" in\n'
            '  core-config-dir-env-name) printf "CODEX_HOME" ;;\n'
            '  core-config-dir-value) printf "%s/codex" "$HOME" ;;\n'
            '  claude-sutando-config-dir) printf "%s/claude" "$HOME" ;;\nesac\n')
        (root / "scripts/connections.py").write_text(
            "import json, sys\n"
            + ("sys.exit(print('connections: fixture refusal', file=sys.stderr) or 1)\n" if not compose_ok else "")
            + "print(json.dumps(['/py', 'connections.py', 'worker-mcp-credential', '--worker', "
              "sys.argv[sys.argv.index('--worker') + 1]]))\n")
        (root / "skills/worker-pool/scripts/worker-watcher-supervisor.sh").write_text("#!/bin/sh\nexit 0\n")
        (home / "claude/.claude.json").write_text(json.dumps({"mcpServers": {"s-space": CLAUDE_ENTRY}}))
        if codex_toml is not None:
            (home / "codex/config.toml").write_text(codex_toml)
        for name, body in {
            "codex": "#!/bin/sh\nexit 0\n", "fswatch": "#!/bin/sh\nexit 0\n", "uname": "#!/bin/sh\necho Linux\n",
            "tmux": '#!/bin/sh\n[ "$1" = -S ] && shift 2\ncase "$1" in\n'
                    '  has-session) [ -f "$TMUX_STATE" ] ;;\n'
                    '  new-session) "$SUTANDO_PY" -c \'import json,sys; print(json.dumps(sys.argv[1:]))\' "$@" '
                    '> "$TMUX_ARGV"; touch "$TMUX_STATE" ;;\nesac\n',
        }.items():
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        env = {"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin", "SUTANDO_PY": sys.executable,
               "SUTANDO_INSTANCE_ID": "w-seat", "SUTANDO_WORKER_ID": "w-seat",
               "SUTANDO_TMUX_SESSION": "worker-session", "SUTANDO_TASKS_DIR": str(ws / "in"),
               "SUTANDO_WORKSPACE_DIR": str(ws), "SUTANDO_RESULTS_DIR": str(ws / "results"),
               "SUTANDO_INBOX_RESOLVER": "/bin/true", "SUTANDO_POOL_DELIVERY_SCRIPT": "/bin/true",
               "TMUX_STATE": str(tmp / "up"), "TMUX_ARGV": str(tmp / "argv.json")}
        r = subprocess.run(["bash", str(root / "skills/worker-pool/scripts/launch-codex-worker-session.sh")],
                           cwd=root, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads((tmp / "argv.json").read_text()), r.stderr

    @staticmethod
    def exported(argv, name):
        vals = [argv[i + 1].split("=", 1)[1] for i, a in enumerate(argv[:-1])
                if a == "-e" and argv[i + 1].startswith(name + "=")]
        return vals[-1] if vals else None

    @staticmethod
    def seat(argv):
        i = next(i for i, a in enumerate(argv) if a.startswith("mcp_servers.s-space="))
        return seat_table(argv[i - 1:i + 1])

    def test_the_session_exports_both_names_and_codex_passes_them_to_the_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv, _ = self.launch(tmp)
            self.assertEqual(self.exported(argv, "SUTANDO_WORKER_ID"), "w-seat")
            self.assertEqual(json.loads(self.exported(argv, "AG2_MCP_WORKER_CREDENTIAL")),
                             ["/py", "connections.py", "worker-mcp-credential", "--worker", "w-seat"])
            t = self.seat(argv)
            self.assertEqual(t["command"], "/a s/node")
            self.assertEqual(t["env_vars"], ["SUTANDO_WORKER_ID", "AG2_MCP_WORKER_CREDENTIAL"])
            self.assertNotIn("bearer", json.dumps(argv).lower())

    def test_the_codex_homes_own_entry_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv, _ = self.launch(tmp, codex_toml=CODEX_TOML)
            self.assertEqual(self.seat(argv)["command"], "/c/node")

    def test_a_failed_compose_still_exports_an_opted_in_seat_the_proxy_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv, err = self.launch(tmp, compose_ok=False)
            self.assertEqual(self.exported(argv, "AG2_MCP_WORKER_CREDENTIAL"), "[]")
            self.assertIn("no MCP credential resolver for this worker (connections: fixture refusal)", err)
            self.assertIn("AG2_MCP_WORKER_CREDENTIAL", self.seat(argv)["env_vars"])


if __name__ == "__main__":
    unittest.main()
