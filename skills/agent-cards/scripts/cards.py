#!/usr/bin/env python3
"""The owner's "show my …" cards (agent panel, src/agent_card.py) and the replies their buttons send.

    python3 cards.py [--workspace WS] show connections|workers|apps|circle --task-file PATH [--print]
    python3 cards.py [--workspace WS] reply --task-file PATH

`show` builds one card from what this machine already knows and posts it as the core's own agent in the
owner's checked DM, as a reply to the asking message, and prints {"event_id"}; `--print` prints
{"body", "card"} and posts nothing. Every card carries the owner's own data, so it is refused for anyone
but the owner and in any room but their DM.

- connections: `connections.list_connections` + `status_for` (what `scripts/connections.py list --json`
  prints): core rows, workers' own agents only as a footer count; the chat-app bridges from
  src/chat_bridges.py (health-check's reading).
- workers: `pool_manage.listing` and each worker's own identity (`connections.worker_identity`).
- apps: the Station connections connect-apps reads (`connectors.Cloud.connection_rows`, its 30 s cache),
  split into Connected and Not connected (signed out, or needed by a request that is waiting for it); a
  Not connected row and "+ Connect an app" sign the owner in on the card, and `show` starts any waiter
  that is not running, so a waiting request resumes once its app is connected.
- circle: the collaboration map's quick-lookup index (collaboration-intelligence), else an empty state.

`reply` handles the owner's click on one of these cards (agent_card.click_verdict decides; a click that
matches no card this agent posted prints the fixed refusal and nothing acts): Reconnect and Make main run
connections' own units; Pause and Restart run pool_manage's. Disconnect and Retire never act on the
first click: it posts a confirm card, and only that card's own "Yes" (the `confirmed` command, offered
nowhere else, used once, live for ten minutes) runs the unit. Switch account hands over to connect-apps.
Every command prints one JSON object. Exit 0 done, 1 a negative answer, 2 refused.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Callable, Optional

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
_SKILLS = _REPO / "skills"
for _p in (str(_REPO / "src"), str(_SKILLS / "worker-pool" / "scripts"), str(_SKILLS / "connect-apps" / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agent_card as ac  # noqa: E402
import connections  # noqa: E402
import dotspace
import issued_cards

KINDS = ("connections", "workers", "apps", "circle")
HANDLER = "skills/agent-cards/scripts/cards.py reply"
# The first click on one of these only asks; what the confirm card says under the question.
DESTRUCTIVE = {("connections", "disconnect"): "Its token is kept, so you can reconnect later.",
               ("workers", "retire"): "It stops for good and loses its own agent."}
CONFIRM_TTL_S = 10 * 60
RUNTIMES = {"claude": "Claude", "codex": "Codex"}
STATE_DOT = {"connected": "on", "failed": "warn", "starting": "warn", "stale": "warn"}
_MXID = re.compile(r"@[^\s:]+:\S+")
_SLUG = re.compile(r"[a-z0-9_]{1,64}")


def _emit(payload: dict, code: int = 0) -> int:
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    return code


# ── builders (pure) ───────────────────────────────────────────────────────────

def _act(label: str, text: str, command: str, **kw) -> dict:
    """A reply button that sends `text`, a sentence the owner reads, and acts as `command` (issued_cards)."""
    return ac.button(label, "reply", reply=" ".join(text.split())[:300], command=command, **kw)


def connections_card(rows: list, bridges: list) -> tuple:
    """(body, card) for the owner's own connections: the core's servers, then the chat-app bridges."""
    core = [r for r in rows if not r.get("worker")]
    workers = [r for r in rows if r.get("worker") and not r.get("disabled")]
    server_rows = []
    servers = [str(r.get("server") or r.get("instance") or "") for r in core]
    for r in core:
        inst, state = str(r.get("instance") or ""), str(r.get("state") or "")
        name = str(r.get("server") or inst)
        name += f" ({inst})" if servers.count(name) > 1 and inst else ""
        dot = "off" if r.get("disabled") or r.get("parked") else STATE_DOT.get(state, "off")
        actions = []
        if inst and state != "connected" and not r.get("parked"):
            actions.append(_act("Reconnect", f"Reconnect {name}", ac.reply("connections", inst, "reconnect")))
        items = []
        if inst and not r.get("primary") and state == "connected":
            items.append(_act("Make main", f"Make {name} my main connection", ac.reply("connections", inst, "make-main")))
        if inst and not r.get("primary") and not r.get("disabled"):
            items.append(_act("Disconnect", f"Disconnect {name}", ac.reply("connections", inst, "disconnect")))
        if items:
            actions.append(ac.button("⋯", "menu", items=items))
        who = r.get("agent_mxid")
        server_rows.append(ac.row(
            str(r.get("agent_display_name") or r.get("server") or inst or dotspace.SUCCESSOR_SERVER),
            subtitle=" · ".join(x for x in (str(r.get("server") or ""), state.replace("_", " ")) if x),
            tag="Main" if r.get("primary") else "", state=dot,
            avatar_=ac.avatar(mxid=str(who)) if who else ac.avatar(seed=str(r.get("server") or "s")),
            actions=actions))
    bridge_rows = [ac.row(b["name"], state={True: "on", False: "off"}.get(b["running"], "warn"),
                          subtitle={True: "Running", False: "Set up, not running"}.get(b["running"], "State unknown"),
                          avatar_=ac.avatar(app=b["channel"], shape="square")) for b in bridges]
    groups = [g for g in (ac.group("Servers", server_rows) if server_rows else None,
                          ac.group("Chat apps", bridge_rows) if bridge_rows else None) if g]
    badges = [{"label": str(r.get("server") or dotspace.SUCCESSOR_SERVER), "state": "on" if r.get("state") == "connected" else "off"}
              for r in core][:8]
    footer = None
    if workers:
        n = len(workers)
        footer = ac.button(f"{n} worker{'s have' if n != 1 else ' has'} {'their' if n != 1 else 'its'} own "
                           f"agent{'s' if n != 1 else ''} · see Workers →", "reply", reply="show my workers")
    up = sum(1 for r in core if r.get("state") == "connected")
    return ac.build("connections", "info", title="Your connections", avatar_=ac.avatar(initial="C"),
                    status=f"{up} of {len(core)} connected" if core else "",
                    badges=badges, groups=groups, footer_link=footer,
                    text="" if groups else "No connections are set up on this Mac yet.",
                    body=f"Your connections: {up} of {len(core)} servers connected"
                         + (f", {sum(1 for b in bridges if b['running'])} of {len(bridges)} chat apps running." if bridges else "."))


def workers_card(workers: list, identities: dict, runtimes: dict) -> tuple:
    """(body, card) for the pool's workers: runtime, own agent, status, and what the owner can do."""
    rows = []
    names = [str(w["name"]) for w in workers]
    for w in workers:
        wid, ident = w["worker_id"], identities.get(w["worker_id"]) or {}
        who = str(w["name"]) + (f" ({wid})" if names.count(str(w["name"])) > 1 else "")
        retired = w.get("state") == "retired"
        status = ("Retired" if retired else "Paused" if w.get("paused")
                  else "Running" if w.get("session_alive") else "Stopped")
        own = f"Own agent on {ident.get('server')}" if ident.get("agent_mxid") else "Answers through your agent"
        actions = []
        if ident.get("agent_mxid") and not retired:
            actions.append(ac.button("Message", "open_dm", user_id=str(ident["agent_mxid"])))
        if not retired:
            items = [_act("Restart", f"Restart {who}", ac.reply("workers", wid, "restart"))]
            if not w.get("paused"):
                items.insert(0, _act("Pause", f"Pause {who}", ac.reply("workers", wid, "pause")))
            items.append(_act("Retire", f"Retire {who}", ac.reply("workers", wid, "retire")))
            actions.append(ac.button("⋯", "menu", items=items))
        runtime = str(runtimes.get(wid) or "claude").lower()
        rows.append(ac.row(str(w["name"]), tag=RUNTIMES.get(runtime, runtime.title()), subtitle=f"{status} · {own}",
                           state="off" if retired or w.get("paused") else "on" if w.get("session_alive") else "warn",
                           avatar_=(ac.avatar(mxid=str(ident["agent_mxid"])) if ident.get("agent_mxid")
                                    else ac.avatar(seed=str(w["name"]))),
                           actions=actions))
    live = [w for w in workers if w.get("state") != "retired"]
    return ac.build("workers", "info", title="Your workers", avatar_=ac.avatar(initial="W"),
                    groups=[ac.group("Workers", rows[:100])] if rows else None,
                    text="" if rows else "You have no workers yet.",
                    buttons=[ac.button("+ New worker", "reply", style="secondary", reply="create a worker")],
                    body=f"Your workers: {len(live)} in the pool.")


