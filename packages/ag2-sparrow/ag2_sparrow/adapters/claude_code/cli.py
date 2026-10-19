#!/usr/bin/env python3
"""`ag2-sparrow-claude-local`: host a local Claude Code session, in a tmux pane, as an AG2 Space agent.

One console script per adapter, as the Hatch adapter has, since the adapter-boundary rule forbids a
shared dispatcher that imports adapters; install, verify and uninstall are `connect_common`'s,
parametrised by this adapter's Shape. `install` records the paths, mints the session id, persists the
bridge's claude-local HITL store root and the canonical work root in a 0600 config, writes the unit
or the plist, has the manager start it, and confirms a fresh holder of the root. `run` is the managed
launcher (`run --launchd` is launchd's entry shim); `hold` is the pane's own command; `inbox` is the
Mod's lifecycle CLI; `attach` opens the pane; `stop --confirm` is the local confirmation of a stall
card. The relay token stays in Sparrow's env file, the Claude login in its config dir. Stdlib only.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from ... import _dirs, connect_common as cm, task_io
from ...connect_common import EXIT_CHILD_HELD, EXIT_HELD, EXIT_UNUSABLE, _v  # noqa: F401
from ... import claude_local_accepter as accepter, claude_local_card as card, claude_local_records as records
from . import hold, inbox, launcher
from .shape import AUTOARM_VAR, HITL_ROOT_NAME, LOCAL, SESSION_NAME  # noqa: F401

SHAPE, COMMAND, MIN_CLI = LOCAL.name, LOCAL.command, LOCAL.min_version
MARKER, PLIST_MARKER, SERVICE_NAME, AGENT_LABEL = LOCAL.marker, LOCAL.plist_marker, LOCAL.service_name, LOCAL.agent_label
ENV_KEEP = LOCAL.env_keep
SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def config_path() -> Path:
    return cm.config_path(LOCAL)


def config_dir() -> Path:
    return config_path().parent


def service_path() -> Path:
    return cm.service_path(LOCAL)


def plist_path() -> Path:
    return cm.plist_path(LOCAL)


def supervisor_path() -> Path:
    return cm.supervisor_path(LOCAL)


def parse_version(text: str) -> "tuple[int, int, int] | None":
    return cm.parse_version(text, LOCAL.version_anchor)


def cli_version(claude: str, run=subprocess.run) -> "tuple[int, int, int] | None":
    return cm.cli_version(claude, LOCAL.version_anchor, run)


def version_problem(claude: str, run=subprocess.run) -> str:
    return cm.version_problem(claude, LOCAL, run)


def render_config(values: dict) -> str:
    return cm.render_config(LOCAL, values)


def read_config(path: Path) -> dict:
    return cm.read_config(path)


def load_config() -> "tuple[dict, str]":
    cfg, problem = cm.load_config(LOCAL)
    if not problem and not SESSION_ID_RE.match(cfg.get("SESSION_ID", "")):
        return {}, f"{config_path()} carries a malformed SESSION_ID; run {COMMAND} install"
    return cfg, problem


def hitl_store_root() -> Path:
    """The bridge's claude-local store root, beside its default store under its state dir."""
    return card.store_root(_dirs.state_dir())


def key_problem(path: str) -> str:
    """'' when `path` holds the key the plugin's verifier accepts (64 hex characters), else why not."""
    if not path:
        return "no key file: strict mode would submit nothing"
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError as e:
        return f"{path} unreadable ({e.strerror or e}): strict mode would submit nothing"
    if not re.fullmatch(r"[0-9a-fA-F]{64}", text):
        return f"{path} is not a 64-hex-character key, so every envelope reads unverifiable"
    return ""


def mint_session_id() -> str:
    return str(uuid.uuid4())


# ── commands ─────────────────────────────────────────────────────────────────

