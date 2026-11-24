#!/usr/bin/env python3
"""A card in the owner's DM that switches the core between Codex and Claude Code, and confirms it.

    python3 runtime_card.py [--workspace WS] post --room ROOM [--reply-to EVENT] [--print]
    python3 runtime_card.py [--workspace WS] reply --task-file PATH
    python3 runtime_card.py [--workspace WS] switch --room ROOM --runtime RT --attempt ID
    python3 runtime_card.py [--workspace WS] settle --room ROOM --runtime RT --since EPOCH [--attempt ID]
    python3 runtime_card.py [--workspace WS] status
    python3 runtime_card.py [--workspace WS] resume     # restart the sign-in waiter if it is gone

`post` sends the menu to the owner's checked DM and records it (src/issued_cards.py), so only the
owner's tap on it, in that DM, acts (agent_card.click_verdict). It has two buttons, "Codex" and
"Claude Code"; the runtime in use (src/sutando_config.py resolve_core_runtime, as the launcher
resolves it) is the primary one and named in the text.

A tap on the other runtime first checks that runtime is signed in on this computer, before anything
is stopped: Claude Code by `claude auth status --json` under the core's config dir, Codex by
src/codex_account.py on the core's Codex home (the account card's probes,
skills/account-switch/scripts/account_card.py). Not signed in, or not readable: it answers with a
"Sign in first" card that carries the account controls' marker for that runtime, so the desktop app on
the core's computer draws the sign-in in the card itself (anywhere else the card says where to open
it), restarts nothing, keeps the tap, and starts `await-sign-in` detached: once the sign-in is there
it switches as the tap asked, with no second ask; after SIGN_IN_WAIT_S it says it stopped waiting.
Signed in: it answers "switching" at once and starts `switch` detached, in a session of its own,
because the core restarts.

`switch` runs src/agent/graceful-restart.sh -- --runtime <rt> (it waits for the core's current turn to
end, then `start-cli.sh --restart --runtime <rt>`). The launcher writes the desktop's selection
(runtime.json coreRuntime) and `<workspace>/state/core-runtime.json` only once the new core's panes
run that CLI. `settle` then confirms from both, never from a heartbeat alone: the record names the
target runtime with a session created after the switch began, and the runtime the launcher would
pick next (desktop selection, else config) is the target too. It posts the outcome in the same DM.
The workspace, tasks, results, state and memory are shared by both runtimes; only the live
conversation does not carry over, and the cards say so.

One card, updated in place (owner 2026-11-23): the "switching" answer is an edit of the tapped card's
own event (`room.message.edit`), and the outcome edits that same event again (the detached `switch`
gets it as `--card`), so a tap adds no message. No Action carries a card in an edit, so the event then
shows the words in place of the card. The "Sign in first" card is still posted as a card of its own:
its sign-in controls cannot ride in an edit. Its event is kept with the wait, and the waiter edits it
to the "switching" words, to "gave up", or, when the core is already on that runtime (switched some
other way), to say so. Where an edit cannot be made the answer is posted as a card, as before.

One switch runs at a time (a lock and a record, as the account card). Every command prints one JSON
object. Exit 0 done, 1 a negative answer, 2 refused. Stdlib only.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
for _p in (str(_REPO / "src"), str(_REPO / "skills" / "worker-pool" / "scripts"),
           str(_REPO / "skills" / "account-switch" / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agent_card as ac  # noqa: E402
import issued_cards  # noqa: E402

CARD_ID, KIND, TARGET = "core-runtime-switch", "runtime", "core"
# The "switching" answer and the outcome have an id of their own: under the menu's id they would
# retire the menu (one live card per id and room) while its buttons are still on screen.
NOTE_ID = "core-runtime-switch-status"
HANDLER = "skills/runtime-switch/scripts/runtime_card.py reply"
RESTART = _REPO / "src" / "agent" / "graceful-restart.sh"
NAMES = {"codex": "Codex", "claude": "Claude Code"}
ORDER = ("codex", "claude")
RUNNING = ("state", "runtime-card-switch.json")
RUNNING_LOCK = ("state", "runtime-card-switch.lock")
EXEC_LOCK = ("state", "runtime-card-switch.exec.lock")
RECORD = ("state", "core-runtime.json")
RESTART_TIMEOUT_S = 1500
# A desktop keeps its gateway as a window of the core's tmux session, which --restart replaces; the
# desktop's own control script, beside the engine, puts that window back.
DESKTOP_CTL = _REPO.parent / "sutando-ctl.sh"
GATEWAY_TIMEOUT_S = 180
GATEWAY_LOST = "Messages may not reach the core until the app is quit and reopened: its gateway did not come back ({why})."
CONFIRM_TIMEOUT_S, CONFIRM_POLL_S = 180, 5
# A restart put off because another one holds graceful-restart.sh's lock: the switch waits for that
# lock to go, this long at most, and then asks once more.
RESTART_LOCK = ("state", "locks", "graceful-restart.lock")
RESTART_RESULT = ("state", "graceful-restart-result.json")     # the launcher's, written before its lock goes
DEFERRED = 4
DEFER_WAIT_S, DEFER_POLL_S = 600, 10
RUNNING_STALE_S = 2 * RESTART_TIMEOUT_S + DEFER_WAIT_S + CONFIRM_TIMEOUT_S + 600
# graceful-restart.sh exits 3/4/5 before it stops the core; any other failure may have restarted it.
NOT_RESTARTED = (3, 4, 5)
RESTART_REASONS = {3: "the core could not get ready to restart",
                   4: "another restart was under way or the core was busy, so the restart was put off",
                   5: "the restart was only rehearsed"}
POST_TRIES, POST_PAUSE_S = 4, 15
CARD_TRIES = 20
# A tap on a runtime that is not signed in: the card carries the account controls (the same marker the
# account cards use, naming the runtime to sign in to) and the tap is kept while a waiter watches for it.
CONTROLS = "account_controls"
WAITING = ("state", "runtime-card-signin.json")
WAITING_LOCK = ("state", "runtime-card-signin.lock")
SIGN_IN_WAIT_S, SIGN_IN_POLL_S = 900, 5
SIGN_IN = ("Sign in to {name} below. I'll switch the core to it as soon as you are signed in; you don't "
           "need to ask again.")
SIGN_IN_ASK_AGAIN = ("Sign in to {name} below, then ask me to switch again: I could not set up the "
                     "automatic switch this time.")
SIGN_IN_ELSEWHERE = "Open this on the computer that runs the core to sign in to {name}."
SIGN_IN_GAVE_UP = ("I stopped waiting for the {name} sign-in after {minutes} minutes. The core is still on "
                   "{was}; nothing changed. Ask me to switch again when you are ready.")
CARRIES = "Tasks, memory and the workspace carry over; our current conversation does not."
GONE = "That choice is no longer on my card. Ask me for the runtime card again."
BUSY = "A runtime switch is already under way. I'll confirm here when it has finished."


def _emit(payload: dict, code: int = 0) -> int:
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    return code


def name(rt: str) -> str:
    return NAMES.get(rt, rt or "an unknown runtime")


# ── which runtime ─────────────────────────────────────────────────────────────────────────────────

def runtime() -> str:
    """The runtime in use as the launcher resolves it ("codex" or "claude"); "claude" when unreadable.
    Inside the core this includes its own SUTANDO_CORE_RUNTIME, which names the running CLI."""
    try:
        from sutando_config import resolve_core_runtime
        return "codex" if resolve_core_runtime() == "codex" else "claude"
    except Exception:  # noqa: BLE001 - an unreadable config is the default runtime
        return "claude"


def configured_runtime() -> str:
    """The runtime the launcher picks on its NEXT start: the desktop's selection, else config. The
    caller's own SUTANDO_CORE_RUNTIME (a detached child inherits the old core's) is left out. "" when
    unreadable."""
    saved = os.environ.pop("SUTANDO_CORE_RUNTIME", None)
    try:
        from sutando_config import resolve_core_runtime
        return resolve_core_runtime()
    except Exception:  # noqa: BLE001 - unreadable: not confirmed
        return ""
    finally:
        if saved is not None:
            os.environ["SUTANDO_CORE_RUNTIME"] = saved


def desktop_hosted() -> bool:
    """Whether a desktop hosts this engine (its runtime.json folder exists), so it holds a selection."""
    try:
        from sutando_config import desktop_runtime_selection_path
        path = desktop_runtime_selection_path()
    except Exception:  # noqa: BLE001 - no config: no desktop
        return False
    return bool(path) and path.parent.is_dir()


def confirmed_record(ws: Path) -> dict:
    """`<workspace>/state/core-runtime.json`, which the launcher writes only once the core's panes run
    the CLI it names: {runtime, session, started_at}; {} when absent or unreadable."""
    try:
        rec = json.loads(ws.joinpath(*RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(rec, dict) or rec.get("runtime") not in NAMES:
        return {}
    started = rec.get("started_at")
    if not isinstance(started, (int, float)) or isinstance(started, bool):
        return {}
    return rec


# ── sign-in, before anything is stopped ───────────────────────────────────────────────────────────

def signed_in(rt: str, *, run=subprocess.run, codex_read=None) -> Optional[bool]:
    """Whether `rt` is signed in on this computer: True, False, or None when it could not be read.
    The account card's probes, which read the core's own config dir / Codex home."""
    import account_card
    try:
        return account_card.current(rt, run=run, codex_read=codex_read)["signed_in"]
    except Exception:  # noqa: BLE001 - a probe that breaks proves nothing either way
        return None


def sign_in_words(rt: str, state: Optional[bool], waiting: bool = True) -> str:
    """What the "Sign in first" card says. The automatic switch is promised only with `waiting`: a
    waiter that is running. Without one the owner is told to ask again after signing in."""
    why = (f"{name(rt)} is not signed in on this computer" if state is False
           else f"I could not confirm {name(rt)} is signed in on this computer")
    return f"{why}, so I have not restarted the core. " + (SIGN_IN if waiting else SIGN_IN_ASK_AGAIN).format(name=name(rt))


def sign_in_card(rt: str, state: Optional[bool], waiting: bool = True) -> tuple:
    """The "Sign in first" card: its words, and the marker by which the desktop app, on the computer that
    runs the core, draws that runtime's sign-in controls in it (`core: true`: the core's own sign-in for
    `runtime`, which need not be the runtime the core is on now). Nothing typed there reaches the room. A
    client that cannot draw them shows the body: where to open the card."""
    body, card = ac.build(NOTE_ID, "pending", title="Core runtime", avatar_=ac.avatar(initial="R"),
                          status="Sign in first", text=sign_in_words(rt, state, waiting),
                          body=SIGN_IN_ELSEWHERE.format(name=name(rt)))
    return body, dict(card, **{CONTROLS: {"runtime": rt, "core": True}})


# ── the cards ─────────────────────────────────────────────────────────────────────────────────────

def menu_card(current: str) -> tuple:
    buttons = [ac.button(name(rt), "reply", style="primary" if rt == current else "secondary",
                         reply=f"Switch the core to {name(rt)}", command=ac.reply(KIND, TARGET, rt))
               for rt in ORDER]
    using = f"The core is running on {name(current)}."
    return ac.build(CARD_ID, "info", title="Core runtime", avatar_=ac.avatar(initial="R"),
                    text=f"{using} Pick a runtime to switch to; the core restarts on it after its current turn.",
                    buttons=buttons, note=CARRIES, note_tone="dim",
                    body=f"{using} Reply \"switch to Codex\" or \"switch to Claude Code\".")


def notice_card(state: str, status: str, words: str) -> tuple:
    return ac.build(NOTE_ID, state, title="Core runtime", avatar_=ac.avatar(initial="R"), status=status,
                    text=words, body=words)


def _core(ws: Path) -> dict:
    import pool_provision
    return pool_provision.core_lane(ws, None)


def _execute(ws: Path, lane: Optional[str]):
    import pool_provision
    return pool_provision.core_actions(ws, lane)[0].execute


def _owner_dm(ws: Path, core: dict) -> str:
    import connections
    return connections.owner_dm(core, ws)


def _post(ws: Path, room: str, body: str, card: dict, operation_id: str, *, reply_to: str = "", core=None,
          execute=None) -> dict:
    """Post `card` in `room`, which must be the owner's checked DM, and record it for its taps."""
    try:
        core = core if core is not None else _core(ws)
        lane = str(core.get("instance") or "")
        dm = _owner_dm(ws, core)
        execute = execute if execute is not None else _execute(ws, lane or None)
        eid = ac.send_card(execute, room, body, card, operation_id=operation_id, reply_to=reply_to, owner_dm=dm)
    except ac.CardError as e:
        return {"status": "failed", "why": str(e)}
    except Exception as e:  # noqa: BLE001 - no core lane or no credential: named, nothing posted
        return {"status": "failed", "why": f"{type(e).__name__}: {e}"}
    try:
        issued_cards.record(ws, eid, card, room=dm, lane=lane, handler=HANDLER)
    except OSError as e:
        return {"status": "posted", "event_id": eid, "why": f"its buttons will not act ({type(e).__name__})"}
    return {"status": "posted", "event_id": eid}


