#!/usr/bin/env python3
"""recovery_actions.py — the host executor for recovery buttons on blocked cards (C2 -> C3).

The owner taps a recovery button on the card of a blocked core or worker seat; the bridge's
reply handler checks the owner, the room and the card's revision and guard, then drops the click
in `state/recovery/actions/<hitl_id>.json` (hitl.recovery). The monitor watching that card's
session (core-input-watch.py, a process outside the session it watches) calls `run_pending`
each tick. It runs each click at most once, within ACTION_TTL_S, from a fixed allowlist; argv
is fixed here and the only argument a click supplies is an id the card itself offered:

    retry_now           what the owner's bare "retry now" does (core_limit_hold): lift the hold, and
                        restart a live core (or a seat, by its pool's --retry-cmd) held at its
                        limit screen; else a refused turn gets `continue` once idle
    switch_model:<m>    scripts/switch-model.sh <m> --confirm on that session (alias from the list)
    switch_runtime:<r>  core only: src/agent/start-cli.sh --runtime <r> --restart
    restart_core        core only: src/agent/start-cli.sh --restart, from outside the session
    escape_prompt       one Escape key into the pane
    buy_credits, add_account, switch_account:<id>
                        never run here: each needs a confirmed, re-authenticated step (P1)

then writes a C3 receipt to `state/recovery/receipts/<hitl_id>.json`, appends an audit line to
`state/recovery/audit.jsonl`, and edits the same card with the outcome (the projector sends the
edit to the owner's DM). A click is claimed by renaming its file before it runs, so a crash
mid-run is reported as interrupted, never re-run. Nothing a subprocess prints is copied out.

It also raises the paused card: while `core_limit_hold` holds the core down for a provider
limit, the owner's DM carries one card per hold with those buttons.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hitl import recovery  # noqa: E402
from workspace_default import status_read_path  # noqa: E402

REPO = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root
HOLD_SOURCE = "core-hold"
SWITCH_TIMEOUT_S = 90
RESTART_TIMEOUT_S = 180
#: A tap older than this when its monitor reaches it is stale, never run.
ACTION_TTL_S = 600.0
#: A recovery card accepted this long ago with no action file, claim or receipt lost its click.
ORPHAN_CLICK_S = 120.0
CLAIM_SUFFIX = ".claimed"
AUDIT_FILE = "audit.jsonl"
AUDIT_KEEP = 500
#: Change billing or the signed-in identity: refused until the confirmed, re-authenticated flow.
STEP_UP = frozenset({recovery.BUY_CREDITS, recovery.ADD_ACCOUNT, recovery.SWITCH_ACCOUNT})
CONTINUE = ("continue", "Enter")
_NAMED_CAP = re.compile(r"\breached your ([A-Za-z][\w.-]{0,24}) limit\b", re.I)


def model_family(model: Optional[str]) -> Optional[str]:
    low = str(model or "").lower()
    return next((a for a in recovery.SWITCH_ALIASES if a in low), None)


def current_model(workspace: Path) -> Optional[str]:
    """The family the core was last switched to (state/model-switch.json), else None."""
    try:
        data = json.loads(status_read_path("model-switch.json", Path(workspace)).read_text())
    except (OSError, ValueError):
        return None
    return model_family(data.get("model")) if isinstance(data, dict) else None


_MODEL_TABLE: Optional[Dict[str, Dict[str, str]]] = None


def model_table() -> Dict[str, Dict[str, str]]:
    """{alias: {id, display}} from the model-switch table (`switch-model.sh --names`), read once per
    process; {} when it cannot be read, and then no switch is offered at all."""
    global _MODEL_TABLE
    if _MODEL_TABLE is None:
        try:
            run = subprocess.run(["bash", str(REPO / "scripts" / "switch-model.sh"), "--names"],
                                 capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
            table = json.loads(run.stdout) if run.returncode == 0 else {}
        except (OSError, ValueError, subprocess.SubprocessError):
            table = {}
        _MODEL_TABLE = {a: e for a, e in table.items() if isinstance(e, dict) and e.get("id") and e.get("display")} \
            if isinstance(table, dict) else {}
    return _MODEL_TABLE


def model_names() -> Dict[str, str]:
    """alias -> the exact model's display name, the text a switch button carries."""
    return {a: e["display"] for a, e in model_table().items()}


