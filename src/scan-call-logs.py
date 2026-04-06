#!/usr/bin/env python3
"""Proactive call log scanner — detects issues and classifies by actionability.

Usage:
    python3 src/scan-call-logs.py                  # scan new entries since last run
    python3 src/scan-call-logs.py --all            # scan all entries
    python3 src/scan-call-logs.py --last N         # scan last N entries
    python3 src/scan-call-logs.py --json           # output as JSON
"""

import json, re, sys, os
from pathlib import Path
from datetime import datetime

CALLS_FILE = Path(__file__).parent.parent / "results" / "calls" / "calls.jsonl"
STATE_FILE = Path(__file__).parent.parent / "results" / "calls" / ".scan-state.json"

# --- Detection patterns ---

def detect_duplicate_responses(transcript: str) -> list[dict]:
    """Detect repeated assistant responses (reconnect bug)."""
    issues = []
    lines = [l.strip() for l in transcript.split('\n') if l.strip().startswith('Sutando:')]
    texts = [l.split(':', 1)[1].strip() for l in lines]
    seen = {}
    for t in texts:
        if len(t) < 15:
            continue
        key = t[:60]
        if key in seen:
            seen[key] += 1
        else:
            seen[key] = 1
    for key, count in seen.items():
        if count > 1:
            issues.append({
                "pattern": "duplicate_response",
                "severity": "medium",
                "category": "team-fixable",
                "summary": f"Repeated response ({count}x): \"{key[:50]}...\"",
                "fix_hint": "Known reconnect bug — duplicate audio on WebSocket reconnect.",
            })
    return issues


def detect_access_issues(transcript: str) -> list[dict]:
    """Detect capability/access control issues."""
    issues = []
    access_patterns = [
        (r"I (?:can't|cannot|don't have) access", "access_denied"),
        (r"(?:not|isn't) authorized", "not_authorized"),
        (r"(?:don't|do not) have permission", "no_permission"),
        (r"owner[- ]level access", "owner_only"),
        (r"(?:not|isn't) available (?:to|for) (?:you|callers)", "feature_unavailable"),
    ]
    for pattern, tag in access_patterns:
        if re.search(pattern, transcript, re.IGNORECASE):
            issues.append({
                "pattern": f"access_issue:{tag}",
                "severity": "low",
                "category": "self-fixable",
                "summary": f"Caller hit access restriction ({tag})",
                "fix_hint": "Caller needs to be added as verified. Ask owner to run /discord:access or update VERIFIED_CALLERS.",
            })
    return issues


def detect_task_timeout(transcript: str) -> list[dict]:
    """Detect signs of task timeouts or work tool not returning."""
    issues = []
    timeout_patterns = [
        r"(?:still|taking|seems to be) (?:working|processing|thinking)",
        r"(?:let me|I'll) (?:check|try) (?:again|once more)",
        r"(?:sorry|apologies).{0,30}(?:taking (?:a while|longer|so long))",
        r"(?:timed? out|timeout|no response)",
    ]
    for p in timeout_patterns:
        if re.search(p, transcript, re.IGNORECASE):
            issues.append({
                "pattern": "task_timeout",
                "severity": "medium",
                "category": "team-fixable",
                "summary": "Possible task timeout — agent indicated long processing or retry",
                "fix_hint": "Check if work tool returned. May need timeout/retry improvements.",
            })
            break
    return issues


def detect_confusion(transcript: str) -> list[dict]:
    """Detect caller confusion or long silences."""
    issues = []
    confusion_patterns = [
        (r"(?:hello|are you (?:there|still there))\??", "caller_confusion"),
        (r"(?:what|huh|I don't understand)", "misunderstanding"),
        (r"(?:that's (?:not|wrong)|no,? (?:that's|I said))", "correction"),
    ]
    # Count recipient confusion signals
    recipient_lines = [l for l in transcript.split('\n') if 'Recipient:' in l or 'Caller:' in l]
    confusion_count = 0
    for line in recipient_lines:
        for pattern, _ in confusion_patterns:
            if re.search(pattern, line, re.IGNORECASE):
                confusion_count += 1
    if confusion_count >= 2:
        issues.append({
            "pattern": "caller_confusion",
            "severity": "low",
            "category": "team-fixable",
            "summary": f"Multiple confusion signals from caller ({confusion_count} instances)",
            "fix_hint": "Review transcript for UX issues — unclear responses or unexpected behavior.",
        })
    return issues


