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
import re
import sys
from datetime import datetime
from pathlib import Path

# Default metrics path — override with --metrics <path>
_cwd_path = Path.cwd() / "data" / "call-metrics.jsonl"
_script_path = Path(__file__).resolve().parents[3] / "data" / "call-metrics.jsonl"
METRICS_PATH = _cwd_path if _cwd_path.exists() else _script_path

# Parse --metrics flag early so load_calls uses the right file
for _i, _arg in enumerate(sys.argv):
    if _arg == "--metrics" and _i + 1 < len(sys.argv):
        METRICS_PATH = Path(sys.argv[_i + 1])
        break

INLINE_KEYWORDS = r"\b(record|recording|screen.?record|scroll.?and.?describe|play.?recording|screenshot|describe.?screen)\b"
HALLUCINATION_PHRASES = [
    "is currently playing", "it is playing", "I'm recording",
    "recording is complete", "I've opened", "subtitled video is now playing",
    "I'm unable to", "I can't find", "I can't seem to", "file isn't found",
    "not found", "couldn't locate",
    "I've closed the video", "closed the video", "making sure it's closed",
]


def load_calls(call_sid=None, last_n=1):
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


def merge_timeline(call):
    """Merge events and toolCalls into sorted timeline."""
    items = []
    for e in call.get("events", []):
        items.append({"ts": e["timestamp"], "type": "event", "detail": e["event"]})
    for t in call.get("toolCalls", []):
        items.append({
            "ts": t["timestamp"], "type": "toolCall",
            "detail": f"{t['name']} ({t['durationMs']}ms)",
            "name": t["name"], "durationMs": t["durationMs"],
        })
    items.sort(key=lambda x: x["ts"])
    return items


def parse_ts(ts_str):
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def _ts_short(ts):
    return ts[11:19] if len(ts) > 19 else ts