def apps_card(rows: list, pending: Optional[list] = None) -> tuple:
    """(body, card) for the owner's apps: one row per connected account, then the apps that are not
    connected (signed out, or `pending`: [{slug, name}] a waiting request needs), each with a Connect
    that signs the owner in on the card."""
    on, off, live, seen = [], [], set(), set()
    for r in rows:
        slug = str(r.get("toolkit") or "").lower()
        if _SLUG.fullmatch(slug) and str(r.get("status") or "").upper() == "ACTIVE":
            live.add(slug)
            name = str(r.get("name") or slug)
            sub = str(r.get("accountLabel") or "") + (" · default" if r.get("isDefault") else "")
            on.append(ac.row(name, subtitle=sub.strip(" ·"), state="on", avatar_=ac.avatar(app=slug, shape="square"),
                             actions=[_act("Switch account", f"Switch my {name} account", ac.reply("apps", slug, "switch"))]))
    known = [(str(r.get("toolkit") or "").lower(), str(r.get("name") or ""), "Signed out", str(r.get("accountLabel") or ""))
             for r in rows] + [(str(p.get("slug") or ""), str(p.get("name") or ""), "Needed for a request you made", "")
                               for p in pending or []]
    for slug, name, why, label in known:
        if not _SLUG.fullmatch(slug) or slug in live or slug in seen:
            continue
        seen.add(slug)
        off.append(ac.row(name or slug, subtitle=why + (f" · {label}" if label else ""), state="warn",
                          avatar_=ac.avatar(app=slug, shape="square"),
                          actions=[ac.button("Reconnect" if why == "Signed out" else "Connect", "connect", toolkit=slug)]))
    groups = [g for g in (ac.group("Connected", on[:80]) if on else None,
                          ac.group("Not connected", off[:20]) if off else None) if g]
    return ac.build("apps", "info", title="Your apps", avatar_=ac.avatar(initial="A"),
                    status=f"{len(live)} connected" if live else "", groups=groups,
                    text="" if groups else "No apps are connected yet.",
                    buttons=[ac.button("+ Connect an app", "connect_picker", style="secondary")],
                    note="Disconnecting happens in Settings → Integrations.", note_tone="dim",
                    body=f"Your apps: {len(live)} connected"
                         + (f", {len(off)} not connected" if off else "") + ". Manage them in Settings → Integrations.")