def install(args) -> int:
    claude = shutil.which(args.claude) or args.claude
    problem = version_problem(claude) or launcher.tmux_problem() or launcher.socket_problem(config_dir())
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    plugin = Path(args.plugin_dir).expanduser().resolve()
    if not (plugin / ".claude-plugin" / "plugin.json").is_file():
        print(f"error: {plugin} is not a Claude Code plugin (no .claude-plugin/plugin.json)", file=sys.stderr)
        return EXIT_UNUSABLE
    key_file = str(Path(args.hmac_key_file).expanduser().resolve()) if args.hmac_key_file else ""
    if args.hmac_strict:
        problem = key_problem(key_file)
        if problem:
            print(f"error: --hmac-strict needs a usable key: {problem}", file=sys.stderr)
            return EXIT_UNUSABLE
    root, problem = cm.canonical_root(args.work, args.inbox or _dirs.task_dir())
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    inbox_dir = Path(root) / "tasks" if args.work else Path(os.path.normpath(os.path.expanduser(args.inbox or _dirs.task_dir())))
    values = {"CLAUDE": os.path.abspath(claude), "PLUGIN_DIR": str(plugin), "INBOX_DIR": str(inbox_dir),
              "RESULTS_DIR": str(Path(root) / "results"),
              "CLAUDE_CONFIG_DIR": str(Path(args.claude_config_dir).expanduser().resolve()) if args.claude_config_dir else "",
              "HMAC_KEY_FILE": key_file, "HMAC_STRICT": "1" if args.hmac_strict else "",
              "PERMISSION_MODE": args.permission_mode, "SESSION_ID": "", "HITL_STORE": str(hitl_store_root())}

    def revise(old: dict, values: dict) -> dict:
        """The session id: kept from the old config; minted when there is none; rotated by
        --new-session only once the old generation is proven gone (the job stopped, the root free
        and the recorded server dead), else kept, and the command says so."""
        kept = (old or {}).get("SESSION_ID") or ""
        if kept and args.new_session:
            rec = records.load_supervisor(root)
            gen = rec.get("generation")
            if gen and task_io.verify_lineage(gen["server_pid"], gen["server_start"]) is not False:
                print(f"kept the session id {kept}: the old session (tmux server pid {gen['server_pid']}) is not proven gone")
            else:
                kept = ""
                print("rotating the session id: the old generation is proven gone")
        session_id = kept or mint_session_id()
        return dict(values, SESSION_ID=session_id)
    return cm.install(LOCAL, values, root, revise=revise)


def child_env(cfg: dict, environ=None) -> dict:
    """What `claude auth status` runs under: the allowlist plus the login's config dir."""
    env = cm.child_env_from(ENV_KEEP, environ)
    if cfg.get("CLAUDE_CONFIG_DIR"):
        env["CLAUDE_CONFIG_DIR"] = cfg["CLAUDE_CONFIG_DIR"]
    return env


def run(args, popen=subprocess.Popen) -> int:
    """What the manager runs, and only it: refused without the marker the unit sets; `--launchd` is
    the entry shim that runs the launcher as a child and maps its outcome."""
    problem = cm.managed_problem(LOCAL)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    if args.launchd:
        script = cm.console_script(LOCAL)
        child = [script, "run"] if script else [sys.executable, "-c", "from ag2_sparrow.adapters.claude_code.cli import main; "
                                                                         "raise SystemExit(main(['run']))"]
        return launcher.run_launchd(child, popen)
    cfg, problem = load_config()
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    problem = version_problem(cfg["CLAUDE"])
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    return launcher.Launcher(cfg, config_dir()).serve()


def auth_problem(cfg: dict, run_=subprocess.run) -> str:
    """'' when `claude auth status` says the config dir's login is in place, else why not."""
    try:
        r = run_([cfg["CLAUDE"], "auth", "status"], capture_output=True, text=True, timeout=cm.RUN_TIMEOUT_S,
                 env=child_env(cfg))
    except (OSError, subprocess.SubprocessError) as e:
        return f"claude auth status did not run ({type(e).__name__})"
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 or re.search(r'"?loggedIn"?\s*:\s*false', out):
        return (f"not logged in under CLAUDE_CONFIG_DIR={cfg.get('CLAUDE_CONFIG_DIR') or '(default)'}; run "
                f"claude login there (on macOS the login lives in the Keychain under USER)")
    return ""


def occupancy_line(root: str) -> str:
    """The shared tri-state beside the manager's state: held names the fencing process (hold until the
    consumer attaches, claude after), unobserved never reads free."""
    occ = task_io.occupancy(root)
    manager = cm.manager_detail(LOCAL)
    if occ == "held":
        rec, consumer = task_io.lock_holder(root), task_io.holder_consumer(root)
        who = f"claude pid {consumer['pid']}" if consumer and consumer.get("live") else f"hold pid {rec.get('pid')}"
        return f"held by {who} (manager {manager})"
    if occ == "unobserved":
        return f"unobserved (the root's holder or consumer cannot be verified; manager {manager})"
    return cm.occupancy_line(LOCAL, root)