def card_words(words: str) -> str:
    """What an edited card says: the card is gone from the event, so the words name it."""
    return f"Core runtime: {words}"


def _edit(ws: Path, room: str, event: str, words: str, operation_id: str, *, core=None, execute=None) -> dict:
    """Show `words` in place of the card at `event` (the owner's checked DM only); {"status": "posted",
    "event_id": event, "edited": True}, or {"status": "failed", "why"} when it cannot be edited."""
    if not event:
        return {"status": "failed", "why": "no card to edit"}
    try:
        core = core if core is not None else _core(ws)
        lane = str(core.get("instance") or "")
        if room != _owner_dm(ws, core):
            return {"status": "failed", "why": "the card is not in the owner's DM"}
        execute = execute if execute is not None else _execute(ws, lane or None)
        answer = execute("room.message.edit", {"event_id": event, "body": card_words(words)},
                         operation_id=operation_id, room_id=room)
    except Exception as e:  # noqa: BLE001 - not edited: the caller posts the card instead
        return {"status": "failed", "why": f"{type(e).__name__}: {e}"}
    if not isinstance(answer, dict) or answer.get("is_error") or not ac._event_id(answer):
        code = str(answer.get("code") or answer.get("error") or "") if isinstance(answer, dict) else ""
        return {"status": "failed", "why": f"room.message.edit did not edit the card{f' ({code})' if code else ''}"}
    return {"status": "posted", "event_id": event, "edited": True}


