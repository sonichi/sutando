"""The agent's own gateway bridge beside a `-local` session, and `connect --code`'s redemption into its seat, as one owner.

A `-local` install whose root is the agent's own (`<agents dir>/<segment of its id>`) wires that agent's
bridge: one launch environment (`launch_env`: the root's tasks/ and results/, the bridge's state beside the
root, and a seat .env's exports with every inherited token cleared), run as a marked unit or launchd agent
of its own, or declared for the host's supervisor. `connect` redeems AG2 Space's one-time code, has the
credential owner write the seat, and hands the seat's .env to the adapter's install. Package level,
parametrised by the adapter's Shape: this module names no adapter and imports none. Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

from . import _dirs, agent_identity, connect_common as cm, gateway_credential_sources as _sources
from .bounded_file_read import read_regular

# Who runs the agent's bridge: this install's own unit, a host supervisor from a declaration, or
# whoever already did (an explicit root); a config without BRIDGE predates the choice: external.
UNIT, EXTERNAL = "unit", "external"
STATUS_MAX_AGE_S = 180
DECLARATION = "worker.json"
BRIDGE_MODULE = "ag2_sparrow.remote_gateway_bridge"
# The config keys every shape that wires a bridge records, in this order.
CONFIG_KEYS = ("AGENT_ID", "BRIDGE", "TOKEN_ENV_FILE", "TOKEN_VAULT_KEY", "BRIDGE_STATE_DIR")


def bridge_shape(local: cm.Shape) -> cm.Shape:
    """The agent's gateway bridge, when `local`'s install runs it: the console script with no arguments."""
    return cm.Shape(name=f"{local.name}-bridge", command="ag2-sparrow", writer=local.command,
                    description=f"the gateway bridge of the {local.name} agent", cli="ag2-sparrow",
                    min_version=(0, 0, 0), version_anchor="", version_note="", required_keys=(), config_keys=(),
                    env_keep=(), kept="", run_args=(), service_args=())


def bridge_mode(cfg: dict) -> str:
    return cfg.get("BRIDGE") or EXTERNAL


def bridge_home(root) -> Path:
    """Beside the root, never in it: the bridge's state and lock workspace, one per agent."""
    return Path(str(root) + ".bridge")


def bridge_state(cfg: dict) -> Path:
    """The state dir of the bridge that feeds this root, as install recorded it (BRIDGE_STATE_DIR: the
    bridge's own beside the root, or --bridge-state-dir for a bridge you run). A config from before the
    key names its HITL store's, as install then derived it; one naming neither, the dir beside the root."""
    if cfg.get("BRIDGE_STATE_DIR"):
        return Path(cfg["BRIDGE_STATE_DIR"])
    if cfg.get("HITL_STORE"):
        return Path(cfg["HITL_STORE"]).parent.parent
    return bridge_home(cm.work_root(cfg)) / "state"


def worker_name(segment: str) -> str:
    """The supervisor's worker name: a plain name, distinct per segment by its digest."""
    readable = re.sub(r"[^a-z0-9-]", "-", segment)[:40].strip("-") or "agent"
    return f"bridge-{readable}-{hashlib.sha256(segment.encode('utf-8')).hexdigest()[:8]}"


def vault_key(segment: str) -> str:
    return "AG2_RELAY_TOKEN_" + worker_name(segment).upper().replace("-", "_")


def url_vault_key(segment: str) -> str:
    """Where the owner stores the agent's gateway URL for a token that is a bare secret."""
    return "AG2_RELAY_URL_" + worker_name(segment).upper().replace("-", "_")


