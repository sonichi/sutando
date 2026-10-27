#!/usr/bin/env python3
"""`ag2-sparrow-claude-local`: host a local Claude Code session, in a tmux pane, as an AG2 Space agent.

One console script per adapter, as the Hatch adapter has, since the adapter-boundary rule forbids a
shared dispatcher that imports adapters; install, verify and uninstall are `connect_common`'s,
parametrised by this adapter's Shape. `install` records the paths, mints the session id, persists the
bridge's claude-local HITL store root and the canonical work root in a 0600 config, writes the unit
or the plist, has the manager start it, and confirms a fresh holder of the root. With no explicit root,
the root is the agent's own, `<agents dir>/<segment of its id>`, and install wires the agent's bridge to
it: a unit of its own, or a declaration a host supervisor runs (`_dirs.bridge_host`). `connect --base --code`
checks the machine, redeems AG2 Space's one-time code for the agent's relay credential, has the credential
owner write it to the agent's seat beside its root, then runs install with that seat. `run` is the managed
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
import time
import uuid
from pathlib import Path

from ... import _dirs, connect_bridge as cb, connect_common as cm, task_io
from ...connect_bridge import (DECLARATION, EXTERNAL, SEAT_ENV_KEEP, STATUS_MAX_AGE_S, UNIT, bridge_env,  # noqa: F401
                               bridge_home, bridge_mode, bridge_state, install_identity, kept_token, launch_env,
                               seat_child_env, seat_env, seat_file, served_elsewhere, statuses, url_vault_key,
                               vault_key, worker_name)
from ...connect_common import EXIT_CHILD_HELD, EXIT_HELD, EXIT_UNUSABLE, _v  # noqa: F401
from ... import claude_local_accepter as accepter, claude_local_card as card, claude_local_records as records
from . import detector, hmac_policy, hold, inbox, launcher, trust
from .shape import AUTOARM_VAR, BRIDGE, HITL_ROOT_NAME, LOCAL, PLUGIN, SESSION_NAME, resolve_plugin_dir  # noqa: F401

SHAPE, COMMAND, MIN_CLI = LOCAL.name, LOCAL.command, LOCAL.min_version
MARKER, PLIST_MARKER, SERVICE_NAME, AGENT_LABEL = LOCAL.marker, LOCAL.plist_marker, LOCAL.service_name, LOCAL.agent_label
ENV_KEEP = LOCAL.env_keep
SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SETUP_VENDOR = "claude"     # the served setup list this adapter follows: acp.claude


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


key_problem = hmac_policy.key_problem


def mint_session_id() -> str:
    return str(uuid.uuid4())


# ── the agent and its bridge: connect_bridge's, as one owner ─────────────────

def bridge_fix(cfg: dict) -> str:
    return cb.bridge_fix(cfg, COMMAND, BRIDGE)


def bridge_problem(cfg: dict, now=None) -> str:
    return cb.bridge_problem(cfg, COMMAND, BRIDGE, now)


def wire_bridge(cfg: dict) -> str:
    return cb.wire_bridge(cfg, COMMAND, BRIDGE)


def unwire_bridge(cfg: dict) -> str:
    return cb.unwire_bridge(cfg, COMMAND, BRIDGE)


def resolve_root(args) -> "tuple[dict, str]":
    """({root, mode, agent_id, token_file}, '') or ({}, why). An explicit root (--work, --inbox, or a
    configured task dir) is served by a bridge someone else runs; else the root is the agent's own
    under the agents dir, and this install wires its bridge."""
    host_env = str(_dirs.device_env_path())
    if args.work or args.inbox or _dirs.task_dir_configured():
        if args.token_env_file:
            return {}, "--token-env-file is for the bridge this install runs; with an explicit root your bridge has its own"
        root, problem = cm.canonical_root(args.work, args.inbox or _dirs.task_dir())
        # Under a host supervisor the host's token and binding are the host's own agent, not this root's.
        token = "" if _dirs.bridge_host() else host_env
        agent_id = install_identity(token, bool(token))
        return {"root": root, "mode": EXTERNAL, "agent_id": agent_id, "token_file": ""}, problem
    mode = _dirs.bridge_host() or UNIT
    token_file = str(Path(args.token_env_file).expanduser().resolve()) if args.token_env_file else \
        (host_env if mode == UNIT else "")
    agent_id = install_identity(token_file, token_file == host_env)
    if not agent_id:
        where = f"in {token_file} or the environment" if token_file else "in the environment"
        return {}, (f"the agent id is unknown, so its root cannot be named: bind the agent first, or set AGENT_MXID "
                    f"{where}, or pass --work <dir>")
    root, problem = cb.agent_root(agent_id)
    if not problem and mode == UNIT and not os.path.isfile(token_file):
        problem = (f"{token_file} is missing: put the agent's REMOTE_TASK_TOKEN there (mode 600), or pass "
                   "--token-env-file")
    if not problem and mode != UNIT and token_file:
        problem = cb.supervised_seat_problem(token_file, root, agent_id, mode, COMMAND)
        problem = problem and f"{problem}; without it the bridge takes the token from the vault"
    return {"root": root, "mode": mode, "agent_id": agent_id, "token_file": token_file}, problem


# ── commands ─────────────────────────────────────────────────────────────────

def install(args) -> int:
    claude = shutil.which(args.claude) or args.claude
    problem = version_problem(claude) or launcher.tmux_problem() or launcher.socket_problem(config_dir())
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    plugin = plugin_dir(args)
    if not (plugin / ".claude-plugin" / "plugin.json").is_file():
        print(f"error: {plugin} is not a Claude Code plugin (no .claude-plugin/plugin.json)", file=sys.stderr)
        return EXIT_UNUSABLE
    key_file, strict, problem, note = hmac_settings(args)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    if note:
        print(f"warning: {note}; it is read per task, so a key written there later is honoured without a reinstall")
    where, problem = resolve_root(args)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    root, mode, agent_id = where["root"], where["mode"], where["agent_id"]
    if args.bridge_state_dir and mode != EXTERNAL:
        print("error: --bridge-state-dir is for a bridge you run (an explicit root); this install's bridge keeps its "
              f"state beside the root, {bridge_home(root) / 'state'}", file=sys.stderr)
        return EXIT_UNUSABLE
    inbox_dir = Path(os.path.normpath(os.path.expanduser(args.inbox or _dirs.task_dir()))) \
        if mode == EXTERNAL and not args.work else Path(root) / "tasks"
    segment = Path(root).name
    # The one record of the bridge's state dir: verify's bridge row reads its status there, and the
    # card store the bridge projects from sits beside it; wire_bridge gives the bridge exactly this dir.
    state = Path(args.bridge_state_dir).expanduser().resolve() if args.bridge_state_dir else \
        _dirs.state_dir() if mode == EXTERNAL else bridge_home(root) / "state"
    values = {"CLAUDE": os.path.abspath(claude), "PLUGIN_DIR": str(plugin), "INBOX_DIR": str(inbox_dir),
              "RESULTS_DIR": str(Path(root) / "results"),
              "CLAUDE_CONFIG_DIR": str(Path(args.claude_config_dir).expanduser().resolve()) if args.claude_config_dir else "",
              "HMAC_KEY_FILE": key_file, "HMAC_STRICT": "1" if strict else hmac_policy.OPT_OUT if args.no_hmac_strict else "",
              "PERMISSION_MODE": args.permission_mode, "SESSION_ID": "", "HITL_STORE": str(card.store_root(state)),
              inbox.ALLOW_NON_OWNER: "1" if args.allow_non_owner else "", "AGENT_ID": agent_id, "BRIDGE": mode,
              "TOKEN_ENV_FILE": where["token_file"],
              "TOKEN_VAULT_KEY": vault_key(segment) if mode not in (UNIT, EXTERNAL) and not where["token_file"] else "",
              "BRIDGE_STATE_DIR": str(state)}

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
    def installed() -> int:
        rc = cm.install(LOCAL, values, root, root_id=getattr(args, "root_identity", None), revise=revise,
                        precheck=lambda r: inbox.root_problem(r) or records.agent_problem(r, agent_id),
                        holder=holder_words)
        if rc != 0 or mode == EXTERNAL:
            return rc
        problem = cb.wire_bridge_bound(dict(values, ROOT=root), COMMAND, BRIDGE, LOCAL,
                                       getattr(args, "root_identity", None))
        if problem:
            print(f"error: the session is installed but its bridge is not: {problem}; run {COMMAND} install again",
                  file=sys.stderr)
            return EXIT_UNUSABLE
        return 0
    return cb.under_agent_lock(root, installed)


def hmac_settings(args) -> "tuple[str, bool, str, str]":
    """(key file, strict, the refusal, a warning) from the hmac options, as install records them and connect
    checks them before anything is spent or stopped."""
    key_file = str(Path(args.hmac_key_file).expanduser().resolve()) if args.hmac_key_file else ""
    strict = args.hmac_strict or bool(key_file and not args.no_hmac_strict)
    problem = key_problem(key_file) if strict else ""
    # A key not there yet is recorded, not refused: strictness is decided per task from the file as it is then.
    if problem and not (key_file and hmac_policy.key_absent(key_file)):
        why = "--hmac-strict" if args.hmac_strict else "strict mode, the default with --hmac-key-file (--no-hmac-strict opts out),"
        return key_file, strict, f"{why} needs a usable key: {problem}", ""
    return key_file, strict, "", problem


def plugin_dir(args) -> Path:
    """--plugin-dir when given, else the ag2space-inbox plugin this package ships."""
    return Path(args.plugin_dir).expanduser().resolve() if args.plugin_dir else PLUGIN


def connect_preflight(args) -> str:
    """'' when this host can run the agent, checked before the single-use code is spent; else why not."""
    problem = cb.host_problem(COMMAND)
    if problem:
        return problem
    claude = shutil.which(args.claude) or args.claude
    plugin = plugin_dir(args)
    if not (plugin / ".claude-plugin" / "plugin.json").is_file():
        return f"{plugin} is not a Claude Code plugin (no .claude-plugin/plugin.json)"
    login = {"CLAUDE": os.path.abspath(claude), "CLAUDE_CONFIG_DIR": str(Path(args.claude_config_dir).expanduser()
                                                                         .resolve()) if args.claude_config_dir else ""}
    return (version_problem(claude) or launcher.tmux_problem() or launcher.socket_problem(config_dir())
            or auth_problem(login) or hmac_settings(args)[2])


def connect(args) -> int:
    """Preflight, redeem the code into the agent's seat beside its root (connect_bridge's), then install
    exactly as `install` does with the seat's .env as the token env file."""
    seat, problem = cb.redeem_into_seat(args.base, args.code, args.trust_relay_host, lambda: connect_preflight(args),
                                         LOCAL)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNUSABLE
    session = {k: v for k, v in vars(args).items() if k not in ("code", "trust_relay_host")}

    def installed() -> int:
        print(cm.served_setup_line(SETUP_VENDOR))
        return install(argparse.Namespace(**session, work=None, inbox=None, token_env_file=seat["seat_env"],
                                          bridge_state_dir=None, root_identity=seat["root_id"]))
    return cb.finish_connect(seat, installed, COMMAND, cb.session_flags(args))


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
        return launcher.run_launchd(cm.run_child_argv(LOCAL, __name__), popen)
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


