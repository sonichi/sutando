"""The "Your agent triggers" and "Your notices" cards: pure builders and the plan for one click.

Both cards are views over the owner's preferences on s.space, read with the account Actions
`agent.triggers.get` / `notice.prefs.get` and changed with `agent.triggers.set` / `notice.prefs.set`.
Nothing here calls them: `cards.py` does, and posts what these functions build.

Every button's command carries the revision the card was drawn from (`r<revision>=…` in its target), so a
click on an older card changes nothing and the card is redrawn from the current values. `plan` turns one
click and the freshly read document into one of: draw the card again (edit, close, keep, out of date), ask
first (a risky change: the same card redrawn with the question on it), or set (the Action's arguments,
with `expected_revision`). A card command cannot hold an agent id (it has a `:`), so it names an agent by
`agent_key`, matched against the agents the fresh read lists.
"""
from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

import agent_card as ac  # noqa: E402
import dotspace  # noqa: E402

TRIGGERS, NOTICES = "triggers", "notices"
GET = {TRIGGERS: "agent.triggers.get", NOTICES: "notice.prefs.get"}
SET = {TRIGGERS: "agent.triggers.set", NOTICES: "notice.prefs.set"}
EVENT_LABELS = {
    "room.mention": "Someone @-mentions the agent",
    "room.joined": "The agent is added to a room",
    "commons.summon": "A Commons row summons it",
    "email.received": "An email reaches its inbox",
    "schedule.fired": "A schedule it owns comes due",
    "commons.app_request": "A Commons app asks it for work",
    "owner.mentioned_by_human": "A person mentions you",
    "owner.mentioned_by_agent": "An agent mentions you",
}
# On, these let an agent read messages that mention the owner: widening, so they ask first.
WIDENING = ("owner.mentioned_by_human", "owner.mentioned_by_agent")
NOTIFIERS_MAX = 5
SEVERITIES = ("info", "action", "blocking")
SEVERITY_LABELS = {"info": "Info", "action": "Needs you", "blocking": "Blocking"}
MIN_LABELS = {"info": "Everything", "action": "Needs you", "blocking": "Blocking only"}
GROUP_LABELS = {"mentions": "Mentions", "agents": "Your agents", "asks": "Agent asks", "shortcuts": "Shortcuts"}
OFF_WARNINGS = {"agent.blocked.terminal": "You won't see when your agent is stuck. It may stop answering without telling you here.",
                "agent.needs_auth": "You won't see when your agent needs you to sign in. It may stop answering without telling you here.",
                "agent.quota": "You won't see when an agent hits its usage limit. It will just go quiet."}
OUT_OF_DATE = "This card was out of date, so I changed nothing. These are your current settings."
_TARGET = re.compile(r"r(\d{1,9})=(.+)")
_MXID = re.compile(r"@[^\s:]+:\S+")
_UNSAFE = re.compile(r"[^A-Za-z0-9._=+/:@-]")


def agent_key(mxid: str) -> str:
    """A short stable name for an agent id inside a card command."""
    return hashlib.sha256(mxid.encode("utf-8")).hexdigest()[:12]


def target(revision: int, *parts: str) -> str:
    return "=".join([f"r{int(revision)}", *parts])


def parse_target(text: str) -> Optional[tuple]:
    """(revision, [parts]) of a command target, or None when it is not one of these cards'."""
    m = _TARGET.fullmatch(text or "")
    return (int(m.group(1)), m.group(2).split("=")) if m else None


def revision_of(doc: Mapping) -> int:
    rev = doc.get("revision")
    return rev if isinstance(rev, int) and not isinstance(rev, bool) and rev >= 0 else 0


def localpart(mxid: str) -> str:
    return str(mxid or "").lstrip("@").split(":", 1)[0] or str(mxid or "")


def names_for(doc: Mapping, known: Optional[Mapping[str, str]] = None) -> dict:
    """agent id -> the name a card shows: a known display name, the listed one, else the id's localpart;
    a name two agents share gets its id."""
    known = known or {}
    raw = {}
    for a in doc.get("agents") or []:
        aid = str(a.get("agent_id") or "") if isinstance(a, dict) else ""
        if aid:
            raw[aid] = str(known.get(aid) or a.get("display_name") or localpart(aid))[:60]
    counts: dict = {}
    for name in raw.values():
        counts[name] = counts.get(name, 0) + 1
    return {aid: (name if counts[name] == 1 else f"{name} ({aid})")[:100] for aid, name in raw.items()}


