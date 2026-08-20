#!/usr/bin/env python3
"""inbox-command.py — manage a room's Inbox/Queue (items to consume) via in-room commands.

Track 13 typed-room framework ([[project_typed_room_framework]]). An **InboxRoom**
manages the `Item` entity on a **drain-queue** state model — new things arrive,
get triaged/assigned, and are drained out. Distinct from PipeRoom (advance through
fixed stages) and TaskRoom (executable work on a DAG): Inbox = arrive → consume → gone.

Entity (v1): Item {id, title, status, priority, owner, notes, updated}.
States (drain): unread → triaged → assigned → done.

Verbs (owner-only; natural language also works — the agent interprets):
  /new <title> [!high|!low] [@owner]     new item (unread)
  /triage <item>                          unread → triaged
  /assign <item> to <who>                 → assigned (+owner)
  /prioritize <item> [high|low|normal]    set priority
  /resolve <item>  (or /done)             → done (drained)
  /escalate <item>                        → high priority + note
  /note <item>: <text>                    append a note
  show                                     re-render the queue

Usage: inbox-command.py --room <id> [--owner <mxid>] [--apply] [--limit N]
Default = dry-run. --apply mutates inboxroom/items.json + posts the queue.
"""
import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request

UA = "sutando-core/1.0"
DEFAULT_OWNER = "@qingyun:ag2.space"
STATES = ["unread", "triaged", "assigned", "done"]
STATE_LABEL = {"unread": "Unread", "triaged": "Triaged", "assigned": "Assigned", "done": "Done"}
PRIOS = {"high": "🔴 high", "normal": "normal", "low": "⚪ low"}


def resolve_token(repo):
    token = (os.environ.get("GATEWAY_TOKEN") or os.environ.get("REMOTE_TASK_TOKEN")
             or os.environ.get("AG2_REMOTE_TOKEN"))
    url = (os.environ.get("GATEWAY_URL") or os.environ.get("RELAY_URL")
           or os.environ.get("REMOTE_TASK_URL") or os.environ.get("AG2_REMOTE_URL"))
    if not token:
        try:
            with open(os.path.join(repo, ".env")) as f:
                for line in f:
                    if line.startswith("AG2_REMOTE_TOKEN="):
                        token = line.split("=", 1)[1].strip().strip('"').strip("'"); break
        except OSError:
            pass
    if not token:
        return None, None
    if "|" in token:
        url, token = token.split("|", 1)
    return (url or None), token


# ── pure logic (unit-tested) ─────────────────────────────────────────────────
def parse_command(body):
    if not body:
        return None
    t = re.sub(r"^\s*@?\S*[:.]ag2\.space\s*", "", body.strip()).lstrip("/").strip()
    low = t.lower()
    m = re.match(r"^(?:new|add)\s+(.+)$", t, re.I)
    if m:
        rest = m.group(1).strip()
        owner = ""
        om = re.search(r"@(\S+)", rest)
        if om:
            owner = om.group(1); rest = rest.replace(om.group(0), " ")
        prio = "normal"
        pm = re.search(r"!(\bhigh\b|\blow\b|\bnormal\b)", rest, re.I)
        if pm:
            prio = pm.group(1).lower(); rest = re.sub(r"!\w+", " ", rest)
        title = re.sub(r"\s+", " ", rest).strip(" ,-–")
        return {"action": "new", "title": title or "Untitled", "priority": prio, "owner": owner}
    m = re.match(r"^triage\s+(.+)$", t, re.I)
    if m:
        return {"action": "triage", "item": m.group(1).strip()}
    m = re.match(r"^assign\s+(.+?)\s+to\s+@?(\S+)$", t, re.I)
    if m:
        return {"action": "assign", "item": m.group(1).strip(), "who": m.group(2).strip()}
    m = re.match(r"^(?:prioriti[sz]e)\s+(.+?)(?:\s+(high|low|normal))?$", t, re.I)
    if m:
        return {"action": "prioritize", "item": m.group(1).strip(), "priority": (m.group(2) or "high").lower()}
    m = re.match(r"^(?:resolve|done|close)\s+(.+)$", t, re.I)
    if m:
        return {"action": "resolve", "item": m.group(1).strip()}
    m = re.match(r"^escalate\s+(.+)$", t, re.I)
    if m:
        return {"action": "escalate", "item": m.group(1).strip()}
    m = re.match(r"^note\s+(.+?)\s*[:\-]\s*(.+)$", t, re.I)
    if m:
        return {"action": "note", "item": m.group(1).strip(), "text": m.group(2).strip()}
    if re.match(r"^(show|queue|inbox|refresh)\b", low):
        return {"action": "show"}
    return None


def _find(q, ref):
    rl = (ref or "").lower().strip()
    for x in q["items"]:
        if x.get("id", "").lower() == rl:
            return x
    for x in q["items"]:
        if rl and rl in x.get("title", "").lower():
            return x
    return None