def _answer(ws: Path, room: str, card_event: str, state: str, status: str, words: str, operation_id: str, *,
            reply_to: str = "", core=None, execute=None, tries: int = 1, sleep=time.sleep) -> dict:
    """Say `words` by editing `card_event` in place; only when that cannot be done (no card, or its last
    try refused) post them as a notice card. Up to `tries` tries, POST_PAUSE_S apart."""
    out = {"status": "failed"}
    for i in range(max(tries, 1)):
        if card_event:
            out = _edit(ws, room, card_event, words, operation_id + "-edit", core=core, execute=execute)
        if out["status"] != "posted" and (not card_event or i + 1 == tries):
            body, card = notice_card(state, status, words)
            out = _post(ws, room, body, card, operation_id, reply_to=reply_to, core=core, execute=execute)
        if out["status"] == "posted":
            break
        if i + 1 < tries:
            sleep(POST_PAUSE_S)
    return out


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def cmd_post(ws: Path, args, *, core=None, execute=None, now=time.time) -> int:
    try:
        body, card = menu_card(runtime())
    except ac.CardError as e:
        return _emit({"status": "failed", "why": str(e)}, 1)
    if args.print:
        return _emit({"body": body, "card": card, "handler": HANDLER})
    out = _post(ws, args.room, body, card, f"runtime-card:menu:{int(now())}", reply_to=args.reply_to or "",
                core=core, execute=execute)
    return _emit(out, 0 if out["status"] == "posted" else 1)


# ── a tap ─────────────────────────────────────────────────────────────────────────────────────────

def running(ws: Path, now: float) -> dict:
    rec = _read_json(ws.joinpath(*RUNNING))
    return rec if rec.get("attempt") and now - float(rec.get("ts") or 0) < RUNNING_STALE_S else {}


def executing(ws: Path) -> bool:
    """True while a `switch` holds the execution lock."""
    from file_lock import try_lock_fd, unlock_fd
    path = ws.joinpath(*EXEC_LOCK)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if try_lock_fd(fd):
            unlock_fd(fd)
            return False
        return True
    finally:
        os.close(fd)


def admit(ws: Path, rec: dict, now: float) -> bool:
    """Record `rec` as the one attempt under way; False when another is running."""
    from file_lock import locked_file
    from local_record import write_whole
    with locked_file(ws.joinpath(*RUNNING_LOCK), create_mode=0o600):
        if running(ws, now) or executing(ws):
            return False
        write_whole(ws.joinpath(*RUNNING), rec)
    return True


def is_recorded(ws: Path, rec: dict) -> bool:
    from file_lock import locked_file
    with locked_file(ws.joinpath(*RUNNING_LOCK), create_mode=0o600):
        return _read_json(ws.joinpath(*RUNNING)) == rec


def release(ws: Path, rec: dict) -> None:
    """Clear the record only while it is still exactly this attempt's."""
    from file_lock import locked_file
    with locked_file(ws.joinpath(*RUNNING_LOCK), create_mode=0o600):
        if _read_json(ws.joinpath(*RUNNING)) == rec:
            ws.joinpath(*RUNNING).unlink(missing_ok=True)


def detached_env() -> dict:
    """The switch's environment: without the old core's SUTANDO_CORE_RUNTIME, which would otherwise
    ride into every config read of the new launch."""
    env = dict(os.environ)
    env.pop("SUTANDO_CORE_RUNTIME", None)
    return env


def start_waiter(ws: Path, wait: dict, popen=subprocess.Popen) -> int:
    """Start `await-sign-in` in a session of its own, so it outlives this turn; its pid."""
    log = ws / "logs" / "runtime-card.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = ["nohup", sys.executable, str(Path(__file__).resolve()), "--workspace", str(ws), "await-sign-in",
            "--room", wait["room"], "--runtime", wait["runtime"], "--attempt", wait["attempt"]]
    with open(log, "ab") as fh:
        fh.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} waiting for the {wait['runtime']} sign-in "
                 f"({wait['attempt']})\n".encode())
        fh.flush()
        proc = popen(argv, cwd=str(_REPO), stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                     start_new_session=True, env=detached_env())
    return int(proc.pid)


FAILED_TO_START = "I couldn't start the switch. Nothing changed."


def say_switching(ws: Path, room: str, rt: str, current: str, attempt: str, event: str, *, core=None,
                  execute=None, new_card: bool = False, before_new_card=None) -> tuple:
    """Put "Switching…" on the card at `event`, or on a new card when that one cannot be edited:
    (words, what was posted). One operation id per attempt and route, so asking again returns the same
    card. `before_new_card` is called before the new card is posted, for a caller that puts the route on
    record: when it answers False the route is not on record and nothing is posted. `new_card` goes
    straight to that route, as a caller that finds it on record does."""
    say = (f"Switching the core from {name(current)} to {name(rt)}. It restarts once this turn has finished; "
           f"I'll confirm here when it is back. {CARRIES}")
    op, out = f"runtime-card:{attempt}:switching", {"status": "failed"}
    if event and not new_card:
        out = _edit(ws, room, event, say, op + "-edit", core=core or None, execute=execute)
    if out["status"] != "posted":
        if before_new_card is not None and before_new_card() is False:
            return say, {"status": "failed", "why": "the new card's route could not be recorded"}
        body, card = notice_card("pending", "Switching", say)
        out = _post(ws, room, body, card, op, reply_to=event, core=core or None, execute=execute)
        if out["status"] != "posted":
            out = dict(out, unknown=True)   # sent with no answer: it may be in the room
    return say, out