def said_names(doc: Mapping) -> dict:
    """agent id -> how the owner's own reply text names it: the id's localpart in a safe charset (the
    whole id when two share one), never an agent-chosen display name."""
    raw = {}
    for a in doc.get("agents") or []:
        aid = str(a.get("agent_id") or "") if isinstance(a, dict) else ""
        if aid:
            raw[aid] = _UNSAFE.sub("", localpart(aid))[:60] or "agent"
    counts: dict = {}
    for name in raw.values():
        counts[name] = counts.get(name, 0) + 1
    return {aid: name if counts[name] == 1 else (_UNSAFE.sub("", aid)[:100] or name) for aid, name in raw.items()}


def saved_line(updated_by: object, names: Mapping[str, str], owner_name: str = "") -> str:
    """"Saved · Mini on behalf of qingyun" from a get/set result's `updated_by`."""
    if not isinstance(updated_by, dict):
        return "Saved"
    actor = str(updated_by.get("actor") or "")
    who = names.get(actor) or localpart(actor)
    if str(updated_by.get("authority") or "") == "delegation" and updated_by.get("on_behalf_of"):
        boss = str(updated_by["on_behalf_of"])
        return f"Saved · {who} on behalf of {owner_name or names.get(boss) or localpart(boss)}"
    return f"Saved · {who}" if who else "Saved"


@dataclass
class View:
    """What a card shows beyond the document: the event opened for per-agent edits, a pending question
    ({msg, yes, no, command, keep}), and the outcome line with its tone."""
    edit: str = ""
    confirm: Optional[dict] = None
    outcome: str = ""
    tone: str = ""


def _act(label: str, text: str, command: str, **kw) -> dict:
    return ac.button(label, "reply", reply=" ".join(text.split())[:300], command=command, **kw)


def _common(view: View, ask: str) -> tuple:
    """(text, buttons, note, tone, state) shared by both cards: the question first, else Refresh."""
    if view.confirm:
        c = view.confirm
        buttons = [_act(c["yes"], c["yes"], c["command"], style="primary"),
                   _act(c["no"], c["no"], c["keep"], style="secondary")]
        return f"Check this change. {c['msg']}", buttons, "Nothing changes until you confirm.", "warn", "pending"
    refresh = ac.button("Refresh", "reply", style="secondary", reply=ask)
    return "", [refresh], view.outcome, view.tone or ("ok" if view.outcome.startswith("Saved") else "dim"), "info"


# ── "Your agent triggers" ─────────────────────────────────────────────────────

def _effective(doc: Mapping, agent: str, event: str) -> bool:
    a = next((x for x in doc.get("agents") or [] if isinstance(x, dict) and x.get("agent_id") == agent), {})
    if event in (a.get("settings") or {}):
        return bool(a["settings"][event])
    et = next((e for e in doc.get("event_types") or [] if e.get("event_type") == event), {})
    return bool(et.get("all_agents", et.get("registry_default")))