def _next_id(q):
    n = len(q["items"]) + 1
    while any(x.get("id") == f"i{n}" for x in q["items"]):
        n += 1
    return f"i{n}"


def apply_command(q, cmd, today="2026-07-12"):
    a = cmd["action"]
    if a == "show":
        return "showing queue"
    if a == "new":
        q["items"].append({"id": _next_id(q), "title": cmd["title"], "status": "unread",
                           "priority": cmd.get("priority", "normal"), "owner": cmd.get("owner", ""),
                           "notes": "", "updated": today})
        return f"added '{cmd['title']}'" + (f" (!{cmd['priority']})" if cmd.get("priority") != "normal" else "")
    x = _find(q, cmd.get("item", ""))
    if not x:
        return f"no item matches '{cmd.get('item','')}'"
    if a == "triage":
        x["status"] = "triaged"; x["updated"] = today
        return f"triaged '{x['title']}'"
    if a == "assign":
        x["status"] = "assigned"; x["owner"] = cmd["who"]; x["updated"] = today
        return f"assigned '{x['title']}' → @{cmd['who']}"
    if a == "prioritize":
        x["priority"] = cmd["priority"]; x["updated"] = today
        return f"set '{x['title']}' priority {cmd['priority']}"
    if a == "resolve":
        x["status"] = "done"; x["updated"] = today
        return f"resolved '{x['title']}' (drained)"
    if a == "escalate":
        x["priority"] = "high"
        x["notes"] = (x.get("notes", "") + " | escalated").strip(" |"); x["updated"] = today
        return f"escalated '{x['title']}' → 🔴 high"
    if a == "note":
        x["notes"] = (x.get("notes", "") + " | " + cmd["text"]).strip(" |"); x["updated"] = today
        return f"noted '{x['title']}'"
    return f"unknown action {a!r}"


# detect-stale / needs-attention for the DRAIN-QUEUE shape: a queue's health is
# "is anything piling up or stuck un-drained?". Pure — no network, no mutation.
def _days_between(a, b):
    """Whole days from date-string a → b (both 'YYYY-MM-DD'). None if unparseable."""
    from datetime import date
    try:
        ya, ma, da_ = (int(x) for x in a.split("-"))
        yb, mb, db_ = (int(x) for x in b.split("-"))
        return (date(yb, mb, db_) - date(ya, ma, da_)).days
    except (ValueError, AttributeError):
        return None


def items_needing_attention(q, today="2026-07-12", stale_days=3):
    """Return the non-done items that need attention, each with reason(s):
      - 'high-priority' : priority high and still open
      - 'unread'        : arrived, not yet triaged (the backlog signal)
      - 'unassigned'    : triaged but no owner → needs routing
      - 'stale_<n>d'    : not updated in >= stale_days (stuck in the queue)
    `done` items are excluded (drained out). Ordered by urgency: high-priority
    first, then unread, then most reasons, then oldest. Pure: no network/mutation."""
    out = []
    for x in q.get("items", []):
        st = x.get("status")
        if st == "done":
            continue
        reasons = []
        if x.get("priority") == "high":
            reasons.append("high-priority")
        if st == "unread":
            reasons.append("unread")
        if st == "triaged" and not (x.get("owner") or "").strip():
            reasons.append("unassigned")
        age = _days_between(x.get("updated", ""), today)
        if age is not None and age >= stale_days:
            reasons.append(f"stale_{age}d")
        if reasons:
            out.append({"id": x.get("id"), "title": x.get("title"), "status": st,
                        "reasons": reasons, "age_days": age})
    out.sort(key=lambda r: ("high-priority" not in r["reasons"], "unread" not in r["reasons"],
                            -len(r["reasons"]), -(r["age_days"] or 0)))
    return out


# reason → recommended next action (turns the report into a triage worklist).
_ACTION = {"high-priority": "handle now", "unread": "triage", "unassigned": "route to an owner"}


def _suggest(reasons):
    """The single most useful next action for an item, by its top reason."""
    for r in reasons:
        if r in _ACTION:
            return _ACTION[r]
    for r in reasons:
        if r.startswith("stale_"):
            return "follow up / nudge"
    return "review"


def render_attention(items, name="Inbox"):
    """Human-readable 'what needs attention' report for a room post / CLI —
    each flagged item shows its reason(s) AND the recommended next action."""
    if not items:
        return f"**✅ {name}** — queue clear (nothing unread, stuck, or unrouted)."
    lines = [f"**🔎 {name}** — {len(items)} item{'' if len(items)==1 else 's'} need attention:"]
    for it in items:
        lines.append(f"  • **{it['title']}** ({it['status']}) — {', '.join(it['reasons'])} → _{_suggest(it['reasons'])}_")
    return "\n".join(lines)


