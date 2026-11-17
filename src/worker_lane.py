#!/usr/bin/env python3
"""A pool worker's own agent served by a gateway bridge: the wiring every bridge entry point installs.

Both the core's lane loader (`remote-gateway-bridge.py`) and a declared Sparrow agent bridge
(`sparrow_agent_bridge.py`) call `install(ns, worker, ...)` on the bridge module's namespace: every task
the bridge queues is addressed to the worker (TASK_ADDRESSEE) and routed to it before it is published
(TASK_PRE_PUBLISH, through the core's declared task-event handler run with `--from-lane`), the worker's
pending pointer is re-raised once the task is published, and the only proactive files the bridge claims
and owns are the ones its worker's session stamped (PROACTIVE_CLAIM_GATE, PROACTIVE_OWN_FILE). The
handler is found through `state/task-event-handler.json`, so no skill is named here.

A declared bridge serves a worker only when the connection record names the declaration's agent as
that worker's own (`serving`); it then runs the lane's own loader under `bridge_overlay` (the lane's
tasks/ and results/, the core workspace's state/ and the lane's identity env), so it carries every host
gate the lane does, and the two share one per-instance poller lock and never poll together.

A worker whose row carries `dirs: "own"` (`connections.worker_dirs`) has its lane run on its own
tasks/ and results/ (`lane_dirs`): its folder is its inbox, so no router is installed, only an
attribution-only pre-publish hook (`attributor`), and its stamped proactive files still written to the
shared results/ are moved into its own (`adopt_stray_proactive`).

The token is the row's own (`credential`), read where the lane reads it, so no second copy is stored.
While a valid declaration exists and its bridge holds the lane's lease, or has had a bounded grace to
take it, the lane keeper does not start the worker's lane (`declared_bridge`, connections).

Run as a script: `declare --worker <w>` writes the declaration sparrowd runs that worker's agent bridge
from (`<workspace>/agents/<segment>.bridge/worker.json`), `withdraw --worker <w>` removes it, and
`status --worker <w>` prints one JSON line: whether it is declared, which process holds the lane's lease
(`held_by`: the declared bridge under sparrowd, another process, none) and each running sparrowd. Each
takes `--workspace`.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parent
_REPO = _SRC.parent
sys.path[:0] = [p for p in (str(_SRC), str(_REPO / "packages" / "ag2-sparrow")) if p not in sys.path]
HANDLER_LOOKUP = _SRC / "agent" / "task-event-handler-lookup.sh"
# The router holds task and folder locks; past this the bridge publishes and the core's watcher routes.
ROUTE_TIMEOUT_S = 10
# A declaration this command wrote; sparrow_agent_bridge accepts it only for a pool worker's agent.
WRITTEN_BY = "worker-lane declare"
DECLARED_HOST = "sparrowd"


class RecordUnreadable(Exception):
    """The connection record could not be read: whether an agent is a worker's is unknown."""