def confirm_card(said: ac.Reply, words: str, *, now: float) -> tuple:
    """(body, card) asking the owner whether to do `words` ("Retire Pylon"): the only card that offers the
    confirmed command, beside a Keep that drops it."""
    what, _, name = words.partition(" ")
    yes, keep = f"Yes, {what.lower()} {name}"[:80], f"Keep {name}"[:80]
    return ac.build(f"confirm:{said.kind}:{said.target or '-'}:{said.verb}", "pending", title=f"{words}?"[:120],
                    avatar_=ac.avatar(initial="?"), text=DESTRUCTIVE[(said.kind, said.verb)],
                    buttons=[ac.button(yes, "reply", style="primary", reply=yes,
                                       command=ac.reply(said.kind, said.target, said.verb, confirmed=True)),
                             ac.button(keep, "reply", style="secondary", reply=keep,
                                       command=ac.reply(said.kind, said.target, "keep"))],
                    note="Nothing changes until you confirm.", note_tone="dim",
                    expires_at_ms=int((now + CONFIRM_TTL_S) * 1000),
                    body=f"{words}? {DESTRUCTIVE[(said.kind, said.verb)]} Confirm on the card.")


def circle_card(entities: list) -> tuple:
    """(body, card) for who the owner works with, from the collaboration map; the app shapes a member's
    own avatar, and an initial is round for a person and square for an agent."""
    people, agents = [], []
    for e in entities:
        if not isinstance(e, dict):
            continue
        eid, kind = str(e.get("entity_id") or ""), str(e.get("kind") or "")
        name = str(e.get("name") or e.get("display_name") or eid)
        if not name or kind not in ("human", "agent"):
            continue
        shape = "square" if kind == "agent" else "round"
        av = ac.avatar(mxid=eid) if _MXID.fullmatch(eid) else ac.avatar(initial=name[:1].upper(), shape=shape)
        item = ac.row(name[:120], subtitle=str(e.get("one_line") or "")[:300], avatar_=av,
                      actions=[ac.button("Message", "open_dm", user_id=eid)] if _MXID.fullmatch(eid) else None)
        (agents if kind == "agent" else people).append(item)
    groups = [g for g in (ac.group("People", people[:50]) if people else None,
                          ac.group("Agents", agents[:50]) if agents else None) if g]
    return ac.build("circle", "info", title="Who you work with", avatar_=ac.avatar(initial="Y"), groups=groups,
                    text="" if groups else "I haven't recorded who I work with yet.",
                    body=(f"Who you work with: {len(people)} people and {len(agents)} agents." if groups
                          else "I haven't recorded who I work with yet."))