def render(q):
    lines = [f"**📥 {q.get('name','Inbox')}** — Queue\n"]
    active = [x for x in q["items"] if x.get("status") != "done"]
    lines[0] = f"**📥 {q.get('name','Inbox')}** — Queue · {len(active)} open\n"
    for st in STATES:
        xs = [x for x in q["items"] if x.get("status") == st]
        if st == "done" and not xs:
            continue
        lines.append(f"**{STATE_LABEL[st]}** ({len(xs)})")
        # high priority first within a state
        xs = sorted(xs, key=lambda x: 0 if x.get("priority") == "high" else 1)
        for x in xs:
            bits = []
            if x.get("priority") and x["priority"] != "normal":
                bits.append(PRIOS.get(x["priority"], x["priority"]))
            if x.get("owner"):
                bits.append(f"@{x['owner']}")
            suffix = (" — " + " · ".join(bits)) if bits else ""
            lines.append(f"  • {x['title']}{suffix}")
        if not xs:
            lines.append("  _(none)_")
    return "\n".join(lines)


# ── network glue ─────────────────────────────────────────────────────────────
def _call(url, secret, payload, timeout=15):
    req = urllib.request.Request(url + "/v1/room", data=json.dumps(payload).encode(),
                                 headers={"Authorization": f"Bearer {secret}",
                                          "Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode())


def load_queue(url, secret, room):
    _, res = _call(url, secret, {"op": "prep_get", "room_id": room, "folder": "inboxroom", "filename": "items.json"})
    if res.get("content_b64"):
        return json.loads(base64.b64decode(res["content_b64"]).decode())
    if res.get("content"):
        return json.loads(res["content"])
    return None


def save_queue(url, secret, room, q):
    b64 = base64.b64encode(json.dumps(q, indent=2).encode()).decode()
    return _call(url, secret, {"op": "prep_put", "room_id": room, "folder": "inboxroom",
                               "filename": "items.json", "content_b64": b64})


def restamp_widget(url, secret, room, q):
    """Re-stamp the ag2-inbox dashboard widget with fresh data so the queue board
    live-updates after a command. Best-effort — never blocks the vault write."""
    d = base64.urlsafe_b64encode(json.dumps(q, separators=(",", ":")).encode()).decode().rstrip("=")
    content = {"type": "m.custom", "url": f"https://ag2.space/skills/inbox-widget.html?d={d}",
               "name": "Inbox", "creatorUserId": "@sutando-qingyun-001:ag2.space",
               "data": {"title": "Inbox dashboard"}}
    try:
        _call(url, secret, {"op": "state", "room_id": room, "type": "im.vector.modular.widgets",
                            "state_key": "ag2-inbox", "content": content})
    except Exception:  # noqa: BLE001 — dashboard refresh is best-effort
        pass


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", required=True)
    ap.add_argument("--owner", default=os.environ.get("AG2_OWNER_MXID") or DEFAULT_OWNER)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--attention", action="store_true",
                    help="report items needing attention (detect-stale behavior); posts to room with --apply")
    ap.add_argument("--stale-days", type=int, default=3)
    args = ap.parse_args(argv)
    url, secret = resolve_token(args.repo)
    if not (url and secret):
        print("no gateway token — no-op"); return 0

    # --attention: run the detect-stale behavior primitive (no owner command needed).
    if args.attention:
        q = load_queue(url, secret, args.room)
        if not q:
            print("no items.json — is this an inboxroom?"); return 1
        items = items_needing_attention(q, stale_days=args.stale_days)
        report = render_attention(items, q.get("name", "Inbox"))
        print(report)
        if args.apply and items:
            try:
                _call(url, secret, {"op": "message", "room_id": args.room,
                                    "body": f"**[core: qingyun-001]** {report}"})
            except urllib.error.URLError:
                pass
        return 0
    try:
        _, ctx = _call(url, secret, {"op": "context", "room_id": args.room, "limit": args.limit})
    except urllib.error.URLError as e:
        print(f"room read failed: {e}"); return 1
    cmd = None
    for m in reversed(ctx.get("messages") or []):
        if m.get("sender") != args.owner:
            continue
        p = parse_command(m.get("body") or "")
        if p:
            cmd = p; break
    if not cmd:
        print("no owner command in recent timeline"); return 0
    q = load_queue(url, secret, args.room)
    if not q:
        print("no items.json — is this an inboxroom?"); return 1
    result = apply_command(q, cmd)
    mutated = cmd["action"] != "show" and not result.startswith(("no ", "unknown"))
    print(f"[{'APPLY' if args.apply else 'DRY-RUN'}] {result}")
    if args.apply:
        if mutated:
            save_queue(url, secret, args.room, q)
            restamp_widget(url, secret, args.room, q)  # live-update the dashboard
        body = f"**[core: qingyun-001]** {result}\n\n{render(q)}" if cmd["action"] != "show" else render(q)
        try:
            _call(url, secret, {"op": "message", "room_id": args.room, "body": body})
        except urllib.error.URLError:
            pass
    else:
        print(render(q))
    return 0


if __name__ == "__main__":
    sys.exit(main())
