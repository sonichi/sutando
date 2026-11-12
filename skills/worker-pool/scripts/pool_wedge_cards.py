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
import json
import os
import re
import subprocess
import time
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
from delivery import codex_banners  # noqa: E402
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


# Claude Code's footer row for a background agent still running ("◯ general-purpose  … 37m 31s · ↓ 159.5k tokens").
BACKGROUND_AGENT_ROW = re.compile(r"^\s*◯ \S.*\d+[ms](?: \d+s)? · [↓↑]", re.MULTILINE)


def live_owed_tasks(workspace, worker_id) -> list[str]:
    """Owed task ids (no ready result) whose task file is still live in tasks/: the ones a
    delivery could still type in. A sentinel left over for an archived or vanished task is not."""
    from delivery import task_dispatch as td
    from task_archive import find_task_file
    owned = td.owned_task_ids(sup.pd.deliveries_dir(workspace, worker_id).parent, worker_id)
    ready = td.ready_result_filenames(sup.pd.results_dir(workspace), [f"{t}.txt" for t in owned])
    tasks = Path(workspace) / "tasks"
    return [t for t in owned if f"{t}.txt" not in ready and find_task_file(tasks, t) is not None]


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
    elif which == ps.CARD_UNDELIVERED:
        if pane != ps.PANE_IDLE:
            return {"worker_id": worker_id, "outcome": "cleared", "pane": pane}
        if BACKGROUND_AGENT_ROW.search(text):
            # The turn ended but a background agent it started is still running.
            return {"worker_id": worker_id, "outcome": "cleared", "pane": "background-agent"}
        if not live_owed_tasks(workspace, worker_id):
            # Owed only by sentinels whose task is archived or gone: nothing to deliver.
            return {"worker_id": worker_id, "outcome": "cleared", "pane": "no-live-task"}
        cause = sup.delivery_block(socket)
        body = [f"{seat} (tmux session {session}) owes work, its watcher is running, and its pane "
                f"has read idle at the prompt for over {int(ps.UNDELIVERED_AFTER_S // 60)} minutes: "
                "its task appears not to have been delivered."]
        if cause == "pane-key-fence":
            body += ["", f"Cause: automated sends on {socket} are blocked by the pane-keys fence "
                     "(docs/codex-core.md). Once no send can still be in flight, clear it with:",
                     f"  python3 src/tmux_pane_keys.py recover {socket}",
                     "The retained tasks are typed in on the notifier's next wake."]
        else:
            body += ["", f"Cause: not identified. The {session}-watcher session logs each delivery "
                     "attempt and why it was refused."]
        subject.update(cause=[cause] if cause else [])
        req = choice_request(
            "\n".join(body), [_jump(session)], f"{SOURCE}:{session}:undelivered:{cause or 'unknown'}",
            subject, runtime=runtime, title=f"{seat} · tasks not delivered", device=device,
            kind="core-blocked", turn_on_action=False)
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
    elif which == ps.CARD_LOGIN and sup.codex_banner(text, runtime) is not None:
        # Codex's own cell ("■ Your access token could not be refreshed … sign in again").
        banner = sup.codex_banner(text, runtime)
        if pane != ps.PANE_LOGGED_OUT or banner.cause != codex_banners.SIGNED_OUT:
            return {"worker_id": worker_id, "outcome": "cleared", "pane": pane}
        body = [f"{seat} (tmux session {session}) is signed out of Codex and its pane shows:",
                f"  {banner.line}", "", "Cause: signed-out.",
                f"On the Mac, run codex login, then restart {seat}. Until then every task routed "
                f"to {seat} waits: a fresh session meets the same expired login."]
        subject.update(cause=[codex_banners.SIGNED_OUT], cause_lines=[banner.line], remedy=None)
        req = choice_request(
            "\n".join(body), [_jump(session)], f"{SOURCE}:{session}:cause:{codex_banners.SIGNED_OUT}",
            subject, runtime=runtime, title=f"{seat} · signed out of Codex", device=device,
            kind="core-blocked", turn_on_action=False)
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