def bridge_env(root, agent_id: str, token_file: str = "", state=None) -> dict:
    """What the agent's bridge runs with: exactly this root's tasks/ and results/, its own state beside it
    (`state`: the dir the config records, so the unit, verify and the card store name one dir)."""
    env = {"AGENT_CONNECT_TASK_DIR": os.path.join(root, "tasks"), "AGENT_CONNECT_RESULT_DIR": os.path.join(root, "results"),
           "AGENT_CONNECT_STATE_DIR": str(state or bridge_home(root) / "state"), "AGENT_MXID": agent_id}
    if token_file:
        env["AG2_DEVICE_ENV"] = token_file
    return env


def seat_env(token_file: str, agent_id: str) -> "tuple[dict, str]":
    """(the seat's launch variables, '') when `token_file` is a seat .env, ({}, '') when it is none, or
    ({}, why): the credential owner exports the seat and clears what the bridge would inherit."""
    if not token_file:
        return {}, ""
    seat, problem = _sources.seat_bridge_environ(token_file, read_regular)
    if not problem and seat and seat["AGENT_MXID"] != agent_id:
        problem = f"the seat .env {token_file} names {seat['AGENT_MXID']}, not {agent_id}"
    return ({}, problem) if problem else (seat, "")


def seat_file(root) -> str:
    """The seat .env connect writes beside the root: under a host supervisor, the only one a bridge runs with."""
    return cm.seat_env_path(bridge_home(root))


def supervised_seat_problem(token_file: str, root: str, agent_id: str, mode: str, command: str) -> str:
    """'' when `token_file` is the agent's own seat beside `root`, valid for `agent_id`; else why. The
    supervisor `mode` derives the seat from the root, so a .env anywhere else is never the one it runs."""
    seat, problem = ({}, "") if token_file != seat_file(root) else seat_env(token_file, agent_id)
    if not seat and not problem:
        problem = f"--token-env-file under {mode} is the agent's seat, {seat_file(root)}, as {command} connect writes it"
    return problem


def launch_env(cfg: dict) -> "tuple[dict, str]":
    """(the bridge's whole launch environment, '') or ({}, why): the one place it is built, for a unit, a
    declaration and the supervisor that runs one."""
    env = bridge_env(cm.work_root(cfg), cfg["AGENT_ID"], cfg.get("TOKEN_ENV_FILE", ""), bridge_state(cfg))
    seat, problem = seat_env(cfg.get("TOKEN_ENV_FILE", ""), cfg["AGENT_ID"])
    if problem:
        return {}, problem
    env.update(seat)
    return env, ""


def install_identity(token_file: str, host_binding: bool) -> str:
    """The agent id by the bridge's own chain over the token file it will run with; the host's binding
    counts only when that file is the host's own device env."""
    files = agent_identity.env_files([token_file]) if token_file else []
    return agent_identity.resolve(_dirs.state_dir() if host_binding else None,
                                  lambda key: agent_identity.file_value(files, key))


def statuses(state_dir: Path, now=None) -> list:
    """[(path, record)] for each gateway status under `state_dir` written in the last STATUS_MAX_AGE_S."""
    out, now = [], time.time() if now is None else now
    for path in sorted(Path(state_dir).glob("gateway-status*.json"))[:32]:
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(rec, dict) and 0 <= now - float(rec["ts"]) <= STATUS_MAX_AGE_S:
                out.append((path, rec))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def served_elsewhere(agent_id: str, root: str) -> str:
    """'' unless a running bridge of the host already serves `agent_id` from another inbox: one token
    polled by two bridges splits the agent's tasks between them."""
    tasks = os.path.realpath(os.path.join(root, "tasks"))
    for path, rec in statuses(_dirs.state_dir()):
        if rec.get("agent_id") == agent_id and rec.get("tasks_dir") != tasks:
            return (f"a running bridge already serves {agent_id} ({path}); one token is one bridge: stop that "
                    f"bridge, or give this session another agent")
    return ""


def agent_root(agent_id: str) -> "tuple[str, str]":
    """(the agent's own root under the agents dir, '') or (root, why it cannot be served here)."""
    root, problem = cm.canonical_root(str(_dirs.agents_dir() / agent_identity.root_segment(agent_id)), "")
    return root, problem or served_elsewhere(agent_id, root)


