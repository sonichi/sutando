"""The quota notice: the account card, unasked, once the session has hit its usage limit.

A session at its limit cannot answer, so nobody learns why it went quiet. The launcher already reads
the pane each tick; when Claude Code's own limit menu is the live dialog there ("Stop and wait for limit
to reset / Wait here ... / Switch to usage credits", detector.limit_menu), at startup as much as mid-turn,
the owner gets the account card in their direct message: what happened, when it resets if the pane says,
and the account controls to switch with. A usage-limit banner with no menu is words, and sends nothing;
the out-of-credits banner, which has no menu, still does. One card per limit: the next is sent only after
the limit has been gone for CLEAR_S and comes back. Nothing here picks any of the menu's options.

Every owner message is told, not only the first: while the limit is on the pane, each owner task with no
answer yet (inbox.unanswered: queued, or claimed and stuck behind the menu) gets the account card once, in
their DM, keyed by its task id, so a supervisor restart never tells it twice. The task itself stays queued
and is answered once the account is switched or the limit resets. A new session (an account switch
restarts it) starts a new incident: the account it runs on may be at its limit as well.

The owner's DM room is not something a connector is told; it is remembered from the owner's own
messages there (`remember_room`, at the claim), in a record of its own: the claim writes only that
one and the supervisor only the incident's, so neither can undo the other's write. Until a room has
been seen no card can be sent, and the launcher's own stall card still reaches the owner.

Queued is not told. The bridge records what became of each queued card (connector_cards.outcome), and
only that record decides: `posted` is told; `dropped` (expired with no send tried) is queued again as
a new card; `unsure` (expired after a send that may have gone through) is queued again under its own
key, which the server takes for the same message. A card with no record and no file left is not known
to be any of these, so it is never sent again: that could tell the owner twice. Nothing here
types into the session or changes an account.
"""
from __future__ import annotations

import collections
import datetime
import json
import re
import time
import uuid
from pathlib import Path

from ... import (account_switch, card_desk, claude_local_records as records, connect_bridge as cb, connector_cards,
                 quota_notice)
from ...cli_wedge_patterns import ABNORMAL_PATTERNS, PROVIDER_LIMIT_PATTERNS
from . import account_card, detector, inbox

FILE = "quota-card.json"        # the incident: the supervisor's alone
ROOM_FILE = "quota-room.json"   # the owner's DM room: the claim's alone

CLEAR_S = 600.0         # the banner gone this long: the limit is over, and a later one is a new card
COVERS_KEEP = 20        # the task cards of one incident whose fate is still followed


ECHO_TASKS, ECHO_BYTES, ECHO_MIN = 40, 200_000, 8    # the task files a pane line is checked against
_CUT = re.compile(r"\bresets?\b[\w ,]{0,24}$", re.IGNORECASE)
SEEN_FILE = "quota-seen.json"   # the limit lines a new session's pane already showed
SETTLE_S = 30.0         # how long a new session takes to draw the conversation before it

RETRY_S = 60.0          # a card that could not be queued is tried again no sooner than this
UNKNOWN_S = 2 * connector_cards.MAX_QUEUED_S
TAIL_LINES = 8          # a banner this near the bottom is the limit the session sits at, not old scrollback

# The reset time as the banner gives it: a clock time, optionally after a date, optionally with a zone.
_CLOCK = r"\d{1,2}(?::\d{2})?\s?[ap]m"
_DAY = r"(?:[A-Z][a-z]{2}\s\d{1,2}|\d{1,2}\s[A-Z][a-z]{2})"
_WHEN = re.compile(r"\bresets?\s+(?:at\s+|on\s+)?(?:(?P<day>%s),?\s+(?:at\s+)?)?(?P<clock>%s)(?:\s*\((?P<zone>[A-Za-z_]+(?:/[A-Za-z_]+){0,2})\))?"
                   % (_DAY, _CLOCK), re.IGNORECASE)
