#!/usr/bin/env python3
"""A pool worker's own agent through s.space's device-connect flow: the agent starts it, the owner approves.

The core asks its own server for a device code (`POST /api/connect/start` with the agent's `label` and
`display_name`), sends the owner the approval link in their DM, and asks again (`POST /api/connect/poll`)
until the owner has approved it in a browser or the code expires. The answered bearer goes straight into
the vault as the worker's connection, and only after the gateway confirms it is the agent the poll
named, a new agent owned by the core's owner. `rotate=True` asks the same way for a new token of the
worker's existing agent and replaces its secret in place.

The device code and the bearer stay in this process and the vault: nothing here returns, prints, logs,
writes or raises either; callers get the agent id and a status.
"""
from __future__ import annotations

import contextlib
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Optional

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
sys.path[:0] = [p for p in (str(_HERE), str(_REPO / "src")) if p not in sys.path]

import connections
import pool_provision as pp
import pool_roster as pr

START_PATH = "/api/connect/start"
POLL_PATH = "/api/connect/poll"
# The server's own bounds on a code's life and on how often it may be asked.
DEFAULT_TTL_S, MAX_TTL_S = 900, 3600
MIN_INTERVAL_S, MAX_INTERVAL_S = 3, 60
SLOW_DOWN_STEP_S = 5
KEEP_POLLING = ("authorization_pending", "slow_down")
# An approval that lands just before expiry is still collected for this long after it.
COLLECT_GRACE_S = 60
LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?")


class DeviceConnectError(pp.ProvisionError):
    def __init__(self, message: str, status: str = "failed"):
        super().__init__(message)
        self.status = status


@contextlib.contextmanager
def fenced(worker: str, workspace: Optional[Path] = None):
    """Hold the roster lock retirement sets `retired` under, while `worker` is still live: a retire then
    lands first and is refused here, or waits and removes what was saved (pool_manage._retire_identity)."""
    ws = connections._workspace(workspace)
    with pr._locked(ws):
        row = ((pr.load_roster(ws) or {}).get("workers") or {}).get(worker)
        if not isinstance(row, dict) or row.get("state") == "retired":
            raise DeviceConnectError(f"worker {worker} was retired while the approval was pending; nothing "
                                     "was saved and no lane started", status="worker_retired")
        yield


def _bounded(value, default: int, low: int, high: int) -> int:
    try:
        return min(high, max(low, int(value)))
    except (TypeError, ValueError):
        return default


def start(base: str, label: str, display_name: str, *, server: str = "", request=pp.http_json,
          clock=time.time) -> dict:
    """A new device code for agent `label`: {device_code, verify_url, interval, expires_at}. The approval
    link must be on `base`'s origin or on `server`'s own https host, and must not carry the device code,
    which alone collects the bearer."""
    if not LABEL_RE.fullmatch(label or ""):
        raise DeviceConnectError("the label must be 1-32 lowercase letters, digits or inner dashes; it names "
                                 "the agent, and without one the server would hand back the owner's own agent")
    body = {"label": label}
    if display_name:
        body["display_name"] = display_name[:64]
    status, _, out = request("POST", base + START_PATH, payload=body)
    code, url = out.get("device_code"), out.get("verify_url")
    if status != 200 or not isinstance(code, str) or not code or not isinstance(url, str):
        raise DeviceConnectError(f"the server did not start a device connect (HTTP {status}"
                                 f"{', ' + pp.error_code(out) if pp.error_code(out) else ''})")
    url = urllib.parse.urljoin(base + "/", url)
    hosts = {pp.origin_of(base)} | ({pp.origin_of("https://" + server)} if server else set())
    if pp.origin_of(url) not in hosts or not url.startswith("https://") or code in url:
        raise DeviceConnectError("the approval link the server answered is not a link on its own host that is "
                                 "safe to send; nothing was sent")
    ttl = _bounded(out.get("expires_in"), DEFAULT_TTL_S, 60, MAX_TTL_S)
    return {"device_code": code, "verify_url": url, "expires_at": clock() + ttl,
            "interval": _bounded(out.get("interval"), MIN_INTERVAL_S, MIN_INTERVAL_S, MAX_INTERVAL_S)}