def detect_fabrication(transcript: str) -> list[dict]:
    """Detect potential hallucination/fabrication markers."""
    issues = []
    fab_patterns = [
        r"(?:the (?:address|number|account) is)\s+\d",
        r"(?:located at|your appointment is at)\s+\d+\s+\w+\s+(?:St|Ave|Blvd|Dr|Rd)",
        r"(?:your (?:balance|total|amount) is)\s+\$[\d,]+",
    ]
    for p in fab_patterns:
        match = re.search(p, transcript, re.IGNORECASE)
        if match:
            # Only flag if Sutando said it (not the recipient)
            context_start = max(0, match.start() - 100)
            context = transcript[context_start:match.start()]
            if 'Sutando:' in context.split('\n')[-1] if '\n' in context else 'Sutando:' in context:
                issues.append({
                    "pattern": "potential_fabrication",
                    "severity": "high",
                    "category": "team-fixable",
                    "summary": f"Agent may have fabricated specific data: \"{match.group()[:60]}\"",
                    "fix_hint": "Check if agent had access to this data. May need prompt guardrails.",
                })
    return issues


def detect_reconnect_leak(transcript: str) -> list[dict]:
    """Detect 'I'm back' reconnect leak — Gemini says this after WebSocket reconnect."""
    issues = []
    sutando_lines = [l for l in transcript.split('\n') if l.strip().startswith('Sutando:')]
    for line in sutando_lines:
        if re.search(r"I'm back|I am back|Welcome back", line, re.IGNORECASE):
            issues.append({
                "pattern": "reconnect_leak",
                "severity": "medium",
                "category": "team-fixable",
                "summary": "Gemini said 'I'm back' after reconnect",
                "fix_hint": "Turn-count replay detection should suppress this. Check PR #105 fix.",
            })
            break
    return issues


def detect_repeated_command(transcript: str) -> list[dict]:
    """Detect user repeating the same command 3+ times (tool not triggering)."""
    issues = []
    recipient_lines = [l.split(':', 1)[1].strip().lower()
                       for l in transcript.split('\n')
                       if 'Recipient:' in l or 'Caller:' in l]
    # Look for repeated summon/share screen attempts
    summon_count = sum(1 for l in recipient_lines
                       if any(w in l for w in ['summon', 'share screen', 'computer to zoom', 'screen to zoom']))
    if summon_count >= 3:
        issues.append({
            "pattern": "repeated_summon",
            "severity": "medium",
            "category": "team-fixable",
            "summary": f"User asked to summon {summon_count}x — tool may not be triggering reliably",
            "fix_hint": "Gemini STT may garble 'summon'. Check summon tool description for speech variants.",
        })
    # Look for repeated tab switch attempts
    switch_phrases = [l for l in recipient_lines if 'switch' in l or 'tab' in l or 'open the' in l]
    if len(switch_phrases) >= 3:
        issues.append({
            "pattern": "repeated_tab_switch",
            "severity": "low",
            "category": "team-fixable",
            "summary": f"User asked to switch tabs {len(switch_phrases)}x — fuzzy matching may be failing",
            "fix_hint": "Check STT corrections in browser-tools.ts and tab alias list.",
        })
    return issues


def detect_identity_confusion(transcript: str) -> list[dict]:
    """Detect agent identity confusion (e.g., claiming to be the owner)."""
    issues = []
    identity_patterns = [
        (r"(?:I'm|I am|my name is) Chi", "claimed_owner_identity"),
        (r"(?:I'm|I am) (?:a human|a person|not an AI)", "denied_ai_identity"),
    ]
    sutando_lines = [l for l in transcript.split('\n') if l.strip().startswith('Sutando:')]
    for line in sutando_lines:
        for pattern, tag in identity_patterns:
            if re.search(pattern, line, re.IGNORECASE):
                issues.append({
                    "pattern": f"identity_confusion:{tag}",
                    "severity": "high",
                    "category": "team-fixable",
                    "summary": f"Agent identity confusion: {tag}",
                    "fix_hint": "Check stand-identity.json and voice-context.txt loading.",
                })
    return issues


