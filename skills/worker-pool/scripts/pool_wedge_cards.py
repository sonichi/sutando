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
import tmux_pane_keys  # noqa: E402

SOURCE = "pool-wedge"
ESCAPE_ACTION = "send_escape"
PROXY_ACTION = "restart_credential_proxy"
PROXY_LABEL = "com.sutando.credential-proxy"
# What src/restart.sh runs for a launchd-supervised proxy; named, never run from here.
PROXY_REMEDY = f"launchctl kickstart -k gui/$(id -u)/{PROXY_LABEL}"
_PROXY_CAUSES = ("api-error", "network-error")
REFUSED_NOTE = "The turn is no longer frozen on the frame this card was raised for; nothing was typed."


def manager_for(workspace):
    from hitl.cards import manager_for as store_manager
    return store_manager(Path(workspace))


def seat_label(workspace, worker_id) -> str:
    row = sup.supervised_workers(workspace).get(worker_id) or {}
    label = sup.pr.display_label(row, worker_id)
    return f"worker {label}" if label == worker_id else f"worker {label} ({worker_id})"


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


def at_limit(text, pane, runtime, *, workspace=None, socket=None, session=None) -> bool:
    """The pane waits on a usage limit (banner, limit menu), not on some other gate."""
    if pane == ps.PANE_LIMIT:
        return True
    adapter = sup.pane_gate.ADAPTERS.get(runtime, sup.pane_gate.CLAUDE)
    verdict = sup.pane_gate.classify_pane(text, adapter, workspace, socket, session)
    return verdict.state == "busy" and sup.pane_gate.is_usage_limit(verdict.reason, text)


def limit_holds(workspace, req, worker_id, runner=None, owes_work=None) -> bool:
    """The seat monitor's episode-end rule, read off this worker's pane now. `owes_work`:
    the observed queue; the pool keeps no banner baseline, so each read is a first sight."""
    from hitl import tui_gate
    if not tui_gate.limit_card(req):
        return False  # skip the pane read; the rule itself is limit_card_holds
    socket, session = sup._open_tmux(workspace, worker_id)
    text = capture(socket, session, runner or subprocess.run) if socket else None
    outrun = False
    if text is not None:
        outrun, _ = sup.pane_gate.limit_step(text, None, workspace, socket, session, owes_work=owes_work)
    return tui_gate.limit_card_holds(req, text, worker_runtime(workspace, worker_id), outrun)


def _jump(session):
    from hitl.schema import Action
    return Action(id="open_terminal", kind="open_terminal", label=f"Open terminal ({session})")