def collect(base: str, pending: dict, *, request=pp.http_json, sleep=time.sleep, clock=time.time) -> dict:
    """Ask until the owner has approved: {relay_url, bearer, agent_id}. A code that expires or is gone is
    refused with the time it expired; a refusal never carries the code or a bearer."""
    known = [pending["device_code"]]
    interval = wait = pending["interval"]
    while True:
        sleep(wait)
        try:
            status, headers, out = request("POST", base + POLL_PATH, payload={"device_code": pending["device_code"]})
        except pp.TransportError:
            status, headers, out = 0, {}, {}
        reason = pp.error_code(out)
        # RFC 8628 device-flow answers: still waiting, or asked to poll more slowly.
        if reason == "slow_down":
            interval = min(MAX_INTERVAL_S, interval + SLOW_DOWN_STEP_S)
        wait = interval
        if status == 429:
            wait = max(interval, _bounded(headers.get("retry-after"), interval, 0, MAX_INTERVAL_S))
        if status == 200 and all(isinstance(out.get(k), str) and out.get(k)
                                 for k in ("relay_url", "bearer", "agent_id")):
            return {k: out[k] for k in ("relay_url", "bearer", "agent_id")}
        expired = time.strftime("%H:%M %Z", time.localtime(pending["expires_at"]))
        if status in (404, 410) or clock() > pending["expires_at"] + COLLECT_GRACE_S:
            raise DeviceConnectError(f"the approval link expired at {expired} without an approval; "
                                     "nothing was saved, so start again for a new link", status="expired")
        if reason in KEEP_POLLING:
            continue
        if status not in (0, 200, 202, 429, 503) and status < 500:
            raise DeviceConnectError(pp.scrub(f"the server refused the device connect (HTTP {status}"
                                              f"{', ' + pp.error_code(out) if pp.error_code(out) else ''})", known))


def approval_note(worker_label: str, label: str, verify_url: str, expires_at: float) -> str:
    """The owner's DM: what they approve, the link, and when it stops working."""
    until = time.strftime("%H:%M %Z", time.localtime(expires_at))
    return (f"[dm-only]\nApprove the agent \"{label}\" for pool worker {worker_label}: open this link "
            f"and approve it. It works until {until}.\n\n{verify_url}\n\nNothing is saved until you approve, "
            "and I check that the agent is yours before the worker uses it.")


def expiry_note(worker_label: str, label: str, expires_at: float) -> str:
    """The owner's DM once an unapproved link has lapsed: nothing changed, and how to get a new one."""
    until = time.strftime("%H:%M %Z", time.localtime(expires_at))
    return (f"[dm-only]\nThe approval link for agent \"{label}\" (pool worker {worker_label}) expired at {until} "
            "before it was approved, so nothing was set up. Ask me for a new link whenever you are ready.")


def send_to_owner(workspace: Path, text: str) -> str:
    """Queue `text` to the owner's DM through the proactive path; the file name it was queued as."""
    from local_record import new_name, write_text_whole
    from proactive_routing import proactive_filename
    return write_text_whole(Path(workspace) / "results" / proactive_filename(new_name("worker-connect"), None),
                            text).name