def begin_switch(ws: Path, room: str, rt: str, current: str, t: float, *, event: str = "", core=None,
                 execute=None, popen=subprocess.Popen, attempt: str = "", tell_failure: bool = True,
                 says_on=None, new_card: bool = False, before_new_card=None, hold_unknown: bool = False) -> dict:
    """Admit one switch to `rt`, say so in the room, and start it detached. What a tap on a signed-in
    runtime does, and what the sign-in waiter does once the sign-in is there. `event` is the card the
    tap answered: it is edited to say so, and the outcome edits it again. `attempt` is the id the caller
    already put on record for it (the waiter's hand-off); one is made otherwise. The waiter also passes
    `tell_failure` False: it records a failed launch itself before the card hears of it; and `says_on`,
    called with the card that says "Switching" before the worker is started, so it can put that on record."""
    attempt = attempt or f"{int(t)}-{secrets.token_hex(4)}"
    rec = {"attempt": attempt, "runtime": rt, "room": room, "ts": t}
    try:
        if not admit(ws, rec, t):
            return {"status": "say", "say": BUSY}
    except OSError as e:
        return {"status": "failed", "say": FAILED_TO_START, "why": type(e).__name__}
    say, posted = say_switching(ws, room, rt, current, attempt, event, core=core, execute=execute,
                                new_card=new_card, before_new_card=before_new_card)
    # The card that says "Switching" now, whether the tapped one edited in place or a new one posted
    # because it could not be: the worker's outcome and any correction go to that card.
    says = str(posted.get("event_id") or "")
    if says and says_on is not None:
        says_on(says)
    if hold_unknown and posted.get("unknown"):
        # A new card may be out with no id known for it. The attempt stays admitted and nothing is
        # started: the caller asks for that same post until it has the card.
        return {"status": "pending", "attempt": attempt, "runtime": rt, "say": say}
    try:
        pid = start_switch(ws, room, rt, attempt, popen, card=says)
    except OSError as e:
        release(ws, rec)
        out = {"status": "failed", "say": FAILED_TO_START, "why": f"{type(e).__name__}: {e}"}
        if says:
            out["event_id"] = says
        if tell_failure:    # the card just said "Switching": it must not go on saying so
            out["told"] = _answer(ws, room, says, "error", "Not switched", FAILED_TO_START,
                                  f"runtime-card:{attempt}:not-started", core=core or None, execute=execute)["status"]
        return out
    out = {"status": "started", "attempt": attempt, "pid": pid, "runtime": rt, "say": say}
    out.update({"event_id": posted["event_id"]} if posted.get("event_id") else {"card_why": posted.get("why", "")})
    return out


def set_wait(ws: Path, wait: dict) -> None:
    """Make `wait` the one sign-in waited for. Written under the lock its waiters claim it under."""
    from file_lock import locked_file
    from local_record import write_whole
    ws.joinpath(*WAITING).parent.mkdir(parents=True, exist_ok=True)
    with locked_file(ws.joinpath(*WAITING_LOCK), create_mode=0o600):
        write_whole(ws.joinpath(*WAITING), wait)


def keep_wait_card(ws: Path, mine: dict, event: str = "", **noted) -> None:
    """Keep `event` (the card the waiter is to edit) and anything else `noted` with the wait still `mine`."""
    from file_lock import locked_file
    from local_record import write_whole
    with locked_file(ws.joinpath(*WAITING_LOCK), create_mode=0o600):
        rec = _read_json(ws.joinpath(*WAITING))
        if {k: rec.get(k) for k in mine} == mine:
            write_whole(ws.joinpath(*WAITING), dict(rec, **({"card": event} if event else {}), **noted))


def claim_wait(ws: Path, mine: Optional[dict], phase: str = "", **noted) -> bool:
    """Take the wait and say whether it was `mine` ({attempt, runtime, room}), in one step under the lock:
    a waiter acts only on a True, so one a newer tap replaced can neither switch nor speak, and never
    removes its replacement. `mine` None ends whatever wait there is (a tap that goes ahead).

    With `phase` the record is not removed but marked with what the waiter is about to do ("switching",
    "expired", "already"): a waiter stopped before it finished leaves that on record, and the one resumed
    for it carries the step out. `finish_wait` removes it once the step is done. `noted` is kept with
    the mark (the id of the switch about to be admitted)."""
    from file_lock import locked_file
    from local_record import write_whole
    ws.joinpath(*WAITING).parent.mkdir(parents=True, exist_ok=True)
    with locked_file(ws.joinpath(*WAITING_LOCK), create_mode=0o600):
        rec = _read_json(ws.joinpath(*WAITING))
        if mine is not None and {k: rec.get(k) for k in mine} != mine:
            return False
        if phase and mine is not None:
            write_whole(ws.joinpath(*WAITING), dict(rec, phase=phase, **noted))
        else:
            ws.joinpath(*WAITING).unlink(missing_ok=True)
        return True


def finish_wait(ws: Path, mine: dict) -> None:
    """The claimed step is done (the switch admitted, or the card's last words recorded): the wait goes."""
    try:
        claim_wait(ws, mine)
    except OSError:
        pass    # left on record: a resumed waiter repeats the step, which is safe to repeat


_ATTEMPT = re.compile(r"^[0-9A-Za-z][0-9A-Za-z-]{0,63}$")     # names a file: no dot, no slash


def _lease_path(ws: Path, attempt: str) -> Path:
    return ws / "state" / f"runtime-card-signin.{attempt}.alive"