def diagnose(call):
    """Run all diagnostics on a single call. Returns list of issues."""
    timeline = merge_timeline(call)
    issues = []
    recent_tool_results = {}
    last_recording_stop = None
    pending_caller_requests = []

    for i, item in enumerate(timeline):
        ts = _ts_short(item["ts"])
        detail = item["detail"]

        # 1. Tool returned too fast
        if item["type"] == "toolCall":
            name = item.get("name", "")
            dur = item.get("durationMs", 0)
            if dur < 10 and name not in ("work", "hang_up"):
                issues.append({"severity": "error", "time": ts,
                    "issue": f"{name} returned in {dur}ms — likely failed silently",
                    "detail": "Tool calls under 10ms usually mean the tool hit an early error return without doing work."})
            recent_tool_results.setdefault(name, []).append(dur)
            if name not in ("work", "hang_up"):
                fast_count = sum(1 for d in recent_tool_results[name] if d < 10)
                if fast_count >= 3 and fast_count == len([d for d in recent_tool_results[name] if d < 10]):
                    issues.append({"severity": "error", "time": ts,
                        "issue": f"{name} failed {fast_count} times in this call",
                        "detail": "Repeated fast returns suggest a systematic issue, not a one-off."})

        # 2. Hallucination
        if item["type"] == "event" and detail.startswith("sutando:"):
            text = detail[8:]
            for phrase in HALLUCINATION_PHRASES:
                if phrase.lower() in text.lower():
                    recent_tools = [t for t in timeline[max(0, i - 5):i]
                                    if t["type"] == "event" and t["detail"].startswith("tool_result:")]
                    if not recent_tools:
                        issues.append({"severity": "warn", "time": ts,
                            "issue": f"Possible hallucination: \"{text[:60]}\"",
                            "detail": "Gemini claimed an action state without a recent tool call/result."})
                    break

        # 3. Inline tool delegated via work
        if item["type"] == "event" and detail.startswith("task_delegated:"):
            task_desc = detail[15:]
            # Only match keywords in the action text, not in file paths
            task_text = re.split(r'[/\\]tmp[/\\]', task_desc)[0]
            if re.search(INLINE_KEYWORDS, task_text, re.IGNORECASE):
                issues.append({"severity": "error", "time": ts,
                    "issue": f"Inline task delegated via work: \"{task_desc[:60]}\"",
                    "detail": "Recording/screenshot/playback should use inline tools directly, not work."})

        # 4. Auto-play after recording
        if item["type"] == "event" and "auto-stop" in detail:
            last_recording_stop = i
        if item["type"] == "event" and detail == "tool_call:play_recording" and last_recording_stop is not None:
            caller_between = any(
                t["type"] == "event" and t["detail"].startswith("caller:")
                for t in timeline[last_recording_stop:i]
            )
            if not caller_between:
                issues.append({"severity": "warn", "time": ts,
                    "issue": "Auto-play after recording — user didn't ask",
                    "detail": "play_recording called immediately after recording stopped with no caller speech in between."})
            last_recording_stop = None

        # 5. Long delay between request and execution
        if item["type"] == "event" and detail.startswith("caller:"):
            text = detail[7:].lower()
            if any(kw in text for kw in ["record", "play", "open the video", "open the record"]):
                pending_caller_requests.append((item["ts"], text[:60]))
        if item["type"] == "event" and detail.startswith("tool_call:"):
            for req_ts, req_text in pending_caller_requests:
                t1, t2 = parse_ts(req_ts), parse_ts(item["ts"])
                if t1 and t2:
                    delay = (t2 - t1).total_seconds()
                    if delay > 30:
                        issues.append({"severity": "warn", "time": ts,
                            "issue": f"{delay:.0f}s delay from request to {detail[10:]}",
                            "detail": f"User asked \"{req_text}\" at {req_ts[11:19]}, tool called at {ts}."})
            pending_caller_requests = []

        # 6. Tool call before caller speech (timestamp lag)
        if item["type"] == "event" and detail.startswith("tool_call:"):
            tool_name = detail[10:]
            for j in range(i + 1, min(i + 5, len(timeline))):
                if timeline[j]["type"] == "event" and timeline[j]["detail"].startswith("caller:"):
                    t1, t2 = parse_ts(item["ts"]), parse_ts(timeline[j]["ts"])
                    if t1 and t2:
                        lag = (t2 - t1).total_seconds()
                        if lag > 5:
                            issues.append({"severity": "info", "time": ts,
                                "issue": f"Caller speech logged {lag:.0f}s after {tool_name} tool call",
                                "detail": "STT transcript committed after tool executed — caller timestamp unreliable."})
                    break

    # 7. Wrong tool for the request
    WRONG_TOOL_PATTERNS = [
        (["branch", "git", "commit", "repo"], "describe_screen",
         "work", "Code/repo questions should use work, not screen description"),
        (["branch", "git", "commit", "repo"], "scroll_and_describe",
         "work", "Code/repo questions should use work, not recording"),
        (["play", "open the video", "open the record", "open it"], "switch_tab",
         "play_recording", "Video playback should use play_recording, not switch_tab"),
        (["change the color", "change subtitle", "change the subtitle"], "describe_screen",
         "work", "Code changes should use work, not screen description"),
    ]
    for i, item in enumerate(timeline):
        if item["type"] != "event" or not item["detail"].startswith("tool_call:"):
            continue
        tool = item["detail"][10:]
        ts = _ts_short(item["ts"])
        recent_caller = []
        for j in range(max(0, i - 10), i):
            if timeline[j]["type"] == "event" and timeline[j]["detail"].startswith("caller:"):
                t1, t2 = parse_ts(timeline[j]["ts"]), parse_ts(item["ts"])
                if t1 and t2 and (t2 - t1).total_seconds() < 30:
                    recent_caller.append(timeline[j]["detail"][7:].lower())
        caller_context = " ".join(recent_caller)
        for keywords, wrong, right, explanation in WRONG_TOOL_PATTERNS:
            if tool == wrong and any(kw in caller_context for kw in keywords):
                issues.append({"severity": "error", "time": ts,
                    "issue": f"Wrong tool: {wrong} instead of {right}",
                    "detail": f"{explanation}. Caller said: \"{caller_context[:80]}\""})
                break

    # 8. Unmet user expectations
    FRUSTRATION_PATTERNS = [
        "not asking you to", "i'm not asking", "no, ", "no no",
        "that's not", "this is not", "it's not", "you're not",
        "i said", "i just need", "i don't need", "can you just",
        "why", "hello?", "are you there", "stuck",
    ]
    CORRECTION_PATTERNS = [
        ("not asking you to record", "User corrected unwanted recording/description"),
        ("this is not the subtitle", "Wrong video version opened"),
        ("not the one with", "Wrong version of file"),
        ("i just need this one", "User wants current file modified, not future"),
        ("you should submit", "User had to explain how to use work tool"),
        ("submit a task", "User had to explain how to use work tool"),
        ("submit the task", "User had to explain how to use work tool"),
    ]
    for i, item in enumerate(timeline):
        if item["type"] != "event" or not item["detail"].startswith("caller:"):
            continue
        text = item["detail"][7:].lower()
        ts = _ts_short(item["ts"])
        for pattern, explanation in CORRECTION_PATTERNS:
            if pattern in text:
                issues.append({"severity": "warn", "time": ts,
                    "issue": f"User correction: \"{text[:60]}\"",
                    "detail": explanation})
                break
        else:
            for pattern in FRUSTRATION_PATTERNS:
                if text.startswith(pattern) or f" {pattern}" in text:
                    for j in range(i + 1, min(i + 6, len(timeline))):
                        if timeline[j]["type"] == "event" and timeline[j]["detail"].startswith("caller:"):
                            next_text = timeline[j]["detail"][7:].lower()
                            if len(set(text.split()) & set(next_text.split())) >= 3:
                                issues.append({"severity": "warn", "time": ts,
                                    "issue": "Unmet expectation — user repeated request",
                                    "detail": f"User: \"{text[:50]}\" then repeated: \"{next_text[:50]}\""})
                            break
                    break

    # 9. Tool called without user request (auto-invocation)
    AUTO_CHECK_TOOLS = {
        "scroll_and_describe": ["record", "recording", "video", "capture"],
        "screen_record": ["record", "recording", "video", "capture"],
        "play_recording": ["play", "open", "video", "watch"],
        "describe_screen": ["screen", "what's on", "describe", "see"],
    }
    for i, item in enumerate(timeline):
        if item["type"] != "event" or not item["detail"].startswith("tool_call:"):
            continue
        tool = item["detail"][10:]
        if tool not in AUTO_CHECK_TOOLS:
            continue
        ts = _ts_short(item["ts"])
        keywords = AUTO_CHECK_TOOLS[tool]
        caller_requested = False
        for j in range(max(0, i - 8), i):
            if timeline[j]["type"] == "event" and timeline[j]["detail"].startswith("caller:"):
                t1, t2 = parse_ts(timeline[j]["ts"]), parse_ts(item["ts"])
                if t1 and t2 and (t2 - t1).total_seconds() < 20:
                    if any(kw in timeline[j]["detail"][7:].lower() for kw in keywords):
                        caller_requested = True
                        break
        if not caller_requested:
            issues.append({"severity": "warn", "time": ts,
                "issue": f"Auto-invoked {tool} — no matching user request",
                "detail": f"Gemini called {tool} without the user asking for it in the preceding 20s."})

    return issues