_LIMIT = tuple(rx for name, rx in ABNORMAL_PATTERNS if name in PROVIDER_LIMIT_PATTERNS)
_ROOM = re.compile(r"^![^\s:]+(:\S+)?$")
_HINT = re.compile(r"/?usage-credits\b", re.I)    # the credits hint under a banner, itself matched as one
_MENU_WHEN = re.compile(r"^[\s>❯]*\d+\.\s+Wait here, then continue automatically (?:at|on)\s+(.{1,60})$", re.M | re.I)


def _read(root, name) -> dict:
    try:
        rec = json.loads((records.state_dir(root) / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def _write(root, name, rec: dict) -> bool:
    try:
        records.write_durable(records.state_dir(root) / name, json.dumps(rec, sort_keys=True).encode("utf-8"))
        return True
    except (OSError, records.RecordError):
        return False


def load(root) -> dict:
    return _read(root, FILE)


def save(root, rec: dict) -> bool:
    return _write(root, FILE, rec)


def room(root) -> str:
    """The owner's DM room as last seen, '' when none has been."""
    kept = str(_read(root, ROOM_FILE).get("room") or "")
    return kept if _ROOM.match(kept) else ""


def remember_room(root, seen) -> None:
    """Keep the owner's DM room, seen on a message of theirs there; anything that is not a room id is ignored."""
    seen = str(seen or "")
    if _ROOM.match(seen) and room(root) != seen:
        _write(root, ROOM_FILE, {"room": seen})


def _said(found) -> str:
    day, zone = found.group("day"), found.group("zone")
    return "resets %s%s%s" % (" ".join(day.split()) + ", " if day else "", "".join(found.group("clock").split()).lower(),
                              " (%s)" % zone if zone else "")


def reset_words(text: str) -> str:
    """When the limit resets ("resets 3pm (America/Los_Angeles)", "resets Nov 28, 9am"), '' when the
    pane does not say. Only the limit banner's own line is read, else the limit menu's "continue
    automatically at <when>" option line, and only a time is taken from it: a clock time, with a date
    before it and a zone after it when given. The words are built from those parts, never copied from
    the pane, so nothing else on the line can reach the DM."""
    text, under = str(text or ""), ""
    if detector.limit_menu(text):
        menu = _MENU_WHEN.search(text)
        found = _WHEN.search("resets " + menu.group(1)) if menu else None
        if found and found.start() == 0:
            return _said(found)
    for line in reversed(text.splitlines()):
        line = detector._DECOR.sub("", line)
        if any(rx.match(line) for rx in _LIMIT):
            # A narrow pane wraps the banner: a line that ends inside its own "resets ..." goes on underneath.
            found = _WHEN.search(line) or (_CUT.search(line) and _WHEN.search(line + " " + under))
            if found:
                return _said(found)
            if not _HINT.match(line):
                break       # the newest banner gives no time: an older limit's, further up, is not this one's
        under = line.strip()
    return ""


def words(limited: list, text: str, using: str, who: str = "") -> str:
    """The notice: "<account> has hit its limit; resets <when>." `who` is the account in use as the card
    names it ('' when it cannot be read: then Claude Code is named and `using` says why)."""
    what = "is out of credits" if "out-of-credits" in limited else "has hit its usage limit"
    when = reset_words(text) or "it does not say when that resets"
    lead = "%s (%s)" % (who, account_card.NAME) if who else account_card.NAME
    return "%s %s; %s. I can't answer until then.%s" % (
        lead, what, when, "" if who else " " + using)


def room_words(text: str) -> str:
    """The limit as said in a shared room: the wording every runtime shares (quota_notice.room_words)."""
    when = reset_words(text)
    return quota_notice.room_words(account_card.NAME, when[len("resets "):] if when.startswith("resets ") else "")


def delivery(bridge, rec: dict, at: float) -> str:
    """What became of the card `rec` queued: 'posted', 'dropped' or 'unsure' as the bridge recorded it, 'pending'
    while it is in the outbox or the bridge may still be settling it, 'unknown' when UNKNOWN_S later
    there is still neither a record nor a file. Absence is never read as either outcome."""
    qid = str(rec.get("qid") or "")
    said = connector_cards.outcome(bridge, qid)
    if said == "posted" or Path(bridge).joinpath(*connector_cards.POSTED, qid + ".json").is_file():
        return "posted"
    # Queued (again) and not settled since: an earlier `unsure` stays on record until the bridge replaces it.
    if Path(bridge).joinpath(*connector_cards.OUTBOX, qid + ".json").is_file():
        return "pending"
    if said:
        return said
    return "pending" if at - float(rec.get("queued") or 0) <= UNKNOWN_S else "unknown"


def _card_words(card, limited: list, text: str, log) -> str:
    try:
        who = card.read()
    except Exception:  # noqa: BLE001 - using() logs it and says it could not tell
        who = ""
    return words(limited, text, "" if who else card.using(), who)


def new_session(root) -> None:
    """A session was just created (an account switch restarts it): what was told was about the session
    before, so a limit on this one is a new incident, told again. The DM room is the claim's and stays."""
    if load(root):
        save(root, {})


def _limit_lines(text: str) -> list:
    lines = (_flat(detector._DECOR.sub("", ln)) for ln in str(text or "").splitlines())
    return [ln for ln in lines if any(rx.match(ln) for rx in _LIMIT)]


def _history(root, text: str, at: float, session: str) -> bool:
    """Whether the limit lines on the pane are only what a new session drew of the conversation before it: Claude
    Code shows the old transcript again, its banner included. They stay history until another limit line joins
    them; the limit menu, which only a live limit opens, is never history. `session` names the session the pane
    belongs to ('' when the caller cannot say: then nothing is history)."""
    if not session or detector.limit_menu(str(text or "")):
        return False
    seen = _read(root, SEEN_FILE)
    if seen.get("session") != session:      # the first read of this session's pane
        seen = {"session": session, "since": at, "lines": None}
    lines = _limit_lines(text)
    if at - float(seen.get("since") or 0) < SETTLE_S:
        if lines != seen.get("lines"):
            _write(root, SEEN_FILE, dict(seen, lines=lines))
        return True
    old = collections.Counter(seen.get("lines") or [])
    return bool(lines) and all(n <= old[ln] for ln, n in collections.Counter(lines).items())


def _sent_at(sent: dict, path) -> float:
    """When the task was sent: its own `timestamp` header, which a rewrite of the file does not move;
    the file's time only for a task that carries none."""
    stamp = str(sent.get("timestamp") or "").strip()
    try:
        return datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return Path(path).stat().st_mtime


def _owner_task(root, task_id: str) -> "tuple[str | None, float, bool]":
    """(the room an owner task's notice goes to, when the task arrived, whether that room is the owner's DM,
    where the card may name the account): the room it was sent in (the owner's
    DM is kept as the DM room too), else the DM room last seen. None for a task that is not the owner's own
    words (another sender, a card tap) or that cannot be read; '' when no room is known yet."""
    agent_card, _ = connector_cards._core()
    path = Path(root) / "tasks" / (task_id + ".txt")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            sent, _ = agent_card.reply_words(f.read())
        arrived = _sent_at(sent, path)
    except (OSError, ValueError):
        return None, 0.0, False
    if agent_card.owner_may_act(sent, dm_only=False) is not None or agent_card.click_tagged(sent):
        return None, 0.0, False
    seen = str(sent.get("channel_id") or "")
    if agent_card.owner_may_act(sent) is None:
        remember_room(root, seen)
    elif _ROOM.match(seen):
        return seen, arrived, False     # asked in a room: the card answers there
    return room(root), arrived, True


def _flat(text: str) -> str:
    return " ".join(str(text or "").split())


def own_ui(root, text: str) -> str:
    """The pane without the lines that only repeat a waiting task's text (its message, a quoted reply, the
    command lines of its instructions), however the terminal wrapped them: what is left is Claude Code's own."""
    said = []
    try:
        files = sorted((Path(root) / "tasks").glob("*.txt"), key=lambda p: p.stat().st_mtime)[-ECHO_TASKS:]
        for path in files:
            said.append(_flat(path.read_text(encoding="utf-8", errors="replace")[:ECHO_BYTES]))
    except OSError:
        pass
    if not said:
        return str(text or "")
    kept = []
    for line in str(text or "").splitlines():
        flat = _flat(detector._DECOR.sub("", line))
        kept.append("" if len(flat) >= ECHO_MIN and any(flat in task for task in said) else line)
    return "\n".join(kept)


def current(text: str) -> bool:
    """Whether the limit is what the session sits at now, not an old banner still in view above later work:
    Claude Code's limit menu is on the pane (it is modal), or a limit banner is among the last TAIL_LINES
    non-blank lines (below it only the prompt box)."""
    text = str(text or "")
    if detector.limit_menu(text):
        return True
    tail = [detector._DECOR.sub("", ln) for ln in text.splitlines() if ln.strip()][-TAIL_LINES:]
    return any(rx.match(ln) for ln in tail for rx in _LIMIT)


def _just_carded(rec: dict, to: str, dm: str, arrived: float) -> bool:
    """Whether the incident's own unasked card, sent to this room, answers this message: one that was already
    waiting when the card was queued. A message that arrived after the card gets its own."""
    sent = (rec.get("qid") or rec.get("told")) and not rec.get("by_tasks") and not rec.get("unknown")
    return bool(sent) and to == dm and arrived <= float(rec.get("queued") or 0)


def _covered(bridge: str, rec: dict, at: float) -> "tuple[str, list]":
    """What became of the cards queued for the owner's own messages this incident (`covers`): ('posted',
    []) once one is posted, ('pending', the ones not settled) while any may still be, ('unknown', []) when
    one may have gone through and none is pending, ('', []) when every one was dropped or none was queued."""
    fates = [(delivery(bridge, c, at), c) for c in rec.get("covers") or [] if isinstance(c, dict)]
    if any(f == "posted" for f, _ in fates):
        return "posted", []
    waiting = [c for f, c in fates if f == "pending"]
    if waiting:
        return "pending", [c for f, c in fates if f != "dropped"]
    return ("unknown" if any(f in ("unsure", "unknown") for f, _ in fates) else ""), []


def _tell_tasks(root, cfg: dict, bridge: str, limited: list, text: str, log, rec: dict, at: float) -> "tuple[dict, list]":
    """Each owner task with no answer gets the card once: (the incident record with its `tasks` entries,
    what was done for each task this tick). An entry keeps its card's key and queue id; a card the bridge
    dropped is queued again under a new key, an unsure one under its own; entries of answered tasks go."""
    kept = rec.get("tasks") if isinstance(rec.get("tasks"), dict) else {}
    if not current(text):
        return rec, []      # an old banner in view: no task is told, and what was told is kept
    pending = inbox.unanswered(root)
    if not kept and not pending:
        return rec, []
    tasks = {t: kept[t] for t in pending if isinstance(kept.get(t), dict)}
    done, card, said = [], None, None
    for task_id in pending:
        entry = dict(tasks.get(task_id) or {})
        if entry.get("skip"):
            continue
        if entry.get("qid"):
            fate = delivery(bridge, entry, at)
            if fate == "dropped":
                entry = {"tried": entry.get("tried")}
            elif fate == "unsure":
                entry = dict({k: v for k, v in entry.items() if k not in ("qid", "queued")}, again=True, tried=0)
            else:
                continue
        if at - float(entry.get("tried") or 0) < RETRY_S:
            continue
        to, arrived, in_dm = _owner_task(root, task_id)
        if to is None:
            tasks[task_id] = {"skip": True}     # not the owner's own words: no account card for it
            continue
        if not to:
            done.append("quota-no-room")
            continue
        if in_dm and _just_carded(rec, to, room(root), arrived):
            if rec.get("told"):
                tasks[task_id] = {"skip": True}     # the incident's card, posted in this room, answers it
            continue        # only queued so far: no second card yet, and none skipped for good until it is posted
        if not card_desk.usable(bridge):
            done.append("quota-not-posted")
            continue
        # The key is kept before the card is queued: a stop between the two queues the same card again.
        entry = dict(entry, tried=at, key=entry.get("key") or "quota-task:%s:%s" % (task_id, uuid.uuid4().hex),
                     at=entry.get("at") or time.time_ns())
        tasks[task_id] = entry
        if not save(root, dict(rec, tasks=tasks)):
            done.append("quota-not-posted")
            continue
        if card is None:
            card = account_card.AccountCard(root, cfg, lambda result_file, body: None, log)
            said = _card_words(card, limited, text, log)
            rec = dict(rec, said=said)
        # In a room that is not the owner's DM the card names no account and switches none.
        built = (account_switch.limited(card.card("pending", said, ask=True)) if in_dm else
                 card.card("pending", room_words(text)))
        try:
            qid = connector_cards.queue(bridge, to, *built,
                                        key=entry["key"],
                                        at_ns=entry["at"], again=bool(entry.get("again")))
        except (OSError, ValueError) as e:
            log("quota card for %s not queued:" % task_id, "%s: %s" % (type(e).__name__, e))
            done.append("quota-not-posted")
            continue
        tasks[task_id] = dict({k: v for k, v in entry.items() if k != "again"}, qid=qid, queued=at)
        # Kept with the incident, not the task: the card's fate still counts once the task is answered.
        covers = ([c for c in rec.get("covers") or [] if isinstance(c, dict) and c.get("qid") != qid]
                  + [{"qid": qid, "queued": at}])[-COVERS_KEEP:]
        rec = dict(rec, covers=covers)
        done.append("quota-task-card")
    rec = dict(rec, tasks=tasks)
    save(root, rec)
    return rec, done


def notice(root, cfg: dict, names, text: str, log, now=time.time, session: str = "") -> str:
    """One tick's reading of the pane: `names` are the banner families on it. What was done: 'quota-card'
    (queued for the owner's DM), 'quota-card-posted', 'quota-card-dropped' (queued again later),
    'quota-card-unsure' (queued again later under the same key),
    'quota-card-unknown' (no outcome was ever recorded: not sent again), 'quota-no-room',
    'quota-not-posted' (tried again later), 'quota-cleared', 'quota-task-card xN' (that many owner tasks
    told this tick), or '' for nothing."""
    limited = [n for n in names or [] if n in PROVIDER_LIMIT_PATTERNS]
    if session and _read(root, SEEN_FILE).get("session") != session:
        # The first read of this session's pane, quiet or not, starts its history window.
        _write(root, SEEN_FILE, {"session": str(session), "since": now(), "lines": None})
    if limited:
        # A task's own words, shown on the pane, can read like the banner ("... hit its usage limit ...").
        text = own_ui(root, text)
        limited = [n for n in detector.abnormal_names(text) if n in PROVIDER_LIMIT_PATTERNS]
    if limited and not detector.limit_menu(text):
        # Words that read like a usage limit are not one: only Claude Code's own limit menu, live on the pane, is.
        limited = [n for n in limited if n != "quota-limit"]
    rec, at = load(root), now()
    if limited and _history(root, text, at, str(session or "")):
        limited = []
    if not limited:
        if rec.get("since") is None:
            return ""
        if rec.get("gone") is None:
            save(root, dict(rec, gone=at))
            return ""
        if at - float(rec["gone"]) < CLEAR_S:
            return ""
        save(root, {})
        return "quota-cleared"
    if rec.get("gone") is not None:
        rec = {k: v for k, v in rec.items() if k != "gone"}
        save(root, rec)
    bridge = str(cb.bridge_state(cfg))
    rec = dict(rec, since=rec.get("since") or at)
    rec, done = _tell_tasks(root, cfg, bridge, limited, text, log, rec, at)
    told_tasks = "quota-task-card x%d" % done.count("quota-task-card") if "quota-task-card" in done else ""
    return "; ".join(w for w in (_tell_incident(root, cfg, bridge, limited, text, log, rec, at), told_tasks) if w)


def asked(root, cfg: dict, sent, result_file, publish, log) -> str:
    """Out of usage, the owner's ask for a card is answered by the limit's card (the words last told); ''
    when no limit is in force."""
    rec = load(root)
    if not rec.get("since") or rec.get("gone") is not None or not rec.get("said"):
        return ""
    card = account_card.AccountCard(root, cfg, publish, log)
    card.say(sent.get("channel_id"), account_switch.limited(card.card("pending", str(rec["said"]), ask=True)),
             result_file)
    return "quota-card-asked"


def _tell_incident(root, cfg: dict, bridge: str, limited: list, text: str, log, rec: dict, at: float) -> str:
    """The one unasked card of the incident, in the owner's DM."""
    if not rec.get("told") and not rec.get("qid") and rec.get("covers"):
        # The owner's own messages carry the card: an unasked one besides would be the same card twice. That
        # holds once one of them is posted; while they are only queued this waits, and after a drop it sends.
        fate, waiting = _covered(bridge, rec, at)
        if fate == "pending":
            if waiting != rec.get("covers"):
                save(root, dict(rec, covers=waiting))
            return ""
        rec = {k: v for k, v in rec.items() if k != "covers"}
        if fate:
            save(root, dict(rec, told=True, by_tasks=True, **({"unknown": True} if fate == "unknown" else {})))
            return ""
        save(root, rec)
    if rec.get("told"):
        return ""
    if rec.get("qid"):
        fate = delivery(bridge, rec, at)
        if fate == "pending":
            return ""
        if fate == "posted":
            save(root, dict(rec, told=True))
            return "quota-card-posted"
        if fate == "unknown":   # it may have been posted: a second card would tell the owner twice
            save(root, dict(rec, told=True, unknown=True))
            return "quota-card-unknown"
        if fate == "unsure":    # the same key and time: the same queue id, one message at the server
            save(root, dict({k: v for k, v in rec.items() if k not in ("qid", "queued", "tried")}, again=True))
            return "quota-card-unsure"
        rec = {k: v for k, v in rec.items() if k not in ("qid", "key", "at", "queued", "tried", "again")}
        save(root, rec)
        return "quota-card-dropped"
    if at - float(rec.get("tried") or 0) < RETRY_S:
        return ""
    # The card's key is saved before it is queued: a stop between the two queues the same card again, not a second.
    rec = dict(rec, since=rec.get("since") or at, tried=at, key=rec.get("key") or uuid.uuid4().hex,
               at=rec.get("at") or time.time_ns())
    if not save(root, rec):
        return "quota-not-posted"
    to = room(root)
    if not to:
        return "quota-no-room"
    if not card_desk.usable(bridge):
        return "quota-not-posted"
    card = account_card.AccountCard(root, cfg, lambda result_file, body: None, log)
    rec = dict(rec, said=_card_words(card, limited, text, log))
    built = account_switch.limited(card.card("pending", rec["said"], ask=True))
    try:
        qid = connector_cards.queue(bridge, to, *built, key=rec["key"], at_ns=rec["at"], again=bool(rec.get("again")))
    except (OSError, ValueError) as e:
        log("quota card not queued:", "%s: %s" % (type(e).__name__, e))
        return "quota-not-posted"
    save(root, dict({k: v for k, v in rec.items() if k != "again"}, qid=qid, queued=at))
    return "quota-card"