_NOT_SESSION = ("help", "base", "code", "trust_relay_host", "work", "inbox", "token_env_file", "bridge_state_dir",
                "experimental_connect")


def session_flags(args) -> list:
    """The session options `args` carries away from their defaults, as install takes them again; read
    through the parser main() records on args."""
    parser, out = getattr(args, "parser", None), []
    for action in (parser._actions if parser is not None else ()):
        value = getattr(args, action.dest, action.default)
        if not action.option_strings or action.dest in _NOT_SESSION or value is None or value == action.default:
            continue
        flag = next(o for o in action.option_strings if o.startswith("--"))
        out += [flag] if value is True else [flag, str(value)]
    return out


def rerun_install(command: str, token_file: str, flags=()) -> str:
    """The install that repeats one given the agent's own seat (`token_file`) and these session flags,
    else the bare one."""
    return f"{command} install " + (shlex.join([*flags, "--token-env-file", token_file]) if token_file else "again")


def spent_code_line(command: str, seat_env: str, flags=()) -> str:
    """After a connect whose install failed: the code is spent, but the seat it wrote recovers it."""
    return (f"the code is spent and its credential is in the seat {seat_env}; once the line above is fixed, run "
            f"{rerun_install(command, seat_env, flags)} (no new code is needed)")


def bridge_fix(cfg: dict, command: str, bridge: cm.Shape) -> str:
    root, mode = cm.work_root(cfg), bridge_mode(cfg)
    if mode == UNIT:
        return f"see {cm.launcher_log(bridge)}, or run {command} install again"
    if mode != EXTERNAL:
        where = (f"the token in the vault under {cfg.get('TOKEN_VAULT_KEY')}" if cfg.get("TOKEN_VAULT_KEY") else
                 f"the token in the seat {cfg.get('TOKEN_ENV_FILE')} names")
        return (f"{mode} runs {bridge_home(root) / DECLARATION} on its next scan of the declarations: start it if it "
                f"is not running (src/sparrowd.py), with {where}")
    return (f"start the bridge with AGENT_CONNECT_TASK_DIR={root}/tasks AGENT_CONNECT_RESULT_DIR={root}/results "
            f"AGENT_CONNECT_STATE_DIR={bridge_state(cfg)}")


def bridge_problem(cfg: dict, command: str, bridge: cm.Shape, now=None) -> str:
    """'' when a running, connected bridge of this agent writes exactly this root's tasks and results."""
    root, state = cm.work_root(cfg), bridge_state(cfg)
    tasks, results = (os.path.realpath(os.path.join(root, d)) for d in ("tasks", "results"))
    fresh = statuses(state, now)
    serving = [(p, r) for p, r in fresh if r.get("tasks_dir") == tasks]
    if not serving:
        seen = ", ".join(sorted({str(r.get("tasks_dir") or "an unreported inbox") for _, r in fresh}))
        why = (f"the running bridge feeds {seen}, not {tasks}" if fresh else
               f"no bridge wrote a status under {state} in the last {STATUS_MAX_AGE_S} s")
        return f"{why}; {bridge_fix(cfg, command, bridge)}"
    path, rec = serving[0]
    if rec.get("results_dir") != results:
        return f"{path}: results go to {rec.get('results_dir')}, not {results}; {bridge_fix(cfg, command, bridge)}"
    if not rec.get("connected"):
        return f"{path}: the bridge runs but is not connected ({rec.get('error') or 'no error given'})"
    if cfg.get("AGENT_ID") and rec.get("agent_id") and rec["agent_id"] != cfg["AGENT_ID"]:
        return f"{path}: the bridge serves {rec['agent_id']}, not {cfg['AGENT_ID']}; {bridge_fix(cfg, command, bridge)}"
    return ""