def projection_lines(cfg: dict) -> list:
    """Pending projection cases: a claude-local card nobody has projected yet, with why."""
    out = []
    store_root = Path(cfg.get("HITL_STORE") or "")
    if not store_root.is_dir():
        return out
    store, mgr = card.manager(store_root)
    for req in card.pending(store):
        if mgr.needs_projection(req.id):
            why = "no primary gateway" if not (store_root.parent.parent / "owner-routing.json").exists() \
                else "gateway predates claude-local"
            out.append(f"INFO pending projection: {req.id} ({why}); on this machine: {COMMAND} stop --confirm")
    return out


def verify(_args) -> int:
    results: list = []
    cfg, problem = load_config()
    cm.check(results, not problem, "config", problem or str(config_path()))
    if problem:
        return 1
    problem = version_problem(cfg["CLAUDE"])
    cm.check(results, not problem, "claude", problem or f"{cfg['CLAUDE']} {_v(cli_version(cfg['CLAUDE']))}")
    if not problem:
        problem = auth_problem(cfg)
        cm.check(results, not problem, "login", problem or "claude auth status: logged in")
    problem = launcher.tmux_problem()
    cm.check(results, not problem, "tmux", problem or "tmux %s" % ".".join(map(str, launcher.tmux_version() or ())))
    problem = launcher.socket_problem(config_dir())
    sock = launcher.socket_path(config_dir())
    cm.check(results, not problem, "socket", problem or f"{sock} ({len(str(sock).encode())} bytes)")
    plugin = Path(cfg["PLUGIN_DIR"])
    cm.check(results, (plugin / ".claude-plugin" / "plugin.json").is_file(), "plugin", str(plugin))
    for key in ("INBOX_DIR", "RESULTS_DIR"):
        cm.check(results, Path(cfg[key]).is_dir(), key.lower().replace("_dir", ""), cfg[key])
    key, strict = cfg.get("HMAC_KEY_FILE"), cfg.get("HMAC_STRICT") == "1"
    problem = key_problem(key) if key or strict else ""
    cm.check(results, not problem, "hmac key",
             (f"strict mode: {problem}" if strict else problem) if problem else key or "none (every task reads unsigned)")
    cm.check(results, cfg.get("HITL_STORE") == str(hitl_store_root()), "hitl store",
             cfg.get("HITL_STORE") or "none" if cfg.get("HITL_STORE") != str(hitl_store_root())
             else f"{cfg['HITL_STORE']} (the bridge's, beside its default store)")
    cm.verify_tail(LOCAL, cfg, results)
    root = cm.work_root(cfg)
    print(f"INFO session id: {cfg['SESSION_ID']}")
    print(f"INFO occupancy: {root} {occupancy_line(root)}")
    rec = records.load_supervisor(root)
    if rec.get("stall"):
        print(f"INFO {'needs attention' if rec['stall'].get('reason', '').startswith('abnormal') else 'suspected hang'}: "
              f"{rec['stall'].get('reason')} (card {(rec.get('hitl') or {}).get('id') or 'not raised'})")
    if rec.get("fence_uncertain"):
        print(f"INFO the pane-key guard on {sock} is uncertain: creation is fenced until recovered")
    for line in projection_lines(cfg):
        print(line)
    print(f"INFO attach: {COMMAND} attach")
    return 0 if all(results) else 1


def stop_stray(cfg: dict) -> str:
    """uninstall, after the manager stop: a session the dead launcher left is stopped through the
    same fenced path (/exit, bounded, kill-session, the server's exit); '' or why not."""
    rec = records.load_supervisor(cm.work_root(cfg))
    if not rec.get("generation"):
        return ""
    lc = launcher.Launcher(cfg, config_dir())
    if lc.tm.has_session() is not True:
        return ""
    outcome = lc.stop_session(rec, "uninstall")
    return "" if outcome == "stopped" else f"the session could not be stopped ({outcome})"


def _stop_and_unregister(cfg: dict) -> str:
    problem = stop_stray(cfg)
    if not problem and cfg.get("HITL_STORE"):
        card.unregister_install(cfg["HITL_STORE"], card.opaque_install(task_io.install_id(), cm.work_root(cfg)))
    return problem


def uninstall(_args) -> int:
    return cm.uninstall(LOCAL, before_free=_stop_and_unregister)


