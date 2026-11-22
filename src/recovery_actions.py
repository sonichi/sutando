#!/usr/bin/env python3
"""recovery_actions.py — the host executor for recovery buttons on blocked cards (C2 -> C3).

The owner taps a recovery button on the card of a blocked core or worker seat; the bridge's
reply handler checks the owner, the room and the card's revision and guard, then drops the click
in `state/recovery/actions/<hitl_id>.json` (hitl.recovery). The monitor watching that card's
session (core-input-watch.py, a process outside the session it watches) calls `run_pending`
each tick. It runs each click at most once, within ACTION_TTL_S of the tap (recovery_gate, the
check every entry path shares), from a fixed allowlist; argv is fixed here and the only argument
a click supplies is an id the card itself offered:

    retry_now           what the owner's bare "retry now" does (core_limit_hold): lift the hold, and
                        restart a live core held at its limit screen; a held seat whose session is
                        alive gets Escape and `continue` once idle, and its pool's --retry-cmd only
                        if still blocked after VERIFY_S; else a refused turn gets `continue` once idle
    switch_model:<m>    scripts/switch-model.sh <m> --confirm on that session (alias from the list)
    switch_runtime:<r>  core only: src/agent/start-cli.sh --runtime <r> --restart, detached
    restart_core        core only: src/agent/start-cli.sh --restart, detached (detached_action): it
                        closes the tmux session this monitor runs in, so it never runs as its child
    escape_prompt       one Escape key into the pane
    buy_credits, add_account, switch_account:<id>
                        never run here: each needs a confirmed, re-authenticated step (P1)

then writes a C3 receipt to `state/recovery/receipts/<hitl_id>.json`, appends an audit line to
`state/recovery/audit.jsonl`, and edits the same card with the outcome (the projector sends the
edit to the owner's DM). A card takes one tap: an action that did not take, or a block that
held, closes it so the next tick posts a fresh card with live controls. A stale tap is answered on
its card too (receipt `stale`). The card's last word says whether the work that was waiting
resumed (verify_step). A click is claimed by renaming its file before it runs, so a crash
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
from hitl.schema import STATUS_IN_PROGRESS, STATUS_PENDING  # noqa: E402
from workspace_default import status_read_path
import detached_action
import recovery_gate
import runtime_models

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


_READ = object()


def current_model(workspace: Path, launch_model=_READ) -> Optional[str]:
    """The model the core was last switched to (state/model-switch.json), else the launch's
    (`launch_model` when the caller already read it)."""
    try:
        data = json.loads(status_read_path("model-switch.json", Path(workspace)).read_text())
        if isinstance(data, dict) and data.get("model"):
            return str(data["model"])
    except (OSError, ValueError):
        pass
    if launch_model is not _READ:
        return launch_model
    try:
        import core_limit_hold
        return core_limit_hold.launch_identity(Path(workspace))[0]
    except Exception:  # noqa: BLE001 -- unknown is no model marked in use
        return None


def model_names(workspace: Path, runtime: Optional[str]) -> Dict[str, str]:
    """key -> the runtime's own label for that model, the text a switch button carries."""
    return {e["key"]: e["label"] for e in runtime_models.entries(workspace, runtime)}


def switch_offer(workspace: Path, runtime: Optional[str], avoid: Optional[str] = None,
                 seat: bool = False, launch_model=_READ) -> List[str]:
    """The rows of the runtime's own model list a card may offer (runtime_models.offer): none for
    Codex (its model is a launch argument) and none for a worker seat (it shares the core's config
    dir, where /model saves the default for every session)."""
    if runtime == "codex" or seat:
        return []
    current = current_model(workspace, launch_model)
    return [e["key"] for e in runtime_models.offer(workspace, runtime, current, avoid)]


