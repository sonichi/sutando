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
- apps: the Station connections connect-apps reads (`connectors.Cloud.connection_rows`, its 30 s cache).
- circle: the collaboration map's quick-lookup index (collaboration-intelligence), else an empty state.

`reply` handles a card:<connections|workers|apps>:<target>:<verb>[:confirmed] task from the owner, in their
DM: Reconnect, Make main and Disconnect run connections' own units; Pause, Restart and Retire run
pool_manage's; Disconnect and Retire act only on their `confirmed` variant, the one the card's confirm
dialog sends. App replies never disconnect an app: they print what to say or which connect-apps step runs.
Every command prints one JSON object. Exit 0 done, 1 a negative answer, 2 refused.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
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
import dotspace  # noqa: E402

KINDS = ("connections", "workers", "apps", "circle")
RUNTIMES = {"claude": "Claude", "codex": "Codex"}
STATE_DOT = {"connected": "on", "failed": "warn", "starting": "warn", "stale": "warn"}
_MXID = re.compile(r"@[^\s:]+:\S+")
_SLUG = re.compile(r"[a-z0-9_]{1,64}")


def _emit(payload: dict, code: int = 0) -> int:
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    return code


# ── builders (pure) ───────────────────────────────────────────────────────────

def _target(instance: str) -> str:
    return instance or "-"


