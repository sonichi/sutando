"""The claude-local adapter's Shape and the names every module of it shares (no command imports)."""
from __future__ import annotations

from ...claude_local_card import HITL_ID_PREFIX, HITL_ROOT_NAME, RUNTIME  # noqa: F401  (the bridge owns them)
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
# The agent's own gateway bridge, when install runs it: the console script with no arguments.
BRIDGE = Shape(name="claude-local-bridge", command="ag2-sparrow", writer=LOCAL.command,
               description="the gateway bridge of the claude-local agent", cli="ag2-sparrow", min_version=(0, 0, 0),
               version_anchor="", version_note="", required_keys=(), config_keys=(), env_keep=(), kept="",
               run_args=(), service_args=())
# The marker the plugin reads at session.start: with it, the watch arms itself, no model turn.
AUTOARM_VAR = "AG2_AUTOARM"
SESSION_NAME = "claude-local"
SOCKET_NAME = "claude-local.sock"
SUPERVISOR_LOCK = "claude-local.supervisor.lock"
LAUNCHER_LOCK = "claude-local.launcher.lock"
MIN_TMUX = (3, 2)
SOCKET_PATH_MAX = 100