def detect_recording_confusion(transcript: str) -> list[dict]:
    """Detect recording state confusion — user thinks recording started/stopped when it didn't."""
    issues = []
    caller_lines = [l for l in transcript.split('\n') if 'Recipient:' in l or 'Caller:' in l]
    recording_complaints = [l for l in caller_lines
                            if re.search(r'not started recording|haven.t started record|still recording|stop.* record', l, re.IGNORECASE)
                            and not re.search(r'entered any numbers|press \d|menu option', l, re.IGNORECASE)]
    if recording_complaints:
        issues.append({
            "pattern": "recording_confusion",
            "severity": "medium",
            "category": "team-fixable",
            "summary": f"Recording state confusion ({len(recording_complaints)} complaints)",
            "fix_hint": "User expected recording to start/stop but it didn't. Check record tool state management.",
        })
    return issues


def detect_scroll_frustration(transcript: str) -> list[dict]:
    """Detect repeated scroll requests (3+) — scroll tool not working or wrong direction."""
    issues = []
    caller_lines = [l for l in transcript.split('\n') if 'Recipient:' in l or 'Caller:' in l]
    scroll_requests = [l for l in caller_lines if re.search(r'scroll', l, re.IGNORECASE)]
    if len(scroll_requests) >= 3:
        # Check if user corrected direction
        direction_change = any(re.search(r'scroll.*(up|top|down|bottom)', l, re.IGNORECASE) for l in scroll_requests)
        issues.append({
            "pattern": "scroll_frustration",
            "severity": "medium",
            "category": "team-fixable",
            "summary": f"Scroll requested {len(scroll_requests)}x — tool may be unresponsive or wrong direction",
            "fix_hint": "Check scroll tool execution, Gemini may be calling wrong direction or not executing.",
        })
    return issues


def detect_stt_retry(transcript: str) -> list[dict]:
    """Detect caller rephrasing the same request — likely STT garbling."""
    issues = []
    caller_lines = [l.split(':', 1)[1].strip().lower()
                    for l in transcript.split('\n')
                    if ('Recipient:' in l or 'Caller:' in l) and ':' in l]
    retry_count = 0
    for i in range(1, len(caller_lines)):
        prev_words = set(caller_lines[i-1].split())
        curr_words = set(caller_lines[i].split())
        if len(prev_words) >= 3 and len(curr_words) >= 3:
            overlap = len(prev_words & curr_words) / max(len(prev_words), len(curr_words))
            if 0.3 < overlap < 0.9:  # similar but not identical = retry
                retry_count += 1
    if retry_count >= 2:
        issues.append({
            "pattern": "stt_retry",
            "severity": "low",
            "category": "team-fixable",
            "summary": f"Caller rephrased {retry_count}x — possible STT recognition issues",
            "fix_hint": "Add failing terms to vocabulary hints in system prompt (Option 1 from STT research).",
        })
    return issues


# --- Scanner ---

ALL_DETECTORS = [
    detect_duplicate_responses,
    detect_access_issues,
    detect_task_timeout,
    detect_confusion,
    detect_fabrication,
    detect_reconnect_leak,
    detect_repeated_command,
    detect_identity_confusion,
    detect_recording_confusion,
    detect_scroll_frustration,
    detect_stt_retry,
]