def connections_card(rows: list, bridges: list) -> tuple:
    """(body, card) for the owner's own connections: the core's servers, then the chat-app bridges."""
    core = [r for r in rows if not r.get("worker")]
    workers = [r for r in rows if r.get("worker") and not r.get("disabled")]
    server_rows = []
    for r in core:
        inst, state = str(r.get("instance") or ""), str(r.get("state") or "")
        dot = "off" if r.get("disabled") or r.get("parked") else STATE_DOT.get(state, "off")
        actions = []
        if inst and state != "connected" and not r.get("parked"):
            actions.append(ac.button("Reconnect", "reply", reply=ac.reply("connections", inst, "reconnect")))
        items = []
        if inst and not r.get("primary") and state == "connected":
            items.append(ac.button("Make main", "reply", reply=ac.reply("connections", inst, "make-main")))
        if inst and not r.get("primary") and not r.get("disabled"):
            items.append(ac.button("Disconnect", "reply", reply=ac.reply("connections", inst, "disconnect", confirmed=True),
                                   confirm=f"Disconnect {r.get('server') or inst}? Its token is kept, so you can "
                                           "reconnect later."))
        if items:
            actions.append(ac.button("⋯", "menu", items=items))
        who = r.get("agent_mxid")
        server_rows.append(ac.row(
            str(r.get("agent_display_name") or r.get("server") or inst or dotspace.SUCCESSOR_SERVER),
            subtitle=" · ".join(x for x in (str(r.get("server") or ""), state.replace("_", " ")) if x),
            tag="Main" if r.get("primary") else "", state=dot,
            avatar_=ac.avatar(mxid=str(who), shape="square") if who else ac.avatar(seed=str(r.get("server") or "s"), shape="square"),
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
                    badges=badges, groups=groups, footer_link=footer,
                    text="" if groups else "No connections are set up on this Mac yet.",
                    body=f"Your connections: {up} of {len(core)} servers connected"
                         + (f", {sum(1 for b in bridges if b['running'])} of {len(bridges)} chat apps running." if bridges else "."))


def workers_card(workers: list, identities: dict, runtimes: dict) -> tuple:
    """(body, card) for the pool's workers: runtime, own agent, status, and what the owner can do."""
    rows = []
    for w in workers:
        wid, ident = w["worker_id"], identities.get(w["worker_id"]) or {}
        retired = w.get("state") == "retired"
        status = ("Retired" if retired else "Paused" if w.get("paused")
                  else "Running" if w.get("session_alive") else "Stopped")
        own = f"Own agent on {ident.get('server')}" if ident.get("agent_mxid") else "Answers through your agent"
        actions = []
        if ident.get("agent_mxid") and not retired:
            actions.append(ac.button("Message", "open_dm", user_id=str(ident["agent_mxid"])))
        if not retired:
            items = [ac.button("Restart", "reply", reply=ac.reply("workers", wid, "restart"))]
            if not w.get("paused"):
                items.insert(0, ac.button("Pause", "reply", reply=ac.reply("workers", wid, "pause")))
            items.append(ac.button("Retire", "reply", reply=ac.reply("workers", wid, "retire", confirmed=True),
                                   confirm=f"Retire {w['name']}? It stops for good and loses its own agent."))
            actions.append(ac.button("⋯", "menu", items=items))
        runtime = str(runtimes.get(wid) or "claude").lower()
        rows.append(ac.row(str(w["name"]), tag=RUNTIMES.get(runtime, runtime.title()), subtitle=f"{status} · {own}",
                           state="off" if retired or w.get("paused") else "on" if w.get("session_alive") else "warn",
                           avatar_=(ac.avatar(mxid=str(ident["agent_mxid"]), shape="square") if ident.get("agent_mxid")
                                    else ac.avatar(seed=str(w["name"]), shape="square")),
                           actions=actions))
    live = [w for w in workers if w.get("state") != "retired"]
    return ac.build("workers", "info", title="Your workers", avatar_=ac.avatar(initial="W"),
                    groups=[ac.group("Workers", rows[:100])] if rows else None,
                    text="" if rows else "You have no workers yet.",
                    buttons=[ac.button("+ New worker", "reply", style="secondary", reply="create a worker")],
                    body=f"Your workers: {len(live)} in the pool.")


def apps_card(rows: list) -> tuple:
    """(body, card) for the owner's connected apps, one row per account."""
    out, live = [], set()
    for r in rows:
        slug = str(r.get("toolkit") or "").lower()
        if not _SLUG.fullmatch(slug):
            continue
        active = str(r.get("status") or "").upper() == "ACTIVE"
        name = str(r.get("name") or slug)
        label = str(r.get("accountLabel") or "")
        sub = (label + (" · default" if r.get("isDefault") else "")) if active else \
            ("Signed out" + (f" · {label}" if label else ""))
        if active:
            live.add(slug)
            actions = [ac.button("⋯", "menu", items=[
                ac.button("Switch account", "reply", reply=ac.reply("apps", slug, "switch")),
                ac.button("Disconnect", "reply", reply=ac.reply("apps", slug, "disconnect"))])]
        else:
            actions = [ac.button("Reconnect", "reply", reply=ac.reply("apps", slug, "reconnect"))]
        out.append(ac.row(name, subtitle=sub.strip(" ·"), state="on" if active else "warn",
                          avatar_=ac.avatar(app=slug, shape="square"), actions=actions))
    return ac.build("apps", "info", title="Your apps", avatar_=ac.avatar(initial="A"),
                    groups=[ac.group("Connected apps", out[:100])] if out else None,
                    text="" if out else "No apps are connected yet.",
                    buttons=[ac.button("+ Connect an app", "reply", style="secondary", reply="connect an app")],
                    note="Disconnecting happens in Settings → Integrations.", note_tone="dim",
                    body=f"Your apps: {len(live)} connected. Manage them in Settings → Integrations.")


def circle_card(entities: list) -> tuple:
    """(body, card) for who the owner works with, from the collaboration map; people round, agents square."""
    people, agents = [], []
    for e in entities:
        if not isinstance(e, dict):
            continue
        eid, kind = str(e.get("entity_id") or ""), str(e.get("kind") or "")
        name = str(e.get("name") or e.get("display_name") or eid)
        if not name or kind not in ("human", "agent"):
            continue
        shape = "square" if kind == "agent" else "round"
        av = ac.avatar(mxid=eid, shape=shape) if _MXID.fullmatch(eid) else ac.avatar(initial=name[:1].upper(), shape=shape)
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


def read_apps(ws: Path) -> list:
    import connectors
    cloud = connectors.Cloud(ws)
    cloud.cache = connectors.ConnectCache(ws)
    return cloud.connection_rows(cached=True)


def read_circle(ws: Path) -> list:
    path = ws / "data" / "collaboration-intelligence" / "quick-lookup.yaml"
    if not path.is_file():
        return []
    try:
        import yaml
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
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
        return apps_card(read_apps(ws))
    return circle_card(read_circle(ws))


# ── the owner gate ────────────────────────────────────────────────────────────

def _core(ws: Path) -> dict:
    import pool_provision
    return pool_provision.core_lane(ws, None)


def gate(ws: Path, task_text: str, core: Optional[dict] = None) -> tuple:
    """(headers, card reply, owner DM, refusal or None) for a task asking for or clicking a card."""
    headers, said = ac.reply_payload(task_text)
    core = core if core is not None else _core(ws)
    why = ac.owner_may_act(headers, str(core.get("owner_mxid") or ""))
    dm = connections.owner_dm(core, ws)
    if why is None and (not dm or str(headers.get("channel_id") or "") != dm):
        why = "these cards carry your own data, so I show them only in our DM"
    return headers, said, dm, why


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_show(ws: Path, args, *, core=None, execute=None) -> int:
    text = Path(args.task_file).read_text(encoding="utf-8")
    try:
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
    task_id = str(headers.get("task_id") or headers.get("id") or "task")
    try:
        eid = ac.send_card(execute, str(headers["channel_id"]), body, card, operation_id=f"{task_id}:{args.kind}-card",
                           reply_to=str(headers.get("source_message_id") or ""), owner_dm=dm)
    except ac.CardError as e:
        return _emit({"status": "failed", "why": str(e), "body": body}, 1)
    return _emit({"status": "posted", "event_id": eid})


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
        if not said.confirmed:
            return {"status": "needs_confirm", "say": f"Tap Disconnect on the card and confirm to disconnect {inst}."}
        connections.disconnect(inst, workspace=ws)
        return {"status": "done", "say": f"Disconnected {inst}. Its token is kept; Reconnect brings it back."}
    return {"status": "refused", "why": f"a connections card has no {said.verb!r} action"}


def _workers_reply(ws: Path, said: ac.Reply) -> dict:
    import pool_manage
    if said.verb == "pause":
        out = pool_manage.stop(ws, said.target)
    elif said.verb == "restart":
        out = pool_manage.enable(ws, str(_REPO), said.target)
    elif said.verb == "retire":
        if not said.confirmed:
            return {"status": "needs_confirm", "say": "Tap Retire on the card and confirm to retire that worker."}
        out = pool_manage.retire(ws, said.target)
    else:
        return {"status": "refused", "why": f"a workers card has no {said.verb!r} action"}
    ok = out.get("result") not in ("failed", "refused")
    return {"status": "done" if ok else "failed", "result": out}


def _apps_reply(ws: Path, said: ac.Reply, headers: dict) -> dict:
    slug = said.target
    if not _SLUG.fullmatch(slug):
        return {"status": "refused", "why": "not an app slug"}
    if said.verb == "disconnect":
        return {"status": "say", "say": f"You can disconnect {slug} in Settings → Integrations."}
    if said.verb == "switch":
        return {"status": "handoff", "skill": "connect-apps", "step": "switch", "slug": slug}
    if said.verb == "reconnect":
        import connectors
        room = str(headers.get("channel_id") or "")
        for marker in connectors.list_markers(ws):
            if marker.get("room") == room and slug in {str(k.get("slug")) for k in marker.get("toolkits") or []
                                                       if isinstance(k, dict)}:
                names = [str(k.get("name") or k.get("slug")) for k in marker["toolkits"] if isinstance(k, dict)]
                message = connectors.card_message(marker, str(marker.get("owner") or ""), str(marker.get("task") or ""),
                                                  connectors.card_intro(names, False), False)
                return {"status": "post", "message": {**message, "operation_id": f"{marker['wait_id']}:reconnect-card"},
                        "wait_id": marker["wait_id"]}
        return {"status": "handoff", "skill": "connect-apps", "step": "connect", "slug": slug}
    return {"status": "refused", "why": f"an apps card has no {said.verb!r} action"}


def cmd_reply(ws: Path, args, *, core=None) -> int:
    text = Path(args.task_file).read_text(encoding="utf-8")
    headers, said = ac.reply_payload(text)
    if said is None or said.kind not in KINDS:
        return _emit({"status": "not_a_card_reply"}, 1)
    try:
        headers, said, _, why = gate(ws, text, core)
    except Exception as e:  # noqa: BLE001
        return _emit({"status": "refused", "why": f"{type(e).__name__}: {e}"}, 2)
    if why:
        return _emit({"status": "refused", "why": why}, 2)
    handlers: dict = {"connections": lambda: _connections_reply(ws, said),
                      "workers": lambda: _workers_reply(ws, said),
                      "apps": lambda: _apps_reply(ws, said, headers)}
    if said.kind not in handlers:
        return _emit({"status": "refused", "why": f"a {said.kind} card has no actions"}, 2)
    try:
        out = handlers[said.kind]()
    except Exception as e:  # noqa: BLE001 — each unit's refusal is reported in its own words
        out = {"status": "failed", "why": f"{type(e).__name__}: {e}"}
    code = {"refused": 2, "failed": 1, "needs_confirm": 1}.get(out["status"], 0)
    return _emit(out, code)


def main(argv=None, *, core=None, execute=None) -> int:
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
        return cmd_show(ws, args, core=core, execute=execute)
    return cmd_reply(ws, args, core=core)


if __name__ == "__main__":
    sys.exit(main())