def categorize_issue(issue):
    """Normalize an issue into a specific, tool-call-centric category."""
    text = issue["issue"].lower()
    detail = issue.get("detail", "").lower()

    if "returned in" in text and "ms" in text:
        return f"{text.split(' returned')[0].strip()} returned too fast (failed)"
    if "wrong tool" in text:
        return f"Wrong tool: {detail.split('.')[0]}" if detail else "Wrong tool called"
    if "hallucination" in text:
        if "playing" in text or "playing" in detail:
            return "Hallucinated: 'video is playing'"
        if "recording" in text or "complete" in text:
            return "Hallucinated: 'recording is complete'"
        if "unable" in text or "can't find" in detail:
            return "Hallucinated: 'can't find file'"
        if "branch" in detail or "develop" in detail:
            return "Hallucinated: fabricated answer"
        return f"Hallucinated: '{text[24:60]}'"
    if "auto-invoked" in text or "auto-play" in text:
        return "Auto-played video without user asking"
    if "inline task delegated" in text:
        task = detail.split('"')[1] if '"' in detail else "unknown"
        if "record" in task:
            return "Recording delegated via work (not inline)"
        if "play" in task:
            return "Playback delegated via work (not inline)"
        return f"Inline task delegated via work: {task[:40]}"
    if "user correction" in text:
        if "submit" in text or "submit" in detail:
            return "User had to explain 'submit task' = work tool"
        if "not asking you to record" in text:
            return "Gemini recorded when user didn't ask"
        if "not the subtitle" in text or "not the one" in text:
            return "Opened wrong video version"
        if "just need this one" in text:
            return "User wants existing file modified (not future)"
        return f"User correction: {text[18:60]}"
    if "unmet expectation" in text:
        return "User repeated request (not understood)"
    if "delay from request" in text:
        return f"Long delay before calling {text.split(' to ')[-1] if ' to ' in text else 'tool'}"
    if "failed" in text and "times" in text:
        return f"{text.split(' failed')[0].strip()} failed repeatedly"
    if "caller speech logged" in text:
        return "STT timestamp lag"
    return f"Other: {text[:50]}"


