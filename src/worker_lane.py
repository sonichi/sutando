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
that worker's own (`serving`); `bridge_overlay` then runs it on the core workspace's tasks/, results/
and state/ under the worker row's lane instance, exactly as the worker's lane runs, so the two share
one per-instance poller lock and never poll together.

Run as a script: `declare --worker <w>` writes the declaration sparrowd runs that worker's agent bridge
from (`<workspace>/agents/<segment>.bridge/worker.json`), `withdraw --worker <w>` removes it.
"""
from __future__ import annotations

import os
import subprocess
import sys
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


def claim_gate(worker: str):
    """A worker's bridge shares results/ with the core's lanes: it claims only the proactive files its
    worker's session stamped (`lane_claims_proactive`), which it posts as the worker's agent."""
    from proactive_routing import lane_claims_proactive
    return lambda path: lane_claims_proactive(path.name, worker)


def install(ns: dict, worker: str, *, workspace, results_dir, log, who: str) -> None:
    """Set the worker hooks in the bridge module namespace `ns`; `who` names the bridge in the log."""
    log(f"{who} runs as worker {worker}'s own agent: it delivers its tasks to that worker, "
        "and it posts only the proactive files that worker's session writes")
    ns["TASK_ADDRESSEE"] = lambda: worker
    ns["PROACTIVE_CLAIM_GATE"] = ns["PROACTIVE_OWN_FILE"] = claim_gate(worker)
    argv = route_argv(workspace, results_dir)
    if not argv:
        log(f"lane for worker {worker}: no task-event handler is declared; the core's watcher routes its tasks")
        return
    ns["TASK_PRE_PUBLISH"] = router(worker, argv, log)


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


def bridge_overlay(workspace, instance: str) -> "dict[str, str]":
    """What a worker's declared bridge runs under beyond its declaration: the lane's dirs and instance."""
    ws = Path(workspace)
    return {"AGENT_CONNECT_TASK_DIR": str(ws / "tasks"), "AGENT_CONNECT_RESULT_DIR": str(ws / "results"),
            "AGENT_CONNECT_STATE_DIR": str(ws / "state"), "GATEWAY_INSTANCE": instance,
            "REMOTE_MEDIA_DIR": str(ws / "data" / "remote-media")}


def _declaration_for(worker: str, workspace) -> "tuple[dict | None, str]":
    """The declaration fields for `worker`'s agent, or (None, why)."""
    from connections import WORKER_RE, worker_identity
    if not WORKER_RE.match(worker or ""):
        return None, f"{worker!r} is not a worker id"
    mxid = str((worker_identity(worker, Path(workspace)) or {}).get("agent_mxid") or "")
    if not mxid:
        return None, f"worker {worker} has no agent identity row"
    _worker, _instance, problem = serving(mxid, workspace)
    if problem:
        return None, problem
    from ag2_sparrow import agent_identity
    from ag2_sparrow import connect_bridge as adapter
    segment = agent_identity.root_segment(mxid)
    root = Path(workspace) / "agents" / segment
    return {"path": adapter.bridge_home(root) / adapter.DECLARATION, "segment": segment,
            "name": adapter.worker_name(segment), "module": adapter.BRIDGE_MODULE,
            "env": adapter.bridge_env(str(root), mxid), "token_vault_key": adapter.vault_key(segment),
            "url_vault_key": adapter.url_vault_key(segment)}, ""


def main(argv=None, workspace=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="worker_lane.py", description="A pool worker's agent-bridge declaration.")
    p.add_argument("action", choices=("declare", "withdraw"))
    p.add_argument("--worker", required=True)
    args = p.parse_args(argv)
    if workspace is None:
        from workspace_default import resolve_workspace
        workspace = resolve_workspace()
    from ag2_sparrow import gateway_credential_sources as sources
    try:
        decl, why = _declaration_for(args.worker, workspace)
    except RecordUnreadable as e:
        decl, why = None, str(e)
    if decl is None:
        print(f"worker_lane: {args.action} refused: {why}", file=sys.stderr)
        return 1
    if args.action == "withdraw":
        removed, problem = sources.remove_bridge_declaration(decl["path"], written_by=WRITTEN_BY)
        if problem:
            print(f"worker_lane: withdraw refused: {problem}", file=sys.stderr)
            return 1
        print(f"removed: {decl['path']}" if removed else f"nothing declared at {decl['path']}")
        return 0
    problem = sources.write_bridge_declaration(
        decl["path"], written_by=WRITTEN_BY, host=DECLARED_HOST, name=decl["name"], module=decl["module"],
        env=decl["env"], token_vault_key=decl["token_vault_key"])
    if problem:
        print(f"worker_lane: declare refused: {problem}", file=sys.stderr)
        return 1
    print(f"declared: {decl['path']} (sparrowd runs it as {decl['name']} on its next scan; vault set "
          f"{decl['token_vault_key']} <the worker's relay token>, and {decl['url_vault_key']} <its gateway URL> "
          "if that token is a bare secret)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