@contextlib.contextmanager
def waiter_lease(ws: Path, attempt: str):
    """A waiter's proof of life: a lock on its attempt's own file, held until it exits however it exits
    (the lock dies with the process). Yields False when another waiter for this attempt holds it."""
    from file_lock import try_lock_fd, unlock_fd
    path = _lease_path(ws, attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if not try_lock_fd(fd):
            yield False
            return
        try:
            yield True
        finally:
            path.unlink(missing_ok=True)
            unlock_fd(fd)
    finally:
        os.close(fd)


def waiter_alive(ws: Path, attempt: str) -> bool:
    """Whether a waiter for `attempt` is running: its lease file is there and locked."""
    from file_lock import try_lock_fd, unlock_fd
    try:
        fd = os.open(_lease_path(ws, attempt), os.O_RDWR)
    except OSError:
        return False
    try:
        if try_lock_fd(fd):
            unlock_fd(fd)
            return False
        return True
    finally:
        os.close(fd)


def resume_wait(ws: Path, popen=subprocess.Popen) -> dict:
    """Start a waiter again for a sign-in still waited for whose waiter is gone (a restart that took it
    down with the core, a reboot). Past its deadline too: that waiter says it stopped waiting, in place,
    so the card never goes on promising a switch nobody is watching for."""
    rec = _read_json(ws.joinpath(*WAITING))
    attempt = str(rec.get("attempt") or "")
    if not _ATTEMPT.match(attempt) or rec.get("runtime") not in NAMES or not rec.get("room"):
        return {"status": "none"}
    if waiter_alive(ws, attempt):
        return {"status": "waiting", "attempt": attempt}
    try:
        return {"status": "resumed", "attempt": attempt, "pid": start_waiter(ws, rec, popen)}
    except OSError as e:
        return {"status": "failed", "attempt": attempt, "why": type(e).__name__}


def cmd_await(ws: Path, args, *, core=None, execute=None, popen=subprocess.Popen, run=subprocess.run,
              codex_read=None, sleep=time.sleep, clock=time.time) -> int:
    """One waiter per attempt (waiter_lease): a second one started for it stops at once."""
    if not _ATTEMPT.match(args.attempt):
        return _emit({"status": "refused", "why": "not an attempt id"}, 2)
    with waiter_lease(ws, args.attempt) as mine:
        if not mine:
            return _emit({"status": "duplicate", "runtime": args.runtime})
        return _await(ws, args, core=core, execute=execute, popen=popen, run=run, codex_read=codex_read,
                      sleep=sleep, clock=clock)


def _await(ws: Path, args, *, core=None, execute=None, popen=subprocess.Popen, run=subprocess.run,
           codex_read=None, sleep=time.sleep, clock=time.time) -> int:
    """Wait for the sign-in a "Sign in first" card asked for, then switch as the tap wanted. It acts only
    after claiming the wait as its own (claim_wait), which it does after the sign-in probe, not before:
    one a newer tap replaced while it was probing stops without a word. It says so in the room when the
    sign-in did not come by the wait's own deadline (its saved `ts` + SIGN_IN_WAIT_S), and a sign-in
    seen only after that deadline switches nothing. It never signs anyone in."""
    mine = {"attempt": args.attempt, "runtime": args.runtime, "room": args.room}
    superseded = {"status": "superseded", "runtime": args.runtime}

    def waited() -> Optional[dict]:
        rec = _read_json(ws.joinpath(*WAITING))
        return rec if {k: rec.get(k) for k in mine} == mine else None

    def note_card(event: str) -> None:
        """The card that says "Switching" goes on record with the wait, for whoever resumes it."""
        try:
            keep_wait_card(ws, mine, event, says=event)
        except OSError:
            pass

    def note_new_card_route() -> bool:
        """"Switching" is about to go out as a new card: on record first, so a resume asks for that card.
        False when it could not be recorded, and then the card is not posted."""
        try:
            keep_wait_card(ws, mine, route="post")
        except OSError:
            return False
        return _read_json(ws.joinpath(*WAITING)).get("route") == "post"

    def refuse(card_event: str, words: str, out: dict) -> int:
        """The switch is not happening, in `words`, which are already on record with the `refused` phase:
        deliver exactly those, under one operation id, and remove the wait on a delivery receipt."""
        out["told"] = _answer(ws, args.room, card_event, "error", "Not switched", words,
                              f"runtime-card:signin:{args.attempt}:not-switched", core=core, execute=execute,
                              tries=POST_TRIES, sleep=sleep)["status"]
        if out["told"] != "posted":
            return _emit(dict(out, kept="refused"), 1)
        finish_wait(ws, mine)
        return _emit(out, 1)

    def conclude(phase: str, card_event: str, switch: str, resumed: bool = False, words: str = "",
                 new_card: bool = False, says: str = "") -> int:
        """Carry out a claimed step and remove the wait only once it is done: the switch's worker
        launched, or the card's last words delivered. Each is safe to do twice: the card edits carry the
        attempt's own operation ids, and a second worker for one attempt is refused by its execution lock."""
        if phase == "already":
            out = {"status": "already", "runtime": args.runtime}
            if card_event:
                # Switched some other way (the desktop, say): the "Sign in first" card says so in place.
                words = f"The core is now running on {name(args.runtime)}. {CARRIES}"
                for i in range(POST_TRIES):
                    out["edited"] = _edit(ws, args.room, card_event, words, f"runtime-card:signin:{args.attempt}:already-edit",
                                          core=core, execute=execute)["status"]
                    if out["edited"] == "posted" or i + 1 == POST_TRIES:
                        break
                    sleep(POST_PAUSE_S)
                if out["edited"] != "posted":
                    return _emit(dict(out, kept="already"), 1)      # not said yet: the phase stays for the next waiter
            finish_wait(ws, mine)
            return _emit(out)
        if phase == "refused":      # decided by a waiter before this one: only the delivery is left
            return refuse(card_event, words, {"status": "refused", "runtime": args.runtime, "say": words,
                                              "after_sign_in": True})
        if phase == "expired":
            words = SIGN_IN_GAVE_UP.format(name=name(args.runtime), minutes=SIGN_IN_WAIT_S // 60, was=name(runtime()))
            posted = _answer(ws, args.room, card_event, "error", "Not switched", words,
                             f"runtime-card:signin:{args.attempt}:gave-up", core=core, execute=execute,
                             tries=POST_TRIES, sleep=sleep)
            out = {"status": "gave_up", "runtime": args.runtime, "say": words, "posted": posted["status"]}
            if posted["status"] != "posted":
                return _emit(dict(out, kept="expired"), 1)
            finish_wait(ws, mine)
            return _emit(out, 1)
        if resumed:
            admitted = _read_json(ws.joinpath(*RUNNING)).get("attempt") == switch
            if admitted and executing(ws):
                finish_wait(ws, mine)       # its worker is running
                return _emit({"status": "handed_off", "runtime": args.runtime, "after_sign_in": True})
            if admitted:
                # Admitted, and no worker holds the execution lock: it was never launched, or died. Ask for
                # "Switching" again under the attempt's id, which returns the card that already says it.
                if says:
                    card_event = says       # the card that says it is on record: used as it is, nothing re-sent
                for tried in range(0 if says else CARD_TRIES):
                    _, posted = say_switching(ws, args.room, args.runtime, runtime(), switch, card_event, core=core,
                                              execute=execute, new_card=new_card,
                                              before_new_card=note_new_card_route)
                    if posted.get("event_id"):
                        card_event = str(posted["event_id"])
                        note_card(card_event)
                        break
                    new_card = new_card or bool(posted.get("unknown"))
                    if not new_card:
                        break               # nothing was posted: the worker speaks for itself
                    if tried + 1 == CARD_TRIES:
                        # A new card may be out there with no id known: no worker is started on a guess.
                        return _emit({"status": "failed", "runtime": args.runtime, "kept": "switching",
                                      "why": "the card that says Switching is not confirmed yet"}, 1)
                    sleep(POST_PAUSE_S)
                try:
                    pid = start_switch(ws, args.room, args.runtime, switch, popen, card=card_event)
                except OSError as e:
                    return _emit({"status": "failed", "runtime": args.runtime, "why": f"{type(e).__name__}: {e}",
                                  "kept": "switching"}, 1)
                finish_wait(ws, mine)
                return _emit({"status": "started", "attempt": switch, "pid": pid, "runtime": args.runtime,
                              "after_sign_in": True, "relaunched": True})
            if runtime() == args.runtime:
                finish_wait(ws, mine)       # not admitted any more because its worker ran and finished
                return _emit({"status": "handed_off", "runtime": args.runtime, "after_sign_in": True})
        out = begin_switch(ws, args.room, args.runtime, runtime(), clock(), event=card_event, core=core,
                           execute=execute, popen=popen, attempt=switch, tell_failure=False, says_on=note_card,
                           new_card=new_card, before_new_card=note_new_card_route, hold_unknown=True)
        if out["status"] == "pending":
            # Admitted, the route on record, the card not known yet: go on as a resumed waiter would.
            return conclude("switching", card_event, switch, resumed=True, new_card=True)
        out["after_sign_in"] = True
        if out["status"] == "started":
            finish_wait(ws, mine)
            return _emit(out)
        # Not started: the outcome, its words and the card that says "Switching" now (a new one when the
        # original could not be edited) go on record first; a resumed waiter only repeats that edit.
        target = str(out.get("event_id") or card_event)
        if not claim_wait(ws, mine, "refused", words=out["say"], card=target):
            return _emit(superseded)
        return refuse(target, out["say"], out)

    while True:
        rec = waited()
        if rec is None:
            return _emit(superseded)
        deadline = float(rec.get("ts") or 0) + SIGN_IN_WAIT_S
        card_event = str(rec.get("card") or "")
        if rec.get("phase") in ("already", "expired", "switching", "refused"):
            # Claimed by a waiter that was stopped before it finished.
            return conclude(rec["phase"], card_event, str(rec.get("switch") or ""), resumed=True,
                            words=str(rec.get("words") or ""), new_card=rec.get("route") == "post",
                            says=str(rec.get("says") or ""))
        if runtime() == args.runtime:
            return conclude("already", card_event, "") if claim_wait(ws, mine, "already") else _emit(superseded)
        there = signed_in(args.runtime, run=run, codex_read=codex_read) is True     # may take a while
        if there and clock() < deadline:
            # The switch's id goes on record with the claim, so its admission can be told from another's.
            switch = f"{int(clock())}-{secrets.token_hex(4)}"
            claimed = claim_wait(ws, mine, "switching", switch=switch)
            return conclude("switching", card_event, switch) if claimed else _emit(superseded)
        if clock() >= deadline:
            return conclude("expired", card_event, "") if claim_wait(ws, mine, "expired") else _emit(superseded)
        sleep(SIGN_IN_POLL_S)


def start_switch(ws: Path, room: str, rt: str, attempt: str, popen=subprocess.Popen, card: str = "") -> int:
    """Start `switch` in a session of its own, so it outlives the core's restart; its pid. `card` is the
    event its outcome edits in place ("" posts the outcome as a card)."""
    log = ws / "logs" / "runtime-card.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = ["nohup", sys.executable, str(Path(__file__).resolve()), "--workspace", str(ws), "switch",
            "--room", room, "--runtime", rt, "--attempt", attempt] + (["--card", card] if card else [])
    with open(log, "ab") as fh:
        fh.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} runtime switch to {rt} ({attempt})\n".encode())
        fh.flush()
        proc = popen(argv, cwd=str(_REPO), stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                     start_new_session=True, env=detached_env())
    return int(proc.pid)