def _make_repair(cat, freq, affected_calls, total_calls, trend, in_recent, repair_type, repair, priority):
    pct = affected_calls * 100 // total_calls if total_calls > 0 else 0
    return {
        "problem": cat,
        "evidence": f"{affected_calls}/{total_calls} calls ({pct}%), trend: {trend}",
        "frequency": freq,
        "trend": trend,
        "repair_type": repair_type,
        "repair": repair,
        "priority": priority,
    }


def _classify_repair(cat, freq, affected_calls, total_calls, trend, in_recent, occurrences):
    """Classify a persistent issue and recommend a specific repair."""
    pct = affected_calls * 100 // total_calls if total_calls > 0 else 0
    cat_lower = cat.lower()
    mk = lambda rt, repair, priority: _make_repair(
        cat, freq, affected_calls, total_calls, trend, in_recent, rt, repair, priority)

    if "stt" in cat_lower and "lag" in cat_lower:
        return mk("unsolvable",
            "STT lag is inherent to the Gemini/Twilio pipeline. Timestamps in observability "
            "are when STT commits the transcript, not when the user spoke. Treat caller "
            "timestamps as approximate. Do not reorder events based on this.", "low")

    if "auto-invoked" in cat_lower:
        tool = cat.split("auto-invoked ")[-1].split(" —")[0] if "auto-invoked" in cat_lower else "unknown"
        priority = "critical" if in_recent and trend != "improving" else "high"
        if "scroll_and_describe" in cat_lower or "screen_record" in cat_lower:
            return mk("prompt",
                f"Gemini calls {tool} without the user asking. "
                "Fix: add to scroll_and_describe/screen_record tool description: "
                "'NEVER call this tool unless the user explicitly says record/recording/capture. "
                "Do NOT start recording based on context or anticipation.'", priority)
        if "play_recording" in cat_lower:
            return mk("prompt",
                "Gemini auto-plays video without user asking. "
                "Fix: strengthen scroll_and_describe return message and play_recording description: "
                "'NEVER call play_recording unless the user explicitly says play/open/watch.'", priority)
        if "describe_screen" in cat_lower:
            return mk("prompt",
                "Gemini calls describe_screen without user asking for screen description. "
                "Fix: add to describe_screen description: 'Only call when user explicitly asks "
                "what is on the screen, to describe the screen, or to see something.'",
                "medium" if not in_recent else "high")

    if "hallucinated" in cat_lower:
        if "playing" in cat_lower:
            return mk("prompt",
                "Gemini claims video is playing without checking. "
                "Fix: add to voice agent prompt: 'NEVER claim a video is playing/paused/open "
                "without calling play_recording(action:status) first to verify.'",
                "high" if in_recent else "medium")
        if "can't find" in cat_lower:
            return mk("code",
                "Gemini says 'can't find file' when file exists. "
                "Fix: play_recording should return the actual file path in the result so Gemini "
                "has concrete evidence. Also add retry logic (already done in play_recording fix).",
                "high" if in_recent else "medium")
        if "fabricated" in cat_lower:
            return mk("prompt",
                "Gemini fabricates answers while waiting for task results. "
                "Fix: add to voice agent prompt: 'When a work task is pending, say ONLY "
                "\"still working on it\" — NEVER guess or fabricate an answer.'",
                "critical" if in_recent else "high")
        return mk("prompt", "Gemini hallucinated — add specific anti-hallucination rule to prompt.", "medium")

    if "user had to explain" in cat_lower or "submit task" in cat_lower:
        return mk("prompt",
            "User says 'submit a task' / 'send to core' but Gemini doesn't understand. "
            "Fix: add aliases in voice agent prompt: 'submit a task', 'send to core', "
            "'ask core' all mean: call the work tool. (Already added — verify deployed.)",
            "high" if in_recent else "medium")
    if "unwanted" in cat_lower or "gemini recorded" in cat_lower:
        return mk("prompt",
            "Gemini starts recording/describing when user didn't ask. "
            "Same root cause as auto-invocation — tighten tool descriptions.",
            "high" if in_recent else "medium")
    if "wrong version" in cat_lower:
        return mk("code",
            "play_recording opens wrong version. Fix: findRecording should prefer "
            "subtitled > narrated > raw (already fixed — verify deployed).", "medium")
    if "modify existing" in cat_lower:
        return mk("code",
            "User wants to modify existing video (e.g. change subtitle color) but system "
            "says 'only for future recordings'. Fix: when subtitle color change task arrives, "
            "re-burn existing video with ffmpeg using the saved SRT file. No code change needed "
            "in browser-tools — core agent can do this directly.", "medium")
    if "long delay" in cat_lower:
        return mk("prompt",
            "Gemini takes >30s to call the right tool after user request. "
            "Often caused by Gemini trying wrong approaches first. "
            "Fix: strengthen 'when in doubt, call work' rule and add specific routing "
            "hints for common requests.", "medium")
    if "returned too fast" in cat_lower:
        tool = cat.split(" returned")[0]
        if "scroll_and_describe" in tool:
            return mk("prompt",
                f"{tool} returns instantly when already recording (duplicate guard — expected). "
                f"The root cause is Gemini calling {tool} multiple times or without user asking. "
                "Fix: tighten tool description to say 'NEVER call more than once per recording. "
                "Do NOT call unless user explicitly says record/recording.'", "medium")
        return mk("code",
            f"{tool} returns in <10ms = early error return. "
            "Fix: check if tool hits an early return path (file not found, cooldown). "
            "Add retry/polling if the file may still be saving.",
            "high" if in_recent and trend != "improving" else "medium")
    if "failed repeatedly" in cat_lower:
        return mk("code", "Tool fails multiple times in same call — indicates systematic issue, not transient.", "high")

    if pct >= 20 or in_recent:
        return mk("unknown", "Persistent issue — needs manual investigation.", "medium")
    return None