def models_line(workspace: Path, runtime: Optional[str], prompt: Optional[str] = None,
                limited: Optional[str] = None) -> Optional[str]:
    """The card line naming every model the runtime offers, the one in use and the capped one marked."""
    cap = _NAMED_CAP.search(prompt or "")
    line = runtime_models.summary(workspace, runtime, current_model(workspace),
                                  limited or (cap.group(1) if cap else None))
    if line is None and runtime != "codex":
        return "Tap Refresh options to read the models this runtime offers and switch to one."
    return line


def refresh_limit_card(manager, workspace: Path, session: str, runtime: Optional[str]) -> List[str]:
    """Once the runtime's model list is read, a still-untapped limit card of the core gains its
    model buttons and the line naming them; the same card, never a new one."""
    from hitl import tui_gate
    keys = switch_offer(workspace, runtime)
    if not keys:
        return []
    done = []
    for r in manager.active():
        subject = r.subject or {}
        if (subject.get("session") != session or subject.get("seat") or not tui_gate.limit_card(r)
                or not subject.get("recovery") or r.status != STATUS_PENDING):
            continue
        have = [a.id for a in r.actions if a.id.startswith(recovery.SWITCH_MODEL + ":")]
        want = [recovery.action_id(recovery.SWITCH_MODEL, k) for k in keys]
        if have == want:
            continue
        names = model_names(workspace, runtime)
        with manager.store.locked():
            cur = manager.get(r.id)
            if cur is None or cur.status != STATUS_PENDING:
                continue
            rest = [a for a in cur.actions if not a.id.startswith(recovery.SWITCH_MODEL + ":")]
            cur.actions = [tui_gate.recovery_action(a, names) for a in want] + rest
            line = models_line(workspace, runtime)
            if line and line not in cur.message:
                cur.message = f"{cur.message}\n\n{line} Tap one below to switch to it."
            cur.revision += 1
            cur.updated_at = time.time()
            manager.store.save(cur)
        done.append(r.id)
    return done


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
    ids.append(recovery.REFRESH_OPTIONS)
    if not seat:
        ids.append(recovery.RESTART_CORE)
    return runtime_carries(ids, runtime)


def runtime_carries(ids: List[str], runtime: Optional[str]) -> List[str]:
    """The ids `runtime`'s host side carries out (core_recovery_state.RUNTIME_ACTIONS), so a card
    never offers a button its executor refuses; a step-up id stays, since its refusal is the answer."""
    import core_recovery_state
    return [i for i in ids if recovery.parse(i) and (recovery.parse(i)[0] in STEP_UP
            or core_recovery_state.parse_action_id(i, runtime or "claude") is not None)]


def incident_for(req) -> str:
    subject = req.subject or {}
    if subject.get("incident"):
        return str(subject["incident"])
    return "inc_" + hashlib.sha256(str(req.guard).encode()).hexdigest()[:8]


def _awaiting(workspace: Path, req) -> bool:
    """A recovery click accepted on `req` that has no receipt yet: only the executor closes it."""
    keyed = (req.subject or {}).get("option_for_action") or {}
    return (req.status == STATUS_IN_PROGRESS and recovery.is_recovery(req.chosen_action)
            and req.chosen_action not in keyed and recovery.read_receipt(workspace, req.id) is None)


