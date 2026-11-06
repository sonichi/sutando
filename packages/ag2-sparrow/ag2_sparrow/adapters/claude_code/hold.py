"""`ag2-sparrow-claude-local hold --generation=<32 hex> --`: the pane's thin parent.

Started by the launcher as the pane's exact command; the work root, config dir, session id and HITL
store root come from the pane's session environment. It sets the pane title to a fresh token, takes
the canonical root (schema 2), publishes the owner record, recovers the dead predecessor generation
under that flock, opens `tasks` O_NOFOLLOW from the held root fd and fchdirs there, spawns the exec
shim gated on one go byte, attaches the shim as the consumer whose liveness fences the root, and only
then writes the go byte. A failed publish, attach or go write reaps the shim: no consumer exists.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from ... import connect_common as cm, task_io
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


def transcript_exists(config_dir: str, session_id: str) -> bool:
    """Whether the session's transcript already lives under CLAUDE_CONFIG_DIR's projects tree."""
    base = os.path.join(config_dir or os.path.join(os.path.expanduser("~"), ".claude"), "projects")
    try:
        projects = os.listdir(base)
    except OSError:
        return False
    return any(os.path.isfile(os.path.join(base, p, session_id + ".jsonl")) for p in projects)


def claude_argv(cfg: dict, session_id: str) -> list:
    """`claude --plugin-dir … --permission-mode … --resume|--session-id <id>`: resume when the
    transcript exists under the session's config dir, else start it under that id."""
    flag = "--resume" if transcript_exists(cfg.get("CLAUDE_CONFIG_DIR", ""), session_id) else "--session-id"
    return [cfg["CLAUDE"], "--plugin-dir", str(resolve_plugin_dir(cfg["PLUGIN_DIR"])[0]), "--permission-mode",
            cfg.get("PERMISSION_MODE") or "bypassPermissions", flag, session_id]


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
        child, pipe["go_w"] = spawn_shim(claude_argv(cfg, session_id), child_env(env, inode), popen)
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