def attach(_args) -> int:
    """tmux attach on the adapter's socket: the same session, under the same lock; /exit ends it and
    the launcher starts the next one by id."""
    cfg, problem = load_config()
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    sock = str(launcher.socket_path(config_dir()))
    tmux = shutil.which("tmux") or "tmux"
    os.execvp(tmux, [tmux, "-u", "-S", sock, "attach", "-t", "=%s" % SESSION_NAME])


def stop(args) -> int:
    """The local confirmation of an open stall card, through the one accepter; the launcher applies it."""
    if not args.confirm:
        print("error: stop takes --confirm: it accepts the open stall card's Stop; without a card, attach and "
              "type /exit", file=sys.stderr)
        return EXIT_UNUSABLE
    cfg, problem = load_config()
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    outcome = accepter.local_stop(_dirs.state_dir(), card.opaque_install(task_io.install_id(), cm.work_root(cfg)))
    print(outcome)
    if outcome.startswith("applied"):
        print("recorded STOPPING; the supervisor stops the session and resumes it by id")
        return 0
    return 1


def hold_cmd(args) -> int:
    return hold.main(args)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=COMMAND, description="A Claude Code session in a tmux pane on this machine as "
                                 "an AG2 Space agent (Mods API, CLI %s+)." % _v(MIN_CLI))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="record the paths, mint the session id, write the unit, have the manager "
                       "start the session (a reinstall stops the old one first)")
    p.add_argument("--plugin-dir", required=True, help="the ag2space-inbox plugin directory from the repo")
    p.add_argument("--work", help="the work root: its tasks/ is the inbox, its results/ the result dir")
    p.add_argument("--inbox", help="Sparrow's task dir, <work>/tasks (default: as ag2-sparrow resolves it); the "
                   "result dir is <work>/results")
    p.add_argument("--claude-config-dir", help="CLAUDE_CONFIG_DIR holding the login the session uses (default: "
                   "the CLI's own)")
    p.add_argument("--hmac-key-file", help="the task-envelope key Sutando stamps with (AG2_HMAC_KEY_FILE)")
    p.add_argument("--hmac-strict", action="store_true", help="submit verified envelopes only")
    p.add_argument("--permission-mode", default="bypassPermissions",
                   help="the session's --permission-mode; bypassPermissions (default) suits an unattended "
                        "session, a narrower mode makes a prompting tool stall the queue")
    p.add_argument("--new-session", action="store_true", help="rotate the session id, only once the old session "
                   "is proven gone (quiescent); else the old id is kept and said so")
    p.add_argument("--claude", default="claude", help="the claude executable (default: claude on PATH)")
    p.set_defaults(func=install)
    run_help = (f"what the manager runs: the launcher of one tmux-hosted session; refused unless {cm.MANAGED_VAR}=1 "
                "is in the environment, as the unit sets it (a guard against an accidental hand start, not a "
                "security boundary; use attach to look at the session)")
    r = sub.add_parser("run", help=run_help, description=run_help)
    r.add_argument("--launchd", action="store_true", help="launchd's entry shim: runs the launcher as a child and "
                   "maps logical 75 to exit 0, everything else (and a signal death) to non-zero")
    r.set_defaults(func=run)
    h = sub.add_parser("hold", help="the pane's own command (started by the launcher, never by hand): takes the "
                       "root, publishes, gates and attaches the claude child")
    h.add_argument("--generation", required=True, help="the 32-hex generation nonce")
    h.add_argument("rest", nargs=argparse.REMAINDER)
    h.set_defaults(func=hold_cmd)
    attach_help = ("tmux attach on the adapter's own socket. Limits: it is the same session under the same lock, so "
                   "what you type is the session's (a /exit ends it and the launcher resumes it by id); /clear and "
                   "an in-pane /resume are not supported (the Mod stops intake on a session-id change: use "
                   "/compact or install --new-session); detach with the tmux prefix + d, never kill the server")
    sub.add_parser("attach", help=attach_help, description=attach_help).set_defaults(func=attach)
    s = sub.add_parser("stop", help="confirm the open stall card's Stop on this machine (requires --confirm)")
    s.add_argument("--confirm", action="store_true")
    s.set_defaults(func=stop)
    sub.add_parser("verify", help="check every part").set_defaults(func=verify)
    sub.add_parser("uninstall", help="stop the job and the session, then remove what install wrote (config "
                   "last)").set_defaults(func=uninstall)
    inbox.add_parser(sub)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