def switch_offer(workspace: Path, runtime: Optional[str], avoid: Optional[str] = None,
                 seat: bool = False) -> List[str]:
    """The model aliases a card may offer: none for Codex (its model is a launch argument) and
    none for a worker seat (it shares the core's config dir, where /model saves the default for
    every session), else every alias the model table names an exact model for, but the one in
    use and `avoid`."""
    if runtime == "codex" or seat:
        return []
    skip = {current_model(workspace), model_family(avoid)} - {None}
    known = model_table()
    return [m for m in recovery.SWITCH_ALIASES if m not in skip and m in known]


def runtime_offer(runtime: Optional[str]) -> List[str]:
    """The other core runtime, when this host can launch it: Claude Code is the default and
    always offered from Codex; Codex only where its CLI resolves."""
    if (runtime or "claude") == "codex":
        return ["claude"]
    return ["codex"] if shutil.which("codex") else []


def card_controls(workspace: Path, runtime: Optional[str], *, seat: bool, kind: Optional[str],
                  prompt: Optional[str], limit: bool) -> List[str]:
    """The recovery ids a blocked card offers. A seat's never act on the core; a limit menu's
    own wait choices replace the switches (a /model cannot be typed under an open menu)."""
    from hitl import tui_gate
    options, caret = tui_gate.parse_options(prompt)
    menu = limit and caret is not None and bool(tui_gate.safe_limit_options(options))
    ids: List[str] = []
    if limit and not menu:
        cap = _NAMED_CAP.search(prompt or "")
        ids += [recovery.action_id(recovery.SWITCH_MODEL, m)
                for m in switch_offer(workspace, runtime, cap.group(1) if cap else None, seat=seat)]
        if not seat:
            ids += [recovery.action_id(recovery.SWITCH_RUNTIME, r) for r in runtime_offer(runtime)]
    if kind == "turn-rejected" or (limit and not seat):
        ids.append(recovery.RETRY_NOW)
    if limit:
        ids.append(recovery.ADD_ACCOUNT)  # no purchase is ever offered on a card
    if not seat:
        ids.append(recovery.RESTART_CORE)
    return ids


def incident_for(req) -> str:
    subject = req.subject or {}
    if subject.get("incident"):
        return str(subject["incident"])
    return "inc_" + hashlib.sha256(str(req.guard).encode()).hexdigest()[:8]


def _awaiting(workspace: Path, req) -> bool:
    """A recovery click accepted on `req` that has no receipt yet: only the executor closes it."""
    from hitl.schema import STATUS_IN_PROGRESS
    keyed = (req.subject or {}).get("option_for_action") or {}
    return (req.status == STATUS_IN_PROGRESS and recovery.is_recovery(req.chosen_action)
            and req.chosen_action not in keyed and recovery.read_receipt(workspace, req.id) is None)


def awaiting_executor(workspace: Path, req) -> bool:
    try:
        return _awaiting(workspace, req)
    except Exception:  # noqa: BLE001 — unreadable means unknown: keep the card open
        return True


# ---- the executor -----------------------------------------------------------

Runner = Callable[..., subprocess.CompletedProcess]


class Target:
    """The session a monitor watches: the core (seat None) or one worker seat."""

    def __init__(self, workspace: Path, socket: str, session: str, seat: Optional[str] = None,
                 send_key: Optional[Callable[[object], bool]] = None, brain: Optional[str] = None,
                 retry_cmd: Optional[List[str]] = None):
        self.workspace, self.socket, self.session, self.seat = Path(workspace), socket, session, seat
        self.send_key = send_key or (lambda k: False)
        self.brain = brain
        self.retry_cmd = list(retry_cmd or [])

    def hold_key(self) -> Optional[str]:
        """The seat key core_limit_hold files a seat's hold under (its tmux session)."""
        return self.session if self.seat else None

    def state_dir(self) -> Path:
        """A seat's model record lives apart from the core's, which the core's card reads."""
        base = self.workspace / "state"
        return base / "seats" / self.session if self.seat else base


#: Who lifted a hold for a card's retry, so only that retry's failure puts it back.
RETRY_BY = "recovery-card"


