#!/usr/bin/env python3
"""A card in the owner's DM that switches the core's model by button, and says how the switch ended.

    python3 model_card.py [--workspace WS] post --room ROOM [--reply-to EVENT] [--print]
    python3 model_card.py [--workspace WS] reply --task-file PATH
    python3 model_card.py [--workspace WS] settle --room ROOM --model ALIAS --rc N
    python3 model_card.py [--workspace WS] status

`post` sends the menu (one reply button per model, naming the model in use) to the owner's checked DM
and records it (src/issued_cards.py), so only a tap on it acts. `reply` handles that tap
(agent_card.click_verdict decides): it starts `switch-model.sh <alias> --confirm` detached, because the
core's CLI takes /model only after the current turn ends, and answers at once with a "switching" card.
The detached line always ends in `settle` with the switch's exit code, which posts the outcome in the
same DM: "Switched. I'm now using X." or "I could not switch to X. I'm still using Y. <reason>".
The menu, the "switching" answer and the outcome share one card id, so the newest stands for the card.

The model in use is read only from `<workspace>/state/model-switch.json`, the record switch-model.sh
writes; with no record it is "the default model". Every command prints one JSON object.
Exit 0 done, 1 a negative answer, 2 refused.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
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
            '"$5" "$6" --workspace "$7" settle --room "$8" --model "$2" --rc $?')


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


def cmd_post(ws: Path, args, *, core=None, execute=None, now=time.time) -> int:
    try:
        body, card = menu_card(current_model(ws)["model"])
    except ac.CardError as e:
        return _emit({"status": "failed", "why": str(e)}, 1)
    if args.print:
        return _emit({"body": body, "card": card, "handler": HANDLER})
    out = _post(ws, args.room, body, card, f"model-card:menu:{int(now())}", reply_to=args.reply_to or "",
                core=core, execute=execute)
    return _emit(out, 0 if out["status"] == "posted" else 1)


def start_switch(ws: Path, alias: str, room: str, popen=subprocess.Popen) -> int:
    """Start the switch and its `settle` in a session of their own, so they outlive this turn; the pid."""
    log = ws / "logs" / "model-card.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = ["nohup", "bash", "-c", DETACHED, "model-card", str(SWITCH), alias, str(ACCEPT_TIMEOUT_S),
            str(ws / "state"), sys.executable, str(Path(__file__).resolve()), str(ws), room]
    with open(log, "ab") as fh:
        fh.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} switch to {alias}\n".encode())
        fh.flush()
        proc = popen(argv, cwd=str(_REPO), stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                     start_new_session=True)
    return int(proc.pid)


def cmd_reply(ws: Path, args, *, core=None, execute=None, popen=subprocess.Popen) -> int:
    try:
        headers, words = ac.reply_words(Path(args.task_file).read_text(encoding="utf-8"))
    except OSError as e:
        return _emit({"status": "refused", "why": f"the task file is unreadable ({type(e).__name__})"}, 2)
    try:
        core = core if core is not None else _core(ws)
    except Exception:  # noqa: BLE001 — a card records its own lane; only one without it needs the core's owner
        core = {}
    verdict = ac.click_verdict(headers, words, ws, ac.lane_owner(ws, str(core.get("owner_mxid") or "")))
    other = ac.click_answer(verdict, words, (KIND,))
    if other is not None:
        return _emit(other, 1)
    alias = verdict.command.verb
    if verdict.command.target != TARGET or alias not in [a for _, a in MODELS]:
        return _emit({"status": "refused", "why": f"the model card has no {alias!r} action"}, 2)
    if current_model(ws)["model"] == alias:
        return _emit({"status": "say", "say": f"I'm already using {name_of(alias)}. Nothing changed."})
    room = str(headers.get("channel_id") or "")
    try:
        pid = start_switch(ws, alias, room, popen)
    except OSError as e:
        return _emit({"status": "failed", "why": f"the switch could not be started ({type(e).__name__}: {e})"}, 1)
    say = switching_words(alias)
    body, card = notice_card("pending", "Switching", say)
    task_id = str(headers.get("task_id") or headers.get("id") or "task")
    posted = _post(ws, room, body, card, f"{task_id}:model-switching-card",
                   reply_to=str(headers.get("reply_to_event") or ""), core=core or None, execute=execute)
    out = {"status": "started", "model": alias, "pid": pid, "say": say}
    if posted.get("event_id"):
        out["event_id"] = posted["event_id"]
    else:
        out["card_why"] = posted.get("why", "")
    return _emit(out)


def cmd_settle(ws: Path, args, *, core=None, execute=None, now=time.time) -> int:
    state, words = outcome(ws, args.model, args.rc)
    body, card = notice_card(state, "Switched" if state == "done" else "Not switched", words)
    out = _post(ws, args.room, body, card, f"model-card:outcome:{args.model}:{int(now())}", core=core, execute=execute)
    return _emit({**out, "outcome": state, "say": words}, 0 if out["status"] == "posted" else 1)


def main(argv=None, *, core=None, execute=None, popen=subprocess.Popen) -> int:
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
        return cmd_settle(ws, args, core=core, execute=execute)
    return _emit(current_model(ws))


if __name__ == "__main__":
    sys.exit(main())
