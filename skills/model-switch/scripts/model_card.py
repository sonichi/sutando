#!/usr/bin/env python3
"""A card in the owner's DM that switches the core's model by button, and says how the switch ended.

    python3 model_card.py [--workspace WS] post --room ROOM [--reply-to EVENT] [--print]
    python3 model_card.py [--workspace WS] reply --task-file PATH
    python3 model_card.py [--workspace WS] settle --room ROOM --model ALIAS --rc N [--attempt ID]
    python3 model_card.py [--workspace WS] status

`post` sends the menu (one reply button per model, naming the model in use) to the owner's checked DM
and records it (src/issued_cards.py), so only a tap on it acts. `reply` handles that tap
(agent_card.click_verdict decides): it starts `switch-model.sh <alias> --confirm` detached, because the
core's CLI takes /model only after the current turn ends. Each tap is one attempt with its own id: the
"switching" card is posted first, then the switch is started, so its outcome can only come after.
The detached line always ends in `settle` with the switch's exit code and the attempt's id. `settle`
saves the outcome in `<workspace>/state/model-card-outcome.json`, then posts it in the same DM:
"Switched. I'm now using X." or "I could not switch to X. I'm still using Y. <reason>". A post that
fails is tried again a few times; an outcome still untold is posted by the next `post` or `reply`
before its own work, oldest first. An attempt's posts carry its id as their operation id, an outcome is
told once, and no retry runs the switch again.
The menu, the "switching" answer and the outcome share one card id, so the newest stands for the card.

The model in use is read only from `<workspace>/state/model-switch.json`, the record switch-model.sh
writes; with no record it is "the default model". Every command prints one JSON object.
Exit 0 done, 1 a negative answer, 2 refused.
"""
from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
for _p in (str(_REPO / "src"), str(_REPO / "skills" / "worker-pool" / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agent_card as ac  # noqa: E402
import issued_cards  # noqa: E402

CARD_ID, KIND, TARGET = "core-model-switch", "model", "core"
HANDLER = "skills/model-switch/scripts/model_card.py reply"
SWITCH = _HERE / "switch-model.sh"
MODELS = (("Opus", "opus"), ("Sonnet", "sonnet"), ("Fable", "fable"), ("Default", "default"))
DEFAULT_NAME = "the default model"
ACCEPT_TIMEOUT_S = 180
OUTCOMES = ("state", "model-card-outcome.json")
OUTCOME_TTL_S, OUTCOMES_KEPT = 86400, 20
SETTLE_TRIES, SETTLE_PAUSE_S = 4, 15
UNFINISHED = "the switch did not finish"
# switch-model.sh's exit codes (its SKILL.md), as one short reason the owner reads.
REASONS = {1: "the switch could not be locked or recorded",
           2: "that is not a model I can switch to",
           3: "the core is not running",
           4: "this core's model is set when it starts, not while it runs",
           5: "there was unsent text in the core's input box",
           6: "the core asked to confirm and the switch was not confirmed",
           7: "the command could not be sent to the core",
           8: "the core did not accept the switch in time",
           9: "the core is running, so its settings were left alone",
           10: "the model is set for the next start, but the core did not start"}
# The detached line: the switch, then always the outcome card with the switch's exit code.
DETACHED = ('bash "$1" "$2" --confirm --accept-timeout "$3" --state-dir "$4"; '
            '"$5" "$6" --workspace "$7" settle --room "$8" --model "$2" --attempt "$9" --rc $?')


def _emit(payload: dict, code: int = 0) -> int:
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    return code


def name_of(model: Optional[str]) -> str:
    """How a model is named to the owner: a menu label, the id itself, or "the default model"."""
    if not model or model == "default":
        return DEFAULT_NAME
    return dict((alias, label) for label, alias in MODELS).get(model, model)


def record(ws: Path) -> dict:
    """The last switch's record, {} when there is none or it cannot be read."""
    try:
        data = json.loads((ws / "state" / "model-switch.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def current_model(ws: Path) -> dict:
    """{model, name, source, ts}: the model in use by the switch record; model None without one."""
    rec = record(ws)
    model = rec.get("model") if isinstance(rec.get("model"), str) and rec.get("model") else None
    return {"model": model, "name": name_of(model), "source": "model-switch.json" if model else None,
            "ts": rec.get("ts") if model else None}


def still_using(ws: Path, alias: str) -> str:
    """The model in use after a switch to `alias` failed. A record that names `alias` without the
    CLI's acceptance (the core was down) says what the next start uses, so the one before it stands."""
    rec = record(ws)
    if rec.get("model") == alias and rec.get("accepted") is False:
        return name_of(rec.get("previous") if isinstance(rec.get("previous"), str) else None)
    return current_model(ws)["name"]


def menu_card(model: Optional[str]) -> tuple:
    """(body, card): one reply button per model, the one in use marked."""
    buttons = [ac.button(label, "reply", style="primary" if alias == model else "secondary",
                         reply=f"Switch the core to {label}", command=ac.reply(KIND, TARGET, alias))
               for label, alias in MODELS]
    name = name_of(model)
    return ac.build(CARD_ID, "info", title="Core model", avatar_=ac.avatar(initial="M"),
                    text=f"I'm using {ac.md_escape(name)}. Pick a model to switch to.", buttons=buttons,
                    note="A switch takes effect after the core finishes its current turn.", note_tone="dim",
                    body=f"I'm using {ac.md_escape(name)}. Reply with a model to switch.")


def switching_words(alias: str) -> str:
    return (f"Switching the core to {name_of(alias)}; it takes effect after this turn. "
            "I'll confirm here once it has switched.")


def notice_card(state: str, status: str, words: str) -> tuple:
    """(body, card) with the menu's id and no buttons: the "switching" answer or the outcome."""
    return ac.build(CARD_ID, state, title="Core model", avatar_=ac.avatar(initial="M"), status=status,
                    text=ac.md_escape(words), body=ac.md_escape(words))


def outcome(ws: Path, alias: str, rc: int) -> tuple:
    """(state, words) for a switch to `alias` that exited `rc`."""
    if rc == 0:
        return "done", f"Switched. I'm now using {name_of(alias)}."
    reason = REASONS.get(rc, UNFINISHED)
    return "error", f"I could not switch to {name_of(alias)}. I'm still using {still_using(ws, alias)}. {reason[:1].upper()}{reason[1:]}."


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
    """Post `card` in `room`, which must be the owner's checked DM, and record it for its taps; a card
    with no buttons retires the older one of its id there."""
    try:
        core = core if core is not None else _core(ws)
        lane = str(core.get("instance") or "")
        dm = _owner_dm(ws, core)
        execute = execute if execute is not None else _execute(ws, lane or None)
        eid = ac.send_card(execute, room, body, card, operation_id=operation_id, reply_to=reply_to, owner_dm=dm)
    except ac.CardError as e:
        return {"status": "failed", "why": str(e), "body": body}
    except Exception as e:  # noqa: BLE001 — no core lane or no credential: named, nothing posted
        return {"status": "failed", "why": f"{type(e).__name__}: {e}", "body": body}
    try:
        issued_cards.record(ws, eid, card, room=dm, lane=lane, handler=HANDLER)
    except OSError as e:
        return {"status": "posted", "event_id": eid, "why": f"its buttons will not act ({type(e).__name__})"}
    return {"status": "posted", "event_id": eid}


def new_attempt(now: float) -> str:
    """An id for one tap's switch: its "switching" card, its switch and its outcome carry it."""
    return f"{int(now)}-{secrets.token_hex(4)}"


def outcomes(ws: Path) -> list:
    """The saved outcomes, oldest first: {attempt, room, model, state, words, told, ts}."""
    try:
        data = json.loads(ws.joinpath(*OUTCOMES).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = data.get("outcomes") if isinstance(data, dict) else None
    return [r for r in rows if isinstance(r, dict) and r.get("attempt")] if isinstance(rows, list) else []


@contextmanager
def _outcome_book(ws: Path, now: float):
    """The outcomes of the last day under their lock; the list as the block leaves it is written back."""
    from file_lock import locked_file
    from local_record import write_whole
    path = ws.joinpath(*OUTCOMES)
    with locked_file(path.with_name(path.name + ".lock")):
        rows = [r for r in outcomes(ws) if now - float(r.get("ts") or 0) < OUTCOME_TTL_S]
        yield rows
        write_whole(path, {"outcomes": rows[-OUTCOMES_KEPT:]})


def save_outcome(ws: Path, attempt: str, room: str, alias: str, rc: int, now: float) -> dict:
    """The outcome of `attempt` as saved; written from `rc` only when the attempt has none yet."""
    with _outcome_book(ws, now) as rows:
        row = next((r for r in rows if r["attempt"] == attempt), None)
        if row is None:
            state, words = outcome(ws, alias, rc)
            row = {"attempt": attempt, "room": room, "model": alias, "state": state, "words": words,
                   "told": False, "ts": now}
            rows.append(row)
        return dict(row)


def _outcome_post(ws: Path, attempt: str, room: str, state: str, words: str, core, execute) -> dict:
    body, card = notice_card(state, "Switched" if state == "done" else "Not switched", words)
    return _post(ws, room, body, card, f"model-card:{attempt}:outcome", core=core, execute=execute)


def tell(ws: Path, attempt: str, *, core=None, execute=None, now: float) -> dict:
    """Post the saved outcome of `attempt` unless it was told, under the outcomes' lock, so the detached
    step and a later `post` or `reply` never tell one outcome twice."""
    try:
        with _outcome_book(ws, now) as rows:
            row = next((r for r in rows if r["attempt"] == attempt), None)
            if row is None:
                return {"status": "failed", "why": "no outcome is saved for that attempt"}
            if row.get("told"):
                return {"status": "already_told", "event_id": row.get("event_id")}
            out = _outcome_post(ws, attempt, str(row.get("room") or ""), str(row.get("state") or "error"),
                                str(row.get("words") or UNFINISHED), core, execute)
            if out["status"] == "posted":
                row.update(told=True, event_id=out["event_id"])
            return out
    except OSError as e:
        return {"status": "failed", "why": f"the outcome record could not be used ({type(e).__name__})"}


def deliver_owed(ws: Path, *, core=None, execute=None, now: float) -> list:
    """Post every saved outcome not yet told, oldest first, one try each; the attempts now told."""
    owed = [str(r["attempt"]) for r in outcomes(ws) if not r.get("told")]
    return [a for a in owed if tell(ws, a, core=core, execute=execute, now=now)["status"] == "posted"]


def settle_attempt(ws: Path, attempt: str, room: str, alias: str, rc: int, *, core=None, execute=None,
                   now=time.time, sleep=time.sleep, tries: int = 1, pause: float = 0) -> dict:
    """Save the outcome of `attempt`, then post it, trying again after `pause` up to `tries` times. An
    outcome that cannot be saved is posted once all the same."""
    try:
        row = save_outcome(ws, attempt, room, alias, rc, now())
    except OSError:
        state, words = outcome(ws, alias, rc)
        out = _outcome_post(ws, attempt, room, state, words, core, execute)
        return {**out, "attempt": attempt, "outcome": state, "say": words, "saved": False}
    out = tell(ws, attempt, core=core, execute=execute, now=now())
    for _ in range(max(tries, 1) - 1):
        if out["status"] != "failed":
            break
        sleep(pause)
        out = tell(ws, attempt, core=core, execute=execute, now=now())
    return {**out, "attempt": attempt, "outcome": row["state"], "say": row["words"]}


def cmd_post(ws: Path, args, *, core=None, execute=None, now=time.time) -> int:
    try:
        body, card = menu_card(current_model(ws)["model"])
    except ac.CardError as e:
        return _emit({"status": "failed", "why": str(e)}, 1)
    if args.print:
        return _emit({"body": body, "card": card, "handler": HANDLER})
    delivered = deliver_owed(ws, core=core, execute=execute, now=now())
    if delivered:
        body, card = menu_card(current_model(ws)["model"])
    out = _post(ws, args.room, body, card, f"model-card:menu:{int(now())}", reply_to=args.reply_to or "",
                core=core, execute=execute)
    return _emit({**out, **({"delivered": delivered} if delivered else {})}, 0 if out["status"] == "posted" else 1)


def start_switch(ws: Path, alias: str, room: str, attempt: str, popen=subprocess.Popen) -> int:
    """Start the switch and its `settle` in a session of their own, so they outlive this turn; the pid."""
    log = ws / "logs" / "model-card.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = ["nohup", "bash", "-c", DETACHED, "model-card", str(SWITCH), alias, str(ACCEPT_TIMEOUT_S),
            str(ws / "state"), sys.executable, str(Path(__file__).resolve()), str(ws), room, attempt]
    with open(log, "ab") as fh:
        fh.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} switch to {alias} ({attempt})\n".encode())
        fh.flush()
        proc = popen(argv, cwd=str(_REPO), stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                     start_new_session=True)
    return int(proc.pid)


def cmd_reply(ws: Path, args, *, core=None, execute=None, popen=subprocess.Popen, now=time.time) -> int:
    try:
        headers, words = ac.reply_words(Path(args.task_file).read_text(encoding="utf-8"))
    except OSError as e:
        return _emit({"status": "refused", "why": f"the task file is unreadable ({type(e).__name__})"}, 2)
    try:
        core = core if core is not None else _core(ws)
    except Exception:  # noqa: BLE001 — a card records its own lane; only one without it needs the core's owner
        core = {}
    verdict = ac.click_verdict(headers, words, ws, ac.lane_owner(ws, str(core.get("owner_mxid") or "")))
    delivered = deliver_owed(ws, core=core or None, execute=execute, now=now())
    extra = {"delivered": delivered} if delivered else {}
    other = ac.click_answer(verdict, words, (KIND,))
    if other is not None:
        return _emit({**other, **extra}, 1)
    alias = verdict.command.verb
    if verdict.command.target != TARGET or alias not in [a for _, a in MODELS]:
        return _emit({"status": "refused", "why": f"the model card has no {alias!r} action", **extra}, 2)
    if current_model(ws)["model"] == alias:
        return _emit({"status": "say", "say": f"I'm already using {name_of(alias)}. Nothing changed.", **extra})
    room, attempt, say = str(headers.get("channel_id") or ""), new_attempt(now()), switching_words(alias)
    body, card = notice_card("pending", "Switching", say)
    posted = _post(ws, room, body, card, f"model-card:{attempt}:switching",
                   reply_to=str(headers.get("reply_to_event") or ""), core=core or None, execute=execute)
    try:
        pid = start_switch(ws, alias, room, attempt, popen)
    except OSError as e:
        told = settle_attempt(ws, attempt, room, alias, -1, core=core or None, execute=execute, now=now)
        return _emit({"status": "failed", "attempt": attempt, "say": told["say"], **extra,
                      "why": f"the switch could not be started ({type(e).__name__}: {e})"}, 1)
    out = {"status": "started", "model": alias, "attempt": attempt, "pid": pid, "say": say, **extra}
    if posted.get("event_id"):
        out["event_id"] = posted["event_id"]
    else:
        out["card_why"] = posted.get("why", "")
    return _emit(out)


def cmd_settle(ws: Path, args, *, core=None, execute=None, now=time.time, sleep=time.sleep) -> int:
    out = settle_attempt(ws, args.attempt or new_attempt(now()), args.room, args.model, args.rc, core=core,
                         execute=execute, now=now, sleep=sleep, tries=SETTLE_TRIES, pause=SETTLE_PAUSE_S)
    return _emit(out, 1 if out["status"] == "failed" else 0)


def main(argv=None, *, core=None, execute=None, popen=subprocess.Popen, sleep=time.sleep) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workspace", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("post")
    s.add_argument("--room", required=True)
    s.add_argument("--reply-to", default="")
    s.add_argument("--print", action="store_true")
    r = sub.add_parser("reply")
    r.add_argument("--task-file", required=True)
    t = sub.add_parser("settle")
    t.add_argument("--room", required=True)
    t.add_argument("--model", required=True)
    t.add_argument("--rc", required=True, type=int)
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
        return cmd_reply(ws, args, core=core, execute=execute, popen=popen)
    if args.cmd == "settle":
        return cmd_settle(ws, args, core=core, execute=execute, sleep=sleep)
    return _emit(current_model(ws))


if __name__ == "__main__":
    sys.exit(main())
