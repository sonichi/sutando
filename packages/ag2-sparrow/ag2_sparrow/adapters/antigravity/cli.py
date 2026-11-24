#!/usr/bin/env python3
"""`ag2-sparrow-antigravity-local`: host Antigravity CLI (`agy`) on this machine as an s.space agent.

`install` checks that the ACP command answers and is signed in (refused otherwise), records it, the
Session working directory and the session store in a 0600 marked config, writes the marked
unit (a systemd user unit on Linux, a launchd agent on macOS) and requires the managed launcher to hold
the work root; `run` is what the manager runs: connect_common's holder sequence starts the connector as
its direct child with `--held`. `verify` checks each part, the sign-in included; `uninstall` removes only
what install wrote; `list` shows the jobs. The lifecycle is connect_common's and the agent's own bridge is
connect_bridge's, as for codex-local and openclaw. With `--token-env-file` (or `--token-stdin`) and no
`--work`, the root is the agent's own and install also wires its bridge. The Antigravity sign-in stays
where agy keeps it (the macOS Keychain): nothing copies it. Stdlib only.
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ... import _dirs, connect_bridge as cb, connect_common as cm, live_view, model_settings, task_io
from ...acp_client import AUTH_REQUIRED, AcpError
from ...connect_bridge import EXTERNAL, UNIT
from ...connect_common import EXIT_CHILD_HELD, EXIT_HELD, EXIT_UNUSABLE, Shape
from . import antigravity_connector as gc
from .gemini_key import authenticate_with_app_key

SHAPE = "antigravity-local"
COMMAND = "ag2-sparrow-antigravity-local"
ENV_KEEP = ("PATH", "HOME", "USER", "LANG", "TMPDIR", "SHELL")
REQUIRED_KEYS = ("ACP_COMMAND", "CWD", "INBOX_DIR", "RESULTS_DIR")
CONFIG_KEYS = ("ACP_COMMAND", "CWD", "SESSION_STORE", "INBOX_DIR", "RESULTS_DIR",
               "ALLOW_NON_OWNER", "ALLOW_OWNER_SHELL_WEB", "MCP_PROXY_COMMAND") + cb.CONFIG_KEYS
LOCAL = Shape(name=SHAPE, command=COMMAND, description="Antigravity CLI on Sparrow",
              cli="Antigravity CLI", min_version=(0, 0, 0), version_anchor=r"^\d",
              version_note="the ACP wake and the sign-in are checked by verify, not by a version",
              required_keys=REQUIRED_KEYS, config_keys=CONFIG_KEYS, env_keep=ENV_KEEP,
              kept="the Antigravity sign-in (agy's own, in the Keychain) and the session store with every room's session id",
              run_args=("run", "--launchd"), keep_alive_on_success=False)
WORK_DIRS = ("tasks", "results", "state", "workspace")
DEFAULT_CWD = "workspace"   # under the work root: the Session directory when --cwd is not given
LABELS = ("launcher", "connector")
PROBE_TIMEOUT_S = 60.0


def config_path(shape: Shape = LOCAL) -> Path:
    return cm.config_path(shape)


def load_config(shape: Shape = LOCAL) -> "tuple[dict, str]":
    return cm.load_config(shape)


def acp_command(acp: "str | None") -> "tuple[str, str]":
    """(the ACP command line with its executable made absolute, '') or ('', why)."""
    argv = shlex.split(acp or gc.DEFAULT_ACP_COMMAND)
    if not argv:
        return "", "--acp-command is empty"
    exe = shutil.which(argv[0])
    if not exe:
        return "", (f"{argv[0]} is not on PATH; install Antigravity CLI (curl -fsSL "
                    "https://antigravity.google/cli/install.sh | bash) or pass --acp-command with its full path")
    return shlex.join([os.path.abspath(exe)] + argv[1:]), ""


def mcp_proxy_command(value: "str | None") -> "tuple[str, str]":
    """(the stdio proxy's command line, each word quoted, '') or ('', why). Its first word must be an executable
    file: an unquoted path with a space (the desktop's engine sits under Application Support) splits there, so
    it is refused with the quoting to use rather than recorded broken."""
    if not value:
        return "", ""
    try:
        argv = shlex.split(value)
    except ValueError as e:
        return "", f"--mcp-proxy-command does not parse ({e})"
    exe = shutil.which(argv[0]) if argv else None
    if not exe or len(argv) < 2:
        return "", ("--mcp-proxy-command must name an executable and its arguments, each path quoted when it has "
                    "a space: --mcp-proxy-command \"'<node>' '<path>/ag2-mcp-proxy.mjs'\"")
    return shlex.join([os.path.abspath(exe)] + argv[1:]), ""


def child_env(cfg: dict, environ=None) -> dict:
    """The allowlist, the ACP command's own directory first on PATH, and the package root for `-m`."""
    env = cm.child_env_from(ENV_KEEP, environ)
    rest = [p for p in env.get("PATH", "/usr/bin:/bin").split(os.pathsep) if p]
    first = os.path.dirname(shlex.split(cfg["ACP_COMMAND"])[0])
    env["PATH"] = os.pathsep.join([first] + [p for p in rest if p != first])
    env["PYTHONPATH"] = str(Path(gc.__file__).resolve().parents[3])
    return env


def connector_argv(cfg: dict, root: str, root_fd: int) -> list:
    argv = [sys.executable, "-m", gc.__name__, "--work", root, "--held", "--root-fd", str(root_fd),
            "--acp-command", cfg["ACP_COMMAND"], "--cwd", cfg["CWD"],
            "--bridge-state", cfg.get("BRIDGE_STATE_DIR") or str(cb.bridge_home(root) / "state")]
    if cfg.get("SESSION_STORE"):
        argv += ["--session-store", cfg["SESSION_STORE"]]
    if cfg.get("ALLOW_NON_OWNER") == "1":
        argv.append("--allow-non-owner")
    if cfg.get("ALLOW_OWNER_SHELL_WEB") == "1":
        argv.append("--allow-owner-shell-web")
    if cfg.get("TOKEN_ENV_FILE"):
        argv += ["--token-env-file", cfg["TOKEN_ENV_FILE"], "--agent-id", cfg.get("AGENT_ID", "")]
    if cfg.get("MCP_PROXY_COMMAND"):
        argv += ["--mcp-proxy-command", cfg["MCP_PROXY_COMMAND"]]
    return argv


def probe(cfg: dict, factory=gc.AcpClient, timeout: float = PROBE_TIMEOUT_S) -> "tuple[str, str]":
    """(what the agent said, '') when it answers `initialize` and opens a Session (so it is signed in), or ('', why
    not). agy lists its sign-in methods at every `initialize`, signed in or not: only `session/new` tells."""
    with tempfile.TemporaryDirectory(prefix="ag2-sparrow-antigravity-probe-") as cwd:
        try:
            client = factory(shlex.split(cfg["ACP_COMMAND"]), cwd=cwd, env=child_env(cfg))
        except OSError as e:
            return "", f"{cfg['ACP_COMMAND']} did not start ({e})"
        try:
            init = client.initialize(timeout)
            keyed = authenticate_with_app_key(client, init, timeout, shlex.split(cfg["ACP_COMMAND"]),
                                              agent_id=cfg.get("AGENT_ID") or "")
            client.call("session/new", {"cwd": cwd, "mcpServers": []}, timeout)
        except AcpError as e:
            if e.code == AUTH_REQUIRED:
                return "", gc.NOT_SIGNED_IN % e
            return "", f"{cfg['ACP_COMMAND']} did not answer ({e})"
        finally:
            client.close(5.0)
    info = init.get("agentInfo") or {}
    load = "yes" if (init.get("agentCapabilities") or {}).get("loadSession") else "no (each task opens a new session)"
    name = " ".join(str(info.get(k) or "") for k in ("name", "version")).strip() or "an ACP agent"
    signed = "on the Gemini API key saved in the app" if keyed else "signed in"
    return f"{name}, {signed}, protocol {init.get('protocolVersion')}, session resume {load}", ""


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


RECORDED = (("ACP_COMMAND", "acp_command", str), ("CWD", "cwd", str),
            ("SESSION_STORE", "session_store", str), ("ALLOW_NON_OWNER", "allow_non_owner", lambda v: v == "1"),
            ("ALLOW_OWNER_SHELL_WEB", "allow_owner_shell_web", lambda v: v == "1"),
            ("MCP_PROXY_COMMAND", "mcp_proxy_command", str))


def signed_in_problem(args, acp: str, agent_id: str = "") -> str:
    """'' when the agent answers and is signed in (checked once per install), else why not. `agent_id` is
    the agent being installed: with it the check also counts a Gemini API key saved for that agent in the
    app, as `verify` and every turn do, so an agent with a key and no Google sign-in can be installed."""
    if getattr(args, "signed_in", False):
        return ""
    _said, problem = probe({"ACP_COMMAND": acp, "AGENT_ID": agent_id or ""})
    args.signed_in = not problem
    return problem


def preflight(args) -> str:
    """'' when this host can run the agent, checked before the token is written; else why not."""
    acp, problem = acp_command(args.acp_command)
    # --token-stdin names its agent with --agent; the seat it then makes is checked against that id.
    return cb.host_problem(COMMAND) or problem or signed_in_problem(args, acp, getattr(args, "agent", None) or "")


def install(args) -> int:
    if getattr(args, "token_stdin", False):
        return cb.install_token_stdin(args, LOCAL, lambda: preflight(args), install, COMMAND, work=None)
    problem = cb.recorded_install_args(args, LOCAL, RECORDED)
    acp, why = ("", problem) if problem else acp_command(args.acp_command)
    # The agent's identity first: the sign-in check needs it to see a key saved for this agent.
    where, why = ({}, why) if why else resolve_root(args)
    if not why:
        why = cb.agent_arg_problem(getattr(args, "agent", None), where["agent_id"])
    why = why or signed_in_problem(args, acp, where["agent_id"])
    proxy, proxy_why = mcp_proxy_command(getattr(args, "mcp_proxy_command", None))
    why = why or proxy_why
    cwd = str(Path(args.cwd).expanduser().resolve()) if args.cwd else ""
    if not why and cwd and not os.path.isdir(cwd):
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
    values = {"ACP_COMMAND": acp, "CWD": cwd or str(Path(root) / DEFAULT_CWD),
              "SESSION_STORE": str(Path(args.session_store).expanduser().resolve()) if args.session_store else "",
              "INBOX_DIR": str(Path(root) / "tasks"), "RESULTS_DIR": str(Path(root) / "results"),
              "ALLOW_NON_OWNER": "1" if args.allow_non_owner else "",
              "ALLOW_OWNER_SHELL_WEB": "1" if getattr(args, "allow_owner_shell_web", False) else "", "AGENT_ID": where["agent_id"],
              "MCP_PROXY_COMMAND": proxy,
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

def verify(args, factory=gc.AcpClient) -> int:
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
        cm.check(results, not problem, "sign-in", problem or said)
    cm.check(results, Path(cfg["CWD"]).is_dir(), "session cwd", cfg["CWD"])
    for key in ("INBOX_DIR", "RESULTS_DIR"):
        cm.check(results, Path(cfg[key]).is_dir(), key.lower().replace("_dir", ""), cfg[key])
    store = cfg.get("SESSION_STORE") or str(Path(cm.work_root(cfg)) / "state" / gc.SESSIONS_NAME)
    print(f"INFO sessions: {store} (the owner's and each collaborator's resumed per room, never retired; any "
          "other non-owner's turn is a throwaway session)")
    print("INFO team (attested collaborator): the owner's turn, on the owner's agy account: the working directory, "
          "agy's own home, the owner's permission policy")
    print("INFO guests, plain team and unknown tiers: served, each turn in its own temporary directory and agy "
          "home, deny-all rules, read-only (plan) mode, no file operations")
    print("INFO owner shell and web: " + ("on (--allow-owner-shell-web): the owner's turns may run commands and "
                                          "fetch or search the web" if cfg.get("ALLOW_OWNER_SHELL_WEB") == "1"
                                          else "off (read and edit under the session cwd only)"))
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


def view(args) -> int:
    """Follow the agent's live view until interrupted. Reads one file under its work root; types nothing."""
    local, problem = selected(args)
    if not problem:
        cfg, problem = load_config(local)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    try:
        live_view.follow(live_view.path_of(cm.work_root(cfg)))
    except KeyboardInterrupt:
        pass
    return 0


def uninstall(args) -> int:
    local, problem = selected(args)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    bridge = cb.bridge_shape(local)
    return cm.uninstall(local, before_free=lambda cfg: cb.unwire_bridge(cfg, COMMAND, bridge), token_file=cb.kept_token)


def list_cmd(_args) -> int:
    return cm.list_jobs(LOCAL)


def model(args) -> int:
    """The app's settings page: the agent's model, and the owner's pick (`--set`), in the state the
    model card keeps; every next turn runs on the pick, whoever sends it (model_settings)."""
    local, problem = selected(args)
    cfg, problem = (None, problem) if problem else load_config(local)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    return model_settings.command(Path(cm.work_root(cfg)) / "state", args.set)


# ── main ─────────────────────────────────────────────────────────────────────

def session_options(p) -> None:
    p.add_argument("--acp-command", help="the ACP agent's command line (default: agy acp)")
    p.add_argument("--cwd", help="the Session working directory, where the owner's turns may read and edit "
                   "(default: <work root>/workspace)")
    p.add_argument("--session-store", help="the sessions.json to resume from and record to (default: the work "
                   "root's state/sessions.json)")
    p.add_argument("--mcp-proxy-command", help="the stdio ag2-mcp-proxy command line ('<node> <the desktop app's "
                   "engine/ag2-mcp-proxy.mjs>'): AG2 Space tools for an agy that declares no HTTP MCP")
    p.add_argument("--allow-non-owner", action="store_true",
                   help="no effect, kept so existing configs parse: an attested collaborator always gets the "
                   "owner's turn, any other non-owner an isolated read-only one (all on the owner's agy sign-in)")
    p.add_argument("--allow-owner-shell-web", action="store_true",
                   help="let the owner's turns run shell commands and fetch or search the web, as a local Claude "
                   "Code agent may, and an attested collaborator's turn run as the owner's (never plain Team's or a "
                   "guest's; default: off)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=COMMAND, description="Antigravity CLI (agy) on this machine as an agent on "
                                 "Sparrow, woken over ACP.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="check the sign-in, record the settings, write the unit, have the manager "
                       "start the connector")
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
    v = sub.add_parser("verify", help="check every part, including the ACP wake and the sign-in")
    cm.job_options(v)
    v.add_argument("--no-probe", action="store_true", help="skip starting the ACP command")
    v.set_defaults(func=verify)
    cm.job_options(sub.add_parser("view", help="follow what the agent is doing, read-only: agy runs over ACP with no "
                                  "terminal of its own, so this prints each turn's words, tool calls and permission "
                                  "decisions as the connector sees them. Ends with Ctrl-C")).set_defaults(func=view)
    cm.job_options(sub.add_parser("uninstall", help="stop the job, then remove what install wrote (config last)")
                   ).set_defaults(func=uninstall)
    sub.add_parser("list", help="each job installed here").set_defaults(func=list_cmd)
    m = cm.job_options(sub.add_parser("model", help="print the agent's model as JSON (current, available, pending), "
                                                    "or pick the one every next turn runs on (--set)"))
    m.add_argument("--set", metavar="ID", help="a model id agy listed")
    m.set_defaults(func=model)
    args = ap.parse_args(argv)
    args.argv = list(sys.argv[1:] if argv is None else argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