def awaiting_executor(workspace: Path, req) -> bool:
    try:
        return _awaiting(workspace, req) or verify_pending(req, time.time())
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
             follow_ups: List[tuple], gate: Optional[str] = None):
    """(outcome, reason for the receipt, line for the card, recovery fields). `gate` is the tap's
    recovery-gate key: a detached restart acts only once that claim is finished."""
    import core_limit_hold
    ws = target.workspace
    refused_turn = ((req.subject or {}).get("gate") == "turn-rejected") if req is not None else False
    if verb in STEP_UP:
        why = ("This changes billing or the signed-in account, so it needs a confirmed sign-in step "
               "that is not available from here yet; nothing was changed.")
        return "refused", why, why, {}
    env = _env(target)
    if verb == recovery.RETRY_NOW:
        hold = core_limit_hold.active_hold(ws, now, target.hold_key())
        if hold is not None and target.seat:
            if not target.retry_cmd:
                why = "This worker's restart is not available here; it continues when the limit resets."
                return "refused", why, why, {}
            core_limit_hold.clear(ws, now, by=RETRY_BY, mirror=False, seat=target.hold_key())
            key = _detached_key(req)
            if target.send_key("Escape"):
                # A live session keeps its turn and context; verify_step restarts it only if still blocked.
                follow_ups.append(CONTINUE)
                return ("done", None, ESC_LINE, {"state": "starting", PATH_FIELD: PATH_ESC,
                                                 VERIFY_FIELD: {"fallback": key, "gate": gate or ""}})
            if not _seat_restart(target, runner, env, key, gate, now):
                why = "The worker did not restart, so it is still paused. " + NEW_CARD
                return "failed", why, why, {"state": "paused_limit", PATH_FIELD: PATH_RESTART}
            return ("done", None, "Restarting this worker; checking that it is working again.",
                    {"state": "starting", PATH_FIELD: PATH_RESTART,
                     VERIFY_FIELD: {"detached": key, "reinstate": target.hold_key()}})
        if hold is not None:
            live = hold.get("source") == core_limit_hold.PANE_SOURCE
            core_limit_hold.clear(ws, now, by=RETRY_BY, mirror=not live)
            if live:
                # A live core at its limit screen retries by a restart; the hold returns if it fails.
                outcome = _execute(recovery.RESTART_CORE, None, req, target, runner, now, follow_ups, gate)
                if outcome[0] != "done":
                    core_limit_hold.reinstate(ws, now, by=RETRY_BY, mirror=False)
                    why = "The core did not restart, so it is still paused. " + NEW_CARD
                    return outcome[0], why, why, {"state": "paused_limit"}
                outcome[3][VERIFY_FIELD]["reinstate"] = ""  # the core's own hold returns if it fails
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
        entry = runtime_models.entry(ws, req.runtime if req is not None else None, arg)
        if entry is None or not entry.get("arg") or arg not in switch_offer(ws, req.runtime if req else None):
            why = "That model is not one the runtime offers now; nothing was changed."
            return "refused", why, why, {}
        arg_id, name = entry["arg"], entry["label"]
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
        # The launcher's --restart closes the tmux session this monitor runs in: it runs detached,
        # and the monitor that comes back with the core reads its outcome (verify_step).
        key = _detached_key(req)
        if not detached_action.spawn(ws, key, argv, env=env, now=now, popen=spawn_detached, after_gate=gate,
                                     log=ws / "logs" / "core-recovery-restart.log"):
            why = "The restart could not be started; nothing was changed. " + NEW_CARD
            return "failed", why, why, {}
        check = {"detached": key, "what": f"the core under {arg}" if arg else "the core"}
        if verb == recovery.SWITCH_RUNTIME:
            return ("done", None, f"Switching the core to {arg}; checking that it is working again.",
                    {"state": "starting", "runtime": arg, VERIFY_FIELD: check})
        return ("done", None, "Restarting the core; checking that it is working again.",
                {"state": "starting", VERIFY_FIELD: check})
    if verb == recovery.REFRESH_OPTIONS:
        # The owner asked: the pane's own menu is read again, and a core idle at its limit has its
        # model picker opened, read and closed once; the card is replaced with what the screen shows.
        if not target.seat:
            runtime_models.request_probe(ws, target.session, now)
        return ("done", None, "Reading the current options again; a new card shows what the screen offers now.",
                {})
    if verb == recovery.ESCAPE_PROMPT:
        if not target.send_key("Escape"):
            why = "The terminal did not take the key."
            return "failed", why, why, {}
        return "done", None, "Pressed Escape on the prompt.", {}
    why = "That button is not one this Mac runs."
    return "refused", why, why, {}


