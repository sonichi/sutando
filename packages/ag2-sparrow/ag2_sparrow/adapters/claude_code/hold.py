"""`ag2-sparrow-claude-local hold --generation=<32 hex> --`: the pane's thin parent.

Started by the launcher as the pane's exact command; the work root, config dir, session id and HITL
store root come from the pane's session environment. It sets the pane title to a fresh token, takes
the canonical root (schema 2), publishes the owner record, recovers the dead predecessor generation
under that flock, opens `tasks` O_NOFOLLOW from the held root fd and fchdirs there, spawns the exec
shim gated on one go byte, attaches the shim as the consumer whose liveness fences the root, and only
then writes the go byte. A failed publish, attach or go write reaps the shim: no consumer exists.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from ... import (claude_local_records as records, claude_model_switch, connect_common as cm, mcp_auth, mcp_name_shim,
                 mcp_stdio, task_io)
from ...bounded_file_read import read_regular
from ...file_lock import locked_file
from . import inbox
from .shape import LOCAL, resolve_plugin_dir

GO = b"g"
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
ENV_ROOT, ENV_CONFIG_DIR, ENV_SESSION_ID, ENV_HITL_STORE = ("AG2_SPARROW_ROOT", "AG2_SPARROW_CONFIG_DIR",
                                                             "AG2_SPARROW_SESSION_ID", "AG2_SPARROW_HITL_STORE")
INODE_VAR = "AG2_INBOX_INODE"
TITLE_PREFIX = "t"
TITLE_RE = re.compile(r"^t[0-9a-f]{32}$")
ATTACH_GRACE_S = 30.0
EXIT_NO_CONSUMER = 3

# The shim: exactly one go byte from the inherited fd, else exit without exec; the fd is closed
# before execv so no descriptor reaches Claude.
SHIM = ("import os, sys\n"
        "fd = int(sys.argv[1]); exe = sys.argv[2]; argv = sys.argv[2:]\n"
        "try:\n    b = os.read(fd, 1)\nexcept OSError:\n    sys.exit(5)\n"
        "if b != %r:\n    sys.exit(3 if not b else 4)\n"
        "os.close(fd)\nos.execv(exe, argv)\n") % GO


def pane_command(command: str, nonce: str) -> str:
    """The exact pane start command: a fixed alphabet, the nonce its only variable part."""
    if not NONCE_RE.match(nonce):
        raise ValueError("the generation nonce is 32 hex characters")
    return "%s hold --generation=%s --" % (command, nonce)


def title_token() -> str:
    return TITLE_PREFIX + os.urandom(16).hex()


def set_title(token: str, out=None) -> None:
    """The per-process discriminator the fence predicate compares with #{pane_title}."""
    out = sys.stdout if out is None else out
    out.write("\033]2;%s\007" % token)
    out.flush()


def claude_home(config_dir: str) -> str:
    """CLAUDE_CONFIG_DIR as the session runs it: the recorded one, else the CLI's default."""
    return config_dir or os.path.join(os.path.expanduser("~"), ".claude")


def transcript_path(config_dir: str, session_id: str) -> str:
    """The session's transcript under any project folder of the config dir's projects tree, else ''.
    `claude --resume <id>` finds it from any working directory, so the pane's own cwd needs no copy."""
    base = os.path.join(claude_home(config_dir), "projects")
    try:
        projects = sorted(os.listdir(base))
    except OSError:
        return ""
    return next((p for p in (os.path.join(base, d, session_id + ".jsonl") for d in projects) if os.path.isfile(p)), "")


def transcript_exists(config_dir: str, session_id: str) -> bool:
    """Whether the session's transcript already lives under CLAUDE_CONFIG_DIR's projects tree."""
    return bool(transcript_path(config_dir, session_id))


