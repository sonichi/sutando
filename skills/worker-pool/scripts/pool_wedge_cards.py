#!/usr/bin/env python3
"""The owner's card for a wedged worker seat, and the one key it may type.

A wedged live session is never restarted: a fresh session meets the same network
error, rate limit or API error and loses the turn in flight. The pool tick raises
a card instead, through the engine's hitl store:

- abnormal text on the pane: the card quotes the banner line and names its cause.
  When the seat is routed through the credential proxy and the cause is a retry,
  an API error or a network error, the card offers the proxy restart, the remedy
  src/restart.sh already runs; the worker's session is never the target.
- a frozen turn: the card offers "Send Escape". Nothing is typed unless the owner
  presses it, and `drive_escapes` re-reads the pane first: a pane that no longer
  shows the same frozen frame is refused and nothing is typed.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _sibling(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sup = _sibling("pool_supervise")
ps, cw = sup.ps, sup.cw
import quota_availability as qa  # noqa: E402  (src/ is on the path via pool_delivery)

SOURCE = "pool-wedge"
ESCAPE_ACTION = "send_escape"
PROXY_ACTION = "restart_credential_proxy"
PROXY_LABEL = "com.sutando.credential-proxy"
# What src/restart.sh runs for a launchd-supervised proxy; named, never run from here.
PROXY_REMEDY = f"launchctl kickstart -k gui/$(id -u)/{PROXY_LABEL}"
_PROXY_CAUSES = ("api-error", "network-error")
REFUSED_NOTE = "The turn is no longer frozen on the frame this card was raised for; nothing was typed."


def manager_for(workspace):
    from hitl.manager import HitlManager, HitlStore, default_store
    return HitlManager(HitlStore(default_store(Path(workspace))))


def seat_label(workspace, worker_id) -> str:
    row = sup.supervised_workers(workspace).get(worker_id) or {}
    label = row.get("label") or worker_id
    return f"worker {label}" if label == worker_id else f"worker {label} ({worker_id[:8]})"


def worker_runtime(workspace, worker_id) -> str | None:
    """Use the roster's runtime, the same source used by the wedge observer."""
    row = sup.supervised_workers(workspace).get(worker_id)
    if not isinstance(row, dict):
        return None
    runtime = row.get("runtime") or "claude"  # legacy roster rows predate this field
    return runtime if runtime in sup.pane_gate.ADAPTERS else None


def capture(socket, session, runner=subprocess.run) -> str | None:
    try:
        done = runner(["tmux", "-S", str(socket), "capture-pane", "-p", "-J", "-t", f"={session}:0"],
                      capture_output=True, text=True, timeout=8)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    text = done.stdout if done.returncode == 0 else None
    return text if text and text.strip() else None


def cause_lines(text: str) -> list[str]:
    """The cause's own words: cli_wedge's live banner lines, else the lines its
    abnormal patterns match. The last three, oldest first."""
    lines = [line for _family, _name, line in cw.live_banner_lines(text)]
    if not lines:
        lines = [ln.strip() for ln in text.splitlines()
                 if ln.strip() and any(rx.search(ln) for _, rx in cw.ABNORMAL_PATTERNS)]
    out: list[str] = []
    for ln in lines:
        if ln not in out:
            out.append(ln)
    return out[-3:]


def proxy_routed(socket, session) -> bool | None:
    """Is the seat's own process routed through the credential proxy? None: unobserved."""
    env = qa.seat_env_base_url(str(socket), session)
    return qa.points_at_credential_proxy(env.base_url) if env.observed else None


def _jump(session):
    from hitl.schema import Action
    return Action(id="open_terminal", kind="open_terminal", label=f"Open terminal ({session})")


