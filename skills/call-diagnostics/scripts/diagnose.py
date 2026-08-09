#!/usr/bin/env python3
"""Diagnose phone call issues from observability data.

Merges events + toolCalls into a single sorted timeline, then detects:
1. Tool returned too fast (<10ms) — likely error/not-found
2. Gemini claimed action without tool call (hallucination)
3. Inline tool delegated via work (recording/screenshot/play)
4. Auto-play after recording (no user request between record stop and play)
5. Long delay between user request and tool execution (>30s)
6. Repeated failed tool calls (same tool, multiple fast returns)
7. Tool called before matching caller speech (timestamp lag)

Usage:
  python3 diagnose.py                  # last call
  python3 diagnose.py --all            # all calls
  python3 diagnose.py --call-sid <sid> # specific call
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Pure analysis policy lives in analysis.py; this module keeps loading, CLI,
# terminal output and HTML rendering. Do not duplicate categorization or repair
# logic in the renderer.
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from analysis import (INLINE_KEYWORDS, HALLUCINATION_PHRASES, merge_timeline,  # noqa: E402
                      parse_ts, _ts_short, diagnose, categorize_issue,
                      _make_repair, _classify_repair, analyze_patterns_and_repair)

# Default metrics source order:
#   1. --metrics <path>           — explicit jsonl override (back-compat)
#   2. data/conversation.sqlite   — primary as of #603 (sessions table)
#   3. data/call-metrics.jsonl    — frozen archive fallback
_cwd_path = Path.cwd() / "data" / "call-metrics.jsonl"
_script_path = Path(__file__).resolve().parents[3] / "data" / "call-metrics.jsonl"
METRICS_PATH = _cwd_path if _cwd_path.exists() else _script_path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from workspace_default import resolve_workspace  # noqa: E402

# conversation.sqlite lives under the resolved workspace (post-v0.8 default
# `<repo>/workspace/`; configurable via `sutando.config.local.json`), the same
# tree the runtime writers use — not the repo root.
SQLITE_PATH = Path(os.environ.get(
    "SUTANDO_CONVERSATION_DB",
    resolve_workspace(migrate=False) / "data" / "conversation.sqlite"))

# Parse early flags so load_calls picks the right source
SOURCE_FILTER = "phone"   # 'voice' | 'phone' | 'all' — selectable via --source
FORCE_JSONL = False       # set by --metrics
for _i, _arg in enumerate(sys.argv):
    if _arg == "--metrics" and _i + 1 < len(sys.argv):
        METRICS_PATH = Path(sys.argv[_i + 1])
        FORCE_JSONL = True
        break
for _i, _arg in enumerate(sys.argv):
    if _arg == "--source" and _i + 1 < len(sys.argv):
        SOURCE_FILTER = sys.argv[_i + 1]
        break

def _load_from_sqlite(call_sid=None, last_n=1):
    """Query sessions table from conversation.sqlite. Returns dicts matching
    the legacy jsonl shape (callSid/sessionId/timestamp/events/toolCalls).

    Issue #1357 fix (2026-05-31): the previous SELECT referenced columns
    `tool_calls`, `events` that don't exist on the current `sessions` schema
    (a schema rev that never landed). Events live in the `session_events`
    table joined by `session_id`; tool-call aggregation is reconstructed
    from per-surface transcript tables (e.g. `phone` / `voice`)
    where `kind='tool_call'` rows carry the tool name in `text` and the
    duration in `duration_ms`. Mini's "minimum diff" suggestion from the
    issue — keeps the downstream detector contract unchanged.
    """
    if not SQLITE_PATH.exists():
        return None
    try:
        db = sqlite3.connect(str(SQLITE_PATH))
        db.row_factory = sqlite3.Row
        sql = (
            "SELECT ts_unix, source, session_id, call_sid, caller, is_owner, is_meeting, "
            "duration_ms, transcript_lines, tool_count, pending_tasks "
            "FROM sessions "
        )
        params = []
        wh = []
        if SOURCE_FILTER != "all":
            wh.append("source = ?")
            params.append(SOURCE_FILTER)
        if call_sid:
            wh.append("(call_sid = ? OR session_id = ?)")
            params.append(call_sid); params.append(call_sid)
        if wh:
            sql += "WHERE " + " AND ".join(wh) + " "
        sql += "ORDER BY ts_unix ASC"
        if not call_sid and last_n:
            sql = "SELECT * FROM (" + sql + f") ORDER BY ts_unix DESC LIMIT {int(last_n)}"
        rows = db.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["timestamp"] = datetime.fromtimestamp(d.pop("ts_unix")).isoformat() + "Z"
            d["durationMs"] = d.pop("duration_ms")
            d["transcriptLines"] = d.pop("transcript_lines")
            d["toolCount"] = d.pop("tool_count")
            d["pendingTasks"] = d.pop("pending_tasks")
            d["callSid"] = d.pop("call_sid") or d.get("session_id") or "unknown"
            d["sessionId"] = d.pop("session_id") or d["callSid"]
            d["isOwner"] = bool(d.pop("is_owner")) if d.get("is_owner") is not None else None
            d["isMeeting"] = bool(d.pop("is_meeting")) if d.get("is_meeting") is not None else None
            # Normalize source for surface-table lookup: recordSession() may
            # store a source with hyphens, but per-surface table names use
            # underscores. (#1357 review — Echo)
            source = (d.get("source") or "phone").replace("-", "_")
            d["events"] = _load_session_events(db, source, d["sessionId"], d["callSid"])
            d["toolCalls"] = _load_session_tool_calls(db, source, d["sessionId"], d["callSid"])
            out.append(d)
        db.close()
        out.sort(key=lambda c: c.get("timestamp", ""))
        if call_sid:
            return out
        return out[-last_n:] if last_n else out
    except Exception as e:
        print(f"  sqlite load failed: {e}", file=sys.stderr)
        return None


_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_NON_SURFACE_TABLES = {"sessions", "session_events", "conversation", "tool_calls"}


def _surface_table(db, source):
    """Resolve `source` to its per-surface transcript table, plugin-agnostically.
    Returns the table name iff `source` is a safe identifier naming a real table
    with the surface schema (ts_unix/kind/text/session_id) — else None. Both
    guards against SQL injection on the table name AND lets the tool diagnose any
    plugin-registered surface without hardcoding one."""
    if not source or not _IDENT_RE.match(source) or source in _NON_SURFACE_TABLES:
        return None
    try:
        cols = {r[1] for r in db.execute(f"PRAGMA table_info({source})").fetchall()}
    except Exception:
        return None
    return source if {"ts_unix", "kind", "text", "session_id"} <= cols else None


def _load_session_events(db, source, session_id, call_sid):
    """Ordered events for a session, in the legacy jsonl `events` shape
    ({timestamp, event}). Combines two sources:

      1. lifecycle events from `session_events` (event_name), and
      2. reconstructed turn events from the per-source surface table
         (e.g. `phone` / `voice`): `user` -> `caller:<text>`,
         `agent` -> `sutando:<text>`, `tool_call` -> `tool_call:<name>`.

    (2) is required because `session_events` deliberately omits the
    user/agent/tool_call rows (conversation-store.ts) — but diagnose()'s
    detectors key off exactly those `caller:` / `sutando:` / `tool_call:`
    prefixes, so without the surface-table reconstruction every detector is
    blind (#1357 review — Echo). Surface tables key on `session_id`, which for
    phone holds the call_sid; we match either."""
    rows = []
    try:
        cur = db.execute(
            "SELECT ts_unix, event_name AS detail FROM session_events "
            "WHERE session_id = ? OR call_sid = ?",
            (session_id, call_sid),
        )
        rows.extend((r["ts_unix"], r["detail"]) for r in cur.fetchall())
    except Exception:
        pass
    table = _surface_table(db, source)
    if table:
        try:
            cur = db.execute(
                f"SELECT ts_unix, kind, text, duration_ms FROM {table} "
                "WHERE kind IN ('user','agent','tool_call') "
                "AND (session_id = ? OR session_id = ?) "
                "ORDER BY ts_unix ASC",
                (session_id, call_sid),
            )
            for r in cur.fetchall():
                kind, text = r["kind"], (r["text"] or "")
                if kind == "user":
                    rows.append((r["ts_unix"], "caller:" + text))
                elif kind == "agent":
                    rows.append((r["ts_unix"], "sutando:" + text))
                elif kind == "tool_call":
                    # Surface rows are written at tool *result* time (recordToolCall
                    # on onToolResult) and carry duration_ms. Detectors read
                    # `tool_call:` as execution START and look for `tool_result:` to
                    # suppress hallucination warnings — so synthesize both from
                    # (ts_unix, duration_ms): tool_call: at start, tool_result: at end
                    # (#1357 review — Echo).
                    end_ts = r["ts_unix"]
                    start_ts = end_ts - (r["duration_ms"] or 0) / 1000.0
                    rows.append((start_ts, "tool_call:" + text))
                    rows.append((end_ts, "tool_result:" + text))
        except Exception:
            pass
    rows.sort(key=lambda x: x[0])
    return [
        {"timestamp": datetime.fromtimestamp(ts).isoformat() + "Z", "event": detail}
        for ts, detail in rows
    ]


def _load_session_tool_calls(db, source, session_id, call_sid):
    """Fetch ordered tool_call rows for a session from the source-specific
    transcript table (e.g. `phone` / `voice`).

    Each row has `kind='tool_call'`, the tool name in `text`, and duration
    in `duration_ms`. Returns a list of {timestamp, name, durationMs} dicts
    matching the legacy jsonl `toolCalls` shape."""
    # Only these source tables carry per-turn rows including tool_call kind.
    # If the source is unrecognized, return empty rather than risking a SQL
    # injection on the table name.
    table = _surface_table(db, source)
    if table is None:
        return []
    try:
        # Surface tables key the per-turn rows on `session_id`, which for phone
        # holds the call_sid; match either so phone calls (session_id null on the
        # sessions row) still line up. (#1357 review — Echo)
        cur = db.execute(
            f"SELECT ts_unix, text, duration_ms FROM {table} "
            "WHERE kind = 'tool_call' AND (session_id = ? OR session_id = ?) "
            "ORDER BY ts_unix ASC",
            (session_id, call_sid),
        )
        rows = cur.fetchall()
        return [
            {
                "timestamp": datetime.fromtimestamp(row["ts_unix"]).isoformat() + "Z",
                "name": row["text"] or "(unknown)",
                "durationMs": row["duration_ms"] or 0,
            }
            for row in rows
        ]
    except Exception:
        return []


def load_calls(call_sid=None, last_n=1):
    # Primary path: sqlite (#603), unless --metrics forced a jsonl file
    if not FORCE_JSONL:
        sqlite_rows = _load_from_sqlite(call_sid=call_sid, last_n=last_n)
        if sqlite_rows is not None and sqlite_rows:
            return sqlite_rows
        # Empty sqlite result is fine — fall through only if file doesn't exist
        if sqlite_rows is not None:
            return []
    if not METRICS_PATH.exists():
        print(f"No metrics file: {METRICS_PATH}")
        return []
    calls = []
    with open(METRICS_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if "callSid" not in d:
                    d["callSid"] = d.get("sessionId", "unknown")
                if call_sid and d.get("callSid", "") != call_sid:
                    continue
                calls.append(d)
            except json.JSONDecodeError:
                continue
    if call_sid:
        return calls
    calls.sort(key=lambda c: c.get("timestamp", ""))
    return calls[-last_n:]


def print_timeline(call):
    for item in merge_timeline(call):
        ts = _ts_short(item["ts"])
        prefix = "  📎" if item["type"] == "toolCall" else "  "
        print(f"{ts} {prefix} {item['detail']}")


def print_issues(issues, timeline=None):
    if not issues:
        print("  ✓ No issues detected")
        return
    for issue in issues:
        sev = {"error": "✗", "warn": "⚠", "info": "ℹ"}.get(issue["severity"], "?")
        print(f"  {sev} [{issue['time']}] {issue['issue']}")
        if "--verbose" in sys.argv or "-v" in sys.argv:
            print(f"    → {issue['detail']}")
        # Show surrounding timeline context (±2 events around the issue timestamp)
        if timeline and ("--context" in sys.argv or "-c" in sys.argv):
            issue_ts = issue.get("time", "")
            for idx, item in enumerate(timeline):
                item_ts = _ts_short(item.get("ts", ""))
                if item_ts == issue_ts:
                    start = max(0, idx - 2)
                    end = min(len(timeline), idx + 3)
                    for ctx in timeline[start:end]:
                        ctx_ts = _ts_short(ctx.get("ts", ""))
                        marker = " >>>" if ctx_ts == issue_ts else "    "
                        print(f"{marker} {ctx_ts} {ctx['detail'][:80]}")
                    break


def main():
    args = sys.argv[1:]
    call_sid = None
    show_all = False
    show_timeline = "--timeline" in args or "-t" in args

    for i, arg in enumerate(args):
        if arg == "--call-sid" and i + 1 < len(args):
            call_sid = args[i + 1]
        if arg == "--all":
            show_all = True

    calls = load_calls(call_sid=call_sid, last_n=999 if show_all else 1)
    if not calls:
        print("No calls found.")
        return

    total_issues = 0
    for call in calls:
        sid = call.get("callSid", "unknown")
        duration = call.get("durationMs", 0)
        tools = call.get("toolCount", 0)
        source_label = "Voice Session" if call.get("source") == "voice" else "Call"
        print(f"\n{'=' * 60}")
        print(f"{source_label}: {sid[:20]}... | {duration / 1000:.0f}s | {tools} tools")
        print(f"{'=' * 60}")

        if show_timeline:
            print("\nTimeline:")
            print_timeline(call)
            print()

        timeline = merge_timeline(call)
        issues = diagnose(call)
        total_issues += len(issues)
        if issues:
            print(f"\n{len(issues)} issue(s):")
        print_issues(issues, timeline)

    print(f"\n{'─' * 40}")
    print(f"Total: {len(calls)} call(s), {total_issues} issue(s)")

    if show_all or len(calls) > 1:
        repairs = analyze_patterns_and_repair(calls)
        if repairs:
            print(f"\n{'=' * 60}")
            print(f"REPAIR RECOMMENDATIONS ({len(repairs)})")
            print(f"{'=' * 60}")
            type_icons = {"prompt": "📝", "code": "🔧", "architecture": "🏗", "unsolvable": "🚫", "unknown": "❓"}
            for r in repairs:
                icon = type_icons.get(r["repair_type"], "❓")
                trend_arrow = {"improving": "↓", "worsening": "↑", "stable": "→"}.get(r["trend"], "?")
                print(f"\n  [{r['priority'].upper()}] {icon} {r['problem']}")
                print(f"    Evidence: {r['evidence']} | Trend: {trend_arrow} {r['trend']}")
                print(f"    Fix ({r['repair_type']}): {r['repair']}")


_CSS = """body{font-family:-apple-system,sans-serif;margin:20px;background:#0d1117;color:#c9d1d9}
h1,h2{color:#58a6ff}
table{border-collapse:collapse;font-size:13px}
th,td{border:1px solid #30363d;padding:6px 10px;text-align:center}
th{background:#161b22;color:#8b949e;position:sticky;top:0}
th.row-header{text-align:left;min-width:260px}
td.row-header{text-align:left;font-weight:500}
.ok{background:#0d1117}
.issue{background:#3b1a1a;color:#f85149;font-weight:bold}
.warn{background:#3b2e1a;color:#d29922}
.info{background:#0d1117;color:#8b949e}
.count{font-size:11px;color:#8b949e}
tr:hover{background:#161b22}
.summary{margin:20px 0;padding:15px;background:#161b22;border-radius:8px}
.legend{display:flex;gap:20px;margin:10px 0}
.legend span{display:flex;align-items:center;gap:5px}
.legend .box{width:14px;height:14px;border-radius:3px;display:inline-block}
.timeline{background:#161b22;border-radius:8px;padding:15px;margin:20px 0;font-family:'SF Mono','Menlo',monospace;font-size:12px;line-height:1.6;max-height:500px;overflow-y:auto;white-space:pre}
.tl-tool{color:#d2a8ff}
.tl-caller{color:#7ee787}
.tl-sutando{color:#79c0ff}
.tl-event{color:#8b949e}"""

_CHART_JS = """const canvas=document.getElementById('chart');
const ctx=canvas.getContext('2d');
canvas.width=canvas.offsetWidth*2;canvas.height=600;
const W=canvas.width,H=canvas.height,pad={l:60,r:20,t:20,b:60};
const maxVal=Math.max(...data.map(d=>d.total),1);
const xStep=(W-pad.l-pad.r)/Math.max(data.length-1,1);
const yScale=(H-pad.t-pad.b)/maxVal;
ctx.strokeStyle='#21262d';ctx.lineWidth=1;
for(let i=0;i<=5;i++){const y=pad.t+(H-pad.t-pad.b)*i/5;ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(W-pad.r,y);ctx.stroke();ctx.fillStyle='#8b949e';ctx.font='20px sans-serif';ctx.textAlign='right';ctx.fillText(Math.round(maxVal*(5-i)/5),pad.l-8,y+6);}
ctx.textAlign='center';ctx.font='18px sans-serif';
const labelEvery=Math.max(1,Math.floor(data.length/15));
data.forEach((d,i)=>{if(i%labelEvery===0||i===data.length-1){const x=pad.l+i*xStep;ctx.save();ctx.translate(x,H-pad.b+15);ctx.rotate(-0.5);ctx.fillStyle='#8b949e';ctx.fillText(d.label,0,0);ctx.restore();}});
function drawLine(key,color,width){ctx.strokeStyle=color;ctx.lineWidth=width;ctx.beginPath();data.forEach((d,i)=>{const x=pad.l+i*xStep,y=H-pad.b-d[key]*yScale;i===0?ctx.moveTo(x,y):ctx.lineTo(x,y);});ctx.stroke();ctx.fillStyle=color;data.forEach((d,i)=>{if(d[key]>0){const x=pad.l+i*xStep,y=H-pad.b-d[key]*yScale;ctx.beginPath();ctx.arc(x,y,4,0,Math.PI*2);ctx.fill();}});}
drawLine('total','#8b949e',1);drawLine('errors','#f85149',3);drawLine('warnings','#d29922',2);
ctx.font='22px sans-serif';const legendY=pad.t+15;
[['Errors','#f85149'],['Warnings','#d29922'],['Total','#8b949e']].forEach(([l,c],i)=>{const x=pad.l+10+i*140;ctx.fillStyle=c;ctx.fillRect(x,legendY-10,14,14);ctx.fillStyle='#c9d1d9';ctx.textAlign='left';ctx.fillText(l,x+20,legendY+2);});"""


def generate_tracker_html(calls, output_path, source_type="phone"):
    """Generate an HTML tracker table: rows=issues, columns=calls."""
    call_data = []
    all_categories = set()
    for call in calls:
        sid = call.get("callSid", "?")[:10]
        first_event_ts = (call.get("events") or [{}])[0].get("timestamp", "")
        ts_date = first_event_ts[:10]
        ts_time = first_event_ts[11:16] if len(first_event_ts) > 16 else ""
        issues = diagnose(call)
        cats = {}
        for iss in issues:
            cat = categorize_issue(iss)
            cats.setdefault(cat, []).append(iss)
            all_categories.add(cat)
        call_data.append({"sid": sid, "date": ts_date, "time": ts_time, "cats": cats, "total": len(issues)})

    severity_order = {"fast_fail": 0, "hallucination": 0, "inline_via_work": 0,
                      "wrong_tool": 0, "repeated_failure": 0, "auto_play": 1,
                      "long_delay": 1, "user_correction": 1, "unmet_expectation": 1,
                      "stt_lag": 2, "other": 3}
    sorted_cats = sorted(all_categories, key=lambda c: (severity_order.get(c.split(":")[0], 3), c))

    recent_data = call_data[-5:]
    recent_cats = set()
    for cd in recent_data:
        recent_cats.update(cd["cats"].keys())
    table_cats = [c for c in sorted_cats if c in recent_cats]

    latest_call = calls[-1] if calls else None
    latest_timeline = merge_timeline(latest_call) if latest_call else []

    last_cd = call_data[-1] if call_data else {}
    parts = [
        '<!DOCTYPE html><html><head><meta charset="utf-8"><title>TRACKER_TITLE_PLACEHOLDER</title>',
        f'<style>{_CSS}</style></head><body>',
        '<h1>TRACKER_TITLE_PLACEHOLDER</h1>',
        f'<div class="summary"><strong>{len(calls)} calls total</strong> | Showing last 5 | '
        f'Issues in view: {len(table_cats)} | Last call: {last_cd.get("date","")} {last_cd.get("time","")}</div>',
    ]

    if latest_timeline:
        parts.append(f'<h2>Latest Call Timeline ({recent_data[-1]["date"]} {recent_data[-1]["time"]})</h2>')
        parts.append('<div class="timeline">')
        for item in latest_timeline:
            ts = _ts_short(item["ts"])
            detail = item["detail"].replace("<", "&lt;").replace(">", "&gt;")
            if item["type"] == "toolCall":
                parts.append(f'<span class="tl-tool">{ts}  📎 {detail}</span>\n')
            elif detail.startswith("caller:"):
                parts.append(f'<span class="tl-caller">{ts}    {detail}</span>\n')
            elif detail.startswith("sutando:"):
                parts.append(f'<span class="tl-sutando">{ts}    {detail}</span>\n')
            elif detail.startswith("tool_call:") or detail.startswith("tool_result:"):
                parts.append(f'<span class="tl-tool">{ts}    {detail}</span>\n')
            else:
                parts.append(f'<span class="tl-event">{ts}    {detail}</span>\n')
        parts.append('</div>')

    parts.append('<h2>Issue Tracker (Last 5 Calls)</h2>')
    parts.append('<div class="legend"><span><span class="box" style="background:#3b1a1a"></span> Error/Warning</span>'
                 '<span><span class="box" style="background:#0d1117;border:1px solid #30363d"></span> Clean</span></div>')
    parts.append('<table><tr><th class="row-header">Issue</th>')
    for cd in recent_data:
        parts.append(f'<th title="{cd["sid"]}">{cd["date"]}<br><span class="count">{cd["time"]}</span></th>')
    parts.append('</tr>')

    for cat in table_cats:
        label = cat.replace("_", " ").replace(":", " → ")
        parts.append(f'<tr><td class="row-header">{label}</td>')
        for cd in recent_data:
            if cat in cd["cats"]:
                n = len(cd["cats"][cat])
                severity = cd["cats"][cat][0].get("severity", "warn")
                cls = "issue" if severity == "error" else "warn" if severity == "warn" else "info"
                tooltip = "; ".join(i["issue"][:60] for i in cd["cats"][cat])
                parts.append(f'<td class="{cls}" title="{tooltip}">{n}</td>')
            else:
                parts.append('<td class="ok">·</td>')
        parts.append('</tr>')

    parts.append('<tr style="border-top:2px solid #58a6ff"><td class="row-header"><strong>Total issues</strong></td>')
    for cd in recent_data:
        parts.append(f'<td><strong>{cd["total"]}</strong></td>')
    parts.append('</tr></table>')

    parts.append('<h2 style="margin-top:40px">Issues Over Time</h2>')
    parts.append('<canvas id="chart" style="width:100%;height:300px;background:#161b22;border-radius:8px"></canvas>')
    parts.append('<script>document.querySelector(\'table\').scrollLeft=99999;\nconst data=[')
    for cd in call_data:
        errors = sum(1 for cat in cd["cats"] for iss in cd["cats"][cat] if iss["severity"] == "error")
        warnings = sum(1 for cat in cd["cats"] for iss in cd["cats"][cat] if iss["severity"] == "warn")
        infos = sum(1 for cat in cd["cats"] for iss in cd["cats"][cat] if iss["severity"] == "info")
        parts.append(f'{{label:"{cd["date"]} {cd["time"]}",errors:{errors},warnings:{warnings},infos:{infos},total:{cd["total"]}}},')
    parts.append(f'];\n{_CHART_JS}\n</script>')

    repairs = analyze_patterns_and_repair(calls)
    if repairs:
        type_colors = {"prompt": "#d2a8ff", "code": "#7ee787", "architecture": "#79c0ff",
                       "unsolvable": "#8b949e", "unknown": "#8b949e"}
        type_icons = {"prompt": "📝", "code": "🔧", "architecture": "🏗", "unsolvable": "🚫", "unknown": "❓"}
        trend_arrows = {"improving": "↓ improving", "worsening": "↑ worsening", "stable": "→ stable"}
        priority_colors = {"critical": "#f85149", "high": "#d29922", "medium": "#8b949e", "low": "#484f58"}
        parts.append(f'<h2 style="margin-top:40px">Repair Recommendations ({len(repairs)})</h2>')
        for r in repairs:
            pc = priority_colors.get(r["priority"], "#8b949e")
            tc = type_colors.get(r["repair_type"], "#8b949e")
            icon = type_icons.get(r["repair_type"], "❓")
            trend = trend_arrows.get(r["trend"], r["trend"])
            parts.append(
                f'<div style="background:#161b22;border-left:3px solid {pc};border-radius:0 8px 8px 0;padding:12px 16px;margin:8px 0">'
                f'<div style="display:flex;justify-content:space-between;align-items:center">'
                f'<strong style="color:{pc}">[{r["priority"].upper()}]</strong>'
                f'<span style="color:{tc}">{icon} {r["repair_type"]}</span></div>'
                f'<div style="margin:6px 0;color:#c9d1d9"><strong>{r["problem"]}</strong></div>'
                f'<div style="color:#8b949e;font-size:12px">{r["evidence"]} | {trend}</div>'
                f'<div style="margin-top:8px;color:#c9d1d9;font-size:13px">{r["repair"]}</div></div>')

    parts.append('</body></html>')
    Path(output_path).write_text("".join(parts))
    return output_path


if __name__ == "__main__":
    main()

    if "--tracker" in sys.argv:
        calls = load_calls(last_n=999)
        # Source selection now from SOURCE_FILTER (--source voice|phone|all),
        # which sqlite path honors in WHERE clause. Default phone.
        source = SOURCE_FILTER if SOURCE_FILTER in ("voice", "phone") else "phone"
        out_path = f"/tmp/{source}-diagnostics-tracker.html"
        out = generate_tracker_html(calls, out_path, source_type=source)
        content = Path(out).read_text()
        title = "Voice Agent Diagnostics Tracker" if source == "voice" else "Phone Call Diagnostics Tracker"
        Path(out).write_text(content.replace("TRACKER_TITLE_PLACEHOLDER", title))
        print(f"\nTracker: {out}")