POOL_DOWN = "pool-down"
# What the owner does, per (runtime, cause); the only remedy is the owner's, at the Mac (no button).
_POOL_REMEDY = {
    ("claude", codex_banners.USAGE_LIMIT): "Wait for the reset, or on the Mac open Sutando's terminal and run "
                                           "/login with another account: the seats restart on it by themselves.",
    ("claude", codex_banners.SIGNED_OUT): "On the Mac, open Sutando's terminal and run /login.",
    ("codex", codex_banners.USAGE_LIMIT): "Wait for the reset, or on the Mac run codex login with another account, "
                                          "then restart the seats.",
    ("codex", codex_banners.SIGNED_OUT): "On the Mac, run codex login, then restart the seats.",
}


def _seat_cause(o: dict) -> str | None:
    if o.get("pane") == ps.PANE_LIMIT or o.get("limit_parked"):
        return codex_banners.USAGE_LIMIT
    if o.get("pane") == ps.PANE_LOGGED_OUT:
        return codex_banners.SIGNED_OUT
    return None


# `codex login status`'s own lines (codex-cli 0.160.1), each to fixed wording. None of the tool's
# text is passed on: "Logged in using an API key - " is followed by the masked key itself.
_CODEX_LOGINS = (
    ("Logged in using ChatGPT", "ChatGPT account"),
    ("Logged in using an API key", "API key"),
    ("Logged in using Amazon Bedrock API key", "Amazon Bedrock"),
    ("Logged in using Amazon Bedrock AWS access keys", "Amazon Bedrock"),
    ("Logged in using personal access token", "access token"),
    ("Logged in using access token", "access token"),
    ("Logged in using workload identity", "workload identity"),
)


