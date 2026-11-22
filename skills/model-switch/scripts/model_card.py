#!/usr/bin/env python3
"""A card in the owner's DM that switches the core's model by button, and says how the switch ended.

    python3 model_card.py [--workspace WS] post --room ROOM [--reply-to EVENT] [--print]
    python3 model_card.py [--workspace WS] reply --task-file PATH
    python3 model_card.py [--workspace WS] settle --room ROOM --model ALIAS --rc N [--attempt ID]
    python3 model_card.py [--workspace WS] status

`post` sends the menu to the owner's checked DM and records it (src/issued_cards.py), so only a tap on
it acts. Its buttons are the skill manifest's MODEL_SWITCH_CHOICES (`id=Title;…`, what the menu-bar
app offers), in that order and under those titles; a button carries a slug of its id ("sonnet-1m"),
unique on the card, and the exact ids the card was built with are kept beside it
(`<workspace>/state/model-card-choices.json`, by the card's event id). A tap is resolved through that
card's own ids and acts only when the id is still on the list, so an edit to the list between the card
and the tap never switches to another id. An entry whose id is not a model name (one starting with "-",
say) or whose title is too long for a button is left out. The model in use is marked by exact id. No
list is built in: with no readable choices the menu is not posted. `reply` handles that tap
(agent_card.click_verdict decides): it starts `switch-model.sh <id> --confirm` detached, because the
core's CLI takes /model only after the current turn ends. Each tap is one attempt with its own id: the
"switching" card is posted first, then the switch is started, so its outcome can only come after.
The detached line runs the switch through `turn`, one at a time and only for the newest attempt
(an older one started late is skipped, so the core is never left on a model the card does not
show), and always ends in `settle` with the exit code and the attempt's id. `settle`
saves the outcome in `<workspace>/state/model-card-outcome.json`, then posts it in the same DM:
"Switched. I'm now using X." or "I could not switch to X. I'm still using Y. <reason>". A post that
fails is tried again a few times; an outcome still untold is posted by the next `post` or `reply`
before its own work. An attempt's posts carry its id as their operation id, an outcome is told once,
and no retry runs the switch again. `reply` records an attempt before its "switching" card, and an
untold outcome with a newer attempt after it is marked `superseded` and never posted: only the newest
attempt's outcome may stand for the card.
The menu, the "switching" answer and the outcome share one card id, so the newest stands for the card.

The model in use is read only from `<workspace>/state/model-switch.json`, the record switch-model.sh
writes; with no record it is "the default model". Every command prints one JSON object.
Exit 0 done, 1 a negative answer, 2 refused.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
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
# The "switching" answer and the outcome are cards of their own: recorded under the menu's id they
# would retire the menu (one live card per id and room) while its buttons are still on screen.
NOTE_ID = "core-model-switch-status"
HANDLER = "skills/model-switch/scripts/model_card.py reply"
SWITCH = _HERE / "switch-model.sh"
MANIFEST = _HERE.parent / "manifest.json"
CHOICES_KEY = "MODEL_SWITCH_CHOICES"
NO_CHOICES = "I can't read the list of models (skills/model-switch/manifest.json), so I have no menu to show."
DEFAULT_NAME = "the default model"
GONE = "That choice is no longer on my list. Ask me for the model card again."
UNRECORDED = "I couldn't start the switch: I can't write my record of it. Nothing changed."
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,79}")
ISSUED = ("state", "model-card-choices.json")
ISSUED_KEPT = 20
MENU_BUTTONS = ac.LIMITS["buttons"]
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
# The detached line: the switch in its turn (`turn` runs it only for the newest attempt), then always
# the outcome card with the exit code.
DETACHED = ('"$5" "$6" --workspace "$7" turn --attempt "$9" -- '
            'bash "$1" "$2" --confirm --accept-timeout "$3" --state-dir "$4"; '
            '"$5" "$6" --workspace "$7" settle --room "$8" --model "$2" --attempt "$9" --rc $?')
TURN_LOCK = ("state", "model-card-turn.lock")
SKIPPED_RC = 75      # `turn` did not run the switch: a newer attempt had been accepted
UNKNOWN_RC = 76      # `turn` did not run the switch: the record does not name the attempt
NOT_RECORDED = "the attempt is not in the record, so no card is posted for it"


def _emit(payload: dict, code: int = 0) -> int:
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    return code


def choices(manifest: Optional[Path] = None) -> list:
    """[(id, title, slug)] from the skill manifest's MODEL_SWITCH_CHOICES, in its order; [] when it
    cannot be read. The slug is the id as a card reply may carry it, numbered when another id has it.
    Left out: an id that is not a model name or is listed twice, and a title no button can carry."""
    from skill_config import manifest_config
    raw = manifest_config(manifest or MANIFEST).get(CHOICES_KEY)
    out: list = []
    for entry in raw.split(";") if isinstance(raw, str) else []:
        model, _, title = entry.partition("=")
        model, title = model.strip(), " ".join(title.split())
        if not _MODEL_ID.fullmatch(model) or not 0 < len(title) <= ac.LIMITS["label"] or model in [m for m, _, _ in out]:
            continue
        base = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
        taken = [s for _, _, s in out]
        slug = next(s for s in [base] + [f"{base}-{n}" for n in range(2, len(out) + 3)] if s not in taken)
        out.append((model, title, slug))
    return out


def issuance(picks: list) -> str:
    """A short name for exactly this menu: its slugs and the exact id behind each. Two menus that look
    the same but stand for different ids are different issuances, so a send repeated for one is never
    answered with the other's event."""
    exact = json.dumps([[slug, model] for model, _, slug in picks], ensure_ascii=True)
    return hashlib.sha256(exact.encode("utf-8")).hexdigest()[:16]


