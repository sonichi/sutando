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
(`held_by`: the declared bridge under sparrowd, another process, none) and each running sparrowd.
`migrate-dirs --worker <w>` shows what a switch would do; with `--apply` it switches the worker onto its
own folders: it creates them, sets `dirs: "own"`,
has the declared handler empty the worker's deliveries folder (its own lane's tasks move into its tasks
folder, the core's are handed back) and moves its unclaimed live results; `--undo` switches it back and
returns its live tasks and results to the shared folders. Both are idempotent. Each takes `--workspace`.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
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


def handler_lookup(workspace) -> "tuple[str, str]":
    """(the core's declared task-event handler, ''); ('', '') when provably none is declared (no pin, no
    config file); ('', why) when the lookup failed or a declared one is unusable."""
    from util_paths import task_event_handler_config_path
    cfg = task_event_handler_config_path(Path(workspace) / "state")
    try:
        out = subprocess.run(["bash", "-c", 'source "$1" && task_event_handler "$2"', "lane-route",
                              str(HANDLER_LOOKUP), str(cfg)], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return "", f"the task-event handler lookup failed ({e!r})"
    handler = os.fsdecode(out.stdout.removesuffix(b"\n"))
    if out.returncode == 0 and handler:
        return handler, ""
    if os.environ.get("SUTANDO_TASK_EVENT_HANDLER") or cfg.exists():
        return "", f"a task-event handler is declared but unusable (lookup exit {out.returncode})"
    return "", ""


def declared_handler(workspace) -> str:
    """The core's declared task-event handler (the pool router); "" when none is declared or usable."""
    return handler_lookup(workspace)[0]


def route_argv(workspace, results_dir) -> "list[str]":
    """The core's declared task-event handler (the pool router) as run from a worker's bridge, short of
    the task's own paths; [] when none is declared (the core's watcher routes the bridge's tasks)."""
    handler = declared_handler(workspace)
    if not handler:
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


def _holds_lane(worker: str, workspace) -> bool:
    """Whether this process holds `worker`'s lane lease: only the bridge serving the lane now may adopt."""
    from connections import BridgeLeaseIndeterminate, bridge_pid, worker_identity
    instance = str((worker_identity(worker, Path(workspace)) or {}).get("instance") or "")
    try:
        return bool(instance) and bridge_pid(instance, Path(workspace)) == os.getpid()
    except BridgeLeaseIndeterminate:
        return False


def _still_own(worker: str, workspace, own_results, lease: bool = True) -> bool:
    """Whether `own_results` is still `worker`'s own results folder and this process still serves its lane:
    a switch replaces the bridge (`migrate-dirs` waits for that), so an older one adopts nothing."""
    from connections import worker_dirs
    dirs = worker_dirs(worker, Path(workspace))
    return bool(dirs) and dirs[1].resolve() == Path(own_results).resolve() and (not lease or _holds_lane(worker, workspace))


def _adopt(worker: str, shared_results, own_results, names, log, belongs=lambda name: True,
           lease: bool = True) -> "list[str]":
    import undelivered_quarantine
    moved = []
    for name in names:
        # Re-checked right before each move: the field can be cleared while a pass runs.
        if not _still_own(worker, Path(shared_results).parent, own_results, lease) or not belongs(name):
            continue
        try:
            Path(own_results).mkdir(parents=True, exist_ok=True)
            undelivered_quarantine.rename_noreplace(Path(shared_results) / name, Path(own_results) / name)
        except FileExistsError as e:
            if undelivered_quarantine.no_primitive(e):
                undelivered_quarantine.report_once("worker-lane:adopt", log or (lambda _m: None),
                                                   f"lane for worker {worker}: no no-replace rename on this "
                                                   "platform; its files in the shared results/ stay there")
                return moved
            continue
        except OSError:
            continue
        moved.append(name)
    return moved


def _names(folder, pattern: str) -> "list[str]":
    try:
        return sorted(p.name for p in Path(folder).glob(pattern))
    except OSError:
        return []


def adopt_stray_proactive(worker: str, shared_results, own_results, log=None, lease: bool = True) -> "list[str]":
    """Move the proactive files `worker`'s session stamped (`lane_claims_proactive`) from the shared
    results/ into its own; returns the names moved. The move never replaces a file: a name already in its
    folder, or one another consumer took first, is left for a later pass. Without a no-replace rename
    nothing is moved (a link-then-unlink could delete a file a producer just republished there). `lease`
    False: a caller that is not the bridge and runs once the old bridge is gone (`migrate-dirs`)."""
    from proactive_routing import lane_claims_proactive
    if not worker:
        return []
    names = [n for n in _names(shared_results, "proactive-*.txt") if lane_claims_proactive(n, worker)]
    return _adopt(worker, shared_results, own_results, names, log, lease=lease)


def adopt_stray_results(worker: str, workspace, own_results, log=None, lease: bool = True) -> "list[str]":
    """Move the results of tasks whose home is `worker`'s own folder, `own_results` (`connections.task_home`),
    from the shared results/ into it, as `adopt_stray_proactive` does: a late answer an older session wrote."""
    from connections import task_home
    ws = Path(workspace)
    if not worker:
        return []
    def belongs(name: str) -> bool:
        home = task_home(Path(name).stem, ws)
        return home["own"] and home["worker"] == worker and home["results"].resolve() == Path(own_results).resolve()
    return _adopt(worker, ws / "results", own_results, _names(ws / "results", "task-*.txt"), log, belongs, lease)


# How often an own-dir bridge looks for its worker's proactive files still landing in the shared results/.
ADOPT_INTERVAL_S = 10.0


def _adopt_loop(worker: str, shared_results, own_results, log, stop, interval_s: float) -> None:
    while True:
        for name in adopt_stray_proactive(worker, shared_results, own_results, log) + \
                adopt_stray_results(worker, Path(shared_results).parent, own_results, log):
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
    from connections import ensure_worker_dirs, lane_worker
    ws = Path(workspace)
    own = ensure_worker_dirs(lane_worker(instance, ws), ws) if instance else None
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


CAPABILITY = re.compile(r"^ACCEPTS_WORKER_DECLARATIONS = 1$", re.M)
# What sparrowd loads when it starts and scans declarations; a newer copy needs a restart.
LOADED_CODE = (("src", AGENT_BRIDGE_SCRIPT), ("src", "worker_lane.py"),
               ("packages", "ag2-sparrow", "ag2_sparrow", "connect_bridge.py"))
_FLAG = re.compile(r"(^|\s)--agent-bridges(\s|$)")


def _script_path(command: str, script: str) -> str:
    """The longest absolute path ending in `/script` that the command line names and that is a file; ''
    when none. `ps` joins argv with spaces unquoted, so a path with a space cannot be split out by tokens."""
    for m in re.finditer(re.escape("/" + script) + r"(?=\s|$)", command):
        end = m.end()
        starts = [i for i, ch in enumerate(command[:end]) if ch == "/" and (i == 0 or command[i - 1] == " ")]
        for i in starts:
            if os.path.isfile(command[i:end]):
                return command[i:end]
    return ""


def _names_script(command: str, script: str) -> bool:
    return re.search(r"(^|[\s/])" + re.escape(script) + r"(\s|$)", command) is not None


def sparrowd_runs(procs: dict, now=None) -> "list[dict]":
    """Each running sparrowd: its pid, whether it runs `--agent-bridges`, its checkout, whether that
    checkout's agent bridge accepts a worker declaration, and whether the code it loaded changed after it
    started."""
    now = time.time() if now is None else now
    found = []
    for pid, (_ppid, age, command) in sorted(procs.items()):
        if not _names_script(command, SPARROWD_SCRIPT):
            continue
        script = _script_path(command, SPARROWD_SCRIPT)
        checkout = Path(script).parent.parent if script else None
        try:
            serves = bool(checkout) and CAPABILITY.search(
                (checkout / "src" / AGENT_BRIDGE_SCRIPT).read_text(encoding="utf-8")) is not None
            stale = serves and any((checkout.joinpath(*rel)).stat().st_mtime > now - age
                                   for rel in LOADED_CODE if checkout.joinpath(*rel).is_file())
        except OSError:
            serves = stale = False
        found.append({"pid": pid, "agent_bridges": _FLAG.search(command) is not None,
                      "checkout": str(checkout) if checkout else "", "serves_workers": serves,
                      "code_changed_since_start": stale})
    return found


def _runs_declaration(command: str, declaration) -> bool:
    """The agent bridge's command line for `declaration`: `<python> <...>/sparrow_agent_bridge.py <path>`,
    the path compared by its realpath, so a spaced or linked path is the same file."""
    marker = "/" + AGENT_BRIDGE_SCRIPT + " "
    at = command.rfind(marker)
    if at < 0:
        return False
    return os.path.realpath(command[at + len(marker):]) == os.path.realpath(str(declaration))


def held_by(pid: int, procs: dict, declaration) -> str:
    """Who holds a lane's lease: `declared-bridge` (the agent bridge for `declaration`, under a sparrowd
    running --agent-bridges), `other`, `none` (no holder) or `unknown` (the process is not listed)."""
    if not pid:
        return "none"
    if pid not in procs:
        return "unknown"
    mine = _runs_declaration(procs[pid][2], declaration)
    ancestor, hops = procs[pid][0], 0
    while mine and ancestor in procs and hops < ANCESTOR_HOPS:
        up = procs[ancestor][2]
        if _names_script(up, SPARROWD_SCRIPT) and _FLAG.search(up):
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


# The declared handler empties the worker's deliveries folder under the router's locks.
MIGRATE_TIMEOUT_S = 120


def _move_live(src_dir, dst_dir, names, failed: "list[str]") -> "list[str]":
    """Move each of `names` from `src_dir` to `dst_dir` without replacing a file; returns those moved. A
    name taken at the destination, or already gone, is left; any other failure is added to `failed`."""
    import undelivered_quarantine
    moved = []
    for name in names:
        try:
            Path(dst_dir).mkdir(parents=True, exist_ok=True)
            undelivered_quarantine.rename_noreplace(Path(src_dir) / name, Path(dst_dir) / name)
        except FileExistsError as e:
            if undelivered_quarantine.no_primitive(e):
                failed.append(f"{name}: no no-replace rename on this platform")
            continue
        except FileNotFoundError:
            continue
        except OSError as e:
            failed.append(f"{name}: {type(e).__name__}: {e}")
            continue
        moved.append(name)
    return moved


def _failed(out: dict, failed: "list[str]") -> str:
    if not failed:
        return ""
    out["move_failures"] = failed
    return f"{len(failed)} file(s) could not be moved; re-run to finish"


def _live_txt(folder) -> "list[str]":
    try:
        return sorted(p.name for p in Path(folder).glob("*.txt") if p.is_file())
    except OSError:
        return []


# How long migrate-dirs waits for sparrowd to replace a declared bridge (its rescan plus restart backoff).
RELAUNCH_WAIT_S = 180.0


def relaunch_lane(worker: str, workspace, kill=None, procs=None, wait_s: "float | None" = None,
                  sleep=None, clock=None, switch=None) -> "tuple[str, str]":
    """Make the process serving `worker`'s lane pick up its folders anew; (what happens, '' or why not).
    A declared bridge is restarted by sparrowd (the field is in its revision): this waits until its old
    process no longer holds the lease. Any other holder is stopped, and the lane keeper starts it again.
    `switch` (the field change) runs first under the record lock, which the keeper's starts take, so a
    stopped lane cannot be started again before the field it must start on is written."""
    from connections import _records_lock
    with _records_lock(Path(workspace)):
        if switch is not None:
            switch()
        kind, pid, why = _stop_unless_declared(worker, workspace, kill, procs)
    if why or kind != "declared":
        return kind, why
    return _await_replacement(worker, workspace, pid, wait_s, sleep or time.sleep, clock or time.monotonic)


def _stop_unless_declared(worker: str, workspace, kill, procs) -> "tuple[str, int, str]":
    from connections import BridgeLeaseIndeterminate, BridgeStillRunning, _stop_row, bridge_pid, worker_identity
    ws = Path(workspace)
    row = worker_identity(worker, ws) or {}
    instance = str(row.get("instance") or "")
    try:
        pid = bridge_pid(instance, ws) if instance else 0
    except BridgeLeaseIndeterminate as e:
        return "", 0, f"whether the lane runs cannot be told ({e}); re-run"
    if not pid:
        return "not-running", 0, ""
    decl, _why = _declaration_for(worker, ws)
    if decl and held_by(pid, processes() if procs is None else procs, decl["path"]) == "declared-bridge":
        return "declared", pid, ""
    try:
        _stop_row(row, ws, kill)
    except (BridgeStillRunning, BridgeLeaseIndeterminate) as e:
        return "", pid, f"lane {instance} did not stop ({e}); re-run"
    return f"stopped pid {pid}; the lane keeper starts it again", pid, ""


def _await_replacement(worker: str, workspace, pid: int, wait_s, sleep, clock) -> "tuple[str, str]":
    from connections import BridgeLeaseIndeterminate, bridge_pid, worker_identity
    ws = Path(workspace)
    instance = str((worker_identity(worker, ws) or {}).get("instance") or "")
    deadline = clock() + (RELAUNCH_WAIT_S if wait_s is None else wait_s)
    while True:
        try:
            now_pid = bridge_pid(instance, ws)
        except BridgeLeaseIndeterminate:
            now_pid = pid
        if now_pid != pid:
            return "restarted-by-sparrowd", ""
        if clock() >= deadline:
            return "", (f"pending: sparrowd has not replaced the declared bridge (pid {pid}) yet; nothing was "
                        "moved, re-run once it has")
        sleep(1.0)


def migrate_dirs(worker: str, workspace, undo: bool = False, handler=None, run=subprocess.run,
                 kill=None, procs=None) -> "tuple[dict, str]":
    """Switch `worker` onto its own folders (or back, `undo`); (what was done, '' or why it stopped).
    Idempotent: a re-run picks up whatever an interrupted one left."""
    from connections import ConnectionsError, ensure_worker_dirs, set_worker_dirs, worker_identity, worker_root
    ws = Path(workspace)
    mxid = str((worker_identity(worker, ws) or {}).get("agent_mxid") or "").strip()
    if not mxid:
        return {"worker": worker}, f"worker {worker} has no agent identity row"
    root = worker_root(mxid, ws)
    tasks, results = root / "tasks", root / "results"
    out = {"worker": worker, "tasks": str(tasks), "results": str(results), "undo": undo, "respawn_needed": True}
    if undo:
        out["lane"], why = relaunch_lane(worker, ws, kill, procs,
                                         switch=lambda: set_worker_dirs(worker, False, workspace=ws))
        if why:
            return out, why
        from delivery.task_dispatch import has_ready_result
        failed: "list[str]" = []
        moved = _move_live(tasks, ws / "tasks", [n for n in _live_txt(tasks) if not has_ready_result(results, n)],
                           failed)
        for name in moved:
            with contextlib.suppress(OSError):
                os.utime(ws / "tasks" / name)  # the core's watcher picks it up again
        out.update(tasks_returned=moved, results_returned=_move_live(results, ws / "results", _live_txt(results), failed))
        return out, _failed(out, failed)
    # Nothing is committed until it is known who can move the worker's delivered work.
    handler, problem = handler_lookup(ws) if handler is None else (handler, "")
    if problem:
        return out, f"{problem}; nothing was switched, re-run once it is fixed"
    if not handler:
        from delivery.task_dispatch import WorkerHoldUnreadable, owned_task_ids
        try:
            owed = owned_task_ids(ws / "deliveries", worker)
        except WorkerHoldUnreadable as e:
            return out, f"{e}; nothing was switched"
        if owed:
            return out, (f"{len(owed)} task(s) delivered to {worker} but no task-event handler can move them; "
                         "nothing was switched")
    def switch():
        set_worker_dirs(worker, True, workspace=ws)
        ensure_worker_dirs(worker, ws)
    try:
        out["lane"], why = relaunch_lane(worker, ws, kill, procs, switch=switch)
    except (ConnectionsError, OSError) as e:
        return out, str(e)
    if why:
        return out, why
    if handler:
        try:
            done = run([handler, "--workspace", str(ws), "--migrate-dirs", "--worker", worker],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=MIGRATE_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as e:
            return out, f"the declared handler did not run ({e!r}); re-run to finish"
        if done.returncode != 0:
            return out, f"the declared handler refused (exit {done.returncode}: {done.stderr.strip()[-300:]}); re-run to finish"
        try:
            out["deliveries"] = json.loads(done.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            out["deliveries"] = done.stdout.strip()[-300:]
    failed: "list[str]" = []
    out["tasks_moved"] = _move_live(ws / "tasks", tasks, _queued_for(worker, ws), failed)
    out["results_moved"] = adopt_stray_results(worker, ws, results, lease=False)
    out["proactive_moved"] = adopt_stray_proactive(worker, ws / "results", results, lease=False)
    return out, _failed(out, failed)


def preview_migrate(worker: str, workspace, undo: bool = False, procs=None) -> "tuple[dict, str]":
    """What `migrate_dirs` would do, writing nothing: the folders, the lane's holder and the files it would
    move; ('', why) parts as `migrate_dirs` reports a refusal it can already see."""
    from connections import BridgeLeaseIndeterminate, bridge_pid, task_home, worker_dirs, worker_identity, worker_root
    from delivery.task_dispatch import WorkerHoldUnreadable, owned_task_ids
    from proactive_routing import lane_claims_proactive
    ws = Path(workspace)
    row = worker_identity(worker, ws) or {}
    mxid = str(row.get("agent_mxid") or "").strip()
    if not mxid:
        return {"worker": worker, "applied": False}, f"worker {worker} has no agent identity row"
    root = worker_root(mxid, ws)
    tasks, results = root / "tasks", root / "results"
    out = {"worker": worker, "tasks": str(tasks), "results": str(results), "undo": undo, "applied": False,
           "own_now": worker_dirs(worker, ws) is not None}
    try:
        pid = bridge_pid(str(row.get("instance") or ""), ws) if row.get("instance") else 0
    except BridgeLeaseIndeterminate:
        pid = -1
    decl, _why = _declaration_for(worker, ws)
    out["lane"] = ("unknown" if pid < 0 else "not-running" if not pid else
                   held_by(pid, processes() if procs is None else procs, decl["path"]) if decl else "other")
    if undo:
        out.update(would_return_tasks=_live_txt(tasks), would_return_results=_live_txt(results))
        return out, ""
    handler, problem = handler_lookup(ws)
    out["handler"] = handler or ""
    try:
        out["delivered"] = owned_task_ids(ws / "deliveries", worker)
    except WorkerHoldUnreadable as e:
        return out, str(e)
    out["would_move_tasks"] = _queued_for(worker, ws)
    out["would_move_results"] = [n for n in _live_txt(ws / "results") if
                                 (task_home(Path(n).stem, ws)["worker"] == worker if n.startswith("task-")
                                  else n.startswith("proactive-") and lane_claims_proactive(n, worker))]
    if problem:
        return out, f"{problem}; nothing would be switched"
    if not handler and out["delivered"]:
        return out, f"{len(out['delivered'])} task(s) delivered to {worker} but no task-event handler can move them"
    return out, ""


def _queued_for(worker: str, ws: Path) -> "list[str]":
    """Live task files in the shared tasks/ that `worker`'s own lane queued and no delivery holds any
    longer (one routed to it moved with its sentinel; one still held stays for a re-run)."""
    from connections import task_home
    from delivery.task_dispatch import WorkerHoldUnreadable, owned_task_ids
    try:
        held = set(owned_task_ids(ws / "deliveries", worker))
    except WorkerHoldUnreadable:
        return []
    return [n for n in _live_txt(ws / "tasks")
            if Path(n).stem not in held and task_home(Path(n).stem, ws)["worker"] == worker]


def main(argv=None, workspace=None, vault=None, procs=None, run=subprocess.run) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="worker_lane.py", description="A pool worker's agent-bridge declaration.")
    p.add_argument("action", choices=("declare", "withdraw", "status", "migrate-dirs"))
    p.add_argument("--worker", required=True)
    p.add_argument("--workspace", type=Path, default=None)
    p.add_argument("--undo", action="store_true", help="migrate-dirs: back onto the shared folders")
    p.add_argument("--apply", action="store_true", help="migrate-dirs: do it (without, only show what it would do)")
    args = p.parse_args(argv)
    workspace = args.workspace or workspace
    if workspace is None:
        from workspace_default import resolve_workspace
        workspace = resolve_workspace()
    if args.action == "migrate-dirs":
        if args.apply:
            out, why = migrate_dirs(args.worker, workspace, args.undo, run=run, procs=procs)
            out["applied"] = True
        else:
            out, why = preview_migrate(args.worker, workspace, args.undo, procs=procs)
        print(json.dumps(dict(out, ok=not why, **({"why": why} if why else {})), sort_keys=True))
        return 1 if why else 0
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