LABELS = ("hold", "claude")     # the holder and its consumer, in verify's and install's words


def holder_words(root: str, _rec=None) -> str:
    """Who holds the root, as verify and install both say it: hold and, once attached, its claude."""
    return cm.holder_words(root, LABELS)


def occupancy_line(root: str) -> str:
    """The shared tri-state beside the manager's state, in this shape's nouns."""
    return cm.occupancy_line(LOCAL, root, LABELS)


def trust_line(cfg: dict, root: str) -> str:
    """Whether the CLI's own folder trust for the inbox is recorded, read as the CLI reads it."""
    inbox_dir = os.path.join(root, "tasks")
    try:
        if trust.trusted(trust.config_file(cfg.get("CLAUDE_CONFIG_DIR", "")), trust.project_key(inbox_dir)):
            return f"{inbox_dir} recorded"
    except (OSError, ValueError) as e:
        return f"{inbox_dir} unreadable ({e})"
    return f"{inbox_dir} not recorded (the launcher records it before it creates the session)"


def pane_dialog(rec: dict) -> "str | None":
    """The startup dialog the recorded session's pane shows now, from a capture; None when there is none
    or no pane answers."""
    gen = rec.get("generation")
    if not gen:
        return None
    tm = launcher.Tmux(launcher.socket_path(config_dir()))
    return trust.dialog(tm.capture(gen["pane_id"])) if tm.has_session() is True else None


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
    plugin, fell_back = resolve_plugin_dir(cfg["PLUGIN_DIR"])
    cm.check(results, (plugin / ".claude-plugin" / "plugin.json").is_file(), "plugin",
             f"recorded plugin dir {cfg['PLUGIN_DIR']} missing; using the shipped plugin {plugin} (re-run "
             f"{COMMAND} install to record it)" if fell_back else str(plugin))
    for key in ("INBOX_DIR", "RESULTS_DIR"):
        cm.check(results, Path(cfg[key]).is_dir(), key.lower().replace("_dir", ""), cfg[key])
    key, mode = cfg.get("HMAC_KEY_FILE"), hmac_policy.mode(cfg)
    problem = key_problem(key) if mode != hmac_policy.OFF else ""
    cm.check(results, not problem, "hmac key",
             (f"strict mode: {problem}" if mode == hmac_policy.STRICT else problem) if problem
             else key or "none (every task reads unsigned)")
    print("INFO hmac mode: " + hmac_policy.describe(cfg))
    if hmac_policy.legacy_opt_out(cfg):
        print("WARN hmac mode: legacy opt-out (HMAC_STRICT recorded empty by an older install --no-hmac-strict): not "
              "strict; unsigned and unverifiable envelopes are submitted. Reinstall without --no-hmac-strict for strict")
    print("INFO non-owner tasks: " + ("submitted with the non-owner note (--allow-non-owner)"
                                      if cfg.get(inbox.ALLOW_NON_OWNER) == "1" else "refused (owner only)"))
    root = cm.work_root(cfg)
    if cfg.get("BRIDGE_STATE_DIR"):
        want = card.store_root(bridge_state(cfg))
        cm.check(results, cfg.get("HITL_STORE") == str(want), "hitl store",
                 cfg.get("HITL_STORE") or "none" if cfg.get("HITL_STORE") != str(want)
                 else f"{cfg['HITL_STORE']} (the bridge's, beside its default store under {bridge_state(cfg)})")
    else:
        # The store names the bridge's state dir itself here, so no row can check it against anything else.
        print(f"INFO hitl store: {cfg.get('HITL_STORE')} (a config from before BRIDGE_STATE_DIR: the bridge's state dir "
              f"is read as its parent; run {COMMAND} install to record it)")
    print(f"INFO agent: {cfg.get('AGENT_ID') or 'not recorded (an explicit root with no known agent id)'}")
    problem = bridge_problem(cfg)
    cm.check(results, not problem, "bridge", problem or f"a connected bridge feeds {root}/tasks and {root}/results")

    cm.verify_tail(LOCAL, cfg, results, occupancy=occupancy_line,
                   relay=lambda results_: cb.relay_rows(results_, cfg, COMMAND, BRIDGE))
    print(f"INFO session id: {cfg['SESSION_ID']}")
    print(f"INFO folder trust: {trust_line(cfg, root)}")
    alarm = records.read_progress_alarm(config_dir())
    if alarm:
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(alarm["since"])) if isinstance(
            alarm.get("since"), (int, float)) else "an unknown time"
        cm.check(results, False, "progress", "progress record unwritable since %s (%s); intake stays closed until a "
                 "durable write" % (since, alarm.get("error") or "unknown"))
    try:
        plugin = records.load_plugin(root)
        stuck = detector.stuck_answers(dict(plugin, stuck=inbox.unresolved_stuck(root, plugin.get("stuck"))))
    except records.RecordError:
        stuck = {}
    if stuck:
        cm.check(results, False, "unanswered", "tasks without an answer, retried with backoff: " + ", ".join(
            "%s since %s (%s)" % (t, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(v["since"])), v.get("detail") or "")
            for t, v in sorted(stuck.items())))
    rec = records.load_supervisor(root)
    waiting = pane_dialog(rec)
    if waiting:
        cm.check(results, False, "session", f"the pane waits at Claude Code's {waiting} dialog, so no task is "
                 f"answered; run {COMMAND} attach and answer it")
    if rec.get("stall"):
        print(f"INFO {'needs attention' if rec['stall'].get('reason', '').startswith('abnormal') else 'suspected hang'}: "
              f"{rec['stall'].get('reason')} (card {(rec.get('hitl') or {}).get('id') or 'not raised'})")
    if rec.get("title_changed"):
        seen = rec["title_changed"]
        print(f"INFO {launcher.TITLE_CHANGED}: the pane title is {seen.get('seen')!r}, not the recorded token; every "
              f"fenced stop is withheld until the session is recreated")
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
    problem = stop_stray(cfg) or unwire_bridge(cfg)
    if not problem and cfg.get("HITL_STORE"):
        card.unregister_install(cfg["HITL_STORE"], card.opaque_install(task_io.install_id(), cm.work_root(cfg)))
    return problem