#: What a card's done action hands to verify_step (not part of the receipt).
VERIFY_FIELD = "_verify"
#: Which way a worker's Retry now went, kept on its receipt and audit line.
PATH_FIELD = "path"
PATH_ESC, PATH_RESTART, PATH_FALLBACK = "esc", "restart", "restart-fallback"
ESC_LINE = ("Pressed Escape on this worker and sending continue once it is idle; checking that it is "
            "working again. If it is still blocked in a minute, it is restarted.")
FALLBACK_LINE = "Still blocked; restarting this worker. Checking that it is working again."
NEW_CARD = "If it is still blocked, a new card follows with what you can do now."
#: How the detached restarts are started: replaced in tests, never run for real there.
spawn_detached = subprocess.Popen


def _detached_key(req) -> str:
    return f"restart:{req.id if req is not None else '-'}:{req.revision if req is not None else 0}"


def _env(target: Target) -> Dict:
    env = dict(os.environ, SUTANDO_TMUX_SOCKET=target.socket, SUTANDO_TMUX_SESSION=target.session)
    env.pop("SUTANDO_CORE_RUNTIME", None)  # the configured runtime, or --runtime, decides
    env.pop("SUTANDO_CORE_SESSION", None)  # the monitor runs outside the core session
    return env


def _seat_restart(target: Target, runner: Runner, env: Dict, key: str, gate: Optional[str],
                  now: float) -> bool:
    """Restart a seat by its pool's command, its hold lifted; False (hold back) when it did not start."""
    import core_limit_hold
    ws = target.workspace
    core_limit_hold.clear(ws, now, by=RETRY_BY, mirror=False, seat=target.hold_key())
    # The pool's command hands the restart to its own detached child, which reports by key.
    detached_action.begin(ws, key, now)
    child_env = dict(env, **{detached_action.KEY_ENV: key, detached_action.GATE_ENV: gate or ""})
    if _rc(runner, target.retry_cmd, child_env, RESTART_TIMEOUT_S) != 0:
        detached_action.report(ws, key, detached_action.FAILED, now=now)
        core_limit_hold.reinstate(ws, now, by=RETRY_BY, mirror=False, seat=target.hold_key())
        return False
    return True


def _rc(runner: Runner, argv: List[str], env: Dict, timeout: float) -> Optional[int]:
    try:
        return runner(argv, capture_output=True, text=True, timeout=timeout, env=env,
                      stdin=subprocess.DEVNULL).returncode
    except (OSError, subprocess.SubprocessError):
        return None


def _check(manager, data: Dict, target: Target, now: float):
    """(requirement, None) when the click may run, else (requirement or None, (outcome, why))."""
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
    return req, None


def _answer_card(manager, req, line: str, outcome: str, now: float, check: Optional[Dict] = None) -> None:
    """Show the outcome on the same card: its message gains the line. The card stays this
    incident's one card: it closes only once the session is seen unblocked (resolve_escalations),
    so a later tick that still sees the block finds it and posts no second card."""
    with manager.store.locked():
        cur = manager.get(req.id)
        if cur is None or cur.terminal:
            return
        cur.message = f"{cur.message}\n\n{line}"
        subject = {k: v for k, v in (cur.subject or {}).items() if k != "receipt"}
        subject["verify"] = {"at": now, "outcome": outcome, **(check or {})}
        if not (outcome == "done" and (check or {}).get("detached")):
            # A detached restart's card carries no receipt until its outcome is known (verify_step):
            # a client reading the card never sees "done" for a restart that then failed.
            subject["receipt"] = {"outcome": outcome, "at": int(now)}
        cur.subject = subject
        cur.revision += 1
        cur.updated_at = time.time()
        manager.store.save(cur)


