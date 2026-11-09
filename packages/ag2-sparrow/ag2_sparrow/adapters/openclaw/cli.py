#!/usr/bin/env python3
"""`ag2-sparrow-openclaw`: host an OpenClaw agent on Sparrow, and the commands it answers with.

`install` records the ACP command (`openclaw acp`, or `openclaw --profile <p> acp`), the Session working
directory and the session store in a 0600 marked config, writes the marked unit (a systemd user unit on
Linux, a launchd agent on macOS) and requires the managed launcher to hold the work root; `run` is what
the manager runs: connect_common's holder sequence starts the connector as its direct child with
`--held`. `verify` checks each part, including that the ACP command answers `initialize`; `uninstall`
removes only what install wrote; `list` shows the jobs. The lifecycle is connect_common's and the agent's
own bridge is connect_bridge's, as for codex-local. With `--token-env-file` (or `--token-stdin`) and no
`--work`, the root is the agent's own and install also wires its bridge.

`reply <result_file>` (text on stdin) and `no-reply <result_file>` are what the woken agent runs: each
claims its task once and publishes the result Sparrow delivers. Stdlib only.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import sysconfig
from pathlib import Path

from ... import _dirs, connect_bridge as cb, connect_common as cm, result_labels, task_io
from ...connect_bridge import EXTERNAL, UNIT
from ...result_markers import parse_markers
from ...connect_common import EXIT_CHILD_HELD, EXIT_HELD, EXIT_UNUSABLE, Shape
from . import openclaw_connector as oc
from .acp_client import AcpClient, AcpError

SHAPE = "openclaw-local"
COMMAND = "ag2-sparrow-openclaw"
ENV_KEEP = ("PATH", "HOME", "USER", "LANG", "TMPDIR", "SHELL")
REQUIRED_KEYS = ("ACP_COMMAND", "CWD", "REPLY_COMMAND", "INBOX_DIR", "RESULTS_DIR")
CONFIG_KEYS = ("ACP_COMMAND", "CWD", "SESSION_STORE", "REPLY_COMMAND", "INBOX_DIR", "RESULTS_DIR",
               "ALLOW_NON_OWNER") + cb.CONFIG_KEYS
LOCAL = Shape(name=SHAPE, command=COMMAND, description="the OpenClaw connector on Sparrow",
              cli="OpenClaw CLI", min_version=(0, 0, 0), version_anchor=r"(?i)openclaw",
              version_note="the ACP wake was checked by verify, not by a version", required_keys=REQUIRED_KEYS,
              config_keys=CONFIG_KEYS, env_keep=ENV_KEEP,
              kept="the OpenClaw profile and its sessions, and the session store with every room's session id",
              run_args=("run", "--launchd"), keep_alive_on_success=False)
WORK_DIRS = ("tasks", "results", "state")
LABELS = ("launcher", "connector")
PROBE_TIMEOUT_S = 30.0
REPLY_MAX_BYTES = 64 * 1024
REASON_MAX = 200
# `reply` / `no-reply` exit codes: queued for Sparrow, already answered, usage.
REPLY_QUEUED, REPLY_ANSWERED, REPLY_USAGE = 0, 1, 2


def config_path(shape: Shape = LOCAL) -> Path:
    return cm.config_path(shape)


def load_config(shape: Shape = LOCAL) -> "tuple[dict, str]":
    return cm.load_config(shape)


def find_reply_command() -> str:
    """The console script beside this interpreter, else on PATH, else this module under this interpreter."""
    here = Path(sysconfig.get_path("scripts") or "") / COMMAND
    if here.is_file() and os.access(here, os.X_OK):
        return shlex.quote(str(here))
    found = shutil.which(COMMAND)
    if found:
        return shlex.quote(os.path.abspath(found))
    return shlex.join([sys.executable, "-m", __name__])


def acp_command(acp: "str | None", profile: "str | None") -> "tuple[str, str]":
    """(the ACP command line with its executable made absolute, '') or ('', why)."""
    if profile and acp:
        return "", "pass --acp-command or --profile, not both"
    argv = ["openclaw", "--profile", profile, "acp"] if profile else shlex.split(acp or "openclaw acp")
    if not argv:
        return "", "--acp-command is empty"
    exe = shutil.which(argv[0])
    if not exe:
        return "", f"{argv[0]} is not on PATH; install OpenClaw (or pass --acp-command with its full path)"
    return shlex.join([os.path.abspath(exe)] + argv[1:]), ""


def child_env(cfg: dict, environ=None) -> dict:
    """The allowlist, the ACP command's own directory first on PATH (its node runtime is usually beside it),
    and the package root for `-m`; nothing else."""
    env = cm.child_env_from(ENV_KEEP, environ)
    rest = [p for p in env.get("PATH", "/usr/bin:/bin").split(os.pathsep) if p]
    first = os.path.dirname(shlex.split(cfg["ACP_COMMAND"])[0])
    env["PATH"] = os.pathsep.join([first] + [p for p in rest if p != first])
    env["PYTHONPATH"] = str(Path(oc.__file__).resolve().parents[3])
    return env


def connector_argv(cfg: dict, root: str, root_fd: int) -> list:
    argv = [sys.executable, "-m", oc.__name__, "--work", root, "--held", "--root-fd", str(root_fd),
            "--acp-command", cfg["ACP_COMMAND"], "--cwd", cfg["CWD"], "--reply-command", cfg["REPLY_COMMAND"]]
    if cfg.get("SESSION_STORE"):
        argv += ["--session-store", cfg["SESSION_STORE"]]
    if cfg.get("ALLOW_NON_OWNER") == "1":
        argv.append("--allow-non-owner")
    return argv


def probe(cfg: dict, factory=AcpClient, timeout: float = PROBE_TIMEOUT_S) -> "tuple[str, str]":
    """(what the ACP agent said at initialize, '') or ('', why it did not answer)."""
    try:
        client = factory(shlex.split(cfg["ACP_COMMAND"]), cwd=cfg["CWD"], env=child_env(cfg))
    except OSError as e:
        return "", f"{cfg['ACP_COMMAND']} did not start ({e})"
    try:
        init = client.initialize(timeout)
    except AcpError as e:
        return "", f"{cfg['ACP_COMMAND']} did not answer initialize ({e})"
    finally:
        client.close(5.0)
    info = init.get("agentInfo") or {}
    load = "yes" if (init.get("agentCapabilities") or {}).get("loadSession") else "no (each task opens a new session)"
    name = " ".join(str(info.get(k) or "") for k in ("name", "version")).strip() or "an ACP agent"
    return f"{name}, protocol {init.get('protocolVersion')}, session resume {load}", ""


def holder_words(root: str, _rec=None) -> str:
    return cm.holder_words(root, LABELS)


def occupancy_line(root: str, local: Shape = LOCAL) -> str:
    return cm.occupancy_line(local, root, LABELS)


# ── install ──────────────────────────────────────────────────────────────────

def resolve_root(args) -> "tuple[dict, str]":
    """({root, mode, agent_id, token_file}, '') or ({}, why), as codex-local resolves it."""
    if args.work:
        if args.token_env_file:
            return {}, "--token-env-file is for the bridge this install runs; with --work your bridge has its own"
        root, problem = cm.canonical_root(args.work, "")
        return {"root": root, "mode": EXTERNAL, "agent_id": "", "token_file": ""}, problem
    if not args.token_env_file:
        return {}, ("pass --work <dir> (a root your bridge feeds) or --token-env-file <the agent's .env> (the agent's "
                    "own root, whose bridge install runs)")
    mode = _dirs.bridge_host() or UNIT
    token_file = str(Path(args.token_env_file).expanduser().resolve())
    if not os.path.isfile(token_file):
        return {}, f"{token_file} is missing: put the agent's REMOTE_TASK_TOKEN there (mode 600)"
    agent_id = cb.install_identity(token_file, False)
    if not agent_id:
        return {}, f"the agent id is unknown: set AGENT_MXID in {token_file}, or pass --work <dir>"
    _seat, problem = cb.seat_env(token_file, agent_id)
    root, problem = ("", problem) if problem else cb.agent_root(agent_id)
    if not problem and mode != UNIT:
        problem = cb.supervised_seat_problem(token_file, root, agent_id, mode, COMMAND)
    return {"root": root, "mode": mode, "agent_id": agent_id, "token_file": token_file}, problem


RECORDED = (("ACP_COMMAND", "acp_command", str), ("CWD", "cwd", str), ("SESSION_STORE", "session_store", str),
            ("ALLOW_NON_OWNER", "allow_non_owner", lambda v: v == "1"))


def preflight(args) -> str:
    return cb.host_problem(COMMAND) or acp_command(args.acp_command, args.profile)[1]


def install(args) -> int:
    if getattr(args, "token_stdin", False):
        return cb.install_token_stdin(args, LOCAL, lambda: preflight(args), install, COMMAND, work=None)
    problem = cb.recorded_install_args(args, LOCAL, RECORDED)
    acp, why = ("", problem) if problem else acp_command(args.acp_command, args.profile)
    where, why = ({}, why) if why else resolve_root(args)
    if not why:
        why = cb.agent_arg_problem(getattr(args, "agent", None), where["agent_id"])
    cwd = str(Path(args.cwd or os.getcwd()).expanduser().resolve())
    if not why and not os.path.isdir(cwd):
        why = f"--cwd {cwd} is not a directory"
    if why:
        print(f"error: {why}", file=sys.stderr)
        return EXIT_UNUSABLE
    root, mode = where["root"], where["mode"]
    local, problem = cm.agent_job(LOCAL, where["agent_id"]) if mode != EXTERNAL else (LOCAL, "")
    problem = problem or cm.install_selector_problem(local, getattr(args, "key", None), getattr(args, "single", False))
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    bridge = cb.bridge_shape(local)
    values = {"ACP_COMMAND": acp, "CWD": cwd,
              "SESSION_STORE": str(Path(args.session_store).expanduser().resolve()) if args.session_store else "",
              "REPLY_COMMAND": args.reply_command or find_reply_command(),
              "INBOX_DIR": str(Path(root) / "tasks"), "RESULTS_DIR": str(Path(root) / "results"),
              "ALLOW_NON_OWNER": "1" if args.allow_non_owner else "", "AGENT_ID": where["agent_id"],
              "BRIDGE": mode if mode != EXTERNAL else "", "TOKEN_ENV_FILE": where["token_file"], "TOKEN_VAULT_KEY": "",
              "BRIDGE_STATE_DIR": str(cb.bridge_home(root) / "state") if mode != EXTERNAL else ""}

    def installed() -> int:
        rc = cm.install(local, values, root, root_id=getattr(args, "root_identity", None), holder=holder_words)
        if rc != 0 or mode == EXTERNAL:
            return rc
        problem = cb.wire_bridge_bound(dict(values, ROOT=root), COMMAND, bridge, local,
                                       getattr(args, "root_identity", None))
        if problem:
            print(f"error: the connector is installed but its bridge is not: {problem}; run "
                  f"{cb.rerun_install(local, where['token_file'], cb.session_flags(args))}", file=sys.stderr)
            return EXIT_UNUSABLE
        return 0
    return cb.under_agent_lock(root, installed, local if mode != EXTERNAL else None, where["agent_id"],
                               cm.fence_name(local, where["agent_id"]),
                               orphan=lambda: cb.orphan_bridge(dict(values, ROOT=root), COMMAND, bridge, local))


# ── run ──────────────────────────────────────────────────────────────────────

def run(args, popen=subprocess.Popen) -> int:
    """What the manager runs (refused without the unit's marker); `debug-run` is the hand start under the
    shape's operation lock; `--launchd` is the shared entry shim."""
    local, why = selected(args) if args.debug else cm.job_from_env(LOCAL)
    problem = why or ("" if args.debug else cm.managed_problem(LOCAL))
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    if getattr(args, "launchd", False):
        return cm.run_launchd(cm.run_child_argv(local, __name__), popen)
    op = cm.operation_lock(local) if args.debug else None
    if op is not None:
        try:
            op.__enter__()
        except cm.Busy as e:
            print(f"error: {e}", file=sys.stderr)
            return EXIT_UNUSABLE
    try:
        return _checked(popen, op, local)
    finally:
        if op is not None:
            op.release()


def _checked(popen, op, local: Shape = LOCAL) -> int:
    cfg, problem = load_config(local)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    state = cm.stop_state()
    before = cm.stop_handlers(state)
    try:
        return cm.retired(state, _held(cfg, cm.work_root(cfg), popen, state,
                                       cm.debug_adapter(LOCAL) if op is not None else "",
                                       op.release if op is not None else None))
    finally:
        cm.restore_handlers(state, before)


def _held(cfg: dict, root: str, popen, state: dict, adapter: str = "", acquired=None) -> int:
    def prepare(owner, root):
        task_io.make_dirs(owner, WORK_DIRS)
        return (task_io.root_path(owner, root), owner.root_fd), ""

    def spawn(ctx):
        held_root, root_fd = ctx
        return popen(connector_argv(cfg, held_root, root_fd), env=child_env(cfg), cwd=held_root,
                     stdin=subprocess.DEVNULL, pass_fds=(root_fd,))
    return cm.hold_and_supervise(LOCAL, root, state, prepare, spawn, lambda child: _supervise(child, state),
                                 cm.kill_and_reap, "run", "connector", adapter=adapter, acquired=acquired)


def _supervise(child, state: dict) -> int:
    rc = cm.supervise_child(child, state, "run", "connector")
    if rc == EXIT_HELD and state["sig"] is None:
        print(f"run: the connector exited {EXIT_HELD}; reported as {EXIT_CHILD_HELD}", file=sys.stderr)
        return EXIT_CHILD_HELD
    return rc


def selected(args) -> "tuple[Shape | None, str]":
    return cm.select_job(LOCAL, getattr(args, "agent", None), getattr(args, "work", None), getattr(args, "single", False),
                         getattr(args, "key", None))


# ── verify / uninstall / list ────────────────────────────────────────────────

def verify(args, factory=AcpClient) -> int:
    local, problem = selected(args)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    bridge = cb.bridge_shape(local)
    results: list = []
    cfg, problem = load_config(local)
    cm.check(results, not problem, "config", cm.config_problem(local, problem) or str(config_path(local)))
    if problem:
        return 1
    exe = shlex.split(cfg["ACP_COMMAND"])[0]
    found = os.path.isfile(exe) and os.access(exe, os.X_OK)
    cm.check(results, found, "acp command", cfg["ACP_COMMAND"] if found else f"{exe} is missing or not executable")
    if found and not getattr(args, "no_probe", False):
        said, problem = probe(cfg, factory)
        cm.check(results, not problem, "acp wake", problem or f"initialize answered: {said}")
    cm.check(results, Path(cfg["CWD"]).is_dir(), "session cwd", cfg["CWD"])
    reply = shlex.split(cfg["REPLY_COMMAND"])[0]
    cm.check(results, os.path.isfile(reply) and os.access(reply, os.X_OK), "reply command", cfg["REPLY_COMMAND"])
    for key in ("INBOX_DIR", "RESULTS_DIR"):
        cm.check(results, Path(cfg[key]).is_dir(), key.lower().replace("_dir", ""), cfg[key])
    store = cfg.get("SESSION_STORE") or str(Path(cm.work_root(cfg)) / "state" / oc.SESSIONS_NAME)
    print(f"INFO sessions: {store} (resumed per room; never retired)")
    print(f"INFO non-owner: {'served, no file operations (--allow-non-owner)' if cfg.get('ALLOW_NON_OWNER') == '1' else 'refused unless attested collaborator'}")
    if cb.bridge_mode(cfg) != EXTERNAL:
        print(f"INFO agent: {cfg.get('AGENT_ID')}")
        problem = cb.bridge_problem(cfg, COMMAND, bridge)
        root = cm.work_root(cfg)
        cm.check(results, not problem, "bridge", problem or f"a connected bridge feeds {root}/tasks and {root}/results")
        relay = lambda results_: cb.relay_rows(results_, cfg, COMMAND, bridge)  # noqa: E731
    else:
        relay = lambda results_: cm.relay_check(results_, _dirs.device_env_path())  # noqa: E731
    cm.verify_tail(local, cfg, results, occupancy=lambda root: occupancy_line(root, local), relay=relay)
    return 0 if all(results) else 1


def uninstall(args) -> int:
    local, problem = selected(args)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    bridge = cb.bridge_shape(local)
    return cm.uninstall(local, before_free=lambda cfg: cb.unwire_bridge(cfg, COMMAND, bridge), token_file=cb.kept_token)


def list_cmd(_args) -> int:
    return cm.list_jobs(LOCAL)


# ── reply / no-reply: what the woken agent runs ─────────────────────────────

def open_task(result_file: str) -> "tuple[dict | None, str]":
    """({task_id, result_file, state}, '') for `<root>/results/<task>.txt` whose `<root>/tasks/<task>.txt` is
    open, else (None, why)."""
    path = Path(result_file).expanduser()
    if not path.is_absolute():
        return None, f"{result_file} is not the absolute result_file this task's prompt named"
    tid = path.stem
    root = path.parent.parent
    if not (path.suffix == ".txt" and oc.TASK_NAME_RE.fullmatch(tid) and path.parent.name == "results"):
        return None, f"{result_file} is not a result_file (<root>/results/task-<id>.txt)"
    if not (root / "tasks" / f"{tid}.txt").is_file():
        return None, f"no open task {tid} in {root / 'tasks'}"
    if not (root / "state").is_dir():
        return None, f"{root} has no state/; it is not a work root this adapter serves"
    return {"task_id": tid, "result_file": str(path), "state": str(root / "state")}, ""


def _answer(args, body: "str | None", why: str) -> int:
    task, problem = open_task(args.result_file)
    problem = problem or why
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return REPLY_USAGE
    try:
        sent, notes = oc.answer_once(task["state"], task["task_id"], task["result_file"], body,
                                     getattr(args, "title", None), getattr(args, "outcome", None))
    except OSError as e:
        print(f"error: not written ({e}); run it again", file=sys.stderr)
        return REPLY_USAGE
    if not sent:
        print(f"NOT sent: {task['task_id']} is already answered; nothing written", file=sys.stderr)
        return REPLY_ANSWERED
    for note in notes:
        print(f"warning: {note}", file=sys.stderr)
    print(f"queued: {Path(task['result_file']).name} {'closed with nothing posted' if body.startswith('[no-send]') else 'goes to its room'}; Sparrow delivers it")
    return REPLY_QUEUED


def _read_draft(result_file: str) -> "tuple[str, str]":
    """(the text of this task's draft, '') or ('', why): a regular file, never a link, read bounded."""
    path = oc.draft_for(result_file)
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as e:
        return "", f"no reply draft at {path} ({e.strerror}); write the reply there first"
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return "", f"{path} is not a regular file"
    with os.fdopen(fd, "rb") as f:
        return f.read(REPLY_MAX_BYTES + 1).decode("utf-8", "replace"), ""


def reply(args, stdin=None) -> int:
    if getattr(args, "draft", False):
        raw, why = _read_draft(args.result_file)
        if why:
            return _answer(args, None, why)
    else:
        raw = (stdin or sys.stdin).read(REPLY_MAX_BYTES + 1)
    if len(raw.encode("utf-8")) > REPLY_MAX_BYTES:
        return _answer(args, None, f"the reply is over {REPLY_MAX_BYTES} bytes")
    if not raw.strip():
        return _answer(args, None, "the reply is empty; to post nothing run no-reply")
    if parse_markers(raw).actions:
        return _answer(args, None, "a reply may not carry a delivery marker (redirect, attachment, skip or thread)")
    return _answer(args, raw.rstrip("\n") + "\n", "")


def no_reply(args) -> int:
    reason = " ".join((args.reason or "").split())
    if len(reason) > REASON_MAX:
        return _answer(args, None, f"--reason is over {REASON_MAX} characters")
    if not (args.outcome or "").strip() and reason:
        args.outcome = result_labels.fit(reason, result_labels.OUTCOME_MAX)
    return _answer(args, "[no-send]" + (f" {reason}" if reason else "") + "\n", "")


# ── main ─────────────────────────────────────────────────────────────────────

def session_options(p) -> None:
    p.add_argument("--acp-command", help="the ACP agent's command line (default: openclaw acp)")
    p.add_argument("--profile", help="the existing OpenClaw profile to wake: openclaw --profile <p> acp")
    p.add_argument("--cwd", help="the Session working directory (default: the current directory); keep "
                   "agent-connect's to resume its sessions")
    p.add_argument("--session-store", help="the sessions.json to resume from and record to (default: the work "
                   "root's state/sessions.json); agent-connect's own file keeps its conversations")
    p.add_argument("--allow-non-owner", action="store_true",
                   help="serve non-owner senders without collaborator access too (no file operations granted)")
    p.add_argument("--reply-command", help=argparse.SUPPRESS)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=COMMAND, description="An OpenClaw agent on Sparrow, woken over ACP.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="record the settings, write the unit, have the manager start the connector")
    p.add_argument("--work", help="an explicit work root, fed by a bridge you run (<work>/tasks, <work>/results)")
    tok = p.add_mutually_exclusive_group()
    tok.add_argument("--token-env-file", help="instead of --work: the agent's 0600 env file; the root is the "
                     "agent's own and install runs its bridge")
    tok.add_argument("--token-stdin", action="store_true", help="instead of --work, with --agent: read the agent's "
                     "relay token from stdin and write it to the agent's seat (mode 600)")
    p.add_argument("--agent", help="the agent's Matrix ID, checked against the one the token names")
    p.add_argument("--key", help="the agent key of the job this install must be (checked, never chosen by it)")
    p.add_argument("--single", action="store_true", help="checked: this install must be the fixed job")
    session_options(p)
    p.set_defaults(func=install, parser=p)
    run_help = (f"what the manager runs; refused unless {cm.MANAGED_VAR}=1 is in the environment, as the unit sets it")
    r = sub.add_parser("run", help=run_help, description=run_help)
    r.add_argument("--launchd", action="store_true", help="launchd's entry shim")
    r.set_defaults(func=run, debug=False)
    d = sub.add_parser("debug-run", help="the same launcher by hand (stop the job first)")
    cm.job_options(d)
    d.set_defaults(func=run, debug=True, launchd=False)
    v = sub.add_parser("verify", help="check every part, including the ACP wake")
    cm.job_options(v)
    v.add_argument("--no-probe", action="store_true", help="skip starting the ACP command for initialize")
    v.set_defaults(func=verify)
    cm.job_options(sub.add_parser("uninstall", help="stop the job, then remove what install wrote (config last)")
                   ).set_defaults(func=uninstall)
    sub.add_parser("list", help="each job installed here").set_defaults(func=list_cmd)
    a = sub.add_parser("reply", help="answer one task: the text on stdin becomes its reply")
    a.add_argument("result_file")
    a.add_argument("--draft", action="store_true", help="read the reply from this task's draft file, not stdin")
    a.add_argument("--title", help="the Activity title (imperative, at most 60 characters)")
    a.add_argument("--outcome", help="the Activity outcome (past tense, at most 120 characters)")
    a.set_defaults(func=reply)
    n = sub.add_parser("no-reply", help="end one task with nothing posted")
    n.add_argument("result_file")
    n.add_argument("--reason", help="why nothing is posted, one line")
    n.add_argument("--title", help="the Activity title")
    n.add_argument("--outcome", help="the Activity outcome (default: the reason)")
    n.set_defaults(func=no_reply)
    args = ap.parse_args(argv)
    args.argv = list(sys.argv[1:] if argv is None else argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