def cmd_reply(ws: Path, args, *, core=None, execute=None, popen=subprocess.Popen, now=time.time,
              run=subprocess.run, codex_read=None) -> int:
    try:
        headers, said = ac.reply_words(Path(args.task_file).read_text(encoding="utf-8"))
    except OSError as e:
        return _emit({"status": "refused", "why": f"the task file is unreadable ({type(e).__name__})"}, 2)
    try:
        core = core if core is not None else _core(ws)
    except Exception:  # noqa: BLE001 - a card records its own lane; only one without it needs the core's owner
        core = {}
    verdict = ac.click_verdict(headers, said, ws, ac.lane_owner(ws, str(core.get("owner_mxid") or "")))
    other = ac.click_answer(verdict, said, (KIND,))
    if other is not None:
        return _emit(other, 1)
    rt, room = verdict.command.verb, str(headers.get("channel_id") or "")
    event = str(headers.get("reply_to_event") or "")
    if verdict.command.target != TARGET or rt not in NAMES:
        return _emit({"status": "refused", "say": GONE}, 2)
    current = runtime()
    if rt == current:
        return _emit({"status": "say", "say": f"The core is already running on {name(rt)}. Nothing changed."})
    state = signed_in(rt, run=run, codex_read=codex_read)
    if state is not True:
        t = now()
        wait = {"attempt": f"{int(t)}-{secrets.token_hex(4)}", "runtime": rt, "room": room, "ts": t}
        mine = {k: wait[k] for k in ("attempt", "runtime", "room")}
        out = {"status": "needs_sign_in", "runtime": rt}
        # The waiter first, the card after: the card promises the automatic switch only once a waiter is
        # running. The newest tap is the one waited for; an earlier waiter finds it is not its and stops.
        try:
            set_wait(ws, wait)
            out.update(waiting=wait["attempt"], pid=start_waiter(ws, wait, popen))
        except OSError as e:
            claim_wait(ws, mine)        # only this attempt's record; a newer tap's stays
            out.update(waiting="", why=f"not waiting for the sign-in ({type(e).__name__}); ask again after it")
        waiting = bool(out["waiting"])
        body, card = sign_in_card(rt, state, waiting)
        posted = _post(ws, room, body, card, f"runtime-card:signin:{wait['attempt']}", reply_to=event,
                       core=core or None, execute=execute)
        if waiting and posted.get("event_id"):
            try:
                keep_wait_card(ws, mine, posted["event_id"])
            except OSError:
                pass  # the waiter then posts its answer as a card of its own
        out.update(say=sign_in_words(rt, state, waiting),
                   **({"event_id": posted["event_id"]} if posted.get("event_id") else {}))
        return _emit(out, 1)
    out = begin_switch(ws, room, rt, current, now(), event=event, core=core, execute=execute, popen=popen)
    if out["status"] == "started":
        # Only a tap whose own switch was admitted and started ends a wait an earlier tap left: one
        # told it is busy leaves it, since the switch under way may be that wait's own hand-off.
        try:
            claim_wait(ws, None)
        except OSError:
            pass
    return _emit(out, 1 if out["status"] == "failed" else 0)


