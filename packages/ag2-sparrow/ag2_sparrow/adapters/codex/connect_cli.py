#!/usr/bin/env python3
"""`ag2-sparrow-codex-local`: host the Codex connector on this machine as an AG2 Space agent.

One console script per adapter, as `ag2-sparrow-claude-local` and `ag2-sparrow-hatch`; the lifecycle
is `ag2_sparrow.connect_common`'s, as one owner: `install` records the work root and the Codex settings
in a 0600 marked config and writes the marked unit (a systemd user unit on Linux, a launchd agent on
macOS) as a quiescent replacement, then requires the managed launcher to hold the root. `run` is what
the manager runs and nothing else (`--launchd` is the shared entry shim): it checks the Codex CLI
version, then the shared holder sequence takes the work root's lock for the launcher's life, starts
the connector as its direct child on its own `codex app-server` (stdio) from the exact executable the
check passed, with `--held`, attaches it as the consumer whose liveness fences the root, and reaps it
on SIGTERM; a root another session holds is exit 75, which the manager does not relaunch over.
`verify` checks each part; `uninstall` removes only what install wrote. The relay token stays in
Sparrow's env file and the Codex login in CODEX_HOME: neither is copied. Stdlib only.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ... import _dirs, connect_common as cm, task_io
from ...connect_common import EXIT_CHILD_HELD, EXIT_HELD, EXIT_UNUSABLE, RUN_TIMEOUT_S, Shape, _v
from . import codex_connector

SHAPE = "codex-local"
COMMAND = "ag2-sparrow-codex-local"
MIN_CODEX = (0, 160, 1)
SANDBOXES = ("read-only", "workspace-write")
ENV_KEEP = ("PATH", "HOME", "USER")
REQUIRED_KEYS = ("CODEX", "INBOX_DIR", "RESULTS_DIR")
CONFIG_KEYS = ("CODEX", "INBOX_DIR", "RESULTS_DIR", "CODEX_HOME", "SANDBOX", "ALLOW_NON_OWNER", "MODEL")
LOCAL = Shape(name=SHAPE, command=COMMAND, description="the Codex connector as an AG2 Space agent",
              cli="Codex CLI", min_version=MIN_CODEX, version_anchor=r"^codex",
              version_note="the app-server stdio shape was measured there", required_keys=REQUIRED_KEYS,
              config_keys=CONFIG_KEYS, env_keep=ENV_KEEP,
              kept="the Codex login (CODEX_HOME) and the work root with state/threads, the owner's thread history",
              run_args=("run", "--launchd"), keep_alive_on_success=False)
MARKER, PLIST_MARKER, SERVICE_NAME, AGENT_LABEL = LOCAL.marker, LOCAL.plist_marker, LOCAL.service_name, LOCAL.agent_label
WORK_DIRS = ("tasks", "results", "state", os.path.join("state", "threads"))
LABELS = ("launcher", "connector")     # the holder and its consumer, in verify's and install's words


def config_path() -> Path:
    return cm.config_path(LOCAL)


def version_problem(codex: str, run=subprocess.run) -> str:
    return cm.version_problem(codex, LOCAL, run)


def load_config() -> "tuple[dict, str]":
    return cm.load_config(LOCAL)


def child_env(cfg: dict, environ=None) -> dict:
    """What the connector and its app-server see: the allowlist, the configured codex first on PATH,
    the package root for `-m`, and CODEX_HOME when one was recorded; nothing else."""
    env = cm.child_env_from(ENV_KEEP, environ)
    rest = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    env["PATH"] = os.pathsep.join([os.path.dirname(cfg["CODEX"])] + rest)
    # The connector runs from the same package as this launcher, wherever Python found it.
    env["PYTHONPATH"] = str(Path(codex_connector.__file__).resolve().parents[3])
    if cfg.get("CODEX_HOME"):
        env["CODEX_HOME"] = cfg["CODEX_HOME"]
    return env


def connector_argv(cfg: dict, root: str, root_fd: int) -> list:
    """The connector under this launcher's lock (--held, --root-fd: the held inode it fchdirs into, so a
    renamed or replaced pathname never reaches it), its app-server from the exact executable the version
    check passed (--codex, never a PATH lookup): never --once, --idle-exit, --push, --thread-id,
    --ephemeral, --developer-instructions, nor the stub or proxy server, none of which belong under a
    manager."""
    argv = [sys.executable, "-m", codex_connector.__name__, "--work", root, "--held", "--root-fd", str(root_fd),
            "--codex", cfg["CODEX"], "--server", "stdio", "--sandbox", cfg.get("SANDBOX") or "read-only"]
    if cfg.get("ALLOW_NON_OWNER") == "1":
        argv.append("--allow-non-owner")
    if cfg.get("MODEL"):
        argv += ["--model", cfg["MODEL"]]
    return argv


def login_problem(cfg: dict, run_=subprocess.run) -> str:
    """'' when `codex login status` exits 0 under the configured CODEX_HOME, else why not."""
    try:
        r = run_([cfg["CODEX"], "login", "status"], capture_output=True, text=True, timeout=RUN_TIMEOUT_S,
                 env=child_env(cfg))
    except (OSError, subprocess.SubprocessError) as e:
        return f"codex login status did not run ({type(e).__name__})"
    if r.returncode != 0:
        return f"not logged in under CODEX_HOME={cfg.get('CODEX_HOME') or '(default)'}; run codex login there"
    return ""


def holder_words(root: str, _rec=None) -> str:
    """Who holds the root, as verify and install both say it: the launcher and, once attached, its connector."""
    return cm.holder_words(root, LABELS)


def occupancy_line(root: str) -> str:
    """The shared tri-state beside the manager's state, in this shape's nouns."""
    return cm.occupancy_line(LOCAL, root, LABELS)


# ── commands ─────────────────────────────────────────────────────────────────

def install(args) -> int:
    codex = shutil.which(args.codex) or args.codex
    problem = version_problem(codex)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    root, problem = cm.canonical_root(args.work, "")
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    values = {"CODEX": os.path.abspath(codex), "INBOX_DIR": str(Path(root) / "tasks"),
              "RESULTS_DIR": str(Path(root) / "results"),
              "CODEX_HOME": str(Path(args.codex_home).expanduser().resolve()) if args.codex_home else "",
              "SANDBOX": args.sandbox, "ALLOW_NON_OWNER": "1" if args.allow_non_owner else "",
              "MODEL": args.model or ""}
    if args.sandbox == "workspace-write":
        print("NOTE sandbox: workspace-write lets an owner turn write under the work root; a turn retried after "
              "the app-server dies can repeat side effects the first attempt already made")
    return cm.install(LOCAL, values, root, holder=holder_words)


def run(args, popen=subprocess.Popen) -> int:
    """What the manager runs, and only it: refused without the marker the unit sets; `--launchd` is the
    shared entry shim that runs the launcher as a child and maps its outcome (logical 75 to exit 0, so
    launchd stops instead of relaunching over the holder). `debug-run` is the hand start: its
    check-then-acquire runs under the shape's operation lock (install and uninstall take the same one)
    and its holder record carries the debug label, which install never accepts as its launcher."""
    problem = "" if args.debug else cm.managed_problem(LOCAL)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    if getattr(args, "launchd", False):
        return cm.run_launchd(cm.run_child_argv(LOCAL, __name__), popen)
    op = cm.operation_lock(LOCAL) if args.debug else None
    if op is not None:
        try:
            op.__enter__()
        except cm.Busy as e:
            print(f"error: {e}", file=sys.stderr)
            return EXIT_UNUSABLE
    try:
        return _checked(popen, op)
    finally:
        if op is not None:
            op.release()


def _checked(popen, op) -> int:
    cfg, problem = load_config()
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    problem = version_problem(cfg["CODEX"])
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    state = cm.stop_state()
    before = cm.stop_handlers(state)
    try:
        return _held(cfg, cm.work_root(cfg), popen, state, cm.debug_adapter(LOCAL) if op is not None else "",
                     op.release if op is not None else None)
    finally:
        cm.restore_handlers(state, before)


def _held(cfg: dict, root: str, popen, state: dict, adapter: str = "", acquired=None) -> int:
    """connect_common's holder sequence with this shape's steps: the connector's dirs under the held
    inode, the connector spawned under the allowlist with the held root's descriptor, its own 75
    reported as 1."""
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
    """The shared wait; the connector's own 75 is reported as 1, so 75 means only another holder of the root."""
    rc = cm.supervise_child(child, state, "run", "connector")
    if rc == EXIT_HELD and state["sig"] is None:
        print(f"run: the connector exited {EXIT_HELD}; reported as {EXIT_CHILD_HELD}", file=sys.stderr)
        return EXIT_CHILD_HELD
    return rc