def _execute(verb: str, arg: Optional[str], req, target: Target, runner: Runner, now: float,
             follow_ups: List[tuple]):
    """(outcome, reason for the receipt, line for the card, recovery fields)."""
    import core_limit_hold
    ws = target.workspace
    refused_turn = ((req.subject or {}).get("gate") == "turn-rejected") if req is not None else False
    if verb in STEP_UP:
        why = ("This changes billing or the signed-in account, so it needs a confirmed sign-in step "
               "that is not available from here yet; nothing was changed.")
        return "refused", why, why, {}
    env = dict(os.environ, SUTANDO_TMUX_SOCKET=target.socket, SUTANDO_TMUX_SESSION=target.session)
    env.pop("SUTANDO_CORE_RUNTIME", None)  # the configured runtime, or --runtime, decides
    env.pop("SUTANDO_CORE_SESSION", None)  # the monitor runs outside the core session
    if verb == recovery.RETRY_NOW:
        hold = core_limit_hold.active_hold(ws, now, target.hold_key())
        if hold is not None and target.seat:
            if not target.retry_cmd:
                why = "This worker's restart is not available here; it continues when the limit resets."
                return "refused", why, why, {}
            core_limit_hold.clear(ws, now, by=RETRY_BY, mirror=False, seat=target.hold_key())
            if _rc(runner, target.retry_cmd, env, RESTART_TIMEOUT_S) != 0:
                core_limit_hold.reinstate(ws, now, by=RETRY_BY, mirror=False, seat=target.hold_key())
                why = 'The worker did not restart, so it is still paused; reply "retry now" here to try again.'
                return "failed", why, why, {"state": "paused_limit"}
            return "done", None, f"Retrying now: I restarted {target.seat}.", {"state": "starting"}
        if hold is not None:
            live = hold.get("source") == core_limit_hold.PANE_SOURCE
            core_limit_hold.clear(ws, now, by=RETRY_BY, mirror=not live)
            if live:
                # A live core at its limit screen retries by a restart; the hold returns if it fails.
                outcome = _execute(recovery.RESTART_CORE, None, req, target, runner, now, follow_ups)
                if outcome[0] != "done":
                    core_limit_hold.reinstate(ws, now, by=RETRY_BY, mirror=False)
                    why = 'The core did not restart, so it is still paused; reply "retry now" here to try again.'
                    return outcome[0], why, why, {"state": "paused_limit"}
                return outcome
            else:
                return ("done", None, "Retrying now: the core is starting. If the limit is still in force it "
                        "pauses again and a new card says so.", {"state": "starting"})
        elif refused_turn:
            follow_ups.append(CONTINUE)
            return "done", None, "Retrying: the refused message is sent again once its terminal is idle.", {}
        else:
            why = "Nothing is paused or refused right now, so there is nothing to retry."
            return "refused", why, why, {}
    if verb == recovery.SWITCH_MODEL:
        if target.seat:
            why = f"{recovery.SEAT_MODEL_LOCKED} Nothing was changed."
            return "refused", why, why, {}
        entry = model_table().get(arg)
        if entry is None:
            why = f"This Mac has no exact model for {arg}; nothing was changed."
            return "refused", why, why, {}
        arg_id, name = entry["id"], entry["display"]
        hold = core_limit_hold.active_hold(ws, now)
        held = hold is not None and hold.get("source") != core_limit_hold.PANE_SOURCE
        argv = ["bash", str(REPO / "scripts" / "switch-model.sh"), arg_id, "--confirm",
                "--socket", target.socket, "--session", target.session]
        try:
            run = runner(argv, capture_output=True, text=True, timeout=SWITCH_TIMEOUT_S, env=env,
                         stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            why = f"The switch to {name} did not finish; it stays on its current model."
            return "failed", why, why, {}
        if run.returncode != 0:
            why = f"It did not take {name}; it stays on its current model."
            return "failed", why, why, {}
        if refused_turn:
            follow_ups.append(CONTINUE)
        return ("done", None, f"Switched the core to {name}; checking that it is working again.",
                {"state": "starting" if held else "running", "model": arg_id})
    if verb in (recovery.RESTART_CORE, recovery.SWITCH_RUNTIME):
        if target.seat:
            why = "That acts on the core, not on this worker; nothing was done."
            return "refused", why, why, {}
        argv = ["bash", str(REPO / "src" / "agent" / "start-cli.sh"), "--restart"]
        if verb == recovery.SWITCH_RUNTIME:
            argv[2:2] = ["--runtime", arg]
        try:
            run = runner(argv, capture_output=True, text=True, timeout=RESTART_TIMEOUT_S, env=env,
                         stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            why = "The restart did not finish in time; the core may still be coming up."
            return "failed", why, why, {}
        if run.returncode == core_limit_hold.HOLD_EXIT:
            why = "The core is paused for a provider limit, so it was not restarted; switch its model instead."
            return "refused", why, why, {"state": "paused_limit"}
        if run.returncode != 0:
            why = (f"The core did not start under {arg}; it stays as it was (exit {run.returncode})."
                   if verb == recovery.SWITCH_RUNTIME else
                   f"The restart did not complete (exit {run.returncode}).")
            return "failed", why, why, {}
        if verb == recovery.SWITCH_RUNTIME:
            return "done", None, f"Switched the core to {arg}; it is starting.", {"state": "starting", "runtime": arg}
        return "done", None, "Restarted the core; it is starting.", {"state": "starting"}
    if verb == recovery.ESCAPE_PROMPT:
        if not target.send_key("Escape"):
            why = "The terminal did not take the key."
            return "failed", why, why, {}
        return "done", None, "Pressed Escape on the prompt.", {}
    why = "That button is not one this Mac runs."
    return "refused", why, why, {}


def _rc(runner: Runner, argv: List[str], env: Dict, timeout: float) -> Optional[int]:
    try:
        return runner(argv, capture_output=True, text=True, timeout=timeout, env=env,
                      stdin=subprocess.DEVNULL).returncode
    except (OSError, subprocess.SubprocessError):
        return None


def _check(manager, data: Dict, target: Target, now: float):
    """(requirement, None) when the click may run, else (requirement or None, (outcome, why))."""
    from hitl.schema import STATUS_IN_PROGRESS
    aid = data.get("action_id")
    if not recovery.is_recovery(aid):
        return None, ("refused", "That button is not one this Mac runs.")
    req = manager.get(str(data.get("hitl_id") or ""))
    if req is None:
        return None, ("stale", "This card is no longer current.")
    subject = req.subject or {}
    if (not subject.get("recovery") or subject.get("session") != target.session
            or bool(subject.get("seat")) != bool(target.seat)):
        return req, ("refused", "That button is not one this card offers.")
    if (recovery.parse(aid)[0] in recovery.CORE_ONLY and target.seat) or \
            aid not in {a.id for a in req.actions if a.kind == recovery.kind_of(a.id) and recovery.is_recovery(a.id)}:
        return req, ("refused", "That button is not one this card offers.")
    import core_recovery_state
    if recovery.parse(aid)[0] not in STEP_UP and core_recovery_state.parse_action_id(aid, req.runtime) is None:
        return req, ("refused", "That button is not one this core's runtime carries out.")
    if (req.status != STATUS_IN_PROGRESS or req.chosen_action != aid
            or str(data.get("guard") or "") != req.guard):
        return req, ("stale", "This card changed before the tap reached the Mac; nothing was done.")
    at = data.get("at")
    if not isinstance(at, (int, float)) or now - at > ACTION_TTL_S:
        return req, ("stale", "The tap reached the Mac too late to act on; nothing was done.")
    return req, None


def _answer_card(manager, req, line: str, outcome: str, now: float) -> None:
    """Show the outcome on the same card: its message gains the line. The card stays this
    incident's one card: it closes only once the session is seen unblocked (resolve_escalations),
    so a later tick that still sees the block finds it and posts no second card."""
    with manager.store.locked():
        cur = manager.get(req.id)
        if cur is None or cur.terminal:
            return
        cur.message = f"{cur.message}\n\n{line}"
        cur.subject = {**(cur.subject or {}), "receipt": {"outcome": outcome, "at": int(now)},
                       "verify": {"at": now, "outcome": outcome}}
        cur.revision += 1
        cur.updated_at = time.time()
        manager.store.save(cur)


#: How long a session may stay blocked after a done action before its card says so.
VERIFY_S = 60.0
STILL_BLOCKED = ('It is still blocked. You can wait for the limit to reset, or reply "retry now" here '
                 "to try again.")
WORKING_AGAIN = "It is working again."


def verify_step(manager, workspace: Path, session: str, blocked: bool, now: Optional[float] = None) -> List[str]:
    """For each of this session's cards an action answered: once the session is seen unblocked,
    the card says so (and resolve_escalations closes it); still blocked VERIFY_S after a done
    action, the same card says that, once. Never a new card. Returns the edited card ids."""
    at = time.time() if now is None else now
    edited = []
    for r in manager.active():
        subject = r.subject or {}
        check = subject.get("verify")
        if subject.get("session") != session or not subject.get("recovery") or not isinstance(check, dict) \
                or check.get("seen"):
            continue
        if blocked and not (check.get("outcome") == "done" and at - float(check.get("at") or at) >= VERIFY_S):
            continue
        line = WORKING_AGAIN if not blocked else STILL_BLOCKED
        with manager.store.locked():
            cur = manager.get(r.id)
            if cur is None or cur.terminal:
                continue
            cur.message = f"{cur.message}\n\n{line}"
            cur.subject = {**(cur.subject or {}), "verify": {**check, "seen": "working" if not blocked else "blocked"}}
            cur.revision += 1
            cur.updated_at = time.time()
            manager.store.save(cur)
        edited.append(r.id)
    return edited


def audit(workspace: Path, entry: Dict) -> None:
    """One line per action or refusal, bounded; ids and outcomes only, never message text."""
    path = recovery.recovery_dir(workspace) / AUDIT_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
        lines = path.read_text().splitlines()
        if len(lines) > 2 * AUDIT_KEEP:
            tmp = path.with_suffix(".tmp")
            tmp.write_text("\n".join(lines[-AUDIT_KEEP:]) + "\n")
            os.replace(tmp, path)
    except OSError:
        pass  # the receipt and the card are the record of the outcome


def _settle(manager, target: Target, data: Dict, req, outcome: str, reason, line: str,
            fields: Dict, now: float) -> Dict:
    hitl_id, aid = str(data.get("hitl_id") or ""), str(data.get("action_id") or "")
    incident = incident_for(req) if req is not None else ""
    body = recovery.receipt(hitl_id, incident, aid, outcome, reason, now, fields)
    recovery.write_receipt(target.workspace, body)
    audit(target.workspace, {"at": int(now), "hitl_id": hitl_id, "action_id": aid, "outcome": outcome,
                             "sender": str(data.get("sender") or ""), "session": target.session,
                             "seat": target.seat or ""})
    if req is not None and outcome != "stale":
        _answer_card(manager, req, line, outcome, now)
    return body


def has_pending(workspace: Path) -> bool:
    d = recovery.actions_dir(workspace)
    return d.is_dir() and any(d.iterdir())


def _mine(manager, data: Dict, target: Target) -> bool:
    """Whether this monitor owns the click: its card names this session. A click naming no card
    the store holds is the core monitor's to refuse."""
    req = manager.get(str(data.get("hitl_id") or "")) if isinstance(data, dict) else None
    if req is None:
        return not target.seat
    return (req.subject or {}).get("session") == target.session


def run_pending(manager, target: Target, *, runner: Optional[Runner] = None,
                now: Optional[float] = None, follow_ups: Optional[List[tuple]] = None) -> List[Dict]:
    """Run every accepted recovery click for `target` once; returns the receipts written.
    Keys owed once the pane is idle (a refused turn's `continue`) are appended to `follow_ups`."""
    if manager is None:
        return []
    run = runner or subprocess.run
    owed = follow_ups if follow_ups is not None else []
    clock = (lambda: now) if now is not None else time.time
    done: List[Dict] = []
    d = recovery.actions_dir(target.workspace)
    for claimed in sorted(d.glob(f"*{CLAIM_SUFFIX}")) if d.is_dir() else []:
        done += _interrupted(manager, target, claimed, clock())
    for path in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = None
        if not _mine(manager, data, target):
            continue
        claimed = path.with_name(path.stem + CLAIM_SUFFIX)
        try:
            os.replace(path, claimed)
        except OSError:
            continue  # another monitor claimed it first
        if not isinstance(data, dict):
            _unlink(claimed)
            continue
        if recovery.read_receipt(target.workspace, str(data.get("hitl_id") or "")) is not None:
            _unlink(claimed)
            continue
        req, refusal = _check(manager, data, target, clock())
        if refusal is not None:
            outcome, why = refusal
            done.append(_settle(manager, target, data, req, outcome, why, why, {}, clock()))
        else:
            verb, arg = recovery.parse(data["action_id"])
            outcome, reason, line, fields = _execute(verb, arg, req, target, run, clock(), owed)
            done.append(_settle(manager, target, data, req, outcome, reason, line, fields, clock()))
        _unlink(claimed)
    done += _orphans(manager, target, clock())
    return done


def _interrupted(manager, target: Target, claimed: Path, now: float) -> List[Dict]:
    """A claim left by a crash: the action may have run, so it is reported, never re-run."""
    try:
        data = json.loads(claimed.read_text())
    except (OSError, ValueError):
        data = None
    if not _mine(manager, data, target):
        return []
    out = []
    if isinstance(data, dict):
        hitl_id = str(data.get("hitl_id") or "")
        if hitl_id and recovery.read_receipt(target.workspace, hitl_id) is None:
            why = "I was interrupted while doing this; check its state before tapping again."
            out.append(_settle(manager, target, data, manager.get(hitl_id), "failed", why, why, {}, now))
    _unlink(claimed)
    return out


def _orphans(manager, target: Target, now: float) -> List[Dict]:
    """Accepted recovery clicks whose hand-off file never landed: closed as failed, so the next
    tick raises a fresh card instead of leaving this one waiting forever."""
    out = []
    d = recovery.actions_dir(target.workspace)
    for req in manager.active():
        if (req.subject or {}).get("session") != target.session or not _awaiting(target.workspace, req):
            continue
        if now - float(req.updated_at or now) < ORPHAN_CLICK_S:
            continue
        if (d / f"{req.id}.json").exists() or (d / f"{req.id}{CLAIM_SUFFIX}").exists():
            continue
        why = "The tap did not reach this Mac; use the card that replaces this one."
        data = {"hitl_id": req.id, "action_id": str(req.chosen_action)}
        out.append(_settle(manager, target, data, req, "failed", why, why, {}, now))
    return out


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# ---- the paused card --------------------------------------------------------

def hold_card_step(manager, workspace: Path, session: str, runtime: Optional[str],
                   queued: int = 0, now: Optional[float] = None):
    """One card per provider-limit hold while it is in force; closed once the hold lifts.
    Returns the hold's requirement, or None when no hold is in force."""
    if manager is None:
        return None
    import core_limit_hold
    from hitl.schema import HumanRequirement
    from hitl.tui_gate import limit_unblock, recovery_action
    workspace = Path(workspace)
    hold = core_limit_hold.active_hold(workspace, now)
    if hold and hold.get("source") == core_limit_hold.PANE_SOURCE:
        hold = None  # a live core at its limit screen already has that screen's card
    guard = f"limit-hold:{session}:{_hold_ref(hold)}" if hold else ""
    for r in manager.active():
        subject = r.subject or {}
        if subject.get("source") == HOLD_SOURCE and subject.get("session") == session \
                and r.guard != guard and not awaiting_executor(workspace, r):
            manager.resolve(r.id)
    if hold is None:
        return None
    ids = [recovery.RETRY_NOW]
    ids += [recovery.action_id(recovery.SWITCH_MODEL, m)
            for m in switch_offer(workspace, runtime, avoid=hold.get("model"))]
    ids += [recovery.action_id(recovery.SWITCH_RUNTIME, r) for r in runtime_offer(runtime)]
    ids.append(recovery.ADD_ACCOUNT)
    what = core_limit_hold.status_line(hold).split(": ", 1)[-1]
    lines = [f"I am paused: {what}. I start again by myself after that, and messages you send "
             "meanwhile wait and are answered then.", "",
             limit_unblock(session, runtime, controls=ids) + " Retry now tries the current model again."]
    if queued:
        import core_gate_notice
        lines += ["", core_gate_notice.card_line(queued)]
    req = HumanRequirement(
        kind="core-blocked", runtime=runtime or "claude", message="\n".join(lines),
        title="Paused: provider limit", guard=guard,
        device={"id": session, "name": session},
        actions=[recovery_action(aid, model_names()) for aid in ids],
        subject={"session": session, "source": HOLD_SOURCE, "recovery": True, "remind": False,
                 "incident": f"inc_{_hold_ref(hold)}"})
    got = manager.create(req)
    if got.id != req.id and got.message != req.message and not (got.subject or {}).get("verify"):
        # The same pause moved on (a later retry time): the one card is edited, never sent again.
        with manager.store.locked():
            cur = manager.get(got.id)
            if cur is not None and not cur.terminal:
                cur.message, cur.revision, cur.updated_at = req.message, cur.revision + 1, time.time()
                manager.store.save(cur)
                got = cur
    return got


def _hold_ref(hold: Dict) -> str:
    return hashlib.sha256(str(hold.get("started_at") or hold.get("set_at") or "").encode()).hexdigest()[:8]


def session_env(socket: str, session: str, name: str, runner: Optional[Runner] = None) -> Optional[str]:
    """One variable from a tmux session's environment, else None."""
    run = runner or subprocess.run
    try:
        r = run(["tmux", "-S", socket, "show-environment", "-t", f"={session}", name],
                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (r.stdout or "").strip() if r.returncode == 0 else ""
    return out.split("=", 1)[1].strip() or None if out.startswith(f"{name}=") else None
