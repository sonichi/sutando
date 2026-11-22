#!/usr/bin/env python3
"""Start a new worker's own s.space identity without waiting for the owner's approval.

`create_worker.py` calls `request` once the worker is registered. It runs the same
device-connect flow as `pool_identity.py add <worker> --device-flow --label <label> --own-dirs`, as that
command in a background process, and returns once the owner has been sent the approval link
(or the start failed, or `wait_s` passed). The background process waits for the approval,
saves the token, puts the worker on its agent's own task and result folders and restarts it as its own
agent, exactly as the command does alone.

Nothing here fails the creation: every way the step cannot start is answered as
{"status": "skipped", "why": ...}, and the worker answers through the core as before.

Statuses: requested (the link was sent; `until` is its expiry), approved (already), starting (the process runs but
had not confirmed the link within `wait_s`), skipped (with why), off (turned off).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
sys.path[:0] = [p for p in (str(_HERE), str(_REPO / "src")) if p not in sys.path]

import skill_config  # noqa: E402

MANIFEST = _HERE.parent / "manifest.json"
FLAG = "WORKER_POOL_IDENTITY_ON_CREATE"
DEFAULT_WAIT_S = 20.0
MAX_LABEL = 32
# Approval records in these states name an agent that exists or may still be approved.
_HOLDS_LABEL = ("pending", "resending", "done")


def on_by_default(environ=None) -> bool:
    """The manifest's default (on); `WORKER_POOL_IDENTITY_ON_CREATE=0` in the env turns it off."""
    return skill_config.enabled(FLAG, MANIFEST, environ=environ)


def slug(name: str, worker_id: str = "") -> str:
    """A device-connect label from a worker's name: lowercase letters, digits, inner dashes, 1-32."""
    s = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]", "-", str(name or "").lower())).strip("-")
    s = s[:MAX_LABEL].strip("-")
    if not s or (worker_id and s == worker_id[:MAX_LABEL]):
        s = f"worker-{worker_id[:8]}" if worker_id else "worker"
    return s


def _uses(mxid: str, label: str) -> bool:
    local = str(mxid or "").lstrip("@").split(":", 1)[0]
    local = local[:-len(".agent")] if local.endswith(".agent") else local
    return local == label or local.endswith("-" + label)


def taken_labels(workspace: Path, worker_id: str) -> tuple:
    """(labels, agent ids) this host already knows other agents by: other workers' live or pending
    approvals, and every connection's agent. The server's own list of the owner's agents is not read."""
    import connections
    import pool_device_connect as dc
    labels = set()
    cards = Path(workspace) / "state" / "agent-cards"
    for path in cards.glob(f"{dc.CARD_KIND}-*.json") if cards.is_dir() else ():
        other = path.stem[len(dc.CARD_KIND) + 1:]
        rec = dc.read_record(workspace, other) if other != worker_id else {}
        if rec.get("label") and rec.get("state") in _HOLDS_LABEL:
            labels.add(str(rec["label"]))
    agents = [str(r.get("agent_mxid") or "") for r in connections.read_rows(workspace)]
    return labels, [a for a in agents if a]


def free_label(base: str, labels=(), agents=()) -> str:
    """`base`, else `base-2`, `base-3`, ... trimmed to fit: the first no known label or agent uses."""
    def used(label: str) -> bool:
        return label in labels or any(_uses(a, label) for a in agents)
    if not used(base):
        return base
    for n in range(2, 100):
        tail = f"-{n}"
        cand = base[:MAX_LABEL - len(tail)].strip("-") + tail
        if not used(cand):
            return cand
    raise ValueError(f"no free label near {base!r}")