# ── the detached switch ───────────────────────────────────────────────────────────────────────────

def restart_core(rt: str, run=subprocess.run) -> int:
    """graceful-restart.sh's exit code for a restart onto `rt`; it waits for the core's turn to end.
    This process has a session of its own, outside the core's, which the in-session guard allows."""
    env = dict(detached_env(), SUTANDO_ALLOW_INSESSION_RESTART="1")
    try:
        return run(["bash", str(RESTART), "--", "--runtime", rt], cwd=str(_REPO), env=env,
                   stdin=subprocess.DEVNULL, timeout=RESTART_TIMEOUT_S).returncode
    except subprocess.TimeoutExpired:
        return 124
    except OSError:
        return 127


def restart_lock_clears(ws: Path, *, sleep=time.sleep, clock=time.time) -> Optional[str]:
    """Wait until graceful-restart.sh's lock is gone (the other restart ended), DEFER_WAIT_S at most.
    The id of the restart that held it ("" when it was gone before it could be read); None when it is
    still held at the end, and then nothing is asked again."""
    lock = ws.joinpath(*RESTART_LOCK)
    deadline, rid = clock() + DEFER_WAIT_S, ""
    while lock.exists():
        try:
            rid = lock.joinpath("rid").read_text(encoding="utf-8").strip() or rid
        except OSError:
            pass
        if clock() >= deadline:
            return None
        sleep(DEFER_POLL_S)
    return rid


def restart_left(ws: Path, rid: str) -> str:
    """What the restart that was in the way came to, from the result its launcher leaves under its id:
    the runtime it confirmed and published, or "" for anything less (no result, another restart's, a
    publication that failed). With no id read off the lock nothing identifies its result, and none is
    taken for it: a time near this switch is not an identity."""
    res = _read_json(ws.joinpath(*RESTART_RESULT))
    if not rid or res.get("rid") != rid or res.get("published") is not True or res.get("runtime") not in NAMES:
        return ""
    return str(res["runtime"])


def restore_desktop_gateway(run=subprocess.run) -> str:
    """After a restart on a desktop-hosted engine, have the desktop recreate its gateway window: "" where
    no desktop hosts the engine, "restored", or why it was not (its control script missing included).
    Without it the restarted core is deaf."""
    if not desktop_hosted():
        return ""
    if not DESKTOP_CTL.is_file():
        return "the desktop's control script is missing"
    try:
        rc = run(["bash", str(DESKTOP_CTL), "restart-gateway"], cwd=str(DESKTOP_CTL.parent), env=detached_env(),
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                 timeout=GATEWAY_TIMEOUT_S).returncode
    except subprocess.TimeoutExpired:
        return "timed out"
    except OSError as e:
        return type(e).__name__
    return "restored" if rc == 0 else f"exit {rc}"


def check(ws: Path, rt: str, since: float) -> dict:
    """{core, selected, record, configured}: whether the confirmed record names `rt` with a session
    created after `since` (tmux stamps whole seconds), and whether the launcher's next pick is `rt`."""
    rec = confirmed_record(ws)
    core = rec.get("runtime") == rt and float(rec.get("started_at") or 0) >= int(since)
    configured = configured_runtime()
    return {"core": core, "selected": configured == rt, "record": rec, "configured": configured}


def confirm(ws: Path, rt: str, since: float, *, sleep=time.sleep, clock=time.time) -> dict:
    """`check` until both hold or CONFIRM_TIMEOUT_S passes; the last result."""
    deadline = clock() + CONFIRM_TIMEOUT_S
    while True:
        got = check(ws, rt, since)
        if (got["core"] and got["selected"]) or clock() >= deadline:
            return got
        sleep(CONFIRM_POLL_S)


def outcome(rt: str, was: str, got: dict, rc: int = 0) -> tuple:
    """(state, words) of a switch from `was` to `rt` from the confirmation `got`."""
    if got["core"] and got["selected"]:
        return "done", f"Switched. The core is now running on {name(rt)}. {CARRIES}"
    if got["core"]:
        if not desktop_hosted():
            return "info", (f"Switched. The core is now running on {name(rt)}, but this computer's config "
                            f"still names {name(got['configured'])}, so a full relaunch goes back to it.")
        return "info", (f"The core is now running on {name(rt)}, but the desktop still selects "
                        f"{name(got['configured'])}, so it would go back on its next relaunch. Choose "
                        f"{name(rt)} in Settings → Connections → the Sutando (on this computer) row.")
    lead = (f"The restart did not finish cleanly (exit {rc}), and" if rc else "I asked the core to restart, but")
    rec = got["record"]
    seen = (f"the last confirmed core runs {name(rec['runtime'])}" if rec else "no core has been confirmed since")
    return "error", (f"{lead} I could not confirm it is running on {name(rt)}: {seen}. "
                     "Ask me for the runtime card to check.")


def settle_and_tell(ws: Path, room: str, rt: str, was: str, since: float, attempt: str, *, rc: int = 0,
                    card: str = "", core=None, execute=None, sleep=time.sleep, clock=time.time,
                    gateway: str = "") -> dict:
    got = confirm(ws, rt, since, sleep=sleep, clock=clock)
    state, words = outcome(rt, was, got, rc)
    if gateway not in ("", "restored"):
        state, words = ("info" if state == "done" else state), f"{words} {GATEWAY_LOST.format(why=gateway)}"
    status = {"done": "Switched", "info": "Switched"}.get(state, "Not confirmed")
    out = _answer(ws, room, card, state, status, words, f"runtime-card:{attempt}:outcome", core=core,
                  execute=execute, tries=POST_TRIES, sleep=sleep)
    return {**out, "outcome": state, "say": words, "core_confirmed": got["core"],
            "selection_confirmed": got["selected"]}


