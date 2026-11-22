#!/usr/bin/env python3
"""recovery_actions.py — the host executor for recovery buttons on blocked cards (C2 -> C3).

The owner taps a recovery button on the card of a blocked core or worker seat; the bridge's
reply handler checks the owner, the room and the card's revision and guard, then drops the click
in `state/recovery/actions/<hitl_id>.json` (hitl.recovery). The monitor watching that card's
session (core-input-watch.py, a process outside the session it watches) calls `run_pending`
each tick. It runs each click at most once, within ACTION_TTL_S, from a fixed allowlist; argv
is fixed here and the only argument a click supplies is an id the card itself offered:

    retry_now           core: core_limit_hold.clear; a refused turn: types `continue` once idle
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

REPO = Path(__file__).resolve().parent.parent
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
        data = json.loads((Path(workspace) / "state" / "model-switch.json").read_text())
    except (OSError, ValueError):
        return None
    return model_family(data.get("model")) if isinstance(data, dict) else None


def switch_offer(workspace: Path, runtime: Optional[str], avoid: Optional[str] = None,
                 seat: bool = False) -> List[str]:
    """The model aliases a card may offer: none for Codex (its model is a launch argument),
    else every alias but the one in use (the core's record; a seat's is unknown) and `avoid`."""
    if runtime == "codex":
        return []
    skip = {None if seat else current_model(workspace), model_family(avoid)} - {None}
    return [m for m in recovery.SWITCH_ALIASES if m not in skip]


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
    if kind == "turn-rejected":
        ids.append(recovery.RETRY_NOW)
    if limit:
        ids += [recovery.ADD_ACCOUNT, recovery.BUY_CREDITS]
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
                 send_key: Optional[Callable[[object], bool]] = None, brain: Optional[str] = None):
        self.workspace, self.socket, self.session, self.seat = Path(workspace), socket, session, seat
        self.send_key = send_key or (lambda k: False)
        self.brain = brain

    def state_dir(self) -> Path:
        """A seat's model record lives apart from the core's, which the core's card reads."""
        base = self.workspace / "state"
        return base / "seats" / self.session if self.seat else base


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
    if verb == recovery.RETRY_NOW:
        if not target.seat and core_limit_hold.active_hold(ws, now) is not None:
            core_limit_hold.clear(ws, now, by="recovery-card")
            return ("done", None, "Retrying now: the core is starting. If the limit is still in force it "
                    "pauses again and a new card says so.", {"state": "starting"})
        if refused_turn:
            follow_ups.append(CONTINUE)
            return "done", None, "Retrying: the refused message is sent again once its terminal is idle.", {}
        why = "Nothing is paused or refused right now, so there is nothing to retry."
        return "refused", why, why, {}
    env = dict(os.environ, SUTANDO_TMUX_SOCKET=target.socket, SUTANDO_TMUX_SESSION=target.session)
    env.pop("SUTANDO_CORE_RUNTIME", None)  # the configured runtime, or --runtime, decides
    if verb == recovery.SWITCH_MODEL:
        held = not target.seat and core_limit_hold.active_hold(ws, now) is not None
        argv = ["bash", str(REPO / "scripts" / "switch-model.sh"), arg, "--confirm",
                "--socket", target.socket, "--session", target.session]
        if target.seat:
            argv += ["--state-dir", str(target.state_dir())] + (["--brain", target.brain] if target.brain else [])
        try:
            run = runner(argv, capture_output=True, text=True, timeout=SWITCH_TIMEOUT_S, env=env,
                         stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            why = f"The switch to {arg} did not finish; it stays on its current model."
            return "failed", why, why, {}
        if run.returncode != 0:
            why = f"It did not take {arg}; it stays on its current model."
            return "failed", why, why, {}
        if refused_turn:
            follow_ups.append(CONTINUE)
        who = target.seat or "the core"
        return ("done", None, f"Switched {who} to {arg}.",
                {"state": "starting" if held else "running", "model": arg})
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
    if (req.status != STATUS_IN_PROGRESS or req.chosen_action != aid
            or str(data.get("guard") or "") != req.guard):
        return req, ("stale", "This card changed before the tap reached the Mac; nothing was done.")
    at = data.get("at")
    if not isinstance(at, (int, float)) or now - at > ACTION_TTL_S:
        return req, ("stale", "The tap reached the Mac too late to act on; nothing was done.")
    return req, None


def _answer_card(manager, req, line: str, outcome: str, now: float) -> None:
    """Show the outcome on the same card: its message gains the line, and it closes."""
    with manager.store.locked():
        cur = manager.get(req.id)
        if cur is None or cur.terminal:
            return
        cur.message = f"{cur.message}\n\n{line}"
        cur.subject = {**(cur.subject or {}), "receipt": {"outcome": outcome, "at": int(now)}}
        manager.store.save(cur)
    (manager.resolve if outcome == "done" else manager.expire)(req.id)


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
    ids += [recovery.ADD_ACCOUNT, recovery.BUY_CREDITS]
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
        actions=[recovery_action(aid) for aid in ids],
        subject={"session": session, "source": HOLD_SOURCE, "recovery": True, "remind": False,
                 "incident": f"inc_{_hold_ref(hold)}"})
    return manager.create(req)


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