def connect(worker: str, *, label: str, display_name: str = "", worker_label: str = "", rotate: bool = False,
            workspace: Optional[Path] = None, vault=None, lane: Optional[str] = None, base: str = "",
            request=pp.http_json, preview=None, deliver: Optional[Callable[[str], object]] = None,
            say: Callable[[str], None] = lambda text: None, sleep=time.sleep, clock=time.time,
            repo: Optional[Path] = None, logs_dir: Optional[Path] = None,
            start_lane: Optional[Callable[[dict], Optional[dict]]] = None) -> dict:
    """Give `worker` its own agent, or with `rotate` a new token for the one it has: {agent_id, status, row}.
    `deliver(text)` sends the owner the approval note (default: their DM). The token is saved only when
    the gateway confirms the agent the poll named, on the core's server and owned by the core's owner, and
    the save and `start_lane(row)` run under `fenced`, so a worker retired during the wait gets neither."""
    current = connections.worker_identity(worker, workspace)
    if rotate and current is None:
        raise DeviceConnectError(f"worker {worker} has no agent of its own to rotate")
    if not rotate and current is not None:
        raise DeviceConnectError(f"worker {worker} already has an agent of its own; rotate it instead")
    core = pp.core_lane(workspace, lane)
    gateway = pp.origin_of(str(core["gateway_url"]))
    base = (base or gateway).rstrip("/")
    if base != gateway:
        raise DeviceConnectError("the connect base must be the core's own gateway origin, so the approval link "
                                 "cannot come from another host")
    pending = start(base, label, display_name, server=str(core.get("server") or ""), request=request, clock=clock)
    known = [pending["device_code"]]
    note = approval_note(worker_label or worker, label, pending["verify_url"], pending["expires_at"])
    send = deliver or (lambda text: send_to_owner(workspace or connections._workspace(None), text))
    send(note)
    say(f"approval link sent to the owner; it works until "
        f"{time.strftime('%H:%M %Z', time.localtime(pending['expires_at']))}")
    try:
        got = collect(base, pending, request=request, sleep=sleep, clock=clock)
    except DeviceConnectError as e:
        if e.status == "expired":
            send(expiry_note(worker_label or worker, label, pending["expires_at"]))
        raise
    known.append(got["bearer"])
    token = f"{got['relay_url']}|{got['bearer']}"
    try:
        with fenced(worker, workspace):
            now = connections.worker_identity(worker, workspace)
            if (now or {}).get("instance") != (current or {}).get("instance"):
                raise DeviceConnectError(f"worker {worker}'s own identity changed while the approval was "
                                         "pending; nothing was saved", status="identity_changed")
            out = _save(worker, token, got["agent_id"], core, current, workspace=workspace, vault=vault,
                        preview=preview, repo=repo, logs_dir=logs_dir)
            if start_lane is not None and not rotate:
                out["launch"] = start_lane(out["row"])
            return out
    except DeviceConnectError as e:
        raise DeviceConnectError(pp.scrub(str(e), known), status=e.status) from None
    except (pp.ProvisionError, connections.ConnectionsError) as e:
        raise DeviceConnectError(pp.scrub(f"{pp._said(e)}; the approved agent {got['agent_id']} was not "
                                          "saved", known)) from None


def _save(worker, token, agent_id, core, current, *, workspace, vault, preview, repo, logs_dir) -> dict:
    if not pp._MXID_RE.fullmatch(agent_id):
        raise DeviceConnectError("the poll answer names no agent id")
    if pp.origin_of(connections.gateway_url_of(token)) != pp.origin_of(str(core["gateway_url"])):
        raise DeviceConnectError("the poll answer names a gateway that is not the core's")
    if agent_id == core.get("agent_mxid"):
        raise DeviceConnectError("the server answered with the core's own agent, not a new one for the worker")
    if current is not None and agent_id != current.get("agent_mxid"):
        raise DeviceConnectError(f"a rotate must answer for {current.get('agent_mxid')}, not another agent")
    seen = (preview or connections.preview_connection)(token)
    mxid = str(seen.get("agent_mxid") or "") if seen.get("verified") else ""
    if seen.get("authenticated") is not True or mxid != agent_id:
        raise DeviceConnectError(f"the gateway does not confirm the token is agent {agent_id}'s "
                                 f"({seen.get('error') or 'it names ' + (mxid or 'no agent')})")
    if seen.get("owner_mxid") != core.get("owner_mxid"):
        raise DeviceConnectError(f"agent {agent_id} is not owned by the core's owner")
    if current is not None:
        row = connections.update_token(str(current["instance"]), token, repo=repo or _REPO,
                                       logs_dir=logs_dir or Path(connections._workspace(workspace)) / "logs",
                                       workspace=workspace, vault=vault)
        return {"agent_id": agent_id, "status": "rotated", "row": row}
    # A pasted-token add takes no roster lock, so one can still land after `fenced` looked.
    row, created = connections.add_worker_identity(worker, token, expected_server=str(seen.get("server") or ""),
                                                   source="agent_link", preview=seen, workspace=workspace,
                                                   vault=vault)
    if not created:
        raise DeviceConnectError(f"worker {worker} already had a connection ({row['instance']}) by the time the "
                                 "approved token was saved; it was left as it is", status="identity_changed")
    return {"agent_id": agent_id, "status": "ok", "row": row}