def triggers_card(doc: Mapping, view: Optional[View] = None, *, names: Optional[Mapping[str, str]] = None) -> tuple:
    """(body, card): each event type once for all agents ("on for N of M"), one opened for per-agent
    edits behind Edit, then the ranked notifiers."""
    view = view or View()
    names, said = names or names_for(doc), said_names(doc)
    rev = revision_of(doc)
    agents = [a for a in doc.get("agents") or [] if isinstance(a, dict) and a.get("agent_id")]
    total = f"{len(agents)}{'+' if doc.get('agents_truncated') else ''}"
    notifiers = [n for n in doc.get("notifiers") or [] if isinstance(n, str)]
    event_rows, editor = [], None
    for et in doc.get("event_types") or []:
        ev = str(et.get("event_type") or "")
        if ev not in EVENT_LABELS:
            continue
        label = EVENT_LABELS[ev]
        own = sum(1 for a in agents if ev in (a.get("overrides") or {}))
        on = bool(et.get("all_agents", et.get("registry_default")))
        sub = f"{ev} · on for {et.get('agents_on', 0)} of {total}"
        sub += f" · {own} own setting{'s' if own != 1 else ''}" if own else ""
        actions = []
        if et.get("configurable"):
            verb = "all-off" if on else "all-on"
            actions.append(_act("Turn off" if on else "Turn on", f"Turn {'off' if on else 'on'} \"{label}\" for all agents",
                                ac.reply(TRIGGERS, target(rev, ev), verb)))
            opened = view.edit == ev
            actions.append(_act("Close" if opened else "Edit", f"{'Close' if opened else 'Edit'} \"{label}\" per agent",
                                ac.reply(TRIGGERS, target(rev, ev), "close" if opened else "edit")))
        else:
            sub += f" · set by {dotspace.SUCCESSOR_SERVER}"
        event_rows.append(ac.row(label, subtitle=sub[:300], state="on" if on else "off", actions=actions))
        if view.edit == ev and et.get("configurable") and agents:
            rows = []
            for a in agents[:50]:
                aid = a["agent_id"]
                v, mine = _effective(doc, aid, ev), ev in (a.get("overrides") or {})
                acts = [_act("Turn off" if v else "Turn on", f"Turn {'off' if v else 'on'} \"{label}\" for {said[aid]}",
                             ac.reply(TRIGGERS, target(rev, ev, agent_key(aid)), "agent-off" if v else "agent-on"))]
                if mine:
                    acts.append(_act("Use all-agents", f"{said[aid]} follows all agents for \"{label}\"",
                                     ac.reply(TRIGGERS, target(rev, ev, agent_key(aid)), "agent-follow")))
                rows.append(ac.row(names.get(aid, aid)[:120], tag="1st notifier" if notifiers[:1] == [aid] else "",
                                   subtitle=f"{'On' if v else 'Off'} · {'own setting' if mine else 'follows all agents'}",
                                   state="on" if v else "off", actions=acts,
                                   avatar_=ac.avatar(mxid=aid) if _MXID.fullmatch(aid) else None))
            editor = ac.group(f"{label}: each agent"[:80], rows)
    nf_rows = []
    for i, aid in enumerate(notifiers[:NOTIFIERS_MAX]):
        name, who = names.get(aid, localpart(aid)), said.get(aid) or _UNSAFE.sub("", aid)
        acts = []
        if i:
            acts.append(_act("↑", f"Move {who} up in my notifiers", ac.reply(TRIGGERS, target(rev, "nf", agent_key(aid)), "nf-up")))
        acts.append(_act("Remove", f"Remove {who} from my notifiers", ac.reply(TRIGGERS, target(rev, "nf", agent_key(aid)), "nf-remove")))
        nf_rows.append(ac.row(f"{i + 1}. {name}"[:120], avatar_=ac.avatar(mxid=aid) if _MXID.fullmatch(aid) else None,
                              actions=acts))
    candidates = [a["agent_id"] for a in agents if a["agent_id"] not in notifiers][:8]
    if len(notifiers) < NOTIFIERS_MAX and candidates:
        items = [_act(names.get(aid, aid)[:80], f"Add {said[aid]} to my notifiers",
                      ac.reply(TRIGGERS, target(rev, "nf", agent_key(aid)), "nf-add")) for aid in candidates]
        nf_rows.append(ac.row("Add a notifier", subtitle="Up to 5, tried in order",
                              actions=[ac.button("+ Add", "menu", items=items)]))
    groups = [ac.group("Events · one setting for all agents", event_rows)] if event_rows else []
    if editor:
        groups.append(editor)
    if nf_rows:
        groups.append(ac.group("Tell me about my mentions through (in order)", nf_rows))
    text, buttons, note, tone, state = _common(view, "show my triggers")
    if not text:
        text = ("The first online agent in this list tells you when you are mentioned; with none listed, the "
                "agent that joined the room first does. Edit sets one agent apart from the all-agents setting.")
    return ac.build(TRIGGERS, state, title="Your agent triggers", avatar_=ac.avatar(initial="T"),
                    subtitle=f"{total} agent{'s' if total != '1' else ''}", groups=groups or None, text=text,
                    buttons=buttons, note=note[:600], note_tone=tone if note else "",
                    body=f"Your agent triggers: {len(event_rows)} event types across {total} agents.")


