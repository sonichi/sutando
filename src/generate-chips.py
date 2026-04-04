#!/usr/bin/env python3
"""Generate contextual chips from all available sources.

Writes contextual-chips.json for the web UI starter tab.
Run each loop pass: python3 src/generate-chips.py
"""

import json
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).parent.parent
chips = []


# 1. Open PRs
try:
    out = subprocess.run(
        ["gh", "pr", "list", "--state", "open", "--limit", "5", "--json", "number,title"],
        capture_output=True, text=True, timeout=10
    )
    prs = json.loads(out.stdout) if out.returncode == 0 else []
    for pr in prs[:3]:
        chips.append({"label": f"Review PR #{pr['number']}", "desc": pr["title"][:40]})
except Exception:
    pass


# 2. Upcoming calendar events (next 30 min)
try:
    cal_script = Path.home() / ".claude/skills/google-calendar/scripts/google-calendar.py"
    if cal_script.exists():
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        soon = now + timedelta(minutes=30)
        out = subprocess.run(
            ["python3", str(cal_script), "events", "list",
             "--time-min", now.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "--time-max", soon.strftime("%Y-%m-%dT%H:%M:%SZ")],
            capture_output=True, text=True, timeout=15
        )
        if out.returncode == 0 and out.stdout.strip():
            for line in out.stdout.strip().split("\n")[:2]:
                line = line.strip()
                if line and "no events" not in line.lower() and "not found" not in line.lower():
                    chips.append({"label": "Join meeting", "desc": line[:40]})
except Exception:
    pass


# 3. GitHub stars/forks
try:
    out = subprocess.run(
        ["gh", "api", "repos/:owner/:repo", "--jq", ".stargazers_count,.forks_count"],
        capture_output=True, text=True, timeout=10
    )
    if out.returncode == 0:
        lines = out.stdout.strip().split("\n")
        if len(lines) >= 2:
            stars, forks = lines[0], lines[1]
            chips.append({"label": f"{stars} stars, {forks} forks", "desc": "GitHub"})
except Exception:
    pass


# 4. Pending questions
try:
    pq = REPO / "pending-questions.md"
    if pq.exists():
        content = pq.read_text()
        # Count actual questions (lines starting with - or numbered)
        questions = [l for l in content.split("\n") if l.strip().startswith(("-", "1", "2", "3", "4", "5")) and "?" in l]
        if questions:
            chips.append({"label": "Show questions", "desc": f"{len(questions)} pending"})
except Exception:
    pass


# 5. Recent unprocessed results (last 10 min)
try:
    results_dir = REPO / "results"
    if results_dir.exists():
        recent = []
        cutoff = time.time() - 600  # 10 min
        for f in results_dir.glob("task-*.txt"):
            if f.stat().st_mtime > cutoff:
                recent.append(f)
        if recent:
            chips.append({"label": "View recent results", "desc": f"{len(recent)} new"})
except Exception:
    pass


# Write output
output = {"chips": chips, "ts": int(time.time())}
(REPO / "contextual-chips.json").write_text(json.dumps(output))
print(json.dumps(output, indent=2))
