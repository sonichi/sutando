#!/usr/bin/env python3
"""A card in the owner's DM that switches the core between Codex and Claude Code, and confirms it.

    python3 runtime_card.py [--workspace WS] post --room ROOM [--reply-to EVENT] [--print]
    python3 runtime_card.py [--workspace WS] reply --task-file PATH
    python3 runtime_card.py [--workspace WS] switch --room ROOM --runtime RT --attempt ID
    python3 runtime_card.py [--workspace WS] settle --room ROOM --runtime RT --since EPOCH [--attempt ID]
    python3 runtime_card.py [--workspace WS] status

`post` sends the menu to the owner's checked DM and records it (src/issued_cards.py), so only the
owner's tap on it, in that DM, acts (agent_card.click_verdict). It has two buttons, "Codex" and
"Claude Code"; the runtime in use (src/sutando_config.py resolve_core_runtime, as the launcher
resolves it) is the primary one and named in the text.

A tap on the other runtime first checks that runtime is signed in on this computer, before anything
is stopped: Claude Code by `claude auth status --json` under the core's config dir, Codex by
src/codex_account.py on the core's Codex home (the account card's probes,
skills/account-switch/scripts/account_card.py). Not signed in, or not readable: it answers "Sign in to
<runtime> first", pointing to the desktop's Settings, and restarts nothing. Signed in: it answers
"switching" at once and starts `switch` detached, in a session of its own, because the core restarts.

`switch` runs src/agent/graceful-restart.sh -- --runtime <rt> (it waits for the core's current turn to
end, then `start-cli.sh --restart --runtime <rt>`). The launcher writes the desktop's selection
(runtime.json coreRuntime) and `<workspace>/state/core-runtime.json` only once the new core's panes
run that CLI. `settle` then confirms from both, never from a heartbeat alone: the record names the
target runtime with a session created after the switch began, and the runtime the launcher would
pick next (desktop selection, else config) is the target too. It posts the outcome in the same DM.
The workspace, tasks, results, state and memory are shared by both runtimes; only the live
conversation does not carry over, and the cards say so.

One switch runs at a time (a lock and a record, as the account card). Every command prints one JSON
object. Exit 0 done, 1 a negative answer, 2 refused. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
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
CONFIRM_TIMEOUT_S, CONFIRM_POLL_S = 180, 5
RUNNING_STALE_S = RESTART_TIMEOUT_S + CONFIRM_TIMEOUT_S + 600
# graceful-restart.sh exits 3/4/5 before it stops the core; any other failure may have restarted it.
NOT_RESTARTED = (3, 4, 5)
RESTART_REASONS = {3: "the core could not get ready to restart",
                   4: "another restart was under way or the core was busy, so the restart was put off",
                   5: "the restart was only rehearsed"}
POST_TRIES, POST_PAUSE_S = 4, 15
SETTINGS = ("Open the desktop app, go to Settings → Connections → the Sutando (on this computer) row, and "
            "sign in to {name} there. Then ask me to switch again.")
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


def sign_in_words(rt: str, state: Optional[bool]) -> str:
    why = (f"{name(rt)} is not signed in on this computer" if state is False
           else f"I could not confirm {name(rt)} is signed in on this computer")
    return f"Sign in to {name(rt)} first. {why}, so I did not restart the core. " + SETTINGS.format(name=name(rt))


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


def start_switch(ws: Path, room: str, rt: str, attempt: str, popen=subprocess.Popen) -> int:
    """Start `switch` in a session of its own, so it outlives the core's restart; its pid."""
    log = ws / "logs" / "runtime-card.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = ["nohup", sys.executable, str(Path(__file__).resolve()), "--workspace", str(ws), "switch",
            "--room", room, "--runtime", rt, "--attempt", attempt]
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
        words = sign_in_words(rt, state)
        body, card = notice_card("error", "Sign in first", words)
        posted = _post(ws, room, body, card, f"runtime-card:signin:{rt}:{int(now())}", reply_to=event,
                       core=core or None, execute=execute)
        return _emit({"status": "needs_sign_in", "runtime": rt, "say": words,
                      **({"event_id": posted["event_id"]} if posted.get("event_id") else {})}, 1)
    t = now()
    attempt = f"{int(t)}-{secrets.token_hex(4)}"
    rec = {"attempt": attempt, "runtime": rt, "room": room, "ts": t}
    try:
        if not admit(ws, rec, t):
            return _emit({"status": "say", "say": BUSY})
    except OSError as e:
        return _emit({"status": "failed", "say": "I couldn't start the switch. Nothing changed.",
                      "why": type(e).__name__}, 1)
    say = (f"Switching the core from {name(current)} to {name(rt)}. It restarts once this turn has finished; "
           f"I'll confirm here when it is back. {CARRIES}")
    body, card = notice_card("pending", "Switching", say)
    posted = _post(ws, room, body, card, f"runtime-card:{attempt}:switching", reply_to=event, core=core or None,
                   execute=execute)
    try:
        pid = start_switch(ws, room, rt, attempt, popen)
    except OSError as e:
        release(ws, rec)
        return _emit({"status": "failed", "say": "I couldn't start the switch. Nothing changed.",
                      "why": f"{type(e).__name__}: {e}"}, 1)
    out = {"status": "started", "attempt": attempt, "pid": pid, "runtime": rt, "say": say}
    out.update({"event_id": posted["event_id"]} if posted.get("event_id") else {"card_why": posted.get("why", "")})
    return _emit(out)


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
                    core=None, execute=None, sleep=time.sleep, clock=time.time) -> dict:
    got = confirm(ws, rt, since, sleep=sleep, clock=clock)
    state, words = outcome(rt, was, got, rc)
    status = {"done": "Switched", "info": "Switched"}.get(state, "Not confirmed")
    body, card = notice_card(state, status, words)
    out = {"status": "failed"}
    for i in range(POST_TRIES):
        out = _post(ws, room, body, card, f"runtime-card:{attempt}:outcome", core=core, execute=execute)
        if out["status"] == "posted":
            break
        if i + 1 < POST_TRIES:
            sleep(POST_PAUSE_S)
    return {**out, "outcome": state, "say": words, "core_confirmed": got["core"],
            "selection_confirmed": got["selected"]}


