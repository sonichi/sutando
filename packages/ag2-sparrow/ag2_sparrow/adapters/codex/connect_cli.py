#!/usr/bin/env python3
"""`ag2-sparrow-codex-local`: host the Codex connector on this machine as an AG2 Space agent.

One console script per adapter, as `ag2-sparrow-claude-local` and `ag2-sparrow-hatch`; the lifecycle
is `ag2_sparrow.connect_common`'s, as one owner: `install` records the work root and the Codex settings
in a 0600 marked config and writes the marked unit (a systemd user unit on Linux, a launchd agent on
macOS) as a quiescent replacement, then requires the managed launcher to hold the root. `run` is what
the manager runs and nothing else: it checks the Codex CLI version, takes the work root's lock for its
own life, starts the connector as its direct child on its own `codex app-server` (stdio) with `--held`,
attaches it as the consumer whose liveness fences the root, and reaps it on SIGTERM; a root another
session holds is exit 75, which the manager does not relaunch over. `verify` checks each part;
`uninstall` removes only what install wrote. The relay token stays in Sparrow's env file and the Codex
login in CODEX_HOME: neither is copied. Stdlib only.
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
              kept="the Codex login (CODEX_HOME) and the work root with state/threads, the owner's thread history")
MARKER, PLIST_MARKER, SERVICE_NAME, AGENT_LABEL = LOCAL.marker, LOCAL.plist_marker, LOCAL.service_name, LOCAL.agent_label
WORK_DIRS = ("tasks", "results", "state", os.path.join("state", "threads"))


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


def connector_argv(cfg: dict, root: str) -> list:
    """The connector under this launcher's lock (--held) on its own app-server: never --once, --idle-exit,
    --push, --thread-id, --ephemeral, --developer-instructions, nor the stub or proxy server, none of
    which belong under a manager."""
    argv = [sys.executable, "-m", codex_connector.__name__, "--work", root, "--held", "--server", "stdio",
            "--sandbox", cfg.get("SANDBOX") or "read-only"]
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
    rec, consumer = task_io.lock_holder(root), task_io.holder_consumer(root)
    return f"launcher pid {rec.get('owner_pid')}" + (f", its connector pid {consumer['pid']}"
                                                     if consumer and consumer.get("live") else ", no live connector attached")


def occupancy_line(root: str) -> str:
    """The shared tri-state beside the manager's state: held names the launcher and, once attached, the
    connector whose liveness fences the root; unobserved never reads free."""
    occ = task_io.occupancy(root)
    manager = cm.manager_detail(LOCAL)
    if occ == "held":
        return f"held by {holder_words(root)} (manager {manager})"
    if occ == "unobserved":
        return f"unobserved (the root's holder or consumer cannot be verified; manager {manager})"
    return cm.occupancy_line(LOCAL, root)


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
    """What the manager runs, and only it: refused without the marker the unit sets (`debug-run` is the
    hand start). The launcher holds the root for its own life and the connector is its consumer."""
    problem = "" if args.debug else cm.managed_problem(LOCAL)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
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
        return _held(cfg, cm.work_root(cfg), popen, state)
    finally:
        cm.restore_handlers(state, before)


def _held(cfg: dict, root: str, popen, state: dict) -> int:
    try:
        owner = task_io.acquire_lock(root, LOCAL.name, retry_s=cm.ACQUIRE_RETRY_S)
    except task_io.LockRefused as e:
        print(e.line, file=sys.stderr)
        return EXIT_HELD if e.code == 3 else EXIT_UNUSABLE
    try:
        if cm._is_link("tasks", owner.root_fd):
            return _refuse("tasks/ under the held root is a symlink, not a directory")
        task_io.make_dirs(owner, WORK_DIRS)
        try:
            root = task_io.root_path(owner, root)
        except task_io.LockRefused as e:
            print(e.line, file=sys.stderr)
            return EXIT_UNUSABLE
        if state["sig"] is not None:
            print("run: stopped before the connector started", file=sys.stderr)
            return 128 + state["sig"]
        expected = task_io.holder_record_bytes(owner)
        try:
            child = popen(connector_argv(cfg, root), env=child_env(cfg), cwd=root, stdin=subprocess.DEVNULL)
        except OSError as e:
            return _refuse(f"the connector could not be spawned ({e.strerror or e})")
        try:
            start = task_io.start_time(child.pid)
            if start is None:
                raise task_io.LockRefused(2, "the connector's start time is unreadable")
            task_io.attach_consumer(owner, expected, child.pid, start)
        except Exception as e:     # holder_lock.LockError arrives as Exception here
            _reap(child)
            return _refuse(f"no consumer: {e}")
        return _supervise(child, state)
    finally:
        if not task_io.release_lock(owner):
            print("run: the connector may still be alive; the root stays held by it and this flock until exit",
                  file=sys.stderr)


def _supervise(child, state: dict) -> int:
    """Wait for the direct child; a recorded stop terminates it, bounded, then kills; its exit code, with
    the connector's own 75 reported as 1 so 75 means only another holder of the root."""
    state["child"] = child
    rc = cm._run_until_end(child, None, state)
    if rc is None:
        print("run: the connector did not die within the bound; the manager's to clean", file=sys.stderr)
        return 128 + state["sig"]
    if state["sig"] is not None:
        return 128 + state["sig"]
    if rc == EXIT_HELD:
        print(f"run: the connector exited {EXIT_HELD}; reported as {EXIT_CHILD_HELD}", file=sys.stderr)
        return EXIT_CHILD_HELD
    return rc


def _reap(child) -> None:
    try:
        child.kill()
    except OSError:
        pass
    try:
        child.wait(timeout=cm.REAP_BOUND_S)
    except subprocess.TimeoutExpired:
        pass


def _refuse(line: str) -> int:
    print(f"run: {line}", file=sys.stderr)
    return EXIT_UNUSABLE


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
    sub.add_parser("run", help=run_help, description=run_help).set_defaults(func=run, debug=False)
    sub.add_parser("debug-run", help="the same launcher by hand, for a look at the connector (stop the job first: a "
                   "held root is refused with 75)").set_defaults(func=run, debug=True)
    sub.add_parser("verify", help="check every part").set_defaults(func=verify)
    sub.add_parser("uninstall", help="stop the job, then remove what install wrote (config last)").set_defaults(func=uninstall)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