def verify(_args) -> int:
    results: list = []
    cfg, problem = load_config()
    cm.check(results, not problem, "config", problem or str(config_path()))
    if problem:
        return 1
    problem = version_problem(cfg["CODEX"])
    cm.check(results, not problem, "codex", problem or f"{cfg['CODEX']} {_v(cm.cli_version(cfg['CODEX'], LOCAL.version_anchor))}")
    if not problem:
        problem = login_problem(cfg)
        cm.check(results, not problem, "login", problem or "codex login status: logged in")
    for key in ("INBOX_DIR", "RESULTS_DIR"):
        cm.check(results, Path(cfg[key]).is_dir(), key.lower().replace("_dir", ""), cfg[key])
    print(f"INFO sandbox: {cfg.get('SANDBOX') or 'read-only'} for owner turns; non-owner turns are always read-only")
    print(f"INFO non-owner: {'served read-only (--allow-non-owner)' if cfg.get('ALLOW_NON_OWNER') == '1' else 'refused unless attested collaborator'}")
    cm.verify_tail(LOCAL, cfg, results, occupancy=occupancy_line,
                   relay=lambda results_: cm.relay_check(results_, _dirs.device_env_path()))
    return 0 if all(results) else 1


def uninstall(_args) -> int:
    return cm.uninstall(LOCAL)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=COMMAND, description="The Codex connector on this machine as an AG2 Space "
                                 "agent (Codex CLI %s+)." % _v(MIN_CODEX))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="record the settings, write the unit, have the manager start the connector "
                       "(a reinstall stops the old one first)")
    p.add_argument("--work", required=True, help="the work root: Sparrow's tasks/ and results/ must be <work>/tasks "
                   "and <work>/results")
    p.add_argument("--codex", default="codex", help="the codex executable (default: codex on PATH)")
    p.add_argument("--codex-home", help="CODEX_HOME holding the login the connector uses (default: the CLI's own)")
    p.add_argument("--sandbox", choices=SANDBOXES, default="read-only",
                   help="the owner threads' sandbox (default read-only; non-owner turns are always read-only)")
    p.add_argument("--allow-non-owner", action="store_true",
                   help="serve non-owner senders without collaborator access too, on an ephemeral read-only thread")
    p.add_argument("--model", help="the model for every turn (default: the connector's)")
    p.set_defaults(func=install)
    run_help = (f"what the manager runs: the launcher of the connector on its own app-server; refused unless "
                f"{cm.MANAGED_VAR}=1 is in the environment, as the unit sets it (a guard against an accidental hand "
                "start, not a security boundary)")
    r = sub.add_parser("run", help=run_help, description=run_help)
    r.add_argument("--launchd", action="store_true", help="launchd's entry shim: runs the launcher as a child and "
                   "maps logical 75 to exit 0, everything else (and a signal death) to non-zero")
    r.set_defaults(func=run, debug=False)
    sub.add_parser("debug-run", help="the same launcher by hand, for a look at the connector (stop the job first: a "
                   "held root is refused with 75; its holder is never taken for the managed one)"
                   ).set_defaults(func=run, debug=True, launchd=False)
    sub.add_parser("verify", help="check every part").set_defaults(func=verify)
    sub.add_parser("uninstall", help="stop the job, then remove what install wrote (config last)").set_defaults(func=uninstall)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