# ── "Your notices" ────────────────────────────────────────────────────────────

def notices_card(doc: Mapping, view: Optional[View] = None) -> tuple:
    """(body, card): the minimum severity, then a toggle per notice kind in its group."""
    view = view or View()
    rev = revision_of(doc)
    current = str(doc.get("min_severity") or "info")
    min_row = ac.row(MIN_LABELS.get(current, current), subtitle="Notices below this stay out of your dock", state="on",
                     actions=[_act(MIN_LABELS[s], f"Show at least: {MIN_LABELS[s]}",
                                   ac.reply(NOTICES, target(rev, "min"), f"min-{s}")) for s in SEVERITIES if s != current])
    grouped: dict = {}
    chosen_any = current != "info"
    for k in doc.get("kinds") or []:
        kind = str(k.get("kind") or "") if isinstance(k, dict) else ""
        if not kind:
            continue
        label = str(k.get("label") or kind)[:120]
        on, shown, chosen = bool(k.get("enabled")), bool(k.get("shown", k.get("enabled"))), bool(k.get("chosen"))
        chosen_any = chosen_any or chosen
        sub = SEVERITY_LABELS.get(str(k.get("severity") or ""), str(k.get("severity") or ""))
        sub += " · your choice" if chosen else f" · default {'on' if k.get('default_enabled') else 'off'}"
        sub += " · below your minimum" if on and not shown else ""
        acts = [_act("Turn off" if on else "Turn on", f"Turn {'off' if on else 'on'} \"{label}\" in my dock",
                     ac.reply(NOTICES, target(rev, kind), "off" if on else "on"))]
        if chosen:
            acts.append(_act("Use default", f"Use the default for \"{label}\"", ac.reply(NOTICES, target(rev, kind), "default")))
        grouped.setdefault(str(k.get("group") or ""), []).append(
            ac.row(label, subtitle=sub, state="on" if shown else "warn" if on else "off", actions=acts))
    groups = [ac.group("Show at least", [min_row])]
    groups += [ac.group(GROUP_LABELS.get(g, g.title() or "Other"), rows) for g, rows in grouped.items()]
    text, buttons, note, tone, state = _common(view, "show my notices")
    if not view.confirm and chosen_any:
        buttons.insert(0, _act("Reset to defaults", "Reset my notices to the defaults", ac.reply(NOTICES, target(rev, "all"), "reset"),
                               style="secondary"))
    n_on = sum(1 for g in grouped.values() for r in g if r["state"] == "on")
    n_all = sum(len(g) for g in grouped.values())
    return ac.build(NOTICES, state, title="Your notices", avatar_=ac.avatar(initial="N"),
                    subtitle="What may show above your message box", status=f"{n_on} of {n_all} shown" if n_all else "",
                    groups=groups, text=text, buttons=buttons, note=note[:600], note_tone=tone if note else "",
                    body=f"Your notices: {n_on} of {n_all} kinds show above your message box, at least "
                         f"{MIN_LABELS.get(current, current)}.")


# ── one click ─────────────────────────────────────────────────────────────────

@dataclass
class Plan:
    """`draw` (the card again, with `view`) or `set` (`arguments` for the set Action, then draw with
    `change` as the outcome); `refused` for a command no card offers."""
    step: str
    view: View = field(default_factory=View)
    arguments: dict = field(default_factory=dict)
    change: str = ""
    why: str = ""


def _ask(said: ac.Reply, msg: str, yes: str, no: str, edit: str = "") -> Plan:
    return Plan("draw", View(edit=edit, confirm={
        "msg": msg, "yes": yes[:80], "no": no[:80],
        "command": ac.reply(said.kind, said.target, said.verb, confirmed=True),
        "keep": ac.reply(said.kind, said.target, "keep")}))