def raise_card(workspace, worker_id, which, *, runner=subprocess.run, routed=proxy_routed,
               manager=None) -> dict:
    """One card for this wedge episode, from the pane as it reads NOW. Types nothing."""
    from hitl.schema import Action, HumanRequirement
    socket, session = sup._open_tmux(workspace, worker_id)
    if not socket:
        return {"worker_id": worker_id, "outcome": "no-recorded-session"}
    runtime = worker_runtime(workspace, worker_id)
    if runtime is None:
        return {"worker_id": worker_id, "outcome": "indeterminate",
                "probe": "worker runtime unavailable"}
    text = capture(socket, session, runner)
    if text is None:
        return {"worker_id": worker_id, "outcome": "indeterminate", "probe": "pane unread"}
    seat = seat_label(workspace, worker_id)
    frame = cw.raw_state_id(text)
    pane = sup.classify_pane_text(text, runtime, workspace=workspace,
                                  socket=socket, session=session)
    subject = {"source": SOURCE, "worker_id": worker_id, "session": session, "wedge": which,
               "frame": frame}
    device = {"id": session, "name": seat, "socket": str(socket)}
    if which == ps.CARD_FROZEN:
        if pane != ps.PANE_WORKING:
            return {"worker_id": worker_id, "outcome": "cleared", "pane": pane}
        req = HumanRequirement(
            kind="confirmation", runtime=runtime, device=device, subject=subject,
            title=f"{seat} · its turn looks frozen",
            message=(f"{seat} (tmux session {session}) owes work and its screen has not changed "
                     "across supervision ticks. Send Escape interrupts the turn; it is typed only "
                     "if you press it, and only if the screen is still the same frozen frame."),
            guard=f"{SOURCE}:{session}:frozen:{frame}",
            actions=[Action(id=ESCAPE_ACTION, kind="confirmation", label="Send Escape"),
                     _jump(session)])
    else:
        abn = cw.frame_abnormal(text)
        if abn is None:
            return {"worker_id": worker_id, "outcome": "cleared", "pane": pane}
        lines = cause_lines(text)
        via_proxy = runtime == "claude" and routed(socket, session) is True and (
            abn.retrying or any(n in _PROXY_CAUSES for n in abn.names))
        cause = ", ".join(abn.names)
        body = [f"{seat} (tmux session {session}) owes work and its pane shows:"]
        body += [f"  {ln}" for ln in lines] + ["", f"Cause: {cause}."]
        if via_proxy:
            body += ["", "This seat is routed through the credential proxy. Restarting the proxy "
                     f"(`{PROXY_REMEDY}`, as src/restart.sh does) can clear it."]
        body += ["The worker's session is not restarted: a fresh session meets the same cause."]
        subject.update(cause=list(abn.names), cause_lines=lines,
                       remedy=PROXY_REMEDY if via_proxy else None)
        actions = ([Action(id=PROXY_ACTION, kind="confirmation", label="Restart the credential proxy")]
                   if via_proxy else []) + [_jump(session)]
        req = HumanRequirement(
            kind="choice" if via_proxy else "core-blocked", runtime=runtime, device=device,
            subject=subject, title=f"{seat} · {abn.kind}: {cause}", message="\n".join(body),
            guard=f"{SOURCE}:{session}:cause:{cause}", actions=actions,
            turn_on_action=via_proxy)
    manager = manager or manager_for(workspace)
    made = manager.create(req)
    return {"worker_id": worker_id, "outcome": "carded", "hitl_id": made.id, "wedge": which}


def _note(manager, req_id, **fields):
    with manager.store.locked():
        cur = manager.get(req_id)
        if cur is not None:
            cur.subject = {**(cur.subject or {}), **fields}
            manager.store.save(cur)


def drive_escapes(workspace, *, runner=subprocess.run, manager=None) -> dict:
    """Type Escape for each pressed frozen card whose pane still shows the frame the
    card was raised for; refuse, typing nothing, for every other."""
    from hitl.schema import STATUS_IN_PROGRESS
    manager = manager or manager_for(workspace)
    out = {}
    for r in manager.active():
        subj = r.subject or {}
        if (subj.get("source") != SOURCE or subj.get("wedge") != ps.CARD_FROZEN
                or r.status != STATUS_IN_PROGRESS or r.chosen_action != ESCAPE_ACTION):
            continue
        socket, session = (r.device or {}).get("socket"), subj.get("session")
        text = capture(socket, session, runner) if socket and session else None
        runtime = worker_runtime(workspace, subj.get("worker_id"))
        still = (runtime is not None and runtime == r.runtime
                 and text is not None and cw.raw_state_id(text) == subj.get("frame")
                 and sup.classify_pane_text(text, runtime, workspace=workspace, socket=socket,
                                            session=session) == ps.PANE_WORKING)
        if not still:
            _note(manager, r.id, refused=REFUSED_NOTE)
            manager.expire(r.id)
            out[r.id] = "refused"
            continue
        try:
            sent = runner(["tmux", "-S", str(socket), "send-keys", "-t", f"={session}:0", "Escape"],
                          capture_output=True, text=True, timeout=8).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            sent = False
        _note(manager, r.id, escape_sent=sent)
        (manager.resolve if sent else manager.expire)(r.id)
        out[r.id] = "sent" if sent else "failed"
    return out


def resolve_cleared(workspace, worker_ids, *, manager=None) -> list:
    """Close the pending cards of seats that are no longer wedged; a pressed one is
    left to `drive_escapes`, which refuses it once the frame has moved."""
    from hitl.schema import STATUS_IN_PROGRESS
    manager = manager or manager_for(workspace)
    closed = []
    for r in manager.active():
        subj = r.subject or {}
        if (subj.get("source") == SOURCE and subj.get("worker_id") in worker_ids
                and r.status != STATUS_IN_PROGRESS):
            manager.resolve(r.id)
            closed.append(r.id)
    return closed
