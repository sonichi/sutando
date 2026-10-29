"""Queue a pending question to the owner in a conversation he reads, and hold it locally.

The skill's provider-neutral half of an ask, fail-open at every step: (1) the question is
QUEUED as a proactive file — to the task's own conversation only for an owner-tier task
in the owner's own DM (`.to-<bridge>` name + `[channel:]` marker), to the owner's DM on
the task's bridge for any other bridge task, else to the owner's DM on the bridge he was
last active on; a drain delivering the file is what makes it sent; (2) the question and
its queue record are saved to the outbox (`pending_questions_outbox`), atomically; (3) the
macOS notification fires last, and a refusal prints the fix instead of a success. The
room-database adapter builds its row on top of `queue_question`; with no room, `ask_owner`
here is the whole ask and the outbox is the record.

Question and context text is embedded, never interpolated: result markers are
neutralized (`[ file:`), so no text can become a drain action.

A question with a default or options that is bound for AG2 Space is asked as a decision card
instead (`hitl.cards`; guard = ask id). Order: the card is built and its id pre-allocated, the
question is held (outbox, then the caller's row) naming that id, and only then is the card stored
(`create_card`), so no card ever exists without a durable question. Once a card exists no text is
sent; text is the ask only when the card cannot be built or stored. Nothing here sees the DM, so
the reminder never texts beside a live card. The clicks are `pending_questions_clicks`'s.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[3]  # lint-workspace-resolution: allow-repo-root
HERE = Path(__file__).resolve().parent
for _p in (REPO / "src", HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from local_record import new_name, write_text_whole
from pending_questions_outbox import Outbox, one_line_title
from proactive_routing import BRIDGE_CHANNELS, proactive_filename, should_claim_proactive
from result_markers import neutralize_markers
from undelivered_quarantine import quarantine_dir
from util_paths import host_label
from pending_questions_clicks import CARD_RUNTIME
import pending_questions_lease as lease_mod

# The only bridge that projects HITL cards (to the owner's DM).
CARD_BRIDGE = "ag2space"
# A live card with no post recorded this long after it was stored gets one text pointer, ever.
CARD_POINTER_GRACE_SEC = 1800
# A pointer claim with no sent mark this old is taken to have died or failed, and is retried.
POINTER_CLAIM_RETRY_SEC = 300
# The most one reminder pass credits a pointer claim: half its window, so no single clock jump
# releases a claim that a live claimant may still be publishing.
POINTER_MAX_STEP_SEC = 150
# A question queued and drained this recently is not re-raised by the reminder.
SENT_QUIET_SEC = 3600
# Grammar of the queue record; the reminder reads it back.
SENT_RE = re.compile(
    r"^\*\*Sent:\*\*\s+queued\s+(?P<where>.*?)\s+via\s+(?P<file>proactive-\S+\.txt)\s+at\s+"
    r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s*$", re.MULTILINE)
# Grammar of a decision-card record; whether the card is live is the HITL store's to say.
CARD_SENT_RE = re.compile(
    r"^\*\*Sent:\*\*\s+card\s+(?P<card>hitl_\S+)\s+for\s+.*?\s+at\s+"
    r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s*$", re.MULTILINE)
# Bridges whose drain honours a `[channel:]` room redirect (telegram drops it).
_ROOM_MARKER_BRIDGES = frozenset({"discord", "slack", "ag2space"})
MACOS_FIX = ("allow notifications for your terminal app under System Settings > "
             "Notifications, or run from a session where osascript is permitted")

# Positive evidence, per bridge, that the task's channel is the owner's own DM.
_DM_EVIDENCE = {
    "ag2space": lambda h: (h.get("channel_kind") or "").strip().lower() == "dm",
    "discord": lambda h: (h.get("guild_name") or "").strip() == "DM"
    and (h.get("channel_name") or "").strip() == "DM",
    "slack": lambda h: (h.get("channel_id") or "").strip().startswith("D"),
    "telegram": lambda h: True,  # the bridge delivers to the owner's chat whatever the id
}


@dataclass
class Destination:
    bridge: Optional[str] = None     # None: the owner's last-active bridge
    channel: Optional[str] = None    # the task's conversation; only an owner DM qualifies


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_ask_id(now: float) -> str:
    """Unique per call: the row key, the outbox file, the proactive file stem and the report share it."""
    return new_name("ask", now)


def entry_heading(question: str, now: float) -> str:
    return f"## {_iso(now)} — {one_line_title(question)}"


def question_dict(ask_id: str, question: str, context: Optional[str], asked_at: float,
                  default_action: Optional[str], reason: Optional[str], options, priority: str) -> dict:
    """The outbox's `question` record; the store skill's Question dataclass reads the same keys."""
    return {"ask_id": ask_id, "question": question, "context": context, "asked_at": asked_at,
            "default_action": default_action, "reason": reason,
            "options": [list(o) for o in options or ()], "priority": priority or "Medium"}


def queued_send(body: str):
    """(proactive file name, epoch) of the entry's queue record; None when it
    carries none or the stamp does not parse (a bad stamp reads as no stamp)."""
    m = SENT_RE.search(body or "")
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group("ts"), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None
    return m.group("file"), ts


def card_posted(workspace: Path, card_id: str, manager=None) -> bool:
    """The gateway recorded this live card as posted."""
    state, pj = card_state(workspace, card_id, manager)
    return state == "live" and pj.get("revision", 0) > 0


def sent_at(body: str, workspace: Optional[Path] = None) -> Optional[float]:
    """When the question last reached the owner: a drained file's queue stamp; for a card, the
    gateway's last post (`notified_at`) once it posted one, else the record's stamp."""
    q = queued_send(body)
    if q is not None:
        return q[1]
    c = card_sent(body)
    if c is None:
        return None
    if workspace is not None:
        state, pj = card_state(workspace, c[0])
        if state == "live" and pj.get("notified_at"):
            return float(pj["notified_at"])
    return c[1]


def card_pointer_body(title: str, host: str) -> str:
    """A pointer to the card, never a decision surface: no options, nothing a reply answers."""
    return (f"[dm-only]\nA question from the {host} core is waiting for you as a decision card in your "
            "AG2 Space DM, and it may not have arrived there.\n\n"
            f"\u201c{' '.join(neutralize_markers(title or '').split())}\u201d\n\n"
            "Open that card to choose; a reply to this message does not answer it.\n")


def claim_pointer(manager, card_id: str, now: float) -> Optional[str]:
    """Under the HITL store lock, for a live card with no post recorded and no pointer sent: a claim
    already on it is credited this pass and saved (pending_questions_lease), before anything else,
    so every pass counts; it is kept until its lease has run POINTER_CLAIM_RETRY_SEC. A new claim
    (token and a new lease) is stamped only when there is no live claim AND the card is past its
    grace. Returns that token, for the one claimant only."""
    with manager.store.locked():
        req = manager.get(card_id)
        if req is None or req.terminal or manager.store.projection(card_id).get("revision", 0) > 0:
            return None
        subject = req.subject or {}
        if subject.get("pointer_sent_at"):
            return None
        if subject.get("pointer_claim"):
            lease = lease_mod.advance(subject.get("pointer_lease"), now, POINTER_MAX_STEP_SEC)
            if lease != subject.get("pointer_lease"):
                subject = {**subject, "pointer_lease": lease}
                req.subject = subject
                manager.store.save(req)
            if not lease_mod.expired(lease, POINTER_CLAIM_RETRY_SEC):
                return None
        if now - req.created_at < CARD_POINTER_GRACE_SEC:
            return None
        token = os.urandom(8).hex()
        req.subject = {**subject, "pointer_claim": token, "pointer_lease": lease_mod.new(now)}
        manager.store.save(req)
        return token


def _pointer_owed(manager, card_id: str, token: str):
    """The live, unposted card this claim still holds; None otherwise. Call under the store lock."""
    req = manager.get(card_id)
    if req is None or req.terminal or (req.subject or {}).get("pointer_claim") != token:
        return None
    return None if manager.store.projection(card_id).get("revision", 0) > 0 else req


def mark_pointer_sent(manager, card_id: str, token: str, now: float) -> None:
    """Record the pointer as sent, under the store lock, for the claim that published it. A card
    settled or archived meanwhile is left as it is: saving it would revive a terminal record."""
    with manager.store.locked():
        req = manager.get(card_id)
        if req is not None and not req.terminal and (req.subject or {}).get("pointer_claim") == token:
            req.subject = {**(req.subject or {}), "pointer_sent_at": now}
            manager.store.save(req)


def send_card_pointers(questions, workspace: Path, now: Optional[float] = None, manager=None,
                       host: Optional[str] = None) -> list:
    """For each question whose card is live, has no post recorded and is older than
    CARD_POINTER_GRACE_SEC, queue a text pointer to the owner's DM. The pointer is claimed first
    (`claim_pointer`, under the HITL store lock), only the claimant publishes, and then it is
    marked sent (`pointer_sent_at`). A claim with no sent mark whose elapsed-time lease has run
    POINTER_CLAIM_RETRY_SEC (pending_questions_lease)
    (its claimant died or failed to publish) is claimed again and retried: at-least-once, so a
    death between the publish and the sent mark can deliver it twice. Overlapping reminders queue
    one pointer per claim. Just before the file is written the card is re-checked under the store
    lock (still live, unposted, this claim's); the projector records a post only after its send
    returns, so a pointer can still meet a card whose send is in flight — bounded by one projector
    send, and the pointer says only that the card may not have arrived. Never raises:
    [(ask_id, proactive file | error)]."""
    now = time.time() if now is None else now
    out = []
    for q in questions:
        c = card_sent(q.get("body", ""))
        if c is None:
            continue
        try:
            if manager is None:
                from hitl.cards import manager_for  # noqa: PLC0415
                manager = manager_for(Path(workspace))
            token = claim_pointer(manager, c[0], now)
            if token is None:
                continue
            name = proactive_filename(f"{q.get('ask_id') or c[0]}-pointer", None)
            with manager.store.locked():  # re-checked and written as one step against the store
                if _pointer_owed(manager, c[0], token) is None:
                    continue  # posted, settled or re-claimed since the claim: no pointer
                write_proactive(Path(workspace) / "results", name, card_pointer_body(q.get("title", ""),
                                                                                   host or host_label()))
            mark_pointer_sent(manager, c[0], token, now)
            out.append((q.get("ask_id"), name))
        except Exception as e:  # noqa: BLE001
            out.append((q.get("ask_id"), f"FAILED — {type(e).__name__}: {e}"))
    return out


def card_sent(body: str):
    """(card id, epoch) of the entry's decision-card record; None without one or with a bad stamp."""
    m = CARD_SENT_RE.search(body or "")
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group("ts"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None
    return m.group("card"), ts


def card_state(workspace: Path, card_id: str, manager=None) -> tuple:
    """("live" | "gone" | "unknown", projection): live is a stored card not yet settled; gone is
    no card, or a settled one; unknown is a store that cannot be read."""
    try:
        if manager is None:
            from pending_questions_clicks import manager_for  # noqa: PLC0415
            manager = manager_for(Path(workspace))
        req = manager.get(card_id)
        if req is None or req.terminal:
            return "gone", {}
        return "live", manager.store.projection(card_id)
    except Exception:  # noqa: BLE001
        return "unknown", {}


def card_live(body: str, workspace: Path, manager=None) -> bool:
    """The entry's question is asked by a card that is live, or that the store cannot rule out."""
    c = card_sent(body)
    return c is not None and card_state(workspace, c[0], manager)[0] != "gone"


def drained(results_dir: Path, name: str) -> bool:
    """A queued file some drain took: neither the file, any claim of it (`.sending`,
    `.sending.<pid>`) nor a parked copy in the undelivered/ quarantine remains."""
    if not name.startswith("proactive-"):
        return False
    p = Path(results_dir) / name
    # one scan recognises the live file and its claims together, so a transient retry that
    # renames a claim back to the live name between two checks cannot read as delivered
    for d, hit in ((Path(results_dir), lambda n: n == name or n.startswith(p.stem + ".sending")),
                   (quarantine_dir(Path(results_dir)), lambda n: n.startswith(p.stem))):
        try:  # scandir raises where glob() reads an unreadable directory as empty; fail closed
            with os.scandir(d) as it:
                if any(hit(e.name) for e in it):
                    return False
        except FileNotFoundError:
            continue
        except OSError:
            return False  # an uninspectable claim or quarantine is not evidence of delivery
    return not p.exists()


def asked_recently(body: str, results_dir: Path, now: Optional[float] = None,
                   within: float = SENT_QUIET_SEC) -> bool:
    """Queued within `within` AND drained; or a decision-card record whose card is still live. An
    undrained file is not a delivery (the reminder's own contract), so that entry stays due; a live
    card is the question's one surface, so the reminder never texts beside it. A card that is gone
    leaves the entry due."""
    q = queued_send(body)
    if q is None:
        c = card_sent(body)
        # A live card (or one the store cannot rule out) is the ask: never text beside it.
        return c is not None and card_state(Path(results_dir).parent, c[0])[0] != "gone"
    name, ts = q
    if (time.time() if now is None else now) - ts >= within:
        return False
    return drained(results_dir, name)


def destination_from_task(text: str, workspace: Optional[Path] = None) -> Destination:
    """Where the task's bridge should put the question. The task's own channel
    only for an owner-tier task in the owner's DM; any other bridge task goes to
    the owner's DM on that bridge; a non-bridge source (voice, chat, cron) yields
    the default. Headers come only from an attested shape, so a body line cannot
    supply a missing tier or DM field; unattested, the verdict is the owner's DM."""
    from task_envelope import attested_task_headers  # noqa: PLC0415
    h = attested_task_headers(text, workspace).headers
    source = (h.get("source") or "").strip()
    if source not in BRIDGE_CHANNELS:
        return Destination()
    tier = (h.get("access_tier") or "").strip().lower()
    channel = (h.get("channel_id") or h.get("source_room_id") or h.get("chat_id") or "").strip()
    if tier == "owner" and channel and _DM_EVIDENCE[source](h):
        return Destination(bridge=source, channel=channel)
    return Destination(bridge=source)


def proactive_body(question: str, context: Optional[str], host: str,
                   dest: Destination, link: Optional[str] = None, intro: Optional[str] = None):
    """(body, routed): routed is True when a `[channel:]` redirect leads the body.
    `link` is where the owner can also answer, when the store knows it; `intro` is one
    extra paragraph the store adds the first time."""
    routed = dest.bridge in _ROOM_MARKER_BRIDGES and bool(dest.channel)
    lines = [f"[channel: {dest.channel}]" if routed else "[dm-only]",
             f"Question for you from the {host} core — it needs your word:", "",
             neutralize_markers(question).strip()]
    if context and context.strip():
        lines += ["", neutralize_markers(context).strip()]
    if link:
        lines += ["", f"Reply here, or set its Status on the row: {neutralize_markers(link)}"]
    else:
        lines += ["", "Reply here."]
    body = "\n".join(lines) + "\n"
    if intro and intro.strip():
        body += "\n" + neutralize_markers(intro).strip() + "\n"
    return body, routed


def wants_card(default_action: Optional[str], options) -> bool:
    return bool((default_action or "").strip()) or any(str(a).strip() for a, _ in options or ())


def card_reaches_owner(dest: Destination, ws: Path) -> bool:
    """A card is projected only by the AG2 Space gateway, to the owner's DM: the question must be
    bound there (its task's bridge, else the bridge the owner was last active on)."""
    if dest.bridge:
        return dest.bridge == CARD_BRIDGE
    return should_claim_proactive(Path(ws) / "state" / "last-owner-activity.json", CARD_BRIDGE)


def card_choices(default_action: Optional[str], options) -> list:
    """(label, consequence) per button: with a default, the row's order (Approve first, carrying it);
    options alone are the buttons exactly as given, with no Approve the asker never proposed."""
    from pending_questions_store import ordered_options  # noqa: PLC0415
    if (default_action or "").strip():
        return ordered_options(default_action, options)
    return [(str(a).strip(), str(b).strip()) for a, b in options or () if str(a).strip()]


def card_actions(default_action: Optional[str], options) -> list:
    """(id, label) per button; `approve` only for the Approve that carries a proposed default."""
    has_default = bool((default_action or "").strip())
    return [("approve" if has_default and i == 0 else f"option-{i}", label)
            for i, (label, _) in enumerate(card_choices(default_action, options))]


def card_consequences(default_action: Optional[str], options) -> dict:
    """{action id: what it does}, kept on the card so a click reports the exact consequence."""
    return {aid: what for (aid, _), (_, what) in zip(card_actions(default_action, options),
                                                    card_choices(default_action, options))}


def card_message(question: str, context: Optional[str], default_action: Optional[str],
                 reason: Optional[str], options, link: Optional[str] = None,
                 intro: Optional[str] = None) -> str:
    parts = [question.strip()]
    if context and context.strip():
        parts.append(context.strip())
    if reason and reason.strip() and (default_action or "").strip():
        parts.append(f"Why Approve: {reason.strip()}")
    parts.append("\n".join(f"{label}: {what}" if what else label
                           for label, what in card_choices(default_action, options)))
    if link:
        parts.append(f"Or set its Status on the row: {link}")
    if intro and intro.strip():
        parts.append(intro.strip())
    return "\n\n".join(parts)


def build_card(ask_id: str, question: str, context: Optional[str], host: str,
               default_action: Optional[str], reason: Optional[str], options,
               link: Optional[str] = None, intro: Optional[str] = None):
    """The question's card, built but not stored, with its id already allocated."""
    from hitl.cards import choice_request, new_id  # noqa: PLC0415
    return choice_request(
        card_message(question, context, default_action, reason, options, link, intro),
        card_actions(default_action, options), ask_id,
        {"ask_id": ask_id, "choices": card_consequences(default_action, options)},
        runtime=CARD_RUNTIME, title=f"Question from the {host} core",
        device={"id": f"{CARD_RUNTIME}:{ask_id}", "name": host}, req_id=new_id())


def write_proactive(results: Path, name: str, body: str) -> Path:
    """Publish atomically: a drain claims proactive-*.txt on sight."""
    return write_text_whole(Path(results) / name, body)


def notify_macos(text: str) -> tuple:
    """(ok, fix). A refused or failed osascript names the fix; it never claims ok."""
    esc = text.replace("\\", "\\\\").replace('"', '\\"')[:200]
    try:
        r = subprocess.run(["osascript", "-e",
                            f'display notification "{esc}" with title "Sutando"'],
                           capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, OSError):
        return False, "osascript not found on PATH (not macOS, or a bare PATH); " + MACOS_FIX
    except subprocess.TimeoutExpired:
        return False, "osascript did not return within 15s; " + MACOS_FIX
    if r.returncode != 0:
        err = " ".join((r.stderr or "").split())[:120]
        return False, f"osascript exit {r.returncode} ({err or 'no stderr'}); {MACOS_FIX}"
    return True, None


def queue_question(question: str, context: Optional[str] = None, task_file: Optional[str] = None,
                   workspace: Optional[Path] = None, host: Optional[str] = None, now: Optional[float] = None,
                   link: Optional[str] = None, intro: Optional[str] = None,
                   default_action: Optional[str] = None, reason: Optional[str] = None,
                   options=(), priority: str = "Medium", manager=None) -> dict:
    """Hold the question in the outbox, naming how it is asked: by a card built here (a default or
    options, bound for AG2 Space) but NOT yet stored — the caller writes its row, then calls
    `create_card` — or by the proactive file, queued here. Nothing raises past here: `card_error`
    says why a card was not used, `send_error` why no file was queued, `outbox_error` why no
    record was saved — and a caller with a store must not write its row without `outbox`.
    `manager` is the HITL store handle (tests); None uses the workspace's."""
    from workspace_default import resolve_workspace  # noqa: PLC0415 — heavy loader
    ws = Path(workspace) if workspace else resolve_workspace(migrate=False)
    host = host or host_label()
    now = time.time() if now is None else now
    ask_id = new_ask_id(now)
    out = {"ask_id": ask_id, "heading": entry_heading(question, now), "workspace": str(ws), "host": host,
           "bridge": None, "channel": None, "where": None, "proactive_file": None, "send_error": None,
           "card": None, "card_error": None,
           "sent_line": None, "outbox": None, "outbox_error": None,
           "question": question_dict(ask_id, question, context, now, default_action, reason, options, priority)}
    dest = Destination()
    if task_file:
        try:
            dest = destination_from_task(Path(task_file).read_text(encoding="utf-8"), ws)
        except (OSError, UnicodeDecodeError) as e:
            out["send_error"] = f"task file unreadable ({e}); queued for the owner's DM instead"
    card = None
    if wants_card(default_action, options) and card_reaches_owner(dest, ws):
        try:
            card = build_card(ask_id, question, context, host, default_action, reason, options, link, intro)
        except Exception as e:  # noqa: BLE001 — asked as text below
            out["card_error"] = f"{type(e).__name__}: {e}"
    if card is not None:
        out["bridge"], out["where"] = CARD_BRIDGE, f"{CARD_BRIDGE} owner-dm (decision card)"
        out["sent_line"] = f"**Sent:** card {card.id} for {out['where']} at {_iso(time.time())}"
        try:
            out["outbox"] = str(Outbox(ws).save(out["question"], out["sent_line"], now))
        except Exception as e:  # noqa: BLE001 — no durable question: no card either; asked as text
            out["outbox_error"] = f"outbox: {type(e).__name__}: {e}"
            out["card_error"] = "the question could not be held, so no card names it"
            card = None
        else:
            out["card"], out["_card"], out["_manager"] = card.id, card, manager
            out["_text"] = (dest, link, intro)
            return out
    _ask_by_text(out, question, context, host, dest, link, intro)
    if out["outbox"] is None and out["outbox_error"] is None:
        try:
            out["outbox"] = str(Outbox(ws).save(out["question"], out["sent_line"], now))
        except Exception as e:  # noqa: BLE001 — reported; the caller must not proceed as if held
            out["outbox_error"] = f"outbox: {type(e).__name__}: {e}"
    return out


def _ask_by_text(out: dict, question, context, host, dest, link, intro) -> None:
    body, routed = proactive_body(question, context, host, dest, link, intro)
    out["bridge"], out["channel"] = dest.bridge, dest.channel if routed else None
    out["where"] = (f"{dest.bridge} {dest.channel}" if routed else
                    f"{dest.bridge} owner-dm" if dest.bridge else "owner-dm (last-active bridge)")
    out["card"] = None
    try:
        name = proactive_filename(out["ask_id"], dest.bridge)
        write_proactive(Path(out["workspace"]) / "results", name, body)
        out["proactive_file"] = name
    except Exception as e:  # noqa: BLE001 — the question is still recorded
        out["send_error"] = f"{type(e).__name__}: {e}"
    if out["proactive_file"]:
        out["sent_line"] = f"**Sent:** queued {out['where']} via {out['proactive_file']} at {_iso(time.time())}"
    else:
        out["sent_line"] = f"**Sent:** FAILED — {out['send_error']} at {_iso(time.time())}"


CARD_STORED, CARD_NOT_STORED, CARD_UNKNOWN, NO_CARD = "stored", "not stored", "unknown", "no card"


def create_card(out: dict, link: Optional[str] = None) -> str:
    """Store the card `queue_question` built, after the caller made the question durable. Returns
    stored | not stored | unknown, or no card for a question asked as text. A failed store whose
    card file is there (a save that raised after its rename) is stored; one that cannot be checked
    is unknown, and is kept as the card too. Only a card provably absent falls back to text."""
    card, manager = out.pop("_card", None), out.pop("_manager", None)
    dest, queued_link, intro = out.pop("_text", (Destination(), None, None))
    link = link or queued_link  # the row's own link once it was written, not the one guessed before
    if card is None:
        return NO_CARD
    from hitl.cards import manager_for  # noqa: PLC0415
    try:
        manager = manager or manager_for(Path(out["workspace"]))
        manager.create(card)
        out["card_outcome"] = CARD_STORED
        return CARD_STORED
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    try:
        present = manager is not None and manager.get(card.id) is not None
    except Exception as e:  # noqa: BLE001
        out["card_outcome"] = CARD_UNKNOWN
        out["card_note"] = f"card {card.id}: store failed ({err}) and could not be checked ({e})"
        return CARD_UNKNOWN
    if present:
        out["card_outcome"] = CARD_STORED
        out["card_note"] = f"card {card.id}: the store reported {err}, but the card is there"
        return CARD_STORED
    out["card_outcome"] = CARD_NOT_STORED
    out["card_error"] = f"card {card.id} not stored ({err})"
    card_to_text(out, dest, link, intro)
    return CARD_NOT_STORED


def card_to_text(out: dict, dest, link, intro) -> None:
    """The definite-failure fallback: the text goes first, to the question's own destination with
    its link; then the question is held again, flagged `redeliver`, naming that file, until its
    row carries the same record (the caller's rewrite, or the next reconcile's replay). A death
    before the hold leaves a record naming an absent card, which the reminder reads as due: text
    again, never silence."""
    q = out["question"]
    _ask_by_text(out, q["question"], q.get("context"), out["host"], dest, link, intro)
    try:
        out["outbox"] = str(Outbox(Path(out["workspace"])).save(q, out["sent_line"], redeliver=True))
    except Exception as e:  # noqa: BLE001
        out["outbox_error"] = f"outbox: delivery record not rewritten ({type(e).__name__}: {e})"


def ask_owner(question: str, context: Optional[str] = None, urgency: str = "live",
              task_file: Optional[str] = None, workspace: Optional[Path] = None,
              host: Optional[str] = None, now: Optional[float] = None,
              default_action: Optional[str] = None, reason: Optional[str] = None,
              options=(), priority: str = "Medium") -> dict:
    """The whole ask with no room store: queue, hold in the outbox, notify."""
    out = queue_question(question, context, task_file, workspace, host, now, None, None,
                         default_action, reason, options, priority)
    create_card(out)
    out.update({"record": f"outbox {out['outbox']}" if out["outbox"] else None,
                "db_error": out["outbox_error"] or "no room database store", "link": None,
                "macos": None, "macos_fix": None})
    if urgency == "live":
        out["macos"], out["macos_fix"] = notify_macos(f"Question: {question}")
    return out


def report_lines(out: dict) -> list:
    """What happened, one line per step; a store's own lines follow these."""
    lines = []
    if out.get("outbox") is None and out.get("record"):
        lines.append(f"recorded: {out['record']} — {out['heading'][3:]}")
    elif out.get("outbox"):
        lines.append(f"recorded: OUTBOX {out['outbox']} — {out['heading'][3:]}")
        if out.get("db_error"):
            lines.append(f"recorded: ROOM DATABASE WRITE FAILED — {out['db_error']}; the outbox holds it and "
                         "the next reconcile files it")
    else:
        lines.append(f"recorded: FAILED — {out.get('db_error')} (NOT recorded anywhere; ask by hand)")
    if out.get("link"):
        lines.append(f"row: {out['link']}")
    if out.get("card"):
        lines.append(f"stored, not yet shown: decision card {out['card']} for {out['where']} — the gateway "
                     "posts it; nothing here sees the DM. His click arrives as a `hitl_click` task: run "
                     "`src/pending_questions_reader.py clicks`")
    elif out.get("proactive_file"):
        lines.append(f"sent: queued {out['where']} via results/{out['proactive_file']} "
                     "(a bridge drain delivers it; the reminder re-raises an undrained file)")
    else:
        lines.append(f"sent: FAILED — {out.get('send_error')} (the record stands; ask by hand)")
    if out.get("send_error") and (out.get("proactive_file") or out.get("card")):
        lines.append(f"note: {out['send_error']}")
    if out.get("card_error"):
        lines.append(f"note: decision card not used ({out['card_error']}); asked as text instead")
    if out.get("card_note"):
        lines.append(f"note: {out['card_note']}; kept as the decision card, and no text was sent")
    if out.get("macos") is True:
        lines.append("macos: notification sent")
    elif out.get("macos") is False:
        lines.append(f"macos: FAILED — {out.get('macos_fix')}")
    return lines