def uninstall(_args) -> int:
    return cm.uninstall(LOCAL, before_free=_stop_and_unregister, token_file=kept_token)


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
    outcome = accepter.local_stop(bridge_state(cfg), card.opaque_install(task_io.install_id(), cm.work_root(cfg)))
    print(outcome)
    if outcome.startswith("applied"):
        print("recorded STOPPING; the supervisor stops the session and resumes it by id")
        return 0
    return 1


def hold_cmd(args) -> int:
    return hold.main(args)


def session_options(p) -> None:
    """The session's options, shared by install and connect."""
    p.add_argument("--plugin-dir", help="an ag2space-inbox plugin directory to use instead of the one this package "
                   "ships")
    p.add_argument("--claude-config-dir", help="CLAUDE_CONFIG_DIR holding the login the session uses (default: "
                   "the CLI's own)")
    p.add_argument("--hmac-key-file", help="the task-envelope key Sutando stamps with (AG2_HMAC_KEY_FILE)")
    hm = p.add_mutually_exclusive_group()
    hm.add_argument("--hmac-strict", action="store_true", help="submit verified envelopes only (the default with "
                    "--hmac-key-file)")
    hm.add_argument("--no-hmac-strict", action="store_true", help="with --hmac-key-file, also submit unsigned and "
                    "unverifiable envelopes")
    p.add_argument("--allow-non-owner", action="store_true", help="submit team and guest tasks (collaborator or not) "
                   "with the non-owner note; off: they get the owner-only refusal and never reach the session")
    p.add_argument("--permission-mode", default="bypassPermissions",
                   help="the session's --permission-mode; bypassPermissions (default) suits an unattended "
                        "session, a narrower mode makes a prompting tool stall the queue")
    p.add_argument("--new-session", action="store_true", help="rotate the session id, only once the old session "
                   "is proven gone (quiescent); else the old id is kept and said so")
    p.add_argument("--claude", default="claude", help="the claude executable (default: claude on PATH)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=COMMAND, description="A Claude Code session in a tmux pane on this machine as "
                                 "an AG2 Space agent (Mods API, CLI %s+)." % _v(MIN_CLI))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="record the paths, mint the session id, write the unit, have the manager "
                       "start the session (a reinstall stops the old one first)")
    p.add_argument("--work", help="an explicit work root, served by a bridge you run (default: the agent's own "
                   "root, <agents dir>/<agent id>, whose bridge install wires)")
    p.add_argument("--inbox", help="an explicit work root by its <work>/tasks, as your bridge's task dir; the "
                   "result dir is <work>/results")
    p.add_argument("--token-env-file", help="the agent's 0600 env file with REMOTE_TASK_TOKEN (and AGENT_MXID) for "
                   "the bridge install runs (default: Sparrow's device env)")
    p.add_argument("--bridge-state-dir", help="with an explicit root: the AGENT_CONNECT_STATE_DIR of the bridge you "
                   "run, where verify reads its status and the card store sits (default: Sparrow's state dir)")
    session_options(p)
    p.set_defaults(func=install, parser=p)
    c = sub.add_parser("connect", help="check this machine, redeem the one-time code from AG2 Space for the agent's "
                       "relay credential, write it to the agent's seat (mode 600), then install as install does")
    c.add_argument("--base", required=True, help="the AG2 Space backend URL the code was issued by")
    c.add_argument("--code", help="the one-time code from AG2 Space (starts with acp.): - reads it from stdin; "
                   "omitted, connect asks for it. A code given here is kept by ps and shell history")
    c.add_argument("--trust-relay-host", help="accept a relay URL on this host although it is not --base's host "
                   "or under it (only when AG2 Space told you to)")
    session_options(c)
    c.set_defaults(func=connect, parser=c)
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