def under_agent_lock(root: str, fn) -> int:
    """fn() under the agent lock for `root`, so an install's local setup and its bridge wiring are one
    operation no other actor (an uninstall, the other adapter) can step into; 1 when it is busy. The job it
    started is settled once the lock is released (cm.settle)."""
    try:
        with cm.agent_lock(root):
            try:
                rc = fn()
            except BaseException:
                cm.abort_pending(root)
                raise
    except cm.Busy as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return cm.settle(root, rc)


def wire_bridge_bound(cfg: dict, command: str, bridge: cm.Shape, local: cm.Shape, root_id) -> str:
    """wire_bridge under the agent lock, held to connect's root (`root_id`, None outside connect): refused
    unless the root path names that directory before wiring and after. On a mismatch, or a wiring that
    fails under connect, this attempt is undone: the bridge artifact it created (none was there before)
    and the job its install started (cm.stop_started); nothing at the new `root` is touched."""
    root = cm.work_root(cfg)
    try:
        with cm.agent_lock(root):
            return _wire_bound(cfg, command, bridge, local, root_id, root)
    except cm.Busy as e:
        return str(e)


def _bridge_artifact(cfg: dict, bridge: cm.Shape) -> "Path | None":
    """The bridge unit this install wires; a host supervisor's declaration is the credential owner's to read."""
    return cm.supervisor_path(bridge) if bridge_mode(cfg) == UNIT else None


def _bytes(path: "Path | None") -> "bytes | None":
    try:
        return path.read_bytes() if path is not None else None
    except OSError:
        return None


def _wire_bound(cfg: dict, command: str, bridge: cm.Shape, local: cm.Shape, root_id, root: str) -> str:
    if root_id is not None and cm.dir_identity(root) != root_id:
        return _replaced(root, local, cm.stop_started(local, root), "")
    artifact = _bridge_artifact(cfg, bridge)
    before = _bytes(artifact)
    problem = wire_bridge(cfg, command, bridge)
    replaced = root_id is not None and cm.dir_identity(root) != root_id
    if root_id is None or not (problem or replaced):
        pending = cm.pending_start(local) if root_id is not None else None
        if not problem and pending is not None and before is None and _bytes(artifact) is not None:
            pending["undo"].append(lambda: unwire_bridge(cfg, command, bridge))
        return problem
    after = _bytes(artifact)
    note = "" if bridge_mode(cfg) in (UNIT, EXTERNAL) else \
        f"the bridge declaration is left for {command} uninstall to remove; "
    if before is None and after is not None:
        unwired = unwire_bridge(cfg, command, bridge)
        note = (f"{cm.job_name(bridge)} could not be removed ({unwired}); " if unwired else
                f"the bridge this attempt wired ({cm.job_name(bridge)}) was removed; ")
    stopped = cm.stop_started(local, root)
    if replaced:
        return _replaced(root, local, stopped, note)
    return (f"{problem}; this attempt was undone: {note}"
            + (stopped or f"{cm.job_name(local)}, the job its install started, was stopped"))


def _replaced(root: str, local: cm.Shape, stopped: str, bridge_note: str) -> str:
    return (f"{root} was replaced after install started {cm.job_name(local)}; {bridge_note}"
            + (stopped or f"{cm.job_name(local)}, which held the directory that was there, was stopped")
            + f"; nothing at {root} was touched; check what is there, then connect again")