def plan(said: ac.Reply, doc: Mapping, names: Optional[Mapping[str, str]] = None) -> Plan:
    """What one click on a triggers or notices card does, against `doc` as read just now."""
    parsed = parse_target(said.target)
    if parsed is None or said.kind not in (TRIGGERS, NOTICES):
        return Plan("refused", why=f"a {said.kind} card has no {said.verb!r} action")
    rev, parts = parsed
    if rev != revision_of(doc):
        return Plan("draw", View(outcome=OUT_OF_DATE, tone="warn"))
    if said.verb == "keep":
        return Plan("draw", View(edit=parts[0] if said.kind == TRIGGERS and parts[0] in EVENT_LABELS else "",
                                 outcome="Okay, I left it as it is.", tone="dim"))
    planner = _plan_triggers if said.kind == TRIGGERS else _plan_notices
    out = planner(said, parts, doc, names or names_for(doc))
    if out.step == "set":
        out.arguments["expected_revision"] = rev
    return out


def _plan_triggers(said: ac.Reply, parts: list, doc: Mapping, names: Mapping[str, str]) -> Plan:
    head, verb = parts[0], said.verb
    agents = [a["agent_id"] for a in doc.get("agents") or [] if isinstance(a, dict) and a.get("agent_id")]
    by_key = {agent_key(a): a for a in agents}
    configurable = {e.get("event_type") for e in doc.get("event_types") or [] if e.get("configurable")}
    notifiers = [n for n in doc.get("notifiers") or [] if isinstance(n, str)]
    if head == "nf" and len(parts) == 2 and verb in ("nf-up", "nf-remove", "nf-add"):
        aid = by_key.get(parts[1]) or next((n for n in notifiers if agent_key(n) == parts[1]), None)
        if aid is None:
            return Plan("draw", View(outcome=OUT_OF_DATE, tone="warn"))
        name, order = names.get(aid, localpart(aid)), list(notifiers)
        if verb == "nf-add":
            if aid in order or len(order) >= NOTIFIERS_MAX:
                return Plan("draw", View(outcome="That notifier list is already full or has it.", tone="warn"))
            order.append(aid)
            change = f"{name} added to your notifiers"
        elif aid not in order:
            return Plan("draw", View(outcome=OUT_OF_DATE, tone="warn"))
        elif verb == "nf-up":
            i = order.index(aid)
            if i == 0:
                return Plan("draw", View(outcome=f"{name} is already first.", tone="dim"))
            order[i - 1], order[i] = order[i], order[i - 1]
            change = f"{name} moved up to {i}"
        else:
            order.remove(aid)
            change = f"{name} removed from your notifiers"
        return Plan("set", arguments={"notifiers": order}, change=change)
    if head not in configurable or head not in EVENT_LABELS:
        return Plan("refused", why=f"{head} is not an event type you can change here")
    label = EVENT_LABELS[head]
    if verb in ("edit", "close") and len(parts) == 1:
        return Plan("draw", View(edit=head if verb == "edit" else ""))
    if verb in ("all-on", "all-off") and len(parts) == 1:
        to = verb == "all-on"
        if not said.confirmed and to and head in WIDENING:
            return _ask(said, f"All {len(agents)} agents will read and may answer any message that mentions you.",
                        "Turn on for all", "Keep off")
        if not said.confirmed and not to and head == "owner.mentioned_by_human":
            return _ask(said, "No agent will tell you when a person mentions you.", "Turn off for all", "Keep on")
        return Plan("set", arguments={"all": {head: to}}, change=f"\"{label}\" {'on' if to else 'off'} for all agents")
    if verb in ("agent-on", "agent-off", "agent-follow") and len(parts) == 2:
        aid = by_key.get(parts[1])
        if aid is None:
            return Plan("draw", View(edit=head, outcome=OUT_OF_DATE, tone="warn"))
        name = names.get(aid, localpart(aid))
        if verb == "agent-follow":
            et = next(e for e in doc["event_types"] if e.get("event_type") == head)
            to, value = bool(et.get("all_agents", et.get("registry_default"))), None
            change = f"{name} follows all agents for \"{label}\""
        else:
            to = value = verb == "agent-on"
            change = f"\"{label}\" {'on' if to else 'off'} for {name}"
        widens = to and head in WIDENING and not _effective(doc, aid, head)
        if not said.confirmed and widens:
            return _ask(said, f"{name} will read and may answer any message that mentions you.", "Turn on", "Keep off", head)
        if not said.confirmed and not to and head == "owner.mentioned_by_human" and notifiers[:1] == [aid]:
            return _ask(said, f"{name} is your 1st notifier. Off, it won't tell you when a person mentions you; "
                              "the next one in your list does.", "Turn off", "Keep on", head)
        return Plan("set", View(edit=head), arguments={"agents": {aid: {head: value}}}, change=change)
    return Plan("refused", why=f"a triggers card has no {verb!r} action")