def remember_choices(ws: Path, event_id: str, picks: list, now: float) -> None:
    """Keep the exact id behind each slug of the menu posted as `event_id`. What an event's buttons
    stand for is fixed when it is first kept: a later call for the same event changes nothing."""
    from file_lock import locked_file
    from local_record import write_whole
    path = ws.joinpath(*ISSUED)
    with locked_file(path.with_name(path.name + ".lock")):
        cards = issued_choices(ws)
        if event_id in cards:
            return
        cards[event_id] = {"ids": {slug: model for model, _, slug in picks}, "ts": now}
        newest = sorted(cards.items(), key=lambda kv: float(kv[1].get("ts") or 0))[-ISSUED_KEPT:]
        write_whole(path, {"cards": dict(newest)})


def issued_choices(ws: Path) -> dict:
    """{event id: {"ids": {slug: exact id}, "ts"}} for the menus this agent posted."""
    try:
        data = json.loads(ws.joinpath(*ISSUED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    cards = data.get("cards") if isinstance(data, dict) else None
    return {k: v for k, v in cards.items() if isinstance(v, dict) and isinstance(v.get("ids"), dict)} \
        if isinstance(cards, dict) else {}


def tapped_model(ws: Path, event_id: str, slug: str) -> str:
    """The exact id `slug` stood for on the menu posted as `event_id`, when the list still has that id;
    "" for a menu with no ids kept, a slug it did not carry, or an id since taken off the list."""
    model = issued_choices(ws).get(event_id, {}).get("ids", {}).get(slug)
    return model if isinstance(model, str) and model in [m for m, _, _ in choices()] else ""


def name_of(model: Optional[str], picks: Optional[list] = None) -> str:
    """How a model is named to the owner: its title in the manifest by exact id, else the id as
    recorded, or "the default model" for none."""
    if not model:
        return DEFAULT_NAME
    title = next((t for m, t, _ in (choices() if picks is None else picks) if m == model), "")
    return model if not title and _MODEL_ID.fullmatch(model) else ac.md_escape(title or model)


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


def menu_card(model: Optional[str], picks: list) -> tuple:
    """(body, card): one reply button per choice in `picks`, the one in use (by exact id) marked. A
    card holds six buttons; with more choices the first six are shown and the text says to ask."""
    limit = MENU_BUTTONS
    buttons = [ac.button(title, "reply", style="primary" if choice == model else "secondary",
                         reply=f"Switch the core to {title}", command=ac.reply(KIND, TARGET, slug))
               for choice, title, slug in picks[:limit]]
    using = f"I'm using {name_of(model, picks)}."
    more = " For a model that is not on a button, tell me its name." if len(picks) > limit else ""
    return ac.build(CARD_ID, "info", title="Core model", avatar_=ac.avatar(initial="M"),
                    text=f"{using} Pick a model to switch to.{more}", buttons=buttons,
                    note="A switch takes effect after the core finishes its current turn.", note_tone="dim",
                    body=f"{using} Reply with a model to switch.")


def switching_words(alias: str) -> str:
    return (f"Switching the core to {name_of(alias)}; it takes effect after this turn. "
            "I'll confirm here once it has switched.")


def notice_card(state: str, status: str, words: str) -> tuple:
    """(body, card) with no buttons, under its own id: the "switching" answer or the outcome."""
    return ac.build(NOTE_ID, state, title="Core model", avatar_=ac.avatar(initial="M"), status=status,
                    text=words, body=words)


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
    """Post `card` in `room`, which must be the owner's checked DM, and record it for its taps; a newer
    menu retires the older one there, and a card with no buttons retires none."""
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
    """The attempts, oldest first: {attempt, room, model, state, words, told, superseded, ts}; `state`
    is "started" until the switch ended."""
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


def save_outcome(ws: Path, attempt: str, room: str, alias: str, rc: Optional[int], now: float,
                 add: bool = True) -> Optional[dict]:
    """The record of `attempt`; its outcome is written from `rc` once, and not at all for `rc` None (the
    attempt has only begun). A new attempt is added as started, which is what accepting a tap does;
    with `add` False (finishing one) an attempt the record does not name stays out of it: None."""
    with _outcome_book(ws, now) as rows:
        row = next((r for r in rows if r["attempt"] == attempt), None)
        if row is None and not add:
            return None
        if row is None:
            row = {"attempt": attempt, "room": room, "model": alias, "state": "started", "words": "",
                   "told": False, "ts": now}
            rows.append(row)
        if rc is not None and row.get("state") == "started":
            state, words = outcome(ws, alias, rc)
            row.update(state=state, words=words)
        return dict(row)


def _outcome_post(ws: Path, attempt: str, room: str, state: str, words: str, core, execute) -> dict:
    body, card = notice_card(state, "Switched" if state == "done" else "Not switched", words)
    return _post(ws, room, body, card, f"model-card:{attempt}:outcome", core=core, execute=execute)


def tell(ws: Path, attempt: str, *, core=None, execute=None, now: float) -> dict:
    """Post the saved outcome of `attempt` unless it was told, under the outcomes' lock, so the detached
    step and a later `post` or `reply` never tell one outcome twice. An attempt with a newer one after
    it is marked superseded instead: its outcome would replace the newer attempt's card."""
    try:
        with _outcome_book(ws, now) as rows:
            at = next((i for i, r in enumerate(rows) if r["attempt"] == attempt), None)
            if at is None:
                return {"status": "failed", "why": "no outcome is saved for that attempt"}
            row = rows[at]
            if row.get("told"):
                return {"status": "already_told", "event_id": row.get("event_id")}
            if row.get("superseded") or at + 1 < len(rows):
                row["superseded"] = True
                return {"status": "superseded"}
            if row.get("state") not in ("done", "error"):
                return {"status": "failed", "why": "that attempt has no outcome yet"}
            out = _outcome_post(ws, attempt, str(row.get("room") or ""), row["state"],
                                str(row.get("words") or UNFINISHED), core, execute)
            if out["status"] == "posted":
                row.update(told=True, event_id=out["event_id"])
            return out
    except OSError as e:
        return {"status": "failed", "why": f"the outcome record could not be used ({type(e).__name__})"}


def deliver_owed(ws: Path, *, core=None, execute=None, now: float) -> list:
    """Post the outcome not yet told, one try; the attempts now told. Older untold ones are marked
    superseded on the way and never posted."""
    owed = [str(r["attempt"]) for r in outcomes(ws)
            if not r.get("told") and not r.get("superseded") and r.get("state") in ("done", "error")]
    return [a for a in owed if tell(ws, a, core=core, execute=execute, now=now)["status"] == "posted"]


def settle_attempt(ws: Path, attempt: str, room: str, alias: str, rc: int, *, core=None, execute=None,
                   now=time.time, sleep=time.sleep, tries: int = 1, pause: float = 0, add: bool = False) -> dict:
    """Save the outcome of `attempt`, then post it, trying again after `pause` up to `tries` times. One
    superseded meanwhile is not posted. Nor is one that cannot be saved, or that the record no longer
    names (only accepting a tap adds an attempt, `add` aside): without its place in the record nothing
    shows whether a newer attempt came after it, and its card would replace that attempt's."""
    try:
        row = save_outcome(ws, attempt, room, alias, rc, now(), add=add)
    except OSError as e:
        state, words = outcome(ws, alias, rc)
        return {"status": "failed", "attempt": attempt, "outcome": state, "say": words, "saved": False,
                "why": f"the outcome could not be saved, so its card was not posted ({type(e).__name__})"}
    if row is None:
        return {"status": "skipped", "attempt": attempt, "why": NOT_RECORDED}
    out = tell(ws, attempt, core=core, execute=execute, now=now())
    for _ in range(max(tries, 1) - 1):
        if out["status"] != "failed":
            break
        sleep(pause)
        out = tell(ws, attempt, core=core, execute=execute, now=now())
    return {**out, "attempt": attempt, "outcome": row["state"], "say": row["words"]}


def cmd_post(ws: Path, args, *, core=None, execute=None, now=time.time) -> int:
    picks = choices()
    if not picks:
        return _emit({"status": "failed", "why": NO_CHOICES}, 1)
    try:
        body, card = menu_card(current_model(ws)["model"], picks)
    except ac.CardError as e:
        return _emit({"status": "failed", "why": str(e)}, 1)
    if args.print:
        return _emit({"body": body, "card": card, "handler": HANDLER})
    delivered = deliver_owed(ws, core=core, execute=execute, now=now())
    if delivered:
        body, card = menu_card(current_model(ws)["model"], picks)
    shown = picks[:MENU_BUTTONS]
    out = _post(ws, args.room, body, card, f"model-card:menu:{int(now())}:{issuance(shown)}",
                reply_to=args.reply_to or "", core=core, execute=execute)
    if out.get("event_id"):
        try:
            remember_choices(ws, out["event_id"], shown, now())
        except OSError as e:
            out["why"] = f"its buttons will not act ({type(e).__name__})"
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
    other, code = ac.click_answer(verdict, words, (KIND,)), 1
    slug = verdict.command.verb if other is None else ""
    alias = tapped_model(ws, str(headers.get("reply_to_event") or ""), slug) if other is None else ""
    if other is None and (verdict.command.target != TARGET or not alias):
        other, code = {"status": "refused", "why": GONE}, 2
    elif other is None and current_model(ws)["model"] == alias:
        other, code = {"status": "say", "say": f"I'm already using {name_of(alias)}. Nothing changed."}, 0
    room, attempt = str(headers.get("channel_id") or ""), new_attempt(now())
    if other is None:
        try:
            save_outcome(ws, attempt, room, alias, None, now())
        except OSError as e:
            # Unrecorded, this attempt could not be told apart from an older one: nothing is started.
            other, code = {"status": "failed", "say": UNRECORDED,
                           "why": f"the attempt could not be recorded ({type(e).__name__})"}, 1
    delivered = deliver_owed(ws, core=core or None, execute=execute, now=now())
    extra = {"delivered": delivered} if delivered else {}
    if other is not None:
        return _emit({**other, **extra}, code)
    say = switching_words(alias)
    body, card = notice_card("pending", "Switching", say)
    posted = _post(ws, room, body, card, f"model-card:{attempt}:switching",
                   reply_to=str(headers.get("reply_to_event") or ""), core=core or None, execute=execute)
    try:
        pid = start_switch(ws, alias, room, attempt, popen)
    except OSError as e:
        told = settle_attempt(ws, attempt, room, alias, -1, core=core or None, execute=execute, now=now)
        told.setdefault("say", outcome(ws, alias, -1)[1])
        return _emit({"status": "failed", "attempt": attempt, "say": told["say"], **extra,
                      "why": f"the switch could not be started ({type(e).__name__}: {e})"}, 1)
    out = {"status": "started", "model": alias, "attempt": attempt, "pid": pid, "say": say, **extra}
    if posted.get("event_id"):
        out["event_id"] = posted["event_id"]
    else:
        out["card_why"] = posted.get("why", "")
    return _emit(out)


def cmd_turn(ws: Path, args, *, call=subprocess.call) -> int:
    """Run an attempt's switch in its turn, one at a time, and only while it is the newest accepted
    attempt. Two taps start two detached lines in no fixed order: without this the older switch could
    run last, leaving the core on a model the card does not show (its outcome is superseded and
    never posted). The command's exit code; SKIPPED_RC when a newer attempt was accepted; UNKNOWN_RC
    when the record does not name this attempt (trimmed, or unreadable): nothing then shows it is
    the newest, so its switch is not run either."""
    from file_lock import locked_file
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    with locked_file(ws.joinpath(*TURN_LOCK)):
        rows = outcomes(ws)
        at = next((i for i, r in enumerate(rows) if r["attempt"] == args.attempt), None)
        if at is None:
            print(f"model-card: attempt {args.attempt} is not in the record; its switch is not run", flush=True)
            return UNKNOWN_RC
        if at + 1 < len(rows):
            print(f"model-card: attempt {args.attempt} is not the newest; its switch is not run", flush=True)
            return SKIPPED_RC
        return int(call(command))


def cmd_settle(ws: Path, args, *, core=None, execute=None, now=time.time, sleep=time.sleep) -> int:
    if args.rc == UNKNOWN_RC:
        # Settling would add the attempt to the record as the newest one, and post over a newer card.
        return _emit({"status": "skipped", "attempt": args.attempt, "why": NOT_RECORDED}, 1)
    # With no --attempt this is a settle by hand: a new attempt of its own. One that names an attempt
    # only finishes it, and never adds it.
    out = settle_attempt(ws, args.attempt or new_attempt(now()), args.room, args.model, args.rc, core=core,
                         execute=execute, now=now, sleep=sleep, tries=SETTLE_TRIES, pause=SETTLE_PAUSE_S,
                         add=not args.attempt)
    return _emit(out, 1 if out["status"] in ("failed", "skipped") else 0)


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
    u = sub.add_parser("turn")
    u.add_argument("--attempt", required=True)
    u.add_argument("command", nargs=argparse.REMAINDER)
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
    if args.cmd == "turn":
        return cmd_turn(ws, args)
    return _emit(current_model(ws))


if __name__ == "__main__":
    sys.exit(main())
