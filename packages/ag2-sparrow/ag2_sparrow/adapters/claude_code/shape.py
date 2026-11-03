"""The claude-local adapter's Shape and the names every module of it shares (no command imports)."""
from __future__ import annotations

from pathlib import Path

from ... import claude_local_records as records, task_io
from ...claude_local_card import HITL_ID_PREFIX, HITL_ROOT_NAME, RUNTIME  # noqa: F401  (the bridge owns them)
from ...connect_bridge import bridge_shape
from ...connect_common import Shape


def session_launcher(root, rec: dict) -> "int | None":
    """The launcher run that spawned the pane process holding `root`: the supervisor record's launcher (its
    pid and start live) when the holder is that run's generation's pane process (or the pane's own child)
    and the pane is a child of the generation's tmux server, live as recorded. None otherwise."""
    try:
        sup = records.load_supervisor(root)
    except (records.RecordError, OSError):
        return None
    gen, launcher, holder = sup.get("generation") or {}, sup.get("launcher") or {}, rec.get("owner_pid", rec.get("pid"))
    pane, server = gen.get("pane_pid"), gen.get("server_pid")
    if not (pane and server and holder and launcher.get("pid")):
        return None
    if holder != pane and task_io.parent_pid(holder) != pane:
        return None
    if task_io.parent_pid(pane) != server or task_io.verify_lineage(server, gen.get("server_start")) is not True:
        return None
    return launcher["pid"] if task_io.verify_lineage(launcher["pid"], launcher.get("start")) is True else None


LOCAL = Shape(name="claude-local", command="ag2-sparrow-claude-local",
              description="a Claude Code session in a tmux pane as an s.space agent", cli="Claude Code CLI",
              min_version=(2, 1, 287), version_anchor=r"\(Claude Code\)",
              version_note="the Mods API it uses arrived there",
              required_keys=("CLAUDE", "PLUGIN_DIR", "INBOX_DIR", "RESULTS_DIR", "SESSION_ID", "HITL_STORE"),
              config_keys=("CLAUDE", "PLUGIN_DIR", "INBOX_DIR", "RESULTS_DIR", "CLAUDE_CONFIG_DIR", "HMAC_KEY_FILE",
                           "HMAC_STRICT", "PERMISSION_MODE", "SESSION_ID", "HITL_STORE", "ALLOW_NON_OWNER",
                           "AGENT_ID", "BRIDGE", "TOKEN_ENV_FILE", "TOKEN_VAULT_KEY", "BRIDGE_STATE_DIR", "TMUX"),
              env_keep=("PATH", "HOME", "USER", "TERM", "ANTHROPIC_BASE_URL"), kept="the Claude login",
              kill_mode="process", stop_timeout_s=180, run_args=("run", "--launchd"), keep_alive_on_success=False,
              manager_timeout_s=240, holder_launcher=session_launcher)
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