def _plan_notices(said: ac.Reply, parts: list, doc: Mapping, names: Mapping[str, str]) -> Plan:
    head, verb = parts[0], said.verb
    if len(parts) != 1:
        return Plan("refused", why="not a notices card target")
    if head == "all" and verb == "reset":
        if not said.confirmed:
            return _ask(said, "Reset to defaults? Person mentions on, anything that blocks your agents on, the rest as "
                              "shipped.", "Reset", "Keep mine")
        return Plan("set", arguments={"reset": True}, change="Reset to defaults")
    if head == "min" and verb.startswith("min-") and verb[4:] in SEVERITIES:
        to = verb[4:]
        if not said.confirmed and to == "blocking":
            return _ask(said, "Mentions and approvals won't show here any more, only blocking notices.",
                        "Change it", "Keep " + MIN_LABELS.get(str(doc.get("min_severity") or "info"), "it"))
        return Plan("set", arguments={"min_severity": to}, change=f"Show at least: {MIN_LABELS[to]}")
    kind = next((k for k in doc.get("kinds") or [] if isinstance(k, dict) and k.get("kind") == head), None)
    if kind is None or verb not in ("on", "off", "default"):
        return Plan("refused", why=f"a notices card has no {verb!r} action for {head}")
    label = str(kind.get("label") or head)
    value = None if verb == "default" else verb == "on"
    lands_on = bool(kind.get("default_enabled")) if value is None else value
    if not said.confirmed and not lands_on and kind.get("enabled") and kind.get("severity") == "blocking":
        return _ask(said, OFF_WARNINGS.get(head, "You won't see these blocking notices any more."), "Turn off", "Keep on")
    return Plan("set", arguments={"kinds": {head: value}},
                change=f"\"{label}\": {'default' if value is None else 'on' if value else 'off'}")


# ── the occasional offer ──────────────────────────────────────────────────────

OFFER_EVERY_S = 7 * 86400
DISMISSALS_FOR_OFFER = 3


def offer_due(state: Mapping, now: float, event_types: list) -> Optional[str]:
    """Why the one-line offer is due now ("new_event_type" or "dismissed:<kind>"), or None: at most once a
    week, and only after an event type appeared that the last offer did not know, or 3+ dismissals of one
    notice kind since then."""
    if now - float(state.get("last_offered_at") or 0) < OFFER_EVERY_S:
        return None
    known = state.get("event_types")
    if isinstance(known, list) and set(event_types) - set(known):
        return "new_event_type"
    counts = state.get("dismissals") if isinstance(state.get("dismissals"), dict) else {}
    kind = next((k for k, n in sorted(counts.items()) if isinstance(n, int) and n >= DISMISSALS_FOR_OFFER), None)
    return f"dismissed:{kind}" if kind else None


def offer_card() -> tuple:
    return ac.build("prefs-offer", "info", title="Review what reaches you?", avatar_=ac.avatar(initial="?"),
                    text="Want to review what I watch for you and what reaches your dock?",
                    buttons=[ac.button("Show my triggers", "reply", style="primary", reply="show my triggers"),
                             ac.button("Show my notices", "reply", style="secondary", reply="show my notices"),
                             ac.button("Not now", "reply", style="secondary", reply="Not now",
                                       command=ac.reply("prefs-offer", "", "not-now"))],
                    body="Want to review what I watch for you and what reaches your dock? Say \"show my triggers\" "
                         "or \"show my notices\".")