def wire_bridge(cfg: dict, command: str, bridge: cm.Shape) -> str:
    """After the launcher's install: the agent's bridge pointed at this root, as its own unit or as a
    declaration the host's supervisor runs. '' or why not."""
    root, mode = cm.work_root(cfg), bridge_mode(cfg)
    env, problem = launch_env(cfg)
    if problem:
        return problem
    bridge_state(cfg).mkdir(parents=True, exist_ok=True)
    if mode != UNIT:
        path = bridge_home(root) / DECLARATION
        name = worker_name(Path(root).name)
        problem = _sources.write_bridge_declaration(
            path, written_by=f"{command} install", host=mode, name=name, module=BRIDGE_MODULE,
            env=env, token_vault_key=cfg["TOKEN_VAULT_KEY"])
        if problem:
            return problem
        if not cfg["TOKEN_VAULT_KEY"]:
            print(f"declared: {path} ({mode} runs it as worker {name} on its next scan, with the seat "
                  f"{cfg['TOKEN_ENV_FILE']} and the token file it names)")
            return ""
        print(f"declared: {path} ({mode} runs it as worker {name} on its next scan; the token is read from "
              f"the vault: vault set {cfg['TOKEN_VAULT_KEY']} <the agent's relay token>)")
        print(f"if that token is a bare secret, not https://<gateway>|<secret>, also: vault set "
              f"{url_vault_key(Path(root).name)} <the agent's gateway URL>")
        return ""
    sup = cm.supervisor_path(bridge)
    text, problem = cm.owned_text(sup, cm.supervisor_marker(bridge), "supervisor file")
    if problem:
        return problem
    unit, problem = cm.render_supervisor(bridge, env)
    if problem:
        return problem
    state = cm.manager_state(bridge)
    if state == "unknown":
        return f"{cm.job_name(bridge)}'s state is unknown"
    if text is not None or state != "absent":
        problem = cm.stop_supervisor(bridge)
        if problem:
            return problem
    cm._write(sup, unit, 0o644)
    print(f"installed: {sup}")
    problem = cm.start_supervisor(bridge)
    if problem:
        return problem
    print(f"started: {cm.job_name(bridge)}, feeding {root}/tasks as {cfg['AGENT_ID']}")
    return ""


def unwire_bridge(cfg: dict, command: str, bridge: cm.Shape) -> str:
    """uninstall: the marked bridge unit stopped and removed, or the marked declaration removed; the
    bridge's state beside the root is kept. '' or why not."""
    root, mode = cm.work_root(cfg), bridge_mode(cfg)
    if mode == EXTERNAL:
        return ""
    if mode != UNIT:
        path = bridge_home(root) / DECLARATION
        removed, problem = _sources.remove_bridge_declaration(path, written_by=f"{command} install")
        if problem:
            return problem
        if removed:
            print(f"removed: {path}")
        return ""
    sup = cm.supervisor_path(bridge)
    text, problem = cm.owned_text(sup, cm.supervisor_marker(bridge), "supervisor file")
    if problem:
        return problem
    state = cm.manager_state(bridge)
    if state == "unknown":
        return f"{cm.job_name(bridge)}'s state is unknown"
    if text is not None or state != "absent":
        problem = cm.stop_supervisor(bridge)
        if problem:
            return problem
        print(f"stopped: {cm.job_name(bridge)}")
    if text is not None:
        sup.unlink()
        print(f"removed: {sup}")
    return ""


def relay_rows(results: list, cfg: dict, command: str, bridge: cm.Shape) -> None:
    """verify's rows for the token and unit of the bridge this install wired."""
    mode = bridge_mode(cfg)
    if mode == UNIT:
        cm.relay_check(results, Path(cfg["TOKEN_ENV_FILE"]))
        sup = cm.supervisor_path(bridge)
        text, problem = cm.supervisor_text(sup)
        cm.check(results, text is not None and cm.marked(text, cm.supervisor_marker(bridge)), "bridge unit",
                 problem or (f"{sup} missing; run {command} install" if text is None else
                             f"{sup} ({cm.job_name(bridge)} {cm.manager_state(bridge)})"))
    elif mode != EXTERNAL:
        where = (f"the vault key {cfg.get('TOKEN_VAULT_KEY')}, read by {mode} at start" if cfg.get("TOKEN_VAULT_KEY")
                 else f"the seat {cfg.get('TOKEN_ENV_FILE')}'s token file, read by the bridge")
        print(f"INFO relay token: {where}; declared in {bridge_home(cm.work_root(cfg)) / DECLARATION}")


