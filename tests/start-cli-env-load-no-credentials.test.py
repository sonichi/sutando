#!/usr/bin/env python3
"""start-cli.sh hands the core session no relay credentials, on every launch path.

Every process the core session starts inherits its environment, so an exported
relay token lets any child poll the gateway as the real agent. A normal boot
reaches start-cli after startup.sh has already exported the repo .env and the
relay lane's token; a direct restart reaches it with a bare environment. Both
must end without AG2_REMOTE_* / REMOTE_TASK_* while other keys pass through.
The tests run the launcher's own blocks, cut from the script, so they test what ships.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "agent" / "start-cli.sh"
LANES = REPO / "scripts" / "restart-gateway-lanes.sh"

FAKE_ENV = """\
SUTANDO_SELF_DEVELOPMENT_ENABLED=false
SUTANDO_CODEX_AUTO_RESET_ENABLED=true
AG2_AGENT_NAME="name with spaces 'and' quotes"
AG2_REMOTE_TOKEN="https://gw.invalid|prd-fake"
AG2_REMOTE_TOKEN_DEV="https://gw-dev.invalid|dev-fake"
AG2_REMOTE_TIER=owner
REMOTE_TASK_URL=https://gw.invalid
GEMINI_API_KEY=gemini-fake
TWILIO_AUTH_TOKEN=twilio-fake
"""

RELAY_KEYS = ("AG2_REMOTE_TOKEN", "AG2_REMOTE_TOKEN_DEV", "AG2_REMOTE_TIER",
              "REMOTE_TASK_URL", "REMOTE_TASK_TOKEN", "REMOTE_TASK_TOKEN_FILE",
              "REMOTE_TASK_TIER")

# What startup.sh does before `exec start-cli.sh`: load the repo .env with
# set -a, then start_gateway_lanes exports the primary lane's token and tier.
STARTUP_PREFIX = """\
set -a
source "$REPO/.env"
set +a
export REMOTE_TASK_TOKEN="${AG2_REMOTE_TOKEN}" REMOTE_TASK_TIER=owner
export REMOTE_TASK_TOKEN_FILE="$REPO/channel.env"
"""


def _env_block() -> str:
    lines = SCRIPT.read_text().splitlines()
    start = lines.index("unset _self_dir") + 1
    end = lines.index("_sutando_withhold_relay_env", start)
    return "\n".join(lines[start:end + 1])


def _tmux_block() -> str:
    lines = SCRIPT.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("# A running tmux server"))
    end = lines.index("fi", start)
    return "\n".join(lines[start:end + 1])


def _run(prefix: str, ambient: dict[str, str], dotenv: str | None = FAKE_ENV
         ) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as tmp:
        if dotenv is not None:
            (Path(tmp) / ".env").write_text(dotenv)
        launcher = Path(tmp) / "start-cli-env.sh"
        launcher.write_text(f'set -euo pipefail\nREPO={tmp!r}\n{_env_block()}\nenv -0\n')
        script = f'set -euo pipefail\nREPO={tmp!r}\n{prefix}exec bash {str(launcher)!r}\n'
        env = {"PATH": "/usr/bin:/bin", **ambient}
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, timeout=30)


def _session_env(prefix: str = "", ambient: dict[str, str] | None = None,
                 dotenv: str | None = FAKE_ENV) -> dict[str, str]:
    out = _run(prefix, ambient or {}, dotenv)
    if out.returncode != 0:
        raise AssertionError(out.stderr.decode())
    pairs = [p.split("=", 1) for p in out.stdout.decode().split("\0") if "=" in p]
    return {k: v for k, v in pairs}


class LaunchPathTest(unittest.TestCase):
    def _assert_no_relay_keys(self, env: dict[str, str]) -> None:
        for name in RELAY_KEYS:
            self.assertNotIn(name, env, name)
        self.assertFalse(any(k.startswith(("AG2_REMOTE_", "REMOTE_TASK_")) for k in env))

    def test_normal_boot_after_startup_env_load(self):
        env = _session_env(STARTUP_PREFIX)
        self._assert_no_relay_keys(env)
        self.assertEqual(env.get("GEMINI_API_KEY"), "gemini-fake")
        self.assertEqual(env.get("TWILIO_AUTH_TOKEN"), "twilio-fake")
        self.assertEqual(env.get("SUTANDO_SELF_DEVELOPMENT_ENABLED"), "false")

    def test_direct_restart(self):
        env = _session_env()
        self._assert_no_relay_keys(env)
        self.assertEqual(env.get("GEMINI_API_KEY"), "gemini-fake")
        self.assertEqual(env.get("SUTANDO_CODEX_AUTO_RESET_ENABLED"), "true")
        self.assertEqual(env.get("AG2_AGENT_NAME"), "name with spaces 'and' quotes")

    def test_ambient_relay_keys_are_withheld_without_an_env_file(self):
        env = _session_env(ambient={"AG2_REMOTE_TOKEN": "ambient-fake",
                                    "REMOTE_TASK_TOKEN": "ambient-fake",
                                    "GEMINI_API_KEY": "ambient-gemini"}, dotenv=None)
        self._assert_no_relay_keys(env)
        self.assertEqual(env.get("GEMINI_API_KEY"), "ambient-gemini")

    def test_ambient_policy_override_is_kept(self):
        env = _session_env(ambient={"SUTANDO_SELF_DEVELOPMENT_ENABLED": ""})
        self.assertEqual(env.get("SUTANDO_SELF_DEVELOPMENT_ENABLED"), "")

    def test_a_broken_env_file_still_stops_the_launcher(self):
        out = _run("", {}, "SUTANDO_X=1\nfalse\n")
        self.assertNotEqual(out.returncode, 0)
        self.assertNotIn(b"SUTANDO_X=1", out.stdout)


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class TmuxGlobalEnvTest(unittest.TestCase):
    """A tmux server started before this launch still carries relay keys in its
    global environment, which every new session (the core's included) inherits."""

    def test_relay_keys_leave_the_server_global_env(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            sock = os.path.join(tmp, "s")
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                   "AG2_REMOTE_TOKEN": "server-fake", "REMOTE_TASK_TOKEN": "server-fake",
                   "GEMINI_API_KEY": "server-gemini"}
            subprocess.run(["tmux", "-f", "/dev/null", "-S", sock, "new-session", "-d",
                            "-s", "keep", "sleep 60"], env=env, check=True, timeout=10)
            try:
                script = f'set -euo pipefail\ntmux_socket={sock!r}\n{_tmux_block()}\n'
                subprocess.run(["bash", "-c", script], check=True, timeout=10,
                               env={"PATH": env["PATH"]})
                glob = subprocess.run(["tmux", "-S", sock, "show-environment", "-g"],
                                      capture_output=True, text=True, timeout=10).stdout
                names = {ln.split("=", 1)[0] for ln in glob.splitlines()}
                self.assertNotIn("AG2_REMOTE_TOKEN", names)
                self.assertNotIn("REMOTE_TASK_TOKEN", names)
                self.assertIn("GEMINI_API_KEY", names)
            finally:
                subprocess.run(["tmux", "-S", sock, "kill-server"], timeout=10,
                               capture_output=True)


class GatewayLanesLoadTest(unittest.TestCase):
    """restart-gateway-lanes.sh runs from the core session, which no longer carries the
    relay token, so it must read the repo .env itself before starting the lanes."""

    def test_lanes_script_sees_the_repo_env_token_without_inheriting_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "src").mkdir()
            shutil.copy(LANES, root / "scripts" / LANES.name)
            (root / "scripts" / "python-binary.sh").write_text(
                'resolve_python() { echo /usr/bin/true; }\n')
            (root / "scripts" / "sutando-config.sh").write_text(f'echo {str(root / "ws")!r}\n')
            (root / "src" / "startup-runtime.sh").write_text(
                'start_gateway_lanes() { [ -n "${AG2_REMOTE_TOKEN:-}" ] && echo LANE_TOKEN_SET'
                ' || echo LANE_TOKEN_MISSING; }\n')
            (root / ".env").write_text('AG2_REMOTE_TOKEN="https://gw.invalid|prd-fake"\n')
            out = subprocess.run(["bash", str(root / "scripts" / LANES.name)],
                                 env={"PATH": "/usr/bin:/bin"}, capture_output=True,
                                 text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("LANE_TOKEN_SET", out.stdout)


if __name__ == "__main__":
    unittest.main()