def analyze_patterns_and_repair(calls):
    """Analyze persistent patterns across all calls and recommend systematic repairs."""
    issue_history = {}
    for idx, call in enumerate(calls):
        first_ts = call.get("events", [{}])[0].get("timestamp", "")[:10]
        for iss in diagnose(call):
            cat = categorize_issue(iss)
            issue_history.setdefault(cat, []).append({"idx": idx, "date": first_ts, "issue": iss})

    total_calls = len(calls)
    recent_5 = set(range(max(0, total_calls - 5), total_calls))
    repairs = []

    for cat, occurrences in issue_history.items():
        freq = len(occurrences)
        affected_calls = len(set(o["idx"] for o in occurrences))
        pct = affected_calls * 100 // total_calls if total_calls > 0 else 0
        mid = total_calls // 2
        first_half = sum(1 for o in occurrences if o["idx"] < mid)
        second_half = sum(1 for o in occurrences if o["idx"] >= mid)
        trend = "worsening" if second_half > first_half * 1.5 else "improving" if second_half < first_half * 0.5 else "stable"
        in_recent = any(o["idx"] in recent_5 for o in occurrences)
        if pct < 10 and not in_recent:
            continue
        repair = _classify_repair(cat, freq, affected_calls, total_calls, trend, in_recent, occurrences)
        if repair:
            repairs.append(repair)

    priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    repairs.sort(key=lambda r: priority_order.get(r["priority"], 4))
    return repairs


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
        first_event_ts = call.get("events", [{}])[0].get("timestamp", "")
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
        f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>TRACKER_TITLE_PLACEHOLDER</title>',
        f'<style>{_CSS}</style></head><body>',
        f'<h1>TRACKER_TITLE_PLACEHOLDER</h1>',
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
        source = "voice" if "voice-metrics" in str(METRICS_PATH) else "phone"
        out_path = f"/tmp/{source}-diagnostics-tracker.html"
        out = generate_tracker_html(calls, out_path, source_type=source)
        content = Path(out).read_text()
        title = "Voice Agent Diagnostics Tracker" if source == "voice" else "Phone Call Diagnostics Tracker"
        Path(out).write_text(content.replace("TRACKER_TITLE_PLACEHOLDER", title))
        print(f"\nTracker: {out}")