def _final_receipt(workspace: Path, req_id: str, check: Dict, outcome: str, reason: Optional[str],
                   now: float) -> Dict:
    """A detached restart's observed outcome: the receipt on disk is corrected when it failed, and
    the card's receipt (returned, for the caller's edit) is the observed one."""
    if check.get("detached") and outcome != "done":
        prior = recovery.read_receipt(workspace, req_id)
        if prior is not None and prior.get("outcome") == "done":
            recovery.write_receipt(workspace, recovery.receipt(
                str(prior.get("hitl_id") or req_id), str(prior.get("incident") or ""),
                str(prior.get("action_id") or ""), outcome, reason, now,
                {k: v for k, v in (prior.get("recovery") or {}).items() if k != "state"}))
    return {"outcome": outcome, "at": int(now)}


#: How long a session may stay blocked after a done action before its card says so.
VERIFY_S = 60.0
STILL_BLOCKED = "It is still blocked. A new card follows with what you can do now."
WORKING_AGAIN = "It is working again."
#: The core is up again and has taken up the work that was waiting when the owner tapped.
RESUMED = "It is working again and has picked up the work that was waiting."
#: Up again, but none of that work was seen starting within RESUME_S: said plainly, not as success.
NOT_RESUMED = ("It is up again, but I have not seen the waiting work start yet. If it does not, "
               "send the message again.")
#: How long after a done action an unblocked core may take to start the waiting work.
RESUME_S = 120.0
#: Unblocked after an action that did not report done: true, and it claims nothing for that action.
CLEARED = "The block has cleared."
#: Longest a card waits on its verification before resolve_escalations may close it anyway.
VERIFY_DEADLINE_S = detached_action.DEADLINE_S + RESUME_S + VERIFY_S
#: Waiting task ids kept on a card's check; the first that leaves the queue is the evidence.
WAITING_MAX = 20


def _resume_check(workspace: Path, req) -> Dict:
    """What a done core action must later see to say the pending work resumed: the tasks waiting
    in the queue now, and, on a refused turn, that turn running again."""
    import task_queue
    try:
        waiting = sorted(p.stem for p in task_queue.pending_files(Path(workspace)))[:WAITING_MAX]
    except OSError:
        waiting = []
    out: Dict = {"waiting": waiting}
    if (req.subject or {}).get("gate") == "turn-rejected":
        out["turn"] = True
    return out


def _resumed(workspace: Path, check: Dict, state: Optional[str]) -> Optional[bool]:
    """None when nothing was waiting; True once the core is seen running or one of the tasks that
    waited has left the queue; False otherwise (so far)."""
    waiting = [w for w in check.get("waiting") or [] if isinstance(w, str)]
    if not waiting and not check.get("turn"):
        return None
    if state == "running":
        return True
    if not waiting:
        return False
    import task_queue
    try:
        still = {p.stem for p in task_queue.pending_files(Path(workspace))}
    except OSError:
        return False
    return any(w not in still for w in waiting)


def verify_pending(req, now: float) -> bool:
    """A done action's card still verifying (a detached restart unanswered, or the waiting work not
    yet seen starting), within its deadline: resolve_escalations leaves it open until it says how."""
    check = (req.subject or {}).get("verify")
    if not isinstance(check, dict) or check.get("seen") or check.get("outcome") != "done":
        return False
    try:
        return now - float(check.get("at") or 0) < VERIFY_DEADLINE_S
    except (TypeError, ValueError):
        return False