def raise_card(workspace, worker_id, which, *, runner=subprocess.run, routed=proxy_routed,
               manager=None) -> dict:
    """One card for this wedge episode, from the pane as it reads NOW. Types nothing."""
    from hitl.cards import choice_request
    from hitl.schema import Action
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
    if which == ps.CARD_HUMAN:
        if pane not in (ps.PANE_GATE, ps.PANE_LIMIT):
            return {"worker_id": worker_id, "outcome": "cleared", "pane": pane}
    if which == ps.CARD_HUMAN and at_limit(text, pane, runtime, workspace=workspace,
                                           socket=socket, session=session):
        from hitl import tui_gate
        # The seat monitor's card for this limit shares the guard, so one card serves both.
        subject.update(episode=tui_gate.LIMIT_EPISODE, remind=False)
        req = choice_request(
            (f"{seat} (tmux session {session}) owes work and has hit its usage limit. "
             + tui_gate.limit_unblock(session, runtime)),
            [_jump(session)], tui_gate.limit_guard(session), subject, runtime=runtime,
            title=f"{seat} · usage limit", device=device, kind="core-blocked", turn_on_action=False)
    elif which == ps.CARD_HUMAN:
        req = choice_request(
            (f"{seat} (tmux session {session}) owes work and is waiting at a gate "
             "or usage limit. Open its terminal to review the decision."),
            [_jump(session)], f"{SOURCE}:{session}:human", subject, runtime=runtime,
            title=f"{seat} · needs your decision", device=device, kind="core-blocked",
            turn_on_action=False)
    elif which == ps.CARD_FROZEN:
        if pane != ps.PANE_WORKING:
            return {"worker_id": worker_id, "outcome": "cleared", "pane": pane}
        req = choice_request(
            (f"{seat} (tmux session {session}) owes work and its screen has not changed "
             "across supervision ticks. Send Escape interrupts the turn; it is typed only "
             "if you press it, and only if the screen is still the same frozen frame."),
            [Action(id=ESCAPE_ACTION, kind="confirmation", label="Send Escape"), _jump(session)],
            f"{SOURCE}:{session}:frozen:{frame}", subject, runtime=runtime,
            title=f"{seat} · its turn looks frozen", device=device, kind="confirmation",
            turn_on_action=False)
    else:
        abn = cw.frame_abnormal(text)
        if abn is None or (which == ps.CARD_LOGIN and pane != ps.PANE_LOGGED_OUT):
            return {"worker_id": worker_id, "outcome": "cleared", "pane": pane}
        lines = cause_lines(text)
        via_proxy = runtime == "claude" and routed(socket, session) is True and (
            abn.retrying or any(n in _PROXY_CAUSES for n in abn.names))
        cause = ", ".join(abn.names)
        if which == ps.CARD_LOGIN:
            body = [f"{seat} (tmux session {session}) is logged out and its pane shows:"]
        else:
            body = [f"{seat} (tmux session {session}) owes work and its pane shows:"]
        body += [f"  {ln}" for ln in lines] + ["", f"Cause: {cause}."]
        if via_proxy:
            body += ["", "This seat is routed through the credential proxy. Restarting the proxy "
                     f"(`{PROXY_REMEDY}`, as src/restart.sh does) can clear it."]
        if which == ps.CARD_LOGIN:
            body += [f"Run /login in that session. Until then every task routed to {seat} waits: "
                     "the worker's session is not restarted, a fresh session meets the same "
                     "expired login."]
        else:
            body += ["The worker's session is not restarted: a fresh session meets the same cause."]
        subject.update(cause=list(abn.names), cause_lines=lines,
                       remedy=PROXY_REMEDY if via_proxy else None)
        actions = ([Action(id=PROXY_ACTION, kind="confirmation", label="Restart the credential proxy")]
                   if via_proxy else []) + [_jump(session)]
        req = choice_request(
            "\n".join(body), actions, f"{SOURCE}:{session}:cause:{cause}", subject,
            runtime=runtime, title=f"{seat} · {abn.kind}: {cause}", device=device,
            kind="choice" if via_proxy else "core-blocked", turn_on_action=via_proxy)
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
            sent = runner(tmux_pane_keys.argv(socket, f"={session}:0", "Escape"),
                          capture_output=True, text=True, timeout=tmux_pane_keys.TIMEOUT_S).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            sent = False
        _note(manager, r.id, escape_sent=sent)
        (manager.resolve if sent else manager.expire)(r.id)
        out[r.id] = "sent" if sent else "failed"
    return out


def resolve_cleared(workspace, worker_ids, *, manager=None, wedges=None, observations=None, recovered=None,
                    runner=None) -> list:
    """Close the pending cards of seats that are no longer wedged, and of seats still
    wedged whose card is not the one their pane asks for now (`wedges`: worker ->
    that card). Pressed proxy cause cards require positive recovery evidence
    (`recovered`, defaulting to `worker_ids`); Escape stays with `drive_escapes`.
    A usage-limit card stays while `limit_holds`: an unwedged seat may still be limited."""
    from hitl.schema import STATUS_IN_PROGRESS
    manager = manager or manager_for(workspace)
    wedges = wedges or {}
    recovered = worker_ids if recovered is None else recovered
    closed = []
    for r in manager.active():
        subj = r.subject or {}
        w = subj.get("worker_id")
        if subj.get("wedge") == ps.CARD_HUMAN and observations is not None:
            o = observations.get(w, {})
            if (o.get("paused") or o.get("session_alive") is not True
                    or o.get("pane") in (None, ps.PANE_UNKNOWN)
                    or (o.get("pane") in (ps.PANE_GATE, ps.PANE_LIMIT)
                        and o.get("work_outstanding") is None)):
                continue
        # The HITL guard can deduplicate a login request onto its older cause card.
        legacy_login = (wedges.get(w) == ps.CARD_LOGIN and subj.get("wedge") == ps.CARD_CAUSE
                        and isinstance(subj.get("cause"), list) and "needs-login" in subj["cause"])
        stale = w in worker_ids or (w in wedges and subj.get("wedge") != wedges[w]
                                   and not legacy_login)
        cleared_proxy = (w in worker_ids and w in recovered
                         and subj.get("wedge") == ps.CARD_CAUSE
                         and r.chosen_action == PROXY_ACTION)
        if (subj.get("source") == SOURCE and stale
                and (r.status != STATUS_IN_PROGRESS or cleared_proxy)
                and not limit_holds(workspace, r, w, runner,
                                    ((observations or {}).get(w) or {}).get("work_outstanding"))):
            manager.resolve(r.id)
            closed.append(r.id)
    return closed