# ── reading this machine ──────────────────────────────────────────────────────

def read_connections(ws: Path) -> tuple:
    import chat_bridges
    from util_paths import claude_home_path
    from sutando_config import resolve_dotenv
    rows = [connections.status_for(r, ws) for r in connections.list_connections(ws)]
    return rows, chat_bridges.states(claude_home_path("channels"), resolve_dotenv(_REPO, ws))


def read_workers(ws: Path) -> tuple:
    import pool_manage
    import pool_roster
    workers = pool_manage.listing(ws)
    roster = (pool_roster.load_roster(ws) or {}).get("workers") or {}
    identities = {w["worker_id"]: connections.worker_identity(w["worker_id"], ws) or {} for w in workers}
    return workers, identities, {wid: (row or {}).get("runtime") for wid, row in roster.items()}


def read_apps(ws: Path) -> tuple:
    """(connection rows, [{slug, name}] the apps a waiting request needs)."""
    import connectors
    cloud = connectors.Cloud(ws)
    cloud.cache = connectors.ConnectCache(ws)
    pending = [{"slug": k.get("slug"), "name": k.get("name")} for m in connectors.list_markers(ws)
               for k in m.get("toolkits") or [] if isinstance(k, dict)]
    return cloud.connection_rows(cached=True), pending


def arm_waits(ws: Path, spawn: Optional[Callable] = None) -> list:
    """Every connect wait on disk with its waiter running, started here when it was not: the ids. The
    client says nothing when a card's Connect succeeds; the waiter is what resumes the request."""
    import connectors
    out = []
    for marker in connectors.list_markers(ws):
        wait_id = marker["wait_id"]
        if connectors.waiter_alive(ws, wait_id) or connectors._try_spawn(spawn or connectors.spawn_waiter, ws, wait_id):
            out.append(wait_id)
    return out


def _yaml_load(text: str):
    import yaml
    return yaml.safe_load(text)


def read_circle(ws: Path, load: Callable[[str], object] = _yaml_load) -> list:
    """The collaboration map's recent entities: under `quick_lookup`, or at the top of a bare map."""
    path = ws / "data" / "collaboration-intelligence" / "quick-lookup.yaml"
    if not path.is_file():
        return []
    try:
        raw = load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — no PyYAML or an unreadable index is an empty map here
        return []
    q = raw.get("quick_lookup") or raw if isinstance(raw, dict) else {}
    return list(q.get("recent_entities") or []) if isinstance(q, dict) else []


def build_kind(kind: str, ws: Path) -> tuple:
    if kind == "connections":
        return connections_card(*read_connections(ws))
    if kind == "workers":
        return workers_card(*read_workers(ws))
    if kind == "apps":
        return apps_card(*read_apps(ws))
    return circle_card(read_circle(ws))