def verify_step(manager, workspace: Path, session: str, blocked: bool, now: Optional[float] = None,
                state: Optional[str] = None, target: Optional[Target] = None,
                runner: Optional[Runner] = None) -> List[str]:
    """For each of this session's cards an action answered: once the session is seen unblocked,
    the card says so, and for the core whether the work that was waiting resumed (then
    resolve_escalations closes it); still blocked VERIFY_S after a done action, the same card says
    that, once, and is closed for a fresh one, unless a worker's Escape is owed its restart
    (`target` carries the pool's command). Never a new card. Returns the edited card ids."""
    at = time.time() if now is None else now
    edited = []
    for r in manager.active():
        subject = r.subject or {}
        check = subject.get("verify")
        if subject.get("session") != session or not subject.get("recovery") or not isinstance(check, dict) \
                or check.get("seen"):
            continue
        if check.get("detached"):
            done = detached_action.status(workspace, check["detached"], at)
            if done.state == detached_action.RUNNING:
                continue  # the card says "checking" until the restart answers or its deadline passes
            if done.state != detached_action.DONE:
                _restart_failed(manager, workspace, r.id, check, done, at)
                edited.append(r.id)
                continue
            if "receipt" not in subject:
                # The restart answered done: the card's receipt says so now, whatever is seen next.
                with manager.store.locked():
                    cur = manager.get(r.id)
                    if cur is not None and not cur.terminal:
                        cur.subject = {**(cur.subject or {}), "receipt": {"outcome": "done", "at": int(at)}}
                        cur.revision += 1
                        cur.updated_at = time.time()
                        manager.store.save(cur)
        if blocked and not (check.get("outcome") == "done" and at - float(check.get("at") or at) >= VERIFY_S):
            continue
        if blocked and check.get("fallback") and target is not None and target.seat \
                and target.retry_cmd and target.session == session:
            _fallback_restart(manager, workspace, r, check, target, runner or subprocess.run, at)
            edited.append(r.id)
            continue
        seen = "blocked" if blocked else "working"
        if blocked:
            line = STILL_BLOCKED
        elif check.get("outcome") != "done":
            line = CLEARED
        else:
            resumed = _resumed(workspace, check, state)
            if resumed is None:
                line = WORKING_AGAIN
            elif resumed:
                line = RESUMED
            elif at - float(check.get("at") or at) < RESUME_S:
                continue  # up, and the card says "checking" until the waiting work starts or RESUME_S
            else:
                line, seen = NOT_RESUMED, "up"
        with manager.store.locked():
            cur = manager.get(r.id)
            if cur is None or cur.terminal:
                continue
            cur.message = f"{cur.message}\n\n{line}"
            cur.subject = {**(cur.subject or {}), "verify": {**check, "seen": seen}}
            cur.revision += 1
            cur.updated_at = time.time()
            manager.store.save(cur)
        if blocked:
            _supersede(manager, r.id)
        edited.append(r.id)
    return edited


def _fallback_restart(manager, workspace: Path, req, check: Dict, target: Target, runner: Runner,
                      now: float) -> None:
    """A worker still blocked after its Escape: the pool restarts it, once, and the same card,
    receipt and audit say the retry fell back to that restart."""
    key = str(check["fallback"])
    rest = {k: v for k, v in check.items() if k not in ("fallback", "gate")}
    # The card gives up its claim before the restart starts: a failed save starts nothing, and a
    # crash after it leaves a detached check that _restart_failed answers, never a second restart.
    try:
        with manager.store.locked():
            cur = manager.get(req.id)
            if cur is None or cur.terminal or (cur.subject or {}).get("verify", {}).get("fallback") != key:
                return
            cur.message = f"{cur.message}\n\n{FALLBACK_LINE}"
            cur.subject = {**(cur.subject or {}), "verify": {**rest, "at": now, "detached": key,
                                                             "reinstate": target.hold_key()}}
            cur.revision += 1
            cur.updated_at = time.time()
            manager.store.save(cur)
    except Exception:  # noqa: BLE001 -- an unrecorded claim must not restart anything
        return
    if detached_action.status(workspace, key, now).state is not None:
        return  # already dispatched under this key: its own record says how it went
    ok = _seat_restart(target, runner, _env(target), key, check.get("gate") or None, now)
    prior = recovery.read_receipt(workspace, req.id) or {}
    aid = str(prior.get("action_id") or req.chosen_action or recovery.RETRY_NOW)
    outcome = "done" if ok else "failed"
    line = None if ok else "The worker did not restart, so it is still paused. " + NEW_CARD
    fields = {**(prior.get("recovery") or {}), "state": "starting" if ok else "paused_limit",
              PATH_FIELD: PATH_FALLBACK}
    recovery.write_receipt(workspace, recovery.receipt(req.id, str(prior.get("incident") or incident_for(req)),
                                                       aid, outcome, line, now, fields))
    audit(workspace, {"at": int(now), "hitl_id": req.id, "action_id": aid, "outcome": outcome,
                      "session": target.session, "seat": target.seat or "", PATH_FIELD: PATH_FALLBACK})
    if ok:
        return
    with manager.store.locked():
        cur = manager.get(req.id)
        if cur is None or cur.terminal:
            return
        cur.message = f"{cur.message}\n\n{line}"
        cur.subject = {**(cur.subject or {}), "verify": {**(cur.subject or {}).get("verify", {}), "seen": "failed"},
                       "receipt": {"outcome": "failed", "at": int(now)}}
        cur.revision += 1
        cur.updated_at = time.time()
        manager.store.save(cur)
    _supersede(manager, req.id)