def codex_account_label(runner=subprocess.run) -> str | None:
    """How Codex is signed in, in fixed wording only ("ChatGPT account", "API key", …, else
    "unknown"), from `codex login status`; never auth.json. None when it cannot be read."""
    try:
        done = runner(["codex", "login", "status"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (done.stdout or "") + (done.stderr or "")
    if "Logged in using" not in text:
        return None
    return next((label for line, label in _CODEX_LOGINS
                 if any(ln.strip().startswith(line) for ln in text.splitlines())), "unknown")


def _account_label(runtime: str, runner) -> str | None:
    try:
        if runtime == "codex":
            return codex_account_label(runner)
        import core_limit_hold
        return core_limit_hold.current_account_label()
    except Exception:  # noqa: BLE001 - a card without a label still says what is down
        return None


def pool_down(workspace, observations: dict, *, host: str | None = None, runner=subprocess.run) -> list:
    """One entry per runtime whose every live, unpaused seat is stopped by the same cause (a usage
    limit, or signed out): the pool for that runtime cannot work. The card data 2b's panel reads:
    id pool-down:<host>:<runtime>-<cause>, the seats by label, the account by its label (never the
    email), the earliest known reset, the banner and the owner's remedy. A pool partly down, or of
    one live seat, is left to the per-seat cards."""
    if host is None:
        from util_paths import host_label
        host = host_label()
    by_runtime: dict = {}
    for w, o in observations.items():
        runtime = worker_runtime(workspace, w)
        if runtime is None or o.get("paused") or o.get("session_alive") is not True:
            continue
        by_runtime.setdefault(runtime, {})[w] = o
    out = []
    for runtime, seats in sorted(by_runtime.items()):
        if len(seats) < 2:
            continue  # a pool of one is its own seat card already
        causes = {_seat_cause(o) for o in seats.values()}
        if len(causes) != 1 or None in causes:
            continue
        cause = causes.pop()
        resets = [o["limit_reset_at"] for o in seats.values() if o.get("limit_reset_at")]
        banners = [o["limit_banner"] for o in seats.values() if o.get("limit_banner")]
        out.append({"id": f"{POOL_DOWN}:{host}:{runtime}-{cause}", "host": host, "runtime": runtime,
                    "cause": cause, "worker_ids": sorted(seats),
                    "workers": sorted(sup.pr.display_label(sup.supervised_workers(workspace).get(w) or {}, w)
                                      for w in seats),
                    "account_label": _account_label(runtime, runner),
                    "reset_at": min(resets) if resets and cause == codex_banners.USAGE_LIMIT else None,
                    "banner": banners[0] if banners else None,
                    "remedy": _POOL_REMEDY[(runtime, cause)]})
    return out


POOL_DOWN_HOLD = 2  # sweeps a pool must read down (or working again) before its card changes


def _pool_down_path(workspace) -> Path:
    return Path(workspace) / "state" / "pool-down.json"


def _pool_body(g: dict) -> str:
    n = len(g["workers"])
    what = "hit its usage limit" if g["cause"] == codex_banners.USAGE_LIMIT else "is signed out"
    when = (f" It resets at {time.strftime('%-I:%M %p on %b %-d', time.localtime(g['reset_at']))}."
            if g.get("reset_at") else "")
    acct = f" ({g['account_label']})" if g.get("account_label") else ""
    return (f"All {n} {g['runtime'].capitalize()} worker{'s' if n != 1 else ''} on {g['host']} are stopped: "
            f"the account{acct} {what}.{when}\nWorkers: {', '.join(g['workers'])}.\n{g['remedy']}")


def raise_pool_down(workspace, groups: list, *, manager=None) -> dict:
    """One card per down pool, keyed on its host, runtime and cause, through the same hitl store as
    the seat cards. A pool is carded only once it has read down for POOL_DOWN_HOLD sweeps, and its
    card resolved only once it has read working that long, so one seat flapping moves nothing; a
    change in its seats or reset updates the card in place. Types nothing."""
    from hitl.cards import choice_request
    manager = manager or manager_for(workspace)
    path = _pool_down_path(workspace)
    try:
        seen = json.loads(path.read_text(encoding="utf-8"))
        seen = seen if isinstance(seen, dict) else {}
    except (OSError, ValueError):
        seen = {}
    now_down = {g["id"]: g for g in groups}
    for gid in set(seen) | set(now_down):
        count = seen.get(gid, {"down": 0, "up": 0})
        seen[gid] = ({"down": count.get("down", 0) + 1, "up": 0} if gid in now_down
                     else {"down": 0, "up": count.get("up", 0) + 1})
    active = {(r.subject or {}).get("pool_down", {}).get("id"): r for r in manager.active()
              if (r.subject or {}).get("source") == SOURCE and (r.subject or {}).get("wedge") == POOL_DOWN}
    raised, updated, closed = [], [], []
    for gid, g in now_down.items():
        card = active.get(gid)
        subject = {"source": SOURCE, "wedge": POOL_DOWN, "pool_down": g}
        if card is not None:
            before = (card.subject or {}).get("pool_down") or {}
            if before != g:
                with manager.store.locked():
                    cur = manager.get(card.id)
                    if cur is not None and not cur.terminal:
                        cur.message, cur.subject = _pool_body(g), subject
                        if before.get("worker_ids") != g["worker_ids"]:
                            cur.revision += 1  # re-projected only when its seats change, never each sweep
                            updated.append(cur.id)
                        cur.updated_at = time.time()
                        manager.store.save(cur)
        elif seen[gid]["down"] >= POOL_DOWN_HOLD:
            # The remedy is the owner's, at the Mac: the one action opens the core's terminal for it.
            raised.append(manager.create(choice_request(
                _pool_body(g), [_jump(cw.DEFAULT_SESSION)], gid, subject, runtime=g["runtime"],
                title=f"{g['runtime'].capitalize()} workers stopped · {g['cause']}",
                device={"id": cw.DEFAULT_SESSION, "name": "Sutando"}, kind="core-blocked",
                turn_on_action=False)).id)
    for gid, card in active.items():
        if gid not in now_down and seen.get(gid, {}).get("up", 0) >= POOL_DOWN_HOLD:
            manager.resolve(card.id)
            closed.append(card.id)
    # Remembered while the pool is down or its card is still up; forgotten once it is closed.
    keep = {gid: c for gid, c in seen.items()
            if gid in now_down or (gid in active and active[gid].id not in closed)}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(keep), encoding="utf-8")
    os.replace(tmp, path)
    return {"raised": raised, "updated": updated, "closed": closed}


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