def _until(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _why_from_log(log: Path) -> str:
    try:
        lines = [ln.strip() for ln in log.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    except OSError:
        return ""
    said = [ln[len("pool_identity: "):] for ln in lines if ln.startswith("pool_identity: ")]
    return (said[-1] if said else (lines[-1] if lines else ""))[:300]


def _spawn(argv: list, log: Path):
    with open(log, "ab") as out:
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                start_new_session=True, cwd=str(_REPO))


def request(workspace: Path, worker_id: str, worker_label: str = "", *, repo: Optional[Path] = None,
            lane: Optional[str] = None, wait_s: float = DEFAULT_WAIT_S,
            spawn: Optional[Callable[[list, Path], object]] = None,
            sleep=time.sleep, clock=time.time) -> dict:
    """Start the worker's identity step in the background: {status, label?, until?, expires_at?, why?}."""
    try:
        return _request(Path(workspace), worker_id, worker_label, repo=repo, lane=lane, wait_s=wait_s,
                        spawn=spawn or _spawn, sleep=sleep, clock=clock)
    except Exception as e:  # noqa: BLE001 — the worker exists; its identity never fails the creation
        return {"status": "skipped", "why": f"the identity step stopped ({type(e).__name__}: {e})"[:300]}


def _request(ws: Path, wid: str, worker_label: str, *, repo, lane, wait_s, spawn, sleep, clock) -> dict:
    import connections
    import pool_device_connect as dc
    import pool_provision as pp
    if connections.worker_identity(wid, ws) is not None:
        return {"status": "skipped", "why": "the worker already has an agent of its own"}
    try:
        core = pp.core_lane(ws, lane)
    except pp.ProvisionError as e:
        return {"status": "skipped", "why": f"no core lane to ask as: {e}"}
    if not connections.owner_dm(core, ws):
        return {"status": "skipped", "why": f"the core lane {core.get('instance') or '<legacy>'} has no "
                                            "owner DM to send the approval link to"}
    label = free_label(slug(worker_label, wid), *taken_labels(ws, wid))
    logs = ws / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"worker-identity-{wid}.log"
    argv = [sys.executable, str(_HERE / "pool_identity.py"), "--workspace", str(ws), "add", wid,
            "--device-flow", "--label", label, "--own-dirs", "--repo", str(repo or _REPO), "--logs-dir", str(logs)]
    if worker_label and worker_label != wid:
        argv += ["--display-name", worker_label[:64]]
    if lane is not None:
        argv += ["--lane", str(lane)]
    started = clock()
    try:
        proc = spawn(argv, log)
    except OSError as e:
        return {"status": "skipped", "label": label, "why": f"the identity process did not start ({type(e).__name__})"}
    deadline = started + max(0.0, wait_s)
    while True:
        rec = dc.read_record(ws, wid)
        if (rec.get("label") == label and rec.get("state") in ("pending", "done")
                and float(rec.get("expires_at") or 0) > started):
            exp = float(rec["expires_at"])
            return {"status": "requested" if rec["state"] == "pending" else "approved", "label": label,
                    "expires_at": exp, "until": _until(exp)}
        rc = proc.poll() if hasattr(proc, "poll") else None
        if rc is not None:
            said = _why_from_log(log)
            why = f"the approval flow did not start: {said}" if said else f"the identity process ended with exit {rc}"
            return {"status": "skipped", "label": label, "why": why}
        if clock() >= deadline:
            return {"status": "starting", "label": label,
                    "why": f"the approval link was not confirmed within {int(wait_s)}s; see {log}"}
        sleep(0.25)


def describe(out: dict) -> str:
    """The creation report's identity line."""
    status = out.get("status")
    if status == "requested":
        return f"requested (link expires {out['until']}; agent \"{out['label']}\")"
    if status == "approved":
        return f"approved (agent \"{out['label']}\")"
    if status == "starting":
        return f"starting (agent \"{out.get('label')}\"; {out.get('why')})"
    if status == "off":
        return "skipped (--no-identity)" if out.get("why") == "--no-identity" else f"skipped ({out.get('why')})"
    return f"skipped ({out.get('why') or 'unknown reason'})"


if __name__ == "__main__":
    raise SystemExit("pool_identity_request is a library; create_worker.py runs it")