def _restart_failed(manager, workspace: Path, req_id: str, check: Dict, done, now: float) -> None:
    """A detached restart that failed or never answered by its deadline: the hold it lifted
    returns, and the card ends on why, so it never stays on "checking"."""
    import core_limit_hold
    if check.get("reinstate") is not None:
        core_limit_hold.reinstate(workspace, now, by=RETRY_BY, mirror=False, seat=check["reinstate"] or None)
    what = check.get("what") or "it"
    if done.state == detached_action.EXPIRED:
        line = f"I couldn't confirm {what} came back within {int(detached_action.DEADLINE_S // 60)} minutes. " + NEW_CARD
    elif done.reason == detached_action.HANDOFF:
        line = "The tap was not recorded in time, so nothing was restarted. " + NEW_CARD
    elif done.reason and done.reason not in ("exit", "could not start"):
        line = done.reason  # the action's own sentence (the pool's, for a worker)
    elif done.rc == core_limit_hold.HOLD_EXIT:
        line = "The core is paused for a provider limit, so it was not restarted. " + NEW_CARD
    else:
        line = (f"The restart did not complete (exit {done.rc}). " if done.rc is not None
                else "The restart did not complete. ") + NEW_CARD
    with manager.store.locked():
        cur = manager.get(req_id)
        if cur is None or cur.terminal:
            return
        cur.message = f"{cur.message}\n\n{line}"
        cur.subject = {**(cur.subject or {}), "verify": {**check, "seen": "failed"},
                       "receipt": _final_receipt(workspace, req_id, check, "failed", line, now)}
        cur.revision += 1
        cur.updated_at = time.time()
        manager.store.save(cur)
    _supersede(manager, req_id)


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
            fields: Dict, now: float, close: bool = False, card: bool = True) -> Dict:
    hitl_id, aid = str(data.get("hitl_id") or ""), str(data.get("action_id") or "")
    incident = incident_for(req) if req is not None else ""
    fields = dict(fields)
    check = fields.pop(VERIFY_FIELD, None)
    body = recovery.receipt(hitl_id, incident, aid, outcome, reason, now, fields)
    recovery.write_receipt(target.workspace, body)
    entry = {"at": int(now), "hitl_id": hitl_id, "action_id": aid, "outcome": outcome,
             "sender": str(data.get("sender") or ""), "session": target.session, "seat": target.seat or ""}
    if fields.get(PATH_FIELD):
        entry[PATH_FIELD] = fields[PATH_FIELD]
    audit(target.workspace, entry)
    if outcome == "done" and req is not None and not target.seat:
        check = dict(check or {}, **_resume_check(target.workspace, req))
    if req is None or not card:
        pass
    elif outcome == "stale":
        # A stale tap is answered on its card (receipt `stale`, a fresh revision), never silently:
        # closed when the card was waiting on exactly this tap; a card running another tap keeps its own.
        took = req.status == STATUS_IN_PROGRESS and req.chosen_action == aid
        if took or req.status == STATUS_PENDING:
            _answer_card(manager, req, line, outcome, now, check)
        if took or close:
            _supersede(manager, req.id)
    else:
        _answer_card(manager, req, line, outcome, now, check)
        if outcome == "done" and aid == recovery.REFRESH_OPTIONS:
            manager.resolve(req.id)  # the owner asked for a fresh read: the next tick posts the new card
        elif outcome != "done":
            _supersede(manager, req.id)
    return body



