#!/usr/bin/env python3
"""piperoom-command.py — manage a room's Pipeline (Pipedrive-style CRM) via in-room commands.

Track 13 (typed rooms as management surfaces). A **room type is defined by the
DATA TYPE it manages** (owner 2026-07-12): a piperoom manages the **Pipeline**
data type — deals moving through stages. The pipeline lives durably in the room
vault (`piperoom/pipeline.json`) + a `space.ag2.piperoom` state event (the
machine-readable header); the room is both its view and its control surface.

The pipeline schema (v1):
  {"type":"space.ag2.pipeline","name":..., "stages":[...],
   "deals":[{"id","name","value","stage","contact","notes","updated"}]}

Verbs (post in the room; owner-only honored):
  add deal <name> [$<value>] [<contact>]   → new deal in the first stage
  move <deal> to <stage>                    → change a deal's stage (fuzzy)
  win <deal> / lose <deal>                  → → Won / Lost
  note <deal>: <text>                       → append a note
  show pipeline / pipeline                   → re-render the pipeline

Usage:
  piperoom-command.py --room <id> [--owner <mxid>] [--apply] [--limit N]
Default = dry-run (prints the parsed action + would-be render). --apply mutates
piperoom/pipeline.json in the vault and posts the updated view to the room.

Token: $GATEWAY_TOKEN / $REMOTE_TASK_TOKEN / $AG2_REMOTE_TOKEN (env) else the
AG2_REMOTE_TOKEN= line in <repo>/.env; combined `url|secret` split. Owner:
--owner else $AG2_OWNER_MXID else @qingyun:ag2.space.
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

# A piperoom manages the generic Pipeline type — named ITEMS through ordered
# STAGES, not sales-only. A preset = stages + item noun + won/lost labels.
PRESETS = {
    "sales":       {"item": "deal",      "name": "Sales Pipeline",
                    "stages": ["Lead In", "Contact Made", "Demo Scheduled", "Proposal Made", "Negotiation", "Won", "Lost"],
                    "won": "Won", "lost": "Lost"},
    "hiring":      {"item": "candidate", "name": "Hiring Pipeline",
                    "stages": ["Applied", "Screen", "Interview", "Onsite", "Offer", "Hired", "Rejected"],
                    "won": "Hired", "lost": "Rejected"},
    "fundraising": {"item": "investor",  "name": "Fundraising Pipeline",
                    "stages": ["Prospect", "Intro", "Pitched", "Diligence", "Term Sheet", "Closed", "Passed"],
                    "won": "Closed", "lost": "Passed"},
    "gtm":         {"item": "target",    "name": "GTM / Outreach Pipeline",
                    "stages": ["Identified", "Contacted", "Engaged", "Activated", "Advocate", "Dropped"],
                    "won": "Advocate", "lost": "Dropped"},
}
# item nouns the `add` verb accepts (so "add candidate Jane" / "add investor Sequoia" work)
_ITEM_NOUNS = "deal|candidate|investor|lead|prospect|target|item|entry"


def new_pipeline(kind="sales"):
    """Build an empty pipeline of a given preset kind."""
    p = PRESETS.get(kind, PRESETS["sales"])
    return {"type": "space.ag2.pipeline", "schema_version": 1, "kind": kind,
            "item_noun": p["item"], "name": p["name"], "stages": list(p["stages"]),
            "won_stage": p["won"], "lost_stage": p["lost"], "deals": []}


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


# ── pure logic (unit-tested) ────────────────────────────────────────────────
def _extract_entry(rest):
    """From a free-text entry description, pull out (name, value, contact)."""
    rest = rest.strip()
    contact = ""
    cm = re.search(r"\S+@\S+\.\S+", rest)                      # email
    if cm:
        contact = cm.group(0); rest = rest.replace(cm.group(0), " ")
    cm2 = re.search(r"\bcontact[:=]\s*(\S+)", rest, re.I)        # contact:xxx form
    if cm2 and not contact:
        contact = cm2.group(1); rest = re.sub(r"\bcontact[:=]\s*\S+", " ", rest, flags=re.I)
    value = 0
    vm = (re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*(k)?\b", rest, re.I)      # $-prefixed
          or re.search(r"\b([\d,]{2,}(?:\.\d+)?)\s*(k)?\b", rest, re.I))  # bare number ≥2 digits
    if vm:
        value = int(float(vm.group(1).replace(",", "")) * (1000 if vm.group(2) else 1))
        rest = rest.replace(vm.group(0), " ")
    name = re.sub(r"\s+", " ", rest).strip(" ,-–")
    return name or "Untitled", value, contact


def parse_command(body):
    """Map a message body → an action dict, or None. Case-insensitive; tolerates
    a leading @-mention and '/'-prefix. Supports natural verbs AND the owner's
    slash commands (/new /update /enrich /close). First recognized verb wins."""
    if not body:
        return None
    t = re.sub(r"^\s*@?\S*[:.]ag2\.space\s*", "", body.strip()).lstrip("/").strip()
    low = t.lower()
    # /new <entry>  — owner slash command; add an entry to the first stage
    m = re.match(r"^new\s+(.+)$", t, re.I)
    if m:
        name, value, contact = _extract_entry(m.group(1))
        return {"action": "add", "name": name, "value": value, "contact": contact}
    # /update <entry>: <text>  — append a dated update
    m = re.match(r"^update\s+(.+?)\s*[:\-]\s*(.+)$", t, re.I)
    if m:
        return {"action": "update", "deal": m.group(1).strip(), "text": m.group(2).strip()}
    # /enrich <entry>  — flag for agent enrichment (research + fill fields)
    m = re.match(r"^enrich\s+(.+)$", t, re.I)
    if m:
        return {"action": "enrich", "deal": m.group(1).strip()}
    # /close <entry> [as won|lost]  — set closed (default won)
    m = re.match(r"^close\s+(.+?)(?:\s+as\s+(won|lost))?$", t, re.I)
    if m:
        return {"action": "close", "deal": m.group(1).strip(), "outcome": (m.group(2) or "won").lower()}
    m = re.match(rf"^add\s+(?:{_ITEM_NOUNS})\s+(.+)$", t, re.I)
    if m:
        name, value, contact = _extract_entry(m.group(1))
        return {"action": "add", "name": name, "value": value, "contact": contact}
    m = re.match(r"^move\s+(.+?)\s+to\s+(.+)$", t, re.I)
    if m:
        return {"action": "move", "deal": m.group(1).strip(), "stage": m.group(2).strip()}
    m = re.match(r"^(win|won)\s+(.+)$", low)
    if m:
        return {"action": "win", "deal": t[m.start(2):].strip()}
    m = re.match(r"^(lose|lost)\s+(.+)$", low)
    if m:
        return {"action": "lose", "deal": t[m.start(2):].strip()}
    m = re.match(r"^note\s+(.+?)\s*[:\-]\s*(.+)$", t, re.I)
    if m:
        return {"action": "note", "deal": m.group(1).strip(), "text": m.group(2).strip()}
    if re.match(r"^(show\s+pipeline|pipeline|show|refresh)\b", low):
        return {"action": "show"}
    return None


def _find_deal(pipeline, ref):
    """Resolve a deal by id or (case-insensitive) name substring. Returns deal or None."""
    ref_l = (ref or "").lower().strip()
    for d in pipeline["deals"]:
        if d.get("id", "").lower() == ref_l:
            return d
    for d in pipeline["deals"]:
        if ref_l and ref_l in d.get("name", "").lower():
            return d
    return None


def _match_stage(pipeline, ref):
    ref_l = (ref or "").lower().strip()
    for st in pipeline["stages"]:
        if st.lower() == ref_l:
            return st
    for st in pipeline["stages"]:
        if ref_l and ref_l in st.lower():
            return st
    return None


def apply_command(pipeline, cmd, today="2026-07-12"):
    """Mutate pipeline in place per cmd. Returns a human result string.
    'show' is read-only. Unknown deal/stage → a legible error string, no mutation."""
    act = cmd["action"]
    if act == "show":
        return "showing pipeline"
    if act == "add":
        n = len(pipeline["deals"]) + 1
        did = f"d{n}"
        while any(d.get("id") == did for d in pipeline["deals"]):
            n += 1; did = f"d{n}"
        pipeline["deals"].append({"id": did, "name": cmd["name"], "value": cmd.get("value", 0),
                                  "stage": pipeline["stages"][0], "contact": cmd.get("contact", ""),
                                  "notes": "", "updated": today})
        return f"added deal '{cmd['name']}'" + (f" (${cmd['value']:,})" if cmd.get("value") else "") + f" → {pipeline['stages'][0]}"
    if act in ("move", "win", "lose"):
        d = _find_deal(pipeline, cmd["deal"])
        if not d:
            return f"no deal matches '{cmd['deal']}'"
        if act == "win":
            stage = pipeline.get("won_stage", "Won")
        elif act == "lose":
            stage = pipeline.get("lost_stage", "Lost")
        else:
            stage = _match_stage(pipeline, cmd["stage"])
            if not stage:
                return f"no stage matches '{cmd['stage']}' (stages: {', '.join(pipeline['stages'])})"
        d["stage"] = stage; d["updated"] = today
        return f"moved '{d['name']}' → {stage}"
    if act in ("note", "update"):
        d = _find_deal(pipeline, cmd["deal"])
        if not d:
            return f"no entry matches '{cmd['deal']}'"
        entry = (f"{today}: " if act == "update" else "") + cmd["text"]
        d["notes"] = (d.get("notes", "") + " | " + entry).strip(" |")
        d["updated"] = today
        return f"{'updated' if act == 'update' else 'noted'} '{d['name']}'"
    if act == "enrich":
        d = _find_deal(pipeline, cmd["deal"])
        if not d:
            return f"no entry matches '{cmd['deal']}'"
        d["needs_enrich"] = True
        d["updated"] = today
        # The actual enrichment (research org, fill contact/value/notes) is done by
        # the agent when it sees the flag — the handler just marks + acknowledges.
        return f"enrichment queued for '{d['name']}' — Ill research it and fill in org/contact/value/notes"
    if act == "close":
        d = _find_deal(pipeline, cmd["deal"])
        if not d:
            return f"no entry matches '{cmd['deal']}'"
        stage = pipeline.get("lost_stage", "Lost") if cmd.get("outcome") == "lost" else pipeline.get("won_stage", "Won")
        d["stage"] = stage; d["updated"] = today
        return f"closed '{d['name']}' → {stage}"
    return f"unknown action {act!r}"


# Analysis half of the behavior loop: pure functions reporting WHAT needs
# attention. No network, no mutation — the act half consumes these findings.
def _days_between(a, b):
    """Whole days from date-string a → b (both 'YYYY-MM-DD'). None if unparseable."""
    from datetime import date
    try:
        ya, ma, da_ = (int(x) for x in a.split("-"))
        yb, mb, db_ = (int(x) for x in b.split("-"))
        return (date(yb, mb, db_) - date(ya, ma, da_)).days
    except (ValueError, AttributeError):
        return None


def items_needing_attention(pipeline, today="2026-07-12", stale_days=7):
    """detect-stale / needs-attention primitive. Given a pipeline, return the
    non-terminal entries the agent should act on, each annotated with the reason(s):
      - 'needs_enrich'   : carries the enrich flag (owner or agent queued it)
      - 'stale_<n>d'     : not updated in >= stale_days
      - 'missing_contact': no contact captured yet
      - 'thin_no_notes'  : empty notes (nothing researched/recorded)
    Terminal-stage (won/lost) entries are excluded — they're closed, so staleness is
    expected. Ordered by urgency: enrich-flagged first, then most reasons, then oldest.
    Pure: no network, no mutation.

    `missing_contact` only fires when the pipeline TRACKS contacts (`track_contact`,
    default True). Some entity kinds have no contact by nature — e.g. a competitor-
    analysis pipeline tracks companies you research, not people you reach — so those
    set `track_contact: false` to keep the report signal, not noise."""
    terminals = {pipeline.get("won_stage", "Won"), pipeline.get("lost_stage", "Lost")}
    track_contact = pipeline.get("track_contact", True)
    out = []
    for d in pipeline.get("deals", []):
        if d.get("stage") in terminals:
            continue
        reasons = []
        if d.get("needs_enrich"):
            reasons.append("needs_enrich")
        age = _days_between(d.get("updated", ""), today)
        if age is not None and age >= stale_days:
            reasons.append(f"stale_{age}d")
        if track_contact and not (d.get("contact") or "").strip():
            reasons.append("missing_contact")
        if not (d.get("notes") or "").strip():
            reasons.append("thin_no_notes")
        if reasons:
            out.append({"id": d.get("id"), "name": d.get("name"), "stage": d.get("stage"),
                        "reasons": reasons, "age_days": age})
    out.sort(key=lambda x: ("needs_enrich" not in x["reasons"], -len(x["reasons"]),
                            -(x["age_days"] or 0)))
    return out


# reason → recommended next action (turns the report into a triage worklist).
_ACTION = {"needs_enrich": "research & fill", "missing_contact": "find a contact",
           "thin_no_notes": "add context"}


def _suggest(reasons):
    """The single most useful next action for an entry, by its top reason."""
    for r in reasons:
        if r.startswith("stale_"):
            return "re-touch / advance"
        if r in _ACTION:
            return _ACTION[r]
    return "review"


def render_attention(items, name="Pipeline"):
    """Human-readable 'what needs attention' report for a room post / CLI —
    each flagged entry shows its reason(s) AND the recommended next action."""
    if not items:
        return f"**✅ {name}** — nothing needs attention (all entries fresh + enriched)."
    lines = [f"**🔎 {name}** — {len(items)} entr{'y' if len(items)==1 else 'ies'} need attention:"]
    for it in items:
        lines.append(f"  • **{it['name']}** ({it['stage']}) — {', '.join(it['reasons'])} → _{_suggest(it['reasons'])}_")
    return "\n".join(lines)


def render(p):
    terminals = {p.get("won_stage", "Won"), p.get("lost_stage", "Lost")}
    lines = [f"**📊 {p['name']}** — Pipeline ({p.get('kind','sales')})\n"]
    for st in p["stages"]:
        ds = [d for d in p["deals"] if d.get("stage") == st]
        if st in terminals and not ds:
            continue
        tot = sum(d.get("value", 0) for d in ds)
        lines.append(f"**{st}**" + (f" · ${tot:,}" if tot else ""))
        for d in ds:
            lines.append(f"  • {d['name']} — ${d.get('value',0):,}" + (f" ({d['contact']})" if d.get('contact') else ""))
        if not ds:
            lines.append("  _(empty)_")
    return "\n".join(lines)


# ── network glue ────────────────────────────────────────────────────────────
def _call(url, secret, payload, timeout=15):
    req = urllib.request.Request(url + "/v1/room", data=json.dumps(payload).encode(),
                                 headers={"Authorization": f"Bearer {secret}",
                                          "Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode())


def load_pipeline(url, secret, room):
    s, res = _call(url, secret, {"op": "prep_get", "room_id": room, "folder": "piperoom", "filename": "pipeline.json"})
    if res.get("content_b64"):
        return json.loads(base64.b64decode(res["content_b64"]).decode())
    if res.get("content"):
        return json.loads(res["content"])
    return None


def save_pipeline(url, secret, room, pipeline):
    b64 = base64.b64encode(json.dumps(pipeline, indent=2).encode()).decode()
    return _call(url, secret, {"op": "prep_put", "room_id": room, "folder": "piperoom",
                               "filename": "pipeline.json", "content_b64": b64})


def restamp_widget(url, secret, room, pipeline):
    """Re-stamp the ag2-pipeline dashboard widget with fresh data so the board
    live-updates after a command. Best-effort — never blocks the vault write."""
    d = base64.urlsafe_b64encode(json.dumps(pipeline, separators=(",", ":")).encode()).decode().rstrip("=")
    content = {"type": "m.custom", "url": f"https://ag2.space/skills/pipeline-widget.html?d={d}",
               "name": "Pipeline", "creatorUserId": "@sutando-qingyun-001:ag2.space",
               "data": {"template": "pipeline"}}
    try:
        _call(url, secret, {"op": "state", "room_id": room, "type": "im.vector.modular.widgets",
                            "state_key": "ag2-pipeline", "content": content})
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
                    help="report entries needing attention (detect-stale behavior); posts to room with --apply")
    ap.add_argument("--stale-days", type=int, default=7)
    args = ap.parse_args(argv)

    url, secret = resolve_token(args.repo)
    if not (url and secret):
        print("no gateway token — no-op"); return 0

    # --attention: run the detect-stale behavior primitive (no owner command needed).
    if args.attention:
        pipeline = load_pipeline(url, secret, args.room)
        if not pipeline:
            print("no pipeline.json in this room's vault — is it a piperoom?"); return 1
        items = items_needing_attention(pipeline, stale_days=args.stale_days)
        report = render_attention(items, pipeline.get("name", "Pipeline"))
        print(report)
        if args.apply and items:  # only speak up when there's something to flag
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
    msgs = ctx.get("messages") or []
    cmd = None
    for m in reversed(msgs):
        if m.get("sender") != args.owner:
            continue
        parsed = parse_command(m.get("body") or "")
        if parsed:
            cmd = parsed; break
    if not cmd:
        print("no owner command in recent timeline"); return 0

    pipeline = load_pipeline(url, secret, args.room)
    if not pipeline:
        print("no pipeline.json in this room's vault — is it a piperoom?"); return 1
    result = apply_command(pipeline, cmd)
    mutated = cmd["action"] != "show" and not result.startswith(("no ", "unknown"))
    print(f"[{'APPLY' if args.apply else 'DRY-RUN'}] {result}")
    if args.apply:
        if mutated:
            save_pipeline(url, secret, args.room, pipeline)
            restamp_widget(url, secret, args.room, pipeline)  # live-update the dashboard
        body = f"**[core: qingyun-001]** {result}\n\n{render(pipeline)}" if cmd["action"] != "show" else render(pipeline)
        try:
            _call(url, secret, {"op": "message", "room_id": args.room, "body": body})
        except urllib.error.URLError:
            pass
    else:
        print(render(pipeline))
    return 0


if __name__ == "__main__":
    sys.exit(main())
