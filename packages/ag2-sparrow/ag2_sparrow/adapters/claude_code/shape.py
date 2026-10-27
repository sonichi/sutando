"""The claude-local adapter's Shape and the names every module of it shares (no command imports)."""
from __future__ import annotations

from pathlib import Path

from ...claude_local_card import HITL_ID_PREFIX, HITL_ROOT_NAME, RUNTIME  # noqa: F401  (the bridge owns them)
from ...connect_bridge import bridge_shape
from ...connect_common import Shape

LOCAL = Shape(name="claude-local", command="ag2-sparrow-claude-local",
              description="a Claude Code session in a tmux pane as an AG2 Space agent", cli="Claude Code CLI",
              min_version=(2, 1, 287), version_anchor=r"\(Claude Code\)",
              version_note="the Mods API it uses arrived there",
              required_keys=("CLAUDE", "PLUGIN_DIR", "INBOX_DIR", "RESULTS_DIR", "SESSION_ID", "HITL_STORE"),
              config_keys=("CLAUDE", "PLUGIN_DIR", "INBOX_DIR", "RESULTS_DIR", "CLAUDE_CONFIG_DIR", "HMAC_KEY_FILE",
                           "HMAC_STRICT", "PERMISSION_MODE", "SESSION_ID", "HITL_STORE", "ALLOW_NON_OWNER",
                           "AGENT_ID", "BRIDGE", "TOKEN_ENV_FILE", "TOKEN_VAULT_KEY", "BRIDGE_STATE_DIR"),
              env_keep=("PATH", "HOME", "USER", "TERM", "ANTHROPIC_BASE_URL"), kept="the Claude login",
              kill_mode="process", stop_timeout_s=180, run_args=("run", "--launchd"), keep_alive_on_success=False,
              manager_timeout_s=240)
# The agent's own gateway bridge, when install runs it.
BRIDGE = bridge_shape(LOCAL)
# The marker the plugin reads at session.start: with it, the watch arms itself, no model turn.
AUTOARM_VAR = "AG2_AUTOARM"
SESSION_NAME = "claude-local"
SOCKET_NAME = "claude-local.sock"
SUPERVISOR_LOCK = "claude-local.supervisor.lock"
LAUNCHER_LOCK = "claude-local.launcher.lock"
MIN_TMUX = (3, 2)
SOCKET_PATH_MAX = 100
PLUGIN = Path(__file__).resolve().with_name("ag2space-inbox")      # package data, shipped in the wheel


def resolve_plugin_dir(recorded: str) -> tuple:
    """(dir, fell_back): the recorded PLUGIN_DIR when it holds a plugin, else the shipped one; an install
    from before the plugin moved into the wheel recorded an in-repo dir that no longer exists."""
    if recorded and (Path(recorded) / ".claude-plugin" / "plugin.json").is_file():
        return Path(recorded), False
    return PLUGIN, True
