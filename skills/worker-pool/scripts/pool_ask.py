#!/usr/bin/env python3
"""Ask another instance of this pool a question, through the front door.

The pool has no back channel between the core and a worker by design; what it
has is the task file. `pool_ask` writes one, addressed by `requested_worker`,
and hands it to the router — the same path a room message takes — so the
recipient sees an ordinary delivery and the owner sees an ordinary task. The
reply is a result file for that task, which the asker can wait on.

    pool_ask.py --workspace WS --who                      who can be reached, and are they alive
    pool_ask.py --workspace WS --to Iiya --ask "..."      deliver a question to a worker (or `core`)
    pool_ask.py --workspace WS --to core --ask "..." --wait 300

The answerer replies by writing `results/<task id>.txt`; a `[no-send]` first
line keeps it off every room, and `--wait` returns the body as soon as it lands.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _sibling(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, str(_HERE))
# The task-body guard is the core's: a question is user content, and the file
# it lands in is parsed for headers. Repo root is parents[3] from here.
sys.path.insert(0, str(_HERE.parents[2] / "src"))
from task_body_guard import confine_user_content, header_safe_value  # noqa: E402

pr = _sibling("pool_roster")
rt = _sibling("pool_router")
sup = _sibling("pool_supervise")

SOURCE = "pool-ask"
NO_SEND = "[no-send]"
# An ask never claims owner authority: the asker is an instance, and relayed content
# keeps the tier of whoever wrote it. `owner` is deliberately not offered.
TIERS = ("team", "other", "guest")


def whoami() -> str:
    """This instance's name as a recipient: a worker's id from its environment,
    else the core."""
    return os.environ.get("SUTANDO_INSTANCE_ID") or pr.CORE


def who(workspace, *, runner=None) -> list:
    """Every recipient an ask can name, with what the supervisor sees of it."""
    roster = pr.load_roster(workspace) or {}
    workers = roster.get("workers") or {}
    rooms: dict = {}
    for room, target in (roster.get("bindings") or {}).items():
        for t in (target if isinstance(target, list) else [target]):
            rooms.setdefault(t, []).append(room)
    obs = sup.observe(workspace, time.time(), **({"runner": runner} if runner else {}))
    out = [{"id": pr.CORE, "label": "core", "display_label": "core",
            "rooms": rooms.get(pr.CORE, []),
            "alive": None, "me": whoami() == pr.CORE}]
    for wid, row in sorted(workers.items()):
        o = obs.get(wid)
        out.append({"id": wid, "label": (row or {}).get("label") or wid,
                    "display_label": pr.display_label(row, wid),
                    "state": (row or {}).get("state"),
                    "rooms": sorted(rooms.get(wid, [])),
                    "alive": None if o is None else o.session_alive,
                    "me": whoami() == wid})
    return out


def resolve(workspace, name: str) -> str:
    """A label, an id or `core` to the recipient id; unknown names are refused,
    never guessed."""
    roster = pr.load_roster(workspace)
    if roster is None:
        raise ValueError("no roster: this host has no pool to ask")
    try:
        rid = pr.resolve_label(roster, name)
    except pr.AmbiguousWorkerName as e:
        raise ValueError(str(e)) from e
    if pr.unknown_targets(roster, [rid]):
        raise ValueError(f"no such recipient: {name!r}")
    return rid


def compose(task_id: str, to: str, question: str, *, sender: str, wait: bool,
            tier: str = "team", relayed_from: "str | None" = None) -> str:
    """The task file. `task:` is the LAST header: everything below it is body."""
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {TIERS}: {tier!r}")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    reply = (f"Reply by writing results/{task_id}.txt whose FIRST line is {NO_SEND} "
             f"(the asker reads the file directly; nothing is posted to a room).")
    # Team-tier collaborator by default (never an owner waiting); relayed content keeps
    # its tier. Branch on intent, sanitize for emission: an unnameable origin is refused.
    origin_of = header_safe_value(relayed_from).strip() if relayed_from is not None else None
    if relayed_from is not None and not origin_of:
        raise ValueError("relayed_from names nobody: a relayed ask must say where it came from")
    sender = header_safe_value(sender)
    lines = [f"id: {task_id}", f"timestamp: {ts}", f"source: {SOURCE}",
             f"sender_name: {sender}", f"reply_to_instance: {sender}",
             f"access_tier: {tier}", "priority: low"]
    if origin_of:
        lines.append(f"relayed_from: {origin_of}")
    elif tier == "team":
        lines.append("collaborator: true")
    if to != pr.CORE:
        lines.append(f"requested_worker: {to}")
    origin = f"[pool-ask from {sender}" + (f", relaying {origin_of}" if origin_of else "") + "]"
    body = confine_user_content(question.strip())
    lines.append(f"task: {origin} {body}\n\n{reply}")
    return "\n".join(lines) + "\n"


def ask(workspace, to: str, question: str, *, wait_s: float = 0.0,
        sleep=time.sleep, tier: str = "team", relayed_from: "str | None" = None) -> dict:
    ws = Path(workspace)
    rid = resolve(ws, to)
    sender = whoami()
    if rid == sender:
        raise ValueError("that is you")
    task_id = f"task-{secrets.token_hex(9)}"
    tasks = ws / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    tmp = tasks / f".{task_id}.txt.tmp"
    tmp.write_text(compose(task_id, rid, question, sender=sender, wait=wait_s > 0,
                           tier=tier, relayed_from=relayed_from), encoding="utf-8")
    os.replace(tmp, tasks / f"{task_id}.txt")
    out = {"task_id": task_id, "to": rid, "from": sender, "task_file": str(tasks / f"{task_id}.txt")}
    if rid != pr.CORE:
        # Routed here as well as by the watcher's own pass: the router is idempotent,
        # so whichever runs second reports `already` and delivers nothing twice.
        task = {"id": task_id, "source": SOURCE, "requested_worker": rid}
        out["route"] = rt.route(ws, task)
    if wait_s > 0:
        out["reply"] = wait_for_reply(ws, task_id, wait_s, sleep=sleep)
    return out


def wait_for_reply(workspace, task_id: str, wait_s: float, *, sleep=time.sleep) -> "str | None":
    """The reply body once `results/<task_id>.txt` exists (live, or already archived
    by a drain), or None at the deadline."""
    ws = Path(workspace)
    deadline = time.monotonic() + wait_s
    while True:
        for p in (ws / "results" / f"{task_id}.txt", ws / "results" / "archive" / f"{task_id}.txt"):
            if p.exists():
                body = p.read_text(encoding="utf-8", errors="replace")
                return body.split("\n", 1)[1] if body.startswith(NO_SEND) else body
        if time.monotonic() >= deadline:
            return None
        sleep(1.0)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workspace", required=True)
    p.add_argument("--who", action="store_true")
    p.add_argument("--to")
    p.add_argument("--ask")
    p.add_argument("--wait", type=float, default=0.0, help="seconds to wait for the reply")
    p.add_argument("--relayed-from", help="who the question really comes from, when it is not you")
    p.add_argument("--tier", choices=TIERS, default="team",
                   help="the tier of relayed content (never owner); default team")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if a.who == bool(a.to or a.ask):
        p.error("pass --who, or --to with --ask")
    if bool(a.to) != bool(a.ask):
        p.error("--to and --ask go together")
    try:
        if a.who:
            rows = who(a.workspace)
            if a.json:
                print(json.dumps(rows, indent=2))
            else:
                for r in rows:
                    alive = {True: "alive", False: "DEAD", None: "?"}[r["alive"]]
                    me = "  (you)" if r["me"] else ""
                    display = r.get("display_label") or r["label"]
                    alias = f" alias={r['label']}" if r["label"] != display else ""
                    name = "core" if r["id"] == pr.CORE else f"{display} ({r['id']})"
                    print(f"{name} {alive} rooms={','.join(r['rooms']) or '-'}{alias}{me}")
            return 0
        if a.tier != "team" and a.relayed_from is None:
            p.error("--tier below team needs --relayed-from: whose content is it?")
        out = ask(a.workspace, a.to, a.ask, wait_s=a.wait, tier=a.tier, relayed_from=a.relayed_from)
    except (ValueError, OSError, rt.RouterRefused) as e:
        print(f"pool_ask: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        print(f"asked {out['to'][:8]} as {out['task_id']}")
        if a.wait > 0:
            print(out["reply"] if out["reply"] is not None else f"(no reply within {a.wait:.0f}s)")
    return 0 if not a.wait or out.get("reply") is not None else 1


if __name__ == "__main__":
    sys.exit(main())