def cmd_switch(ws: Path, args, *, core=None, execute=None, restart=restart_core, sleep=time.sleep,
               clock=time.time) -> int:
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
            if rc in NOT_RESTARTED:
                reason = RESTART_REASONS[rc]
                words = (f"I could not switch to {name(args.runtime)}. The core is still on {name(was)}. "
                         f"{reason[:1].upper()}{reason[1:]}.")
                body, card = notice_card("error", "Not switched", words)
                out = {"status": "failed"}
                for i in range(POST_TRIES):
                    out = _post(ws, args.room, body, card, f"runtime-card:{args.attempt}:outcome", core=core,
                                execute=execute)
                    if out["status"] == "posted":
                        break
                    if i + 1 < POST_TRIES:
                        sleep(POST_PAUSE_S)
                return _emit({**out, "outcome": "error", "say": words, "restart": "not-restarted", "rc": rc}, 1)
            told = settle_and_tell(ws, args.room, args.runtime, was, started, args.attempt, rc=rc, core=core,
                                   execute=execute, sleep=sleep, clock=clock)
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
    t = sub.add_parser("settle")
    t.add_argument("--room", required=True)
    t.add_argument("--runtime", required=True, choices=ORDER)
    t.add_argument("--since", required=True, type=float)
    t.add_argument("--attempt", default="")
    sub.add_parser("status")
    args = p.parse_args(argv)
    if args.workspace:
        ws = Path(args.workspace)
    else:
        from workspace_default import resolve_workspace
        ws = Path(resolve_workspace())
    if args.cmd == "post":
        return cmd_post(ws, args, core=core, execute=execute)
    if args.cmd == "reply":
        return cmd_reply(ws, args, core=core, execute=execute, popen=popen, run=run, codex_read=codex_read)
    if args.cmd == "switch":
        return cmd_switch(ws, args, core=core, execute=execute, restart=restart, sleep=sleep, clock=clock)
    if args.cmd == "settle":
        attempt = args.attempt or f"settle-{int(args.since)}"
        told = settle_and_tell(ws, args.room, args.runtime, "", args.since, attempt, core=core, execute=execute,
                               sleep=sleep, clock=clock)
        return _emit(told, 0 if told["outcome"] == "done" else 1)
    rec = confirmed_record(ws)
    return _emit({"runtime": runtime(), "next_start": configured_runtime(), "confirmed_core": rec,
                  "desktop": desktop_hosted()})


if __name__ == "__main__":
    sys.exit(main())