def _supersede(manager, req_id: str) -> None:
    """Close a card whose action did not take, so the next tick posts a fresh one with live
    controls: a card takes one tap, so text on it may not ask for a second. It owes no "Cleared"
    notice (renotify.said_on_card): the block is not cleared, and the fresh card carries that notice."""
    with manager.store.locked():
        cur = manager.get(req_id)
        if cur is None or cur.terminal:
            return
        cur.subject = {**(cur.subject or {}), "superseded": True}
        manager.store.save(cur)
    manager.resolve(req_id)


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
        key = _gate_key(data)
        gated = None
        if refusal is None:
            # The one gate every entry path shares: fresh, and claimed durably before it runs.
            admitted = recovery_gate.admit(target.workspace, key, issued_at(data), clock(), ACTION_TTL_S)
            if not admitted.ok:
                refusal, gated = ("stale", _GATE_REFUSALS[admitted.why]), admitted.why
        if refusal is not None:
            outcome, why = refusal
            # An expired tap the card took: the card says so and a fresh one follows. A replayed or
            # interrupted one leaves the card to the tap that already ran.
            done.append(_settle(manager, target, data, req, outcome, why, why, {}, clock(),
                                close=gated == "stale", card=gated in (None, "stale")))
        else:
            verb, arg = recovery.parse(data["action_id"])
            outcome, reason, line, fields = _execute(verb, arg, req, target, run, clock(), owed, key)
            done.append(_settle(manager, target, data, req, outcome, reason, line, fields, clock()))
            recovery_gate.finish(target.workspace, key, outcome, clock())
        _unlink(claimed)
    done += _orphans(manager, target, clock())
    return done


_GATE_REFUSALS = {"stale": "The tap reached the Mac too late to act on; nothing was done.",
                  "replay": "That tap was already acted on; nothing was done again.",
                  "interrupted": "I was interrupted while doing this; check its state before tapping again."}


def _gate_key(data: Dict) -> str:
    return f"card:{data.get('hitl_id')}:{data.get('action_id')}:{data.get('expected_revision')}"


def issued_at(data: Dict):
    """When the tap was made: the event's own time when the bridge recorded one, else when the
    bridge received it, whichever is earlier (a tap held in a backlog is never fresher than it was)."""
    ok = [t for t in (data.get("at"), data.get("tapped_at"))
          if isinstance(t, (int, float)) and not isinstance(t, bool)]
    return min(ok) if ok else None


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
        manager.expire(req.id)  # nothing ran: the next tick raises the card that replaces it
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
    ids += [recovery.ADD_ACCOUNT, recovery.REFRESH_OPTIONS]
    ids = runtime_carries(ids, runtime)
    what = core_limit_hold.status_line(hold).split(": ", 1)[-1]
    models = models_line(workspace, runtime, limited=hold.get("model"))
    lines = [f"I am paused: {what}. I start again by myself after that, and messages you send "
             "meanwhile wait and are answered then.", "",
             limit_unblock(session, runtime, controls=ids) + " Retry now tries the current model again."]
    if models:
        lines += ["", models]
    if queued:
        import core_gate_notice
        lines += ["", core_gate_notice.card_line(queued)]
    req = HumanRequirement(
        kind="core-blocked", runtime=runtime or "claude", message="\n".join(lines),
        title="Paused: provider limit", guard=guard,
        device={"id": session, "name": session},
        actions=[recovery_action(aid, model_names(workspace, runtime)) for aid in ids],
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