def live_session_pid(config_dir: str, session_id: str) -> "int | None":
    """The pid of a running Claude Code process that has `session_id` open, from the CLI's own
    `sessions/<pid>.json` registry; None when none is recorded live."""
    base = os.path.join(claude_home(config_dir), "sessions")
    try:
        names = os.listdir(base)
    except OSError:
        return None
    for name in names:
        if not re.match(r"^\d+\.json$", name):
            continue
        try:
            with open(os.path.join(base, name), encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        pid = rec.get("pid") if isinstance(rec, dict) else None
        if rec and rec.get("sessionId") == session_id and isinstance(pid, int) and pid > 0 and _alive(pid):
            return pid
    return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


MODEL_RECORD = ("state", "model-switch.json")
MODEL_LOCK = ("state", "model-switch.lock")   # every read-then-write of MODEL_RECORD holds it


def switched_model(root) -> str:
    """The model the owner last switched the session to (`model`, the record the launcher writes once
    Claude Code said "Set model to"), '' when none or one /model would not take."""
    try:
        with open(Path(root or ".").joinpath(*MODEL_RECORD), encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return ""
    model = str(record.get("model") or "") if isinstance(record, dict) else ""
    return model if claude_model_switch.MODEL_RE.match(model) else ""


def wanted_model(root) -> str:
    """A pick the settings page saved while the session could not take /model (`wanted`): the next start
    runs on it. '' when none."""
    try:
        with open(Path(root or ".").joinpath(*MODEL_RECORD), encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return ""
    model = str(record.get("wanted") or "") if isinstance(record, dict) else ""
    return model if claude_model_switch.MODEL_RE.match(model) else ""


def write_model_record(root, record: dict) -> None:
    try:
        records.write_durable(Path(root or ".").joinpath(*MODEL_RECORD), json.dumps(record).encode())
    except records.RecordError as e:
        raise OSError(str(e)) from e


def settle_wanted(root, now=time.time) -> str:
    """At a session's start: a waiting pick becomes the session's model (`--model` starts it on that one).
    The pick, or '' when none waited or the record could not be written (the start then keeps the old model)."""
    try:
        with locked_file(Path(root or ".").joinpath(*MODEL_LOCK)):
            wanted = wanted_model(root)
            if wanted:
                write_model_record(root, {"model": wanted, "at": now()})
            return wanted
    except OSError:
        return ""


def claude_argv(cfg: dict, session_id: str) -> list:
    """`claude --plugin-dir … --permission-mode … --resume|--session-id <id> [--model <m>]`: resume when
    the transcript exists under the session's config dir, else start it under that id. A session the
    owner switched starts again on that model: the pick outlives a restart, for every sender's turn."""
    flag = "--resume" if transcript_exists(cfg.get("CLAUDE_CONFIG_DIR", ""), session_id) else "--session-id"
    model = switched_model(cfg.get("ROOT"))
    return [cfg["CLAUDE"], "--plugin-dir", str(resolve_plugin_dir(cfg["PLUGIN_DIR"])[0]), "--permission-mode",
            cfg.get("PERMISSION_MODE") or "bypassPermissions", flag, session_id] + (["--model", model] if model else [])


# The AG2 Space room tools as this agent (MCP_PROXY_COMMAND): the shim, the descriptor, the proxy's log and the
# `--mcp-config` file live in one directory of the root's state (task_lifecycle.STATE_OPTIONAL_DIRS names it).
ROOM_TOOLS_DIR = ("state", "room-tools")
MCP_CONFIG_NAME = "ag2-mcp-config.json"
# What the shim grants the session started for the tools (tools_session): only the owner's tasks enter it, and
# the shim's turn gate (GATE_ENV) still forwards a call only while one of them is in flight.
ROOM_TOOLS = mcp_auth.OWNER_TOOLS
# The descriptor's token exchange runs before the session starts: well inside the launcher's wait for its
# consumer (launcher.CONSUMER_WAIT_S, 30 s), so a slow relay costs the tools, never the session.
ROOM_TOOLS_EXCHANGE_S = 12.0
CLAUDE_JSON_MAX = 64 << 20


def exchange_run(argv, **kw):
    """subprocess.run for the descriptor exchange, bounded by ROOM_TOOLS_EXCHANGE_S."""
    return subprocess.run(argv, **dict(kw, timeout=min(kw.get("timeout") or ROOM_TOOLS_EXCHANGE_S,
                                                        ROOM_TOOLS_EXCHANGE_S)))


def _servers_of(path, *keys) -> set:
    """The names under `keys` (a path into the JSON object at `path`, ending at an mcpServers mapping); an empty
    set when the file or the path is not there. ValueError for a file Claude Code could not read either."""
    text, why = read_regular(path, CLAUDE_JSON_MAX)
    if text is None:
        if why:
            raise ValueError("%s was refused (%s)" % (path, why))
        return set()
    node = json.loads(text)
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    return set(node) if isinstance(node, dict) else set()


def configured_servers(cfg: dict, cwd: str) -> set:
    """The MCP servers the session's Claude Code already loads by name, without --strict-mcp-config: the user
    scope (`mcpServers` of <CLAUDE_CONFIG_DIR, else ~>/.claude.json), the local scope (its
    `projects[<cwd>].mcpServers`) and the project scope (<cwd>/.mcp.json). A plugin's servers are named
    `plugin:<plugin>:<name>` and never collide. ValueError when one of them cannot be read."""
    config_dir = cfg.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~")
    claude_json = os.path.join(config_dir, ".claude.json")
    names = _servers_of(claude_json, "mcpServers")
    for project in {cwd, os.path.realpath(cwd)}:
        names |= _servers_of(claude_json, "projects", project, "mcpServers")
    return names | _servers_of(os.path.join(cwd, ".mcp.json"), "mcpServers")


def write_mcp_config(state: str, entry: dict) -> str:
    """`<state>/ag2-mcp-config.json` (0600): Claude Code's `--mcp-config` with the one stdio server `entry`
    (mcp_stdio.stdio_entry's: the shim, the proxy's command line, the descriptor's path). No secret is in it."""
    path = os.path.join(state, MCP_CONFIG_NAME)
    server = {"type": "stdio", "command": entry["command"], "args": list(entry["args"]),
              "env": {e["name"]: e["value"] for e in entry["env"]}}
    cm._write(Path(path), json.dumps({"mcpServers": {entry["name"]: server}}, indent=2) + "\n", 0o600)
    return path


def gate_argv(root: str) -> list:
    """The inbox CLI's `room-tools` verb for `root`, run from this very package, isolated (-I), as mcp_stdio
    runs its own helpers: what the name shim runs before each room-tool call."""
    pkg = str(Path(__file__).resolve().parents[3])
    # The gate reads the install config this session was started from, whatever env the MCP server is given.
    conf = os.environ.get(ENV_CONFIG_DIR) or ""
    code = ("import os, sys; %ssys.path.insert(0, %r); from ag2_sparrow.adapters.claude_code import cli; "
            "raise SystemExit(cli.main(sys.argv[1:]))" % ("os.environ[%r] = %r; " % (ENV_CONFIG_DIR, conf) if conf else "", pkg))
    return [sys.executable, "-I", "-c", code, "inbox", "--root", root, "room-tools"]


def tools_conversation(cfg: dict, session_id=None) -> bool:
    """Whether `session_id` (default: the configured one) is a conversation that has, or once had, the room
    tools: the one install started for them (ROOM_TOOLS_SESSION) or one it left since (OWNER_ONLY_SESSIONS).
    Only the owner's tasks ever enter it, whether or not the tools are on now: it holds their results."""
    mine = cfg.get("SESSION_ID") or "" if session_id is None else session_id
    if not mine or mine != (cfg.get("SESSION_ID") or ""):
        return False
    return mine == (cfg.get("ROOM_TOOLS_SESSION") or "") or mine in (cfg.get("OWNER_ONLY_SESSIONS") or "").split()


def owner_alone(cfg: dict) -> bool:
    """Whether the install serves nobody but the owner: both opt-outs recorded, each exactly "0". A key that is
    missing or holds anything else counts as serving them (inbox.owner_alone reads the same from the live config)."""
    return cfg.get(inbox.ALLOW_COLLABORATOR) == "0" and cfg.get(inbox.ALLOW_NON_OWNER) == "0"


def tools_session(cfg: dict, session_id=None) -> bool:
    """Whether this start gets the tools: the conversation install started for them, with the preview on and
    nobody but the owner served."""
    mine = cfg.get("SESSION_ID") or "" if session_id is None else session_id
    return (cfg.get("ROOM_TOOLS_PREVIEW") == "1" and owner_alone(cfg) and bool(mine)
            and mine == (cfg.get("ROOM_TOOLS_SESSION") or "") and mine == (cfg.get("SESSION_ID") or ""))


def room_tools_argv(cfg: dict, root: str, cwd: str, entry=None, log=None, session_id=None) -> list:
    """`--mcp-config <file>` adding the ag2-space MCP server as this agent to the session, or []: with no
    MCP_PROXY_COMMAND (an install from before it, or the desktop passed none), no TOKEN_ENV_FILE of the agent's
    own, an `ag2-space` server the agent's Claude Code config already has (kept; ours would shadow or merge
    into it), a config that cannot be read, or a failed token exchange. Each is one line in `log`; none stops
    the session, which then starts without the tools. Rebuilt at every start (a restart is a new hold)."""
    log = log or (lambda line: print("hold: " + line, file=sys.stderr))
    proxy, token_file = cfg.get("MCP_PROXY_COMMAND") or "", cfg.get("TOKEN_ENV_FILE") or ""
    if not proxy:
        return []
    if not token_file:
        log("ag2-space MCP tools not offered: no TOKEN_ENV_FILE (the agent's own token file)")
        return []
    if cfg.get("ROOM_TOOLS_PREVIEW") == "1" and not owner_alone(cfg):
        log("ag2-space MCP tools not offered: the install serves collaborators or other non-owners; they need "
            "both --no-allow-collaborator and --no-allow-non-owner")
        return []
    if not tools_session(cfg, session_id):
        log("ag2-space MCP tools not offered: this conversation was not started for them (it may hold other "
            "senders' turns); install again with --mcp-proxy-command once the job is stopped")
        return []
    try:
        # Recorded at install: an app update can move the bundled node, and the server would then fail to
        # start on every session with nothing said.
        exe = (shlex.split(proxy) or [""])[0]
        if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
            log("ag2-space MCP tools not offered: the recorded proxy's executable is gone (%s); install again "
                "with --mcp-proxy-command" % exe)
            return []
        names = configured_servers(cfg, cwd)
        if mcp_auth.SERVER_NAME in names:
            log("ag2-space MCP tools not offered: the agent's Claude Code config already has an %s server"
                % mcp_auth.SERVER_NAME)
            return []
        state = os.path.join(root, *ROOM_TOOLS_DIR)
        os.makedirs(state, mode=0o700, exist_ok=True)
        if os.path.islink(state) or not os.path.isdir(state):
            log("ag2-space MCP tools not offered: %s is not a directory" % state)
            return []
        got, why = (entry or mcp_stdio.stdio_entry)(proxy, token_file, cfg.get("AGENT_ID") or "", state,
                                                    run=exchange_run, turn=uuid.uuid4().hex, tools=ROOM_TOOLS)
        if got is None:
            log("ag2-space MCP tools not offered: " + why)
            return []
        # Defence in depth beside the owner-only session: the shim runs `inbox room-tools` before every call and
        # forwards it only while a task of the owner's is in flight here.
        gate = {"name": mcp_name_shim.GATE_ENV, "value": json.dumps(gate_argv(os.path.abspath(root)))}
        got = dict(got, env=list(got["env"]) + [gate])
        path = write_mcp_config(state, got)
    except Exception as e:     # the tools never cost the session its start
        log("ag2-space MCP tools not offered: %s: %s" % (type(e).__name__, str(e)[:200]))
        return []
    log("ag2-space MCP tools offered (%d tools granted)" % len(ROOM_TOOLS))
    return ["--mcp-config", path]


TOOLS_MARK = ROOM_TOOLS_DIR + ("session.json",)


def mark_tools_generation(root, pid=None, session_id="", start_time=None) -> bool:
    """Record that the session process `pid` (with its start time) runs the room tools' conversation, so
    admission and the gate read it from the running generation, never from a config that may have changed
    since. With no pid: remove the mark (another conversation). False when it could not be written or removed."""
    path = Path(root).joinpath(*TOOLS_MARK)
    try:
        if pid is None:
            path.unlink(missing_ok=True)
            return True
        start = (start_time or task_io.start_time)(pid)
        if start in (None, ""):     # half a mark is read as unknown: say it could not be written
            return False
        cm._write(path, json.dumps({"pid": pid, "start": start, "session_id": session_id}) + "\n", 0o600)
        return True
    except OSError:
        return False


TITLE_OFF_VAR = "CLAUDE_CODE_DISABLE_TERMINAL_TITLE"


def child_env(environ: dict, inode: str) -> dict:
    """The pane's environment plus the inode bind and the relative task I/O paths; Claude's own
    title updates are off so the pane keeps hold's token, the fence's per-process discriminator."""
    env = dict(environ)
    env.update({INODE_VAR: inode, "AG2_INBOX_DIR": ".", "AG2_RESULTS_DIR": os.path.join("..", "results"),
                TITLE_OFF_VAR: "1"})
    return env


def _refuse(line: str) -> int:
    print("hold: refusing to start: %s" % line, file=sys.stderr)
    return cm.EXIT_UNUSABLE


def spawn_shim(argv: list, env: dict, popen=subprocess.Popen):
    """The shim with only the go pipe's read end passed (close_fds); returns (child, write_fd)."""
    go_r, go_w = os.pipe()
    try:
        child = popen([sys.executable, "-c", SHIM, str(go_r), *argv], env=env, close_fds=True, pass_fds=(go_r,))
    except BaseException:
        os.close(go_w)
        raise
    finally:
        os.close(go_r)
    return child, go_w


def reap(child, go_w) -> None:
    """A failed publish, attach or go write: close the pipe unwritten (the shim exits on EOF) and reap."""
    try:
        os.close(go_w)
    except OSError:
        pass
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=10)


def main(args, popen=subprocess.Popen, environ=None) -> int:
    env = dict(os.environ if environ is None else environ)
    nonce = args.generation or ""
    if not NONCE_RE.match(nonce):
        return _refuse("--generation is 32 hex characters")
    root, config_dir, session_id = env.get(ENV_ROOT, ""), env.get(ENV_CONFIG_DIR, ""), env.get(ENV_SESSION_ID, "")
    if not (root and os.path.isabs(root) and config_dir and session_id):
        return _refuse("%s, %s and %s must be set in the pane's environment" % (ENV_ROOT, ENV_CONFIG_DIR, ENV_SESSION_ID))
    try:
        cfg = cm.read_config(Path(config_dir) / cm.config_path(LOCAL).name)
    except (OSError, ValueError) as e:
        return _refuse("the config could not be read (%s)" % e)
    if not all(cfg.get(k) for k in ("CLAUDE", "PLUGIN_DIR")):
        return _refuse("the config lacks CLAUDE or PLUGIN_DIR")
    set_title(title_token())
    state = cm.stop_state()
    before = cm.stop_handlers(state)
    hup = signal.signal(signal.SIGHUP, signal.getsignal(signal.SIGTERM))   # kill-session hangs up the pane
    try:
        return _held(root, cfg, session_id, nonce, env, popen, state)
    finally:
        signal.signal(signal.SIGHUP, hup)
        cm.restore_handlers(state, before)


def _held(root, cfg, session_id, nonce, env, popen, state) -> int:
    """connect_common's holder sequence with this shape's steps: prepare the root and fchdir into
    `tasks` under the held fd, spawn the gated shim, write its go byte once attached, reap it otherwise."""
    pipe = {}

    def prepare(owner, root):
        problem = inbox.prepare_root(root)
        if problem:
            return None, problem
        recovered = inbox.recover_dead_generation(root)
        if recovered:
            print("hold: recovered %d claim(s) of the dead generation" % recovered, file=sys.stderr)
        try:
            inbox_fd = os.open("tasks", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=owner.root_fd)
        except OSError as e:
            what = "tasks/ under the held root is a symlink, not a directory" if cm._is_link("tasks", owner.root_fd) \
                else "no tasks/ under the held root %s (%s)" % (root, e.strerror or e)
            return None, what
        try:
            st = os.fstat(inbox_fd)
            os.fchdir(inbox_fd)
        finally:
            os.close(inbox_fd)
        return "%d:%d" % (st.st_dev, st.st_ino), ""

    def spawn(inode):
        picked = settle_wanted(cfg.get("ROOT"))
        if picked:
            print("hold: starting on %s, the model picked while the session could not switch" % picked, file=sys.stderr)
        # Last: --mcp-config takes every word after it, and nothing positional follows.
        plain = claude_argv(cfg, session_id)
        tools = room_tools_argv(cfg, root, os.path.join(root, "tasks"), session_id=session_id)
        child, pipe["go_w"] = spawn_shim(plain + tools, child_env(env, inode), popen)
        # Before the gated child's go byte: a session on the tools' conversation is marked owner-only, tools on
        # or not, and is not started when it cannot be marked. Any other start clears an older mark.
        if tools_conversation(cfg, session_id):
            if not mark_tools_generation(root, child.pid, session_id):
                reap(child, pipe["go_w"])
                raise OSError("the session's owner-only mark could not be written, and without it a "
                              "collaborator's task could enter the conversation that has the room tools' results")
        elif not mark_tools_generation(root):
            print("hold: an older owner-only mark could not be removed; collaborators' tasks stay outside this "
                  "session until it is", file=sys.stderr)
        return child

    def started(child):
        while True:
            try:
                if os.write(pipe["go_w"], GO) != 1:
                    raise OSError("short write")
                break
            except InterruptedError:
                continue
        os.close(pipe["go_w"])
    return cm.hold_and_supervise(LOCAL, root, state, prepare, spawn, lambda child: _supervise(child, state),
                                 lambda child: reap(child, pipe["go_w"]), "hold", "session", "shim", started)


def _supervise(child, state) -> int:
    """Wait for the direct child; a recorded stop terminates it, bounded, then kills; its exit code."""
    return cm.supervise_child(child, state, "hold", "session")