def scan_entry(entry: dict):
    """Scan a single call log entry. Returns issues dict or None if clean."""
    transcript = entry.get("transcript", "")
    if not transcript or len(transcript) < 20:
        return None

    all_issues = []
    for detector in ALL_DETECTORS:
        all_issues.extend(detector(transcript))

    if not all_issues:
        return None

    return {
        "callSid": entry.get("callSid", "unknown"),
        "timestamp": entry.get("timestamp", ""),
        "transcript_preview": transcript[:200],
        "issues": all_issues,
        "issue_count": len(all_issues),
        "max_severity": max(
            (i["severity"] for i in all_issues),
            key=lambda s: {"high": 3, "medium": 2, "low": 1}.get(s, 0),
        ),
    }


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_scanned_index": 0}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def main():
    if not CALLS_FILE.exists():
        print("No call logs found.")
        return

    entries = [json.loads(l) for l in CALLS_FILE.read_text().strip().split('\n') if l.strip()]
    as_json = "--json" in sys.argv
    scan_all = "--all" in sys.argv

    # Determine range
    if scan_all:
        start = 0
    elif "--last" in sys.argv:
        idx = sys.argv.index("--last")
        n = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 10
        start = max(0, len(entries) - n)
    else:
        state = load_state()
        start = state.get("last_scanned_index", 0)

    to_scan = entries[start:]
    if not to_scan:
        if not as_json:
            print(f"No new calls to scan (total: {len(entries)}, already scanned: {start}).")
        else:
            print(json.dumps({"scanned": 0, "issues": []}))
        return

    results = []
    for entry in to_scan:
        result = scan_entry(entry)
        if result:
            results.append(result)

    # Save state
    save_state({"last_scanned_index": len(entries), "last_scan": datetime.now().isoformat()})

    if as_json:
        print(json.dumps({"scanned": len(to_scan), "with_issues": len(results), "results": results}, indent=2))
    else:
        print(f"Scanned {len(to_scan)} calls ({start}→{len(entries)})")
        if not results:
            print("No issues detected.")
        else:
            print(f"Found issues in {len(results)} calls:\n")
            for r in results:
                severity_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(r["max_severity"], "⚪")
                print(f"  {severity_icon} {r['timestamp'][:16]}  (call {r['callSid'][:12]}...)")
                for issue in r["issues"]:
                    cat_icon = {"self-fixable": "👤", "config-fixable": "⚙️", "team-fixable": "🔧"}.get(issue["category"], "❓")
                    print(f"     {cat_icon} [{issue['category']}] {issue['summary']}")
                    print(f"        → {issue['fix_hint']}")
                print()


def summary():
    """Print a quality trend summary grouped by date."""
    if not CALLS_FILE.exists():
        print("No call logs found.")
        return

    entries = [json.loads(l) for l in CALLS_FILE.read_text().strip().split('\n') if l.strip()]
    from collections import Counter

    by_date = {}
    for entry in entries:
        ts = entry.get("timestamp", "")[:10]  # YYYY-MM-DD
        if not ts:
            continue
        by_date.setdefault(ts, {"total": 0, "with_issues": 0, "patterns": Counter()})
        by_date[ts]["total"] += 1
        result = scan_entry(entry)
        if result:
            by_date[ts]["with_issues"] += 1
            for issue in result["issues"]:
                by_date[ts]["patterns"][issue["pattern"]] += 1

    print("=== Call Quality Trend ===\n")
    print(f"{'Date':<12} {'Calls':>5} {'Clean':>5} {'Issues':>6} {'Rate':>6}  Top issues")
    print("-" * 75)
    for date in sorted(by_date):
        d = by_date[date]
        clean = d["total"] - d["with_issues"]
        rate = f"{d['with_issues']/d['total']*100:.0f}%" if d["total"] else "—"
        top = ", ".join(f"{p}({c})" for p, c in d["patterns"].most_common(3))
        print(f"{date:<12} {d['total']:>5} {clean:>5} {d['with_issues']:>6} {rate:>6}  {top}")

    total = sum(d["total"] for d in by_date.values())
    issues = sum(d["with_issues"] for d in by_date.values())
    print("-" * 75)
    print(f"{'Total':<12} {total:>5} {total-issues:>5} {issues:>6} {issues/total*100:.0f}%")


if __name__ == "__main__":
    if "--summary" in sys.argv:
        summary()
    else:
        main()
