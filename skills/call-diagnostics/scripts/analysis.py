#!/usr/bin/env python3
"""Pure call-diagnostics analysis engine — timeline, detections, repairs.

Extracted from `diagnose.py` so every threshold and recommendation is testable
against in-memory call dicts. This module is deliberately import-safe: it does
NOT resolve a workspace, read sys.argv, open SQLite or files, print, generate
HTML, or mutate module-global source selection. Loading, CLI, terminal output
and the HTML tracker stay in `diagnose.py`, which imports and calls this.

Feature-specific by design — it stays inside skills/call-diagnostics/ and must
not be promoted into src/.

Public API:
    merge_timeline(call)             -> list[dict]
    diagnose(call)                   -> list[dict]
    categorize_issue(issue)          -> str
    analyze_patterns_and_repair(calls) -> list[dict]

A call with `events: []` is handled: `(call.get("events") or [{}])[0]` covers
both a MISSING key and a present-but-empty list. The bare-default form
`call.get("events", [{}])` only applies its default when the key is absent, so
an empty list indexed out of range (fixed in the PR stacked on the extraction).
"""
import re
from datetime import datetime


INLINE_KEYWORDS = r"\b(record|recording|screen.?record|scroll.?and.?describe|play.?recording|screenshot|describe.?screen)\b"
HALLUCINATION_PHRASES = [
    "is currently playing", "it is playing", "I'm recording",
    "recording is complete", "I've opened", "subtitled video is now playing",
    "I'm unable to", "I can't find", "I can't seem to", "file isn't found",
    "not found", "couldn't locate",
    "I've closed the video", "closed the video", "making sure it's closed",
]



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
        first_ts = (call.get("events") or [{}])[0].get("timestamp", "")[:10]
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