def kept_token(cfg: dict):
    """The token file uninstall names as kept: the agent's own, none under a vault, else the host's."""
    mode = bridge_mode(cfg)
    return cfg.get("TOKEN_ENV_FILE", "") if mode == UNIT else None if mode == EXTERNAL else ""


# ── connect --code: preflight, redeem, the seat ──────────────────────────────

def host_problem(command: str) -> str:
    """'' when connect may wire an agent's own bridge on this host, else why not."""
    if _dirs.task_dir_configured():
        return "AGENT_CONNECT_TASK_DIR is set; connect wires the agent's own root, so unset it first"
    return ""


def redeem_into_seat(base: str, code_arg, trust_relay_host: str, preflight,
                     local: cm.Shape) -> "tuple[dict | None, str]":
    """({agent_id, root, seat_env, root_id, lock}, '') or (None, the line to print): the code read, checked with
    `preflight()` and refused while `local`'s job runs, both before it is spent; redeemed once; the
    credential written to the agent's seat only while this process holds the free root and its path still
    names it (connect_common.under_root). `root_id` is that root's identity, for install to hold to; `lock`
    is the agent lock, taken once the root is known and held until finish_connect. The bearer never leaves
    the seat's token file."""
    code, problem = cm.read_code(code_arg)
    problem = problem or cm.code_problem(base, code) or preflight() or cm.active_job_problem(local)
    if problem:
        return None, f"{problem}; the code was not used"
    redeemed, problem = cm.redeem_code(base, code)
    if problem:
        return None, problem
    problem = cm.relay_problem(base, redeemed["relay_url"], trust_relay_host or "")
    root, problem = ("", problem) if problem else cm.canonical_root(
        str(_dirs.agents_dir() / agent_identity.root_segment(redeemed["agent_id"])), "")
    if problem:
        return None, f"the code is spent but the seat was not written: {problem}; make a new code"
    lock = cm.agent_lock(root)
    try:
        lock.__enter__()
    except cm.Busy as e:
        return None, f"{e}; the code is spent and nothing was written: make a new code once it is done"
    try:
        seat, problem = _seat_under_lock(local, root, redeemed)
    except BaseException:
        lock.release()
        raise
    if problem:
        lock.release()
        return None, problem
    return dict(seat, lock=lock), ""


def _seat_under_lock(local: cm.Shape, root: str, redeemed: dict) -> "tuple[dict, str]":
    def act(owner):
        seat_env, why = cm.write_seat_held(owner, root, bridge_home(root), redeemed)
        return seat_env, cm.held_identity(owner), why
    written, held = cm.under_root(local, root, act, precheck=lambda: cm.seat_problem(bridge_home(root)))
    if held:
        return {}, (f"{held}; the code is spent and nothing was written ({bridge_home(root)} is unchanged): "
                    "make a new code in AG2 Space once the root is free")
    seat, root_id, problem = written
    if problem:
        return {}, f"the code is spent but the seat was not written: {problem}; make a new code"
    print(f"connected: {redeemed['agent_id']}; credentials in {Path(seat).parent} (mode 600)")
    return {"agent_id": redeemed["agent_id"], "root": root, "seat_env": seat, "root_id": root_id}, ""


def finish_connect(seat: dict, install, command: str, flags) -> int:
    """install() still under the agent lock connect took for the seat, then that lock released, and
    connect's closing line: what to verify, or how the spent code's seat is installed again."""
    try:
        rc = install()
    except BaseException:
        cm.abort_pending(seat["root"])
        raise
    finally:
        seat["lock"].release()
    rc = cm.settle(seat["root"], rc)
    if rc == 0:
        print(f"next: {command} verify (the bridge reports connected within a minute)")
    else:
        print(f"error: {spent_code_line(command, seat['seat_env'], flags)}", file=sys.stderr)
    return rc