def route_argv(workspace, results_dir) -> "list[str]":
    """The core's declared task-event handler (the pool router) as run from a worker's bridge, short of
    the task's own paths; [] when none is declared (the core's watcher routes the bridge's tasks)."""
    from util_paths import task_event_handler_config_path
    cfg = task_event_handler_config_path(Path(workspace) / "state")
    try:
        out = subprocess.run(["bash", "-c", 'source "$1" && task_event_handler "$2"', "lane-route",
                              str(HANDLER_LOOKUP), str(cfg)], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    handler = os.fsdecode(out.stdout.removesuffix(b"\n"))
    if out.returncode != 0 or not handler:
        return []
    return [handler, "--workspace", str(workspace), "--results-dir", str(results_dir), "--from-lane"]


def router(worker: str, argv: "list[str]", log):
    """TASK_PRE_PUBLISH for `worker`'s bridge: route the staged task before it reaches `tasks/`, so it is
    the worker's when it appears and the core's watcher, which never routes a held task, leaves it."""

    def run(tid: str, args: "list[str]", step: str) -> "int | None":
        try:
            return subprocess.run(argv + args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=ROUTE_TIMEOUT_S).returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            log(f"lane for worker {worker}: {step} {tid} failed ({exc!r})")
            return None

    def route(tid: str, staged: Path, dest: Path):
        rc = run(tid, ["--task-file", str(dest), "--payload", str(staged)], "routing")
        if rc is not None and rc != 0:
            log(f"lane for worker {worker}: {tid} not delivered to it (router exit {rc}); "
                "the core's watcher routes it")
            return None
        # The worker's watcher may have given up on the sentinel before the payload appeared (a slow or
        # killed router, or an earlier serve's): once published, its pending sentinel is raised afresh.
        return lambda: run(tid, ["--task-file", str(dest), "--renotify"], "re-raising")
    return route


def attributor(worker: str, argv: "list[str]", log):
    """TASK_PRE_PUBLISH for an own-dir worker's bridge: its folder is its inbox, so nothing is routed;
    the declared handler only records the task as `worker`'s (`--attribute-only`)."""

    def attribute(tid: str, staged: Path, dest: Path):
        try:
            rc = subprocess.run(argv + ["--task-file", str(dest), "--payload", str(staged), "--attribute-only",
                                        "--worker", worker], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=ROUTE_TIMEOUT_S).returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            rc = repr(exc)
        if rc != 0:
            log(f"lane for worker {worker}: {tid} not attributed to it ({rc}); published to its own folder")
        return None
    return attribute


def adopt_stray_proactive(worker: str, shared_results, own_results) -> "list[str]":
    """Move the proactive files `worker`'s session stamped (`lane_claims_proactive`) from the shared
    results/ into its own; returns the names moved. The move never replaces a file: a name already in its
    folder, or one another consumer took first, is left for a later pass."""
    import undelivered_quarantine
    from proactive_routing import lane_claims_proactive
    moved = []
    try:
        names = sorted(p.name for p in Path(shared_results).glob("proactive-*.txt"))
    except OSError:
        return moved
    for name in names:
        if not worker or not lane_claims_proactive(name, worker):
            continue
        src, dst = Path(shared_results) / name, Path(own_results) / name
        try:
            Path(own_results).mkdir(parents=True, exist_ok=True)
            try:
                undelivered_quarantine.rename_noreplace(src, dst)
            except FileExistsError as e:
                if not undelivered_quarantine.no_primitive(e):
                    raise
                os.link(src, dst)  # refuses a taken name just as atomically
                src.unlink()
        except OSError:
            continue
        moved.append(name)
    return moved


# How often an own-dir bridge looks for its worker's proactive files still landing in the shared results/.
ADOPT_INTERVAL_S = 10.0


def _adopt_loop(worker: str, shared_results, own_results, log, stop, interval_s: float) -> None:
    while True:
        for name in adopt_stray_proactive(worker, shared_results, own_results):
            log(f"lane for worker {worker}: moved {name} from the shared results/ to its own")
        if stop.wait(interval_s):
            return


def claim_gate(worker: str):
    """A worker's bridge shares results/ with the core's lanes: it claims only the proactive files its
    worker's session stamped (`lane_claims_proactive`), which it posts as the worker's agent."""
    from proactive_routing import lane_claims_proactive
    return lambda path: lane_claims_proactive(path.name, worker)


def install(ns: dict, worker: str, *, workspace, results_dir, log, who: str, own: bool = False,
            adopt_interval_s: "float | None" = None) -> None:
    """Set the worker hooks in the bridge module namespace `ns`; `who` names the bridge in the log. `own`:
    the bridge runs on the worker's own folders (`lane_dirs`), so no router is installed and its adopt
    thread's stop event is `ns["WORKER_ADOPT_STOP"]`."""
    ns["TASK_ADDRESSEE"] = lambda: worker
    ns["PROACTIVE_CLAIM_GATE"] = ns["PROACTIVE_OWN_FILE"] = claim_gate(worker)
    argv = route_argv(workspace, results_dir)
    if own:
        ns["WORKER_ADOPT_STOP"] = _install_own(ns, worker, argv, workspace=workspace, results_dir=results_dir, log=log, who=who,
                            interval_s=ADOPT_INTERVAL_S if adopt_interval_s is None else adopt_interval_s)
        return
    log(f"{who} runs as worker {worker}'s own agent: it delivers its tasks to that worker, "
        "and it posts only the proactive files that worker's session writes")
    if not argv:
        log(f"lane for worker {worker}: no task-event handler is declared; the core's watcher routes its tasks")
        return
    ns["TASK_PRE_PUBLISH"] = router(worker, argv, log)


def _install_own(ns: dict, worker: str, argv, *, workspace, results_dir, log, who: str, interval_s: float):
    import threading
    log(f"{who} runs as worker {worker}'s own agent on its own folders: its tasks are queued there for that "
        "worker alone, and it posts only the proactive files that worker's session writes")
    if argv:
        ns["TASK_PRE_PUBLISH"] = attributor(worker, argv, log)
    else:
        log(f"lane for worker {worker}: no task-event handler is declared; its tasks are not attributed")
    stop = threading.Event()
    threading.Thread(target=_adopt_loop, args=(worker, Path(workspace) / "results", Path(results_dir), log, stop,
                                               interval_s), name=f"adopt-{worker}", daemon=True).start()
    return stop


def lane_dirs(workspace, instance: str) -> "tuple[Path, Path, bool]":
    """(tasks dir, results dir, own) a lane `instance` runs on: its worker's own folders when that worker
    has them (`connections.worker_dirs`), else the workspace's shared tasks/ and results/. Paths are
    lexical under `workspace` as given; resolve both sides before comparing with a recorded realpath."""
    from connections import lane_worker, worker_dirs
    ws = Path(workspace)
    own = worker_dirs(lane_worker(instance, ws), ws) if instance else None
    return (own[0], own[1], True) if own else (ws / "tasks", ws / "results", False)


def serving(agent_mxid: str, workspace) -> "tuple[str, str, str]":
    """(worker, lane instance, '') for a pool worker's own agent, ('', '', why) otherwise; RecordUnreadable
    when the connection record cannot be read."""
    from connections import SchemaError, worker_serving_agent
    try:
        worker, instance, problem = worker_serving_agent(agent_mxid, Path(workspace))
    except (SchemaError, OSError) as e:
        raise RecordUnreadable(f"connection record unreadable ({type(e).__name__})") from e
    if problem:
        return "", "", problem
    return (worker, instance, "") if worker else ("", "", f"{agent_mxid} is no pool worker's own agent")


def runtime_identity(worker: str, instance: str, workspace) -> bytes:
    """What a worker's bridge takes from its row beyond the declaration, as bytes for its supervised
    revision: a record-only change to any of it is a changed bridge that sparrowd restarts."""
    from connections import worker_identity
    row = worker_identity(worker, Path(workspace)) or {}
    fields = [worker, instance] + [str(row.get(k) or "") for k in ("token_ref", "gateway_url", "server")]
    # Appended only when set, so a shared-dir worker's revision is unchanged.
    fields += [str(row[k]) for k in ("dirs",) if row.get(k)]
    return "\0".join(fields).encode("utf-8")


class NotServable(Exception):
    """The worker's lane is not in the launch plan (its row changed since the declaration was checked)."""


def bridge_overlay(workspace, instance: str) -> "dict[str, str]":
    """What a worker's declared bridge runs under beyond its declaration: the lane's dirs (`lane_dirs`) and the lane's
    own identity env (`connections.lane_identity_env`), so it runs the loader exactly as its lane does."""
    from connections import ConnectionsError, SchemaError, lane_identity_env, plan_entry
    ws = Path(workspace)
    try:
        entry = plan_entry(instance, workspace=ws)
    except (ConnectionsError, SchemaError, OSError) as e:
        raise NotServable(f"lane {instance} is not in the launch plan ({type(e).__name__})") from e
    tasks, results, _own = lane_dirs(ws, instance)
    return {"AGENT_CONNECT_TASK_DIR": str(tasks), "AGENT_CONNECT_RESULT_DIR": str(results),
            "AGENT_CONNECT_STATE_DIR": str(ws / "state"), "REMOTE_MEDIA_DIR": str(ws / "data" / "remote-media"),
            **lane_identity_env(entry)}


def credential(worker: str, workspace, environ, vault=None) -> "tuple[str, str]":
    """(token, '') for `worker`'s bridge, from its row's own source as its lane reads it
    (`connections.connection_credential`), or ('', why)."""
    from connections import ConnectionsError, connection_credential, worker_identity
    row = worker_identity(worker, Path(workspace))
    if row is None:
        return "", f"worker {worker} has no agent identity row"
    try:
        return connection_credential(row, environ, vault=vault)[1], ""
    except ConnectionsError as e:
        return "", str(e)


def _declaration_for(worker: str, workspace) -> "tuple[dict | None, str]":
    """Where `worker`'s declaration lives and what it holds, from its identity row's agent, or (None, why)."""
    from connections import WORKER_RE, worker_identity, worker_root
    if not WORKER_RE.match(worker or ""):
        return None, f"{worker!r} is not a worker id"
    mxid = str((worker_identity(worker, Path(workspace)) or {}).get("agent_mxid") or "").strip()
    if not mxid:
        return None, f"worker {worker} has no agent identity row"
    from ag2_sparrow import connect_bridge as adapter
    root = worker_root(mxid, Path(workspace))
    segment = root.name
    return {"path": adapter.bridge_home(root) / adapter.DECLARATION, "agent": mxid,
            "name": adapter.worker_name(segment), "module": adapter.BRIDGE_MODULE,
            "env": adapter.bridge_env(str(root), mxid)}, ""


def declared_bridge(worker: str, workspace) -> bool:
    """Whether `worker`'s agent has a declaration the agent bridge runs as that worker: the one check the
    lane keeper asks before starting the worker's lane. Raises when the record or the file cannot be read."""
    import sparrow_agent_bridge
    from connections import worker_agents_dir
    decl, _why = _declaration_for(worker, workspace)
    if decl is None:
        return False
    loaded, _why = sparrow_agent_bridge.load(decl["path"], worker_agents_dir(Path(workspace)))
    return bool(loaded) and loaded.get("worker") == worker


SPARROWD_SCRIPT = "sparrowd.py"
AGENT_BRIDGE_SCRIPT = "sparrow_agent_bridge.py"
# A declared bridge is sparrowd's own child; the hops allow for an interpreter shim between them.
ANCESTOR_HOPS = 3
_ETIME = re.compile(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)$")


def processes() -> "dict[int, tuple[int, int, str]]":
    """{pid: (ppid, seconds running, command line)} for every process `ps` lists; {} when it cannot."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,etime=,command="], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    procs = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        m = _ETIME.match(parts[2]) if len(parts) == 4 else None
        if m and parts[0].isdigit() and parts[1].isdigit():
            d, h, mi, se = (int(x or 0) for x in m.groups())
            procs[int(parts[0])] = (int(parts[1]), ((d * 24 + h) * 60 + mi) * 60 + se, parts[3])
    return procs


def _argv(command: str) -> "list[str]":
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _same_file(a: str, b) -> bool:
    return os.path.realpath(a) == os.path.realpath(str(b))


def sparrowd_runs(procs: dict, now=None) -> "list[dict]":
    """Each running sparrowd: its pid, whether it runs `--agent-bridges`, its checkout, whether that
    checkout's agent bridge accepts a worker declaration, and whether that code changed after it started."""
    now = time.time() if now is None else now
    found = []
    for pid, (_ppid, age, command) in sorted(procs.items()):
        argv = _argv(command)
        script = next((a for a in argv if Path(a).name == SPARROWD_SCRIPT), "")
        if not script:
            continue
        src = Path(script).parent if os.path.isabs(script) else None
        bridge = src / AGENT_BRIDGE_SCRIPT if src else None
        try:
            serves = bool(bridge) and (src / "worker_lane.py").is_file() and WRITTEN_BY in \
                (src / "worker_lane.py").read_text(encoding="utf-8") and "worker_lane" in bridge.read_text(encoding="utf-8")
            stale = serves and bridge.stat().st_mtime > now - age
        except OSError:
            serves = stale = False
        found.append({"pid": pid, "agent_bridges": "--agent-bridges" in argv,
                      "checkout": str(src.parent) if src else "", "serves_workers": serves,
                      "code_changed_since_start": stale})
    return found


def held_by(pid: int, procs: dict, declaration) -> str:
    """Who holds a lane's lease: `declared-bridge` (the agent bridge for `declaration`, under a sparrowd
    running --agent-bridges), `other`, `none` (no holder) or `unknown` (the process is not listed)."""
    if not pid:
        return "none"
    if pid not in procs:
        return "unknown"
    argv = _argv(procs[pid][2])
    mine = len(argv) >= 2 and any(Path(a).name == AGENT_BRIDGE_SCRIPT for a in argv[:-1]) \
        and _same_file(argv[-1], declaration)
    ancestor, hops = procs[pid][0], 0
    while mine and ancestor in procs and hops < ANCESTOR_HOPS:
        up = _argv(procs[ancestor][2])
        if any(Path(a).name == SPARROWD_SCRIPT for a in up) and "--agent-bridges" in up:
            return "declared-bridge"
        ancestor, hops = procs[ancestor][0], hops + 1
    return "other"


def status(worker: str, workspace, procs=None) -> dict:
    """What serves `worker`'s agent now; `ok` False with `why` when the worker has no servable identity."""
    from connections import BridgeLeaseIndeterminate, bridge_pid, worker_identity
    decl, why = _declaration_for(worker, workspace)
    if decl is None:
        return {"worker": worker, "ok": False, "why": why}
    instance = str((worker_identity(worker, Path(workspace)) or {}).get("instance") or "")
    procs = processes() if procs is None else procs
    try:
        pid, lease = bridge_pid(instance, Path(workspace)), ""
    except BridgeLeaseIndeterminate as e:
        pid, lease = 0, str(e)
    try:
        declared, unreadable = declared_bridge(worker, workspace), ""
    except Exception as e:  # noqa: BLE001 - reported as unknown, never as "not declared"
        declared, unreadable = None, f"{type(e).__name__}: {e}"
    out = {"worker": worker, "ok": True, "agent": decl["agent"], "instance": instance, "declaration": str(decl["path"]),
           "declared": declared, "lease_pid": pid, "held_by": "unknown" if lease else held_by(pid, procs, decl["path"]),
           "sparrowd": sparrowd_runs(procs)}
    if lease or unreadable:
        out["why"] = lease or unreadable
    return out


def main(argv=None, workspace=None, vault=None, procs=None) -> int:
    import argparse
    import json
    p = argparse.ArgumentParser(prog="worker_lane.py", description="A pool worker's agent-bridge declaration.")
    p.add_argument("action", choices=("declare", "withdraw", "status"))
    p.add_argument("--worker", required=True)
    p.add_argument("--workspace", type=Path, default=None)
    args = p.parse_args(argv)
    workspace = args.workspace or workspace
    if workspace is None:
        from workspace_default import resolve_workspace
        workspace = resolve_workspace()
    if args.action == "status":
        try:
            out = status(args.worker, workspace, procs)
        except RecordUnreadable as e:
            out = {"worker": args.worker, "ok": False, "why": str(e)}
        print(json.dumps(out, sort_keys=True))
        return 0 if out["ok"] else 1
    from ag2_sparrow import gateway_credential_sources as sources
    decl, why = _declaration_for(args.worker, workspace)
    if decl is not None and args.action == "declare":
        try:
            why = serving(decl["agent"], workspace)[2] or credential(args.worker, workspace, None, vault)[1]
        except RecordUnreadable as e:
            why = str(e)
        decl = None if why else decl
    if decl is None:
        print(f"worker_lane: {args.action} refused: {why}", file=sys.stderr)
        return 1
    if args.action == "withdraw":
        removed, problem = sources.remove_bridge_declaration(decl["path"], written_by=WRITTEN_BY)
        if problem:
            print(f"worker_lane: withdraw refused: {problem}", file=sys.stderr)
            return 1
        print(f"removed: {decl['path']} (the lane keeper starts the worker's lane on its next pass)" if removed
              else f"nothing declared at {decl['path']}")
        return 0
    problem = sources.write_bridge_declaration(
        decl["path"], written_by=WRITTEN_BY, host=DECLARED_HOST, name=decl["name"], module=decl["module"],
        env=decl["env"], token_vault_key="")
    if problem:
        print(f"worker_lane: declare refused: {problem}", file=sys.stderr)
        return 1
    print(f"declared: {decl['path']} (sparrowd runs it as {decl['name']} on its next scan, with the token its "
          "connection row names; the lane keeper no longer starts the worker's lane)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