# ── the owner gate ────────────────────────────────────────────────────────────

def _core(ws: Path) -> dict:
    import pool_provision
    return pool_provision.core_lane(ws, None)


def gate(ws: Path, task_text: str, core: Optional[dict] = None) -> tuple:
    """(headers, card reply, owner DM, refusal or None) for a task asking for or clicking a card."""
    headers, said = ac.reply_words(task_text)
    core = core if core is not None else _core(ws)
    why = ac.owner_may_act(headers, str(core.get("owner_mxid") or ""))
    dm = connections.owner_dm(core, ws)
    if why is None and (not dm or str(headers.get("channel_id") or "") != dm):
        why = "these cards carry your own data, so I show them only in our DM"
    return headers, said, dm, why


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_show(ws: Path, args, *, core=None, execute=None, spawn=None) -> int:
    text = Path(args.task_file).read_text(encoding="utf-8")
    try:
        core = core if core is not None else _core(ws)
        headers, _, dm, why = gate(ws, text, core)
    except Exception as e:  # noqa: BLE001 — no core lane: nothing can be shown as the core
        return _emit({"status": "refused", "why": f"{type(e).__name__}: {e}"}, 2)
    if why:
        return _emit({"status": "refused", "why": why}, 2)
    try:
        body, card = build_kind(args.kind, ws)
    except Exception as e:  # noqa: BLE001 — a source that cannot be read is named, never guessed
        return _emit({"status": "failed", "why": f"{args.kind} could not be read ({type(e).__name__}: {e})"}, 1)
    if args.print:
        return _emit({"body": body, "card": card})
    if execute is None:
        import pool_provision
        execute = pool_provision.core_actions(ws, None)[0].execute
    waits = arm_waits(ws, spawn) if args.kind == "apps" else []
    task_id = str(headers.get("task_id") or headers.get("id") or "task")
    out = _post(ws, execute, headers, body, card, f"{task_id}:{args.kind}-card", dm, str(core.get("instance") or ""))
    return _emit({**out, **({"waits": waits} if waits else {})}, 0 if out["status"] == "posted" else 1)


def _post(ws: Path, execute, headers: dict, body: str, card: dict, operation_id: str, dm: str, lane: str,
          ttl_s: float = issued_cards.TTL_S) -> dict:
    """Post `card` in the owner's DM as a reply to the task's message and record it for its clicks."""
    try:
        eid = ac.send_card(execute, str(headers.get("channel_id") or ""), body, card, operation_id=operation_id,
                           reply_to=str(headers.get("source_message_id") or ""), owner_dm=dm)
    except ac.CardError as e:
        return {"status": "failed", "why": str(e), "body": body}
    try:
        issued_cards.record(ws, eid, card, room=dm, lane=lane, handler=HANDLER, ttl_s=ttl_s)
    except OSError as e:
        return {"status": "posted", "event_id": eid, "why": f"its buttons will not act ({type(e).__name__})"}
    return {"status": "posted", "event_id": eid}


def _connections_reply(ws: Path, said: ac.Reply) -> dict:
    inst = said.target
    if not inst or not any(r.get("instance") == inst and not r.get("worker") for r in connections.read_rows(ws)):
        return {"status": "refused", "why": f"no core connection {inst or '(legacy)'} to change"}
    if said.verb == "reconnect":
        rec = connections.reconnect(inst, repo=_REPO, logs_dir=ws / "logs", workspace=ws)
        return {"status": "done", "say": f"Reconnected {inst}.", "pid": rec.get("pid")}
    if said.verb == "make-main":
        row = connections.set_primary(inst, workspace=ws)
        return {"status": "done", "say": f"{row.get('server') or inst} is now your main connection"
                + ("; it takes over after a reconnect." if row.get("relaunch_needed") else ".")}
    if said.verb == "disconnect":
        connections.disconnect(inst, workspace=ws)
        return {"status": "done", "say": f"Disconnected {inst}. Its token is kept; Reconnect brings it back."}
    return {"status": "refused", "why": f"a connections card has no {said.verb!r} action"}