def cmd_switch(ws: Path, args, *, core=None, execute=None, restart=restart_core, sleep=time.sleep,
               clock=time.time, run=subprocess.run) -> int:
    from file_lock import try_lock_fd, unlock_fd
    rec = _read_json(ws.joinpath(*RUNNING))
    if rec.get("attempt") != args.attempt or rec.get("runtime") != args.runtime or rec.get("room") != args.room \
            or not is_recorded(ws, rec):
        return _emit({"status": "refused", "why": "that attempt is not the one under way"}, 2)
    lock = ws.joinpath(*EXEC_LOCK)
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if not try_lock_fd(fd):
            return _emit({"status": "refused", "why": "another switch is running"}, 2)
        try:
            if not is_recorded(ws, rec):
                return _emit({"status": "refused", "why": "that attempt is not the one under way"}, 2)
            was = configured_runtime()
            started = clock()
            rc = restart(args.runtime)
            restarted = rc not in NOT_RESTARTED
            waited = False
            if rc == DEFERRED:
                # Not a failure yet: say so on the card, wait for the other restart, and ask once more.
                waited = True
                _answer(ws, args.room, args.card, "pending", "Waiting",
                        f"Waiting for another restart to finish, then I'll switch to {name(args.runtime)}. "
                        f"The core is still on {name(was)}.",
                        f"runtime-card:{args.attempt}:waiting", core=core, execute=execute)
                held_by = restart_lock_clears(ws, sleep=sleep, clock=clock)
                if held_by is not None:
                    # Only what that very restart published counts: an older record says nothing about
                    # the core it left, and one it could not publish is not known either.
                    left, now_on = restart_left(ws, held_by), confirmed_record(ws)
                    if left == args.runtime and now_on.get("runtime") == args.runtime:
                        rc, started = 0, float(now_on["started_at"])    # already there: nothing to restart
                    elif left and left != args.runtime:
                        was, started = configured_runtime(), clock()    # confirmed on another runtime
                        rc = restart(args.runtime)
                        restarted = rc not in NOT_RESTARTED
                    else:
                        words = (f"Another restart finished, but I could not read which runtime the core is on "
                                 f"now, so I did not restart it again for {name(args.runtime)}. "
                                 "Ask me for the runtime card to check.")
                        out = _answer(ws, args.room, args.card, "error", "Not confirmed", words,
                                      f"runtime-card:{args.attempt}:outcome", core=core, execute=execute,
                                      tries=POST_TRIES, sleep=sleep)
                        return _emit({**out, "outcome": "error", "say": words, "restart": "not-restarted",
                                      "rc": rc, "why": "inconclusive"}, 1)
            if rc in NOT_RESTARTED:
                reason = RESTART_REASONS[rc]
                words = (f"I could not switch to {name(args.runtime)}. The core is still on {name(was)}. "
                         f"{reason[:1].upper()}{reason[1:]}.")
                if waited and rc == DEFERRED:
                    words = (f"Still busy: another restart was still in the way after I waited, so I did not "
                             f"switch to {name(args.runtime)}. The core is still on {name(was)}. "
                             "Ask me for the runtime card to try again.")
                out = _answer(ws, args.room, args.card, "error", "Not switched", words,
                              f"runtime-card:{args.attempt}:outcome", core=core, execute=execute, tries=POST_TRIES,
                              sleep=sleep)
                return _emit({**out, "outcome": "error", "say": words, "restart": "not-restarted", "rc": rc}, 1)
            # This switch's own restart replaced the session: the desktop's gateway window went with it.
            gateway = restore_desktop_gateway(run) if restarted else ""
            told = settle_and_tell(ws, args.room, args.runtime, was, started, args.attempt, rc=rc, card=args.card,
                                   core=core, execute=execute, sleep=sleep, clock=clock, gateway=gateway)
            told = dict(told, **({"gateway": gateway} if gateway else {}))
        finally:
            release(ws, rec)
            unlock_fd(fd)
    finally:
        os.close(fd)
    return _emit({**told, "restart": "requested", "rc": rc}, 0 if told["outcome"] == "done" else 1)


def main(argv=None, *, core=None, execute=None, popen=subprocess.Popen, run=subprocess.run, codex_read=None,
         restart=restart_core, sleep=time.sleep, clock=time.time) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workspace", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("post")
    s.add_argument("--room", required=True)
    s.add_argument("--reply-to", default="")
    s.add_argument("--print", action="store_true")
    r = sub.add_parser("reply")
    r.add_argument("--task-file", required=True)
    w = sub.add_parser("switch")
    w.add_argument("--room", required=True)
    w.add_argument("--runtime", required=True, choices=ORDER)
    w.add_argument("--attempt", required=True)
    w.add_argument("--card", default="")
    a = sub.add_parser("await-sign-in")
    a.add_argument("--room", required=True)
    a.add_argument("--runtime", required=True, choices=ORDER)
    a.add_argument("--attempt", required=True)
    t = sub.add_parser("settle")
    t.add_argument("--room", required=True)
    t.add_argument("--runtime", required=True, choices=ORDER)
    t.add_argument("--since", required=True, type=float)
    t.add_argument("--attempt", default="")
    t.add_argument("--card", default="")
    sub.add_parser("status")
    sub.add_parser("resume")
    args = p.parse_args(argv)
    if args.workspace:
        ws = Path(args.workspace)
    else:
        from workspace_default import resolve_workspace
        ws = Path(resolve_workspace())
    if args.cmd == "resume":
        return _emit(resume_wait(ws, popen))
    if args.cmd in ("post", "status"):
        try:
            resume_wait(ws, popen)      # a waiter a restart took down is back before the card is looked at
        except Exception:  # noqa: BLE001 - never in the way of the card itself
            pass
    if args.cmd == "post":
        return cmd_post(ws, args, core=core, execute=execute)
    if args.cmd == "reply":
        return cmd_reply(ws, args, core=core, execute=execute, popen=popen, run=run, codex_read=codex_read, now=clock)
    if args.cmd == "switch":
        return cmd_switch(ws, args, core=core, execute=execute, restart=restart, sleep=sleep, clock=clock, run=run)
    if args.cmd == "await-sign-in":
        return cmd_await(ws, args, core=core, execute=execute, popen=popen, run=run, codex_read=codex_read,
                         sleep=sleep, clock=clock)
    if args.cmd == "settle":
        attempt = args.attempt or f"settle-{int(args.since)}"
        told = settle_and_tell(ws, args.room, args.runtime, "", args.since, attempt, card=args.card, core=core,
                               execute=execute, sleep=sleep, clock=clock)
        return _emit(told, 0 if told["outcome"] == "done" else 1)
    rec = confirmed_record(ws)
    return _emit({"runtime": runtime(), "next_start": configured_runtime(), "confirmed_core": rec,
                  "desktop": desktop_hosted()})


if __name__ == "__main__":
    sys.exit(main())