def _workers_reply(ws: Path, said: ac.Reply) -> dict:
    import pool_manage
    if said.verb == "pause":
        out = pool_manage.stop(ws, said.target)
    elif said.verb == "restart":
        out = pool_manage.reseat(ws, str(_REPO), said.target)
        if out.get("result") == "skipped":
            out = pool_manage.enable(ws, str(_REPO), said.target)
    elif said.verb == "retire":
        out = pool_manage.retire(ws, said.target)
    else:
        return {"status": "refused", "why": f"a workers card has no {said.verb!r} action"}
    ok = out.get("result") not in ("failed", "refused")
    return {"status": "done" if ok else "failed", "result": out}


def _apps_reply(ws: Path, said: ac.Reply) -> dict:
    if not _SLUG.fullmatch(said.target):
        return {"status": "refused", "why": "not an app slug"}
    if said.verb == "switch":
        return {"status": "handoff", "skill": "connect-apps", "step": "switch", "slug": said.target}
    return {"status": "refused", "why": f"an apps card has no {said.verb!r} action"}


def cmd_reply(ws: Path, args, *, core=None, execute=None, now=time.time) -> int:
    headers, words = ac.reply_words(Path(args.task_file).read_text(encoding="utf-8"))
    try:
        core = core if core is not None else _core(ws)
    except Exception:  # noqa: BLE001 — a card records its own lane; only one without it needs the core's owner
        core = {}
    verdict = ac.click_verdict(headers, words, ws, ac.lane_owner(ws, str(core.get("owner_mxid") or "")))
    other = ac.click_answer(verdict, words, KINDS)
    if other is not None:
        return _emit(other, 1)
    said, clicked = verdict.command, str(headers.get("reply_to_event") or "")
    destructive = (said.kind, said.verb) in DESTRUCTIVE
    try:
        if said.verb == "keep":
            out = {"status": "say", "say": "Okay, I left it as it is."}
        elif destructive and not said.confirmed:
            out = _ask(ws, said, words, headers, verdict.lane or str(core.get("instance") or ""), execute, now())
        else:
            handlers: dict = {"connections": lambda: _connections_reply(ws, said),
                              "workers": lambda: _workers_reply(ws, said),
                              "apps": lambda: _apps_reply(ws, said)}
            out = handlers.get(said.kind, lambda: {"status": "refused", "why": f"a {said.kind} card has no actions"})()
        if said.confirmed or said.verb == "keep":
            issued_cards.settle(ws, clicked)  # a confirm card answers once
    except Exception as e:  # noqa: BLE001 — each unit's refusal is reported in its own words
        out = {"status": "failed", "why": f"{type(e).__name__}: {e}"}
    code = {"refused": 2, "failed": 1}.get(out["status"], 0)
    return _emit(out, code)


def _ask(ws: Path, said: ac.Reply, words: str, headers: dict, lane: str, execute, now: float) -> dict:
    """Post the confirm card for a destructive click in the room it came from; nothing is done yet."""
    if execute is None:
        import pool_provision
        execute = pool_provision.core_actions(ws, lane or None)[0].execute
    body, card = confirm_card(said, words, now=now)
    task_id = str(headers.get("task_id") or headers.get("id") or "task")
    out = _post(ws, execute, headers, body, card, f"{task_id}:confirm-card", str(headers.get("channel_id") or ""), lane,
                CONFIRM_TTL_S)
    return {**out, "status": "asked"} if out.get("event_id") and "why" not in out else {**out, "status": "failed"}


def main(argv=None, *, core=None, execute=None, spawn=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workspace", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show")
    s.add_argument("kind", choices=KINDS)
    s.add_argument("--task-file", required=True)
    s.add_argument("--print", action="store_true")
    r = sub.add_parser("reply")
    r.add_argument("--task-file", required=True)
    args = p.parse_args(argv)
    if args.workspace:
        ws = Path(args.workspace)
    else:
        from workspace_default import resolve_workspace
        ws = Path(resolve_workspace())
    if args.cmd == "show":
        return cmd_show(ws, args, core=core, execute=execute, spawn=spawn)
    return cmd_reply(ws, args, core=core, execute=execute)


if __name__ == "__main__":
    sys.exit(main())
