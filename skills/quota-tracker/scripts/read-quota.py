#!/usr/bin/env python3
"""
Read Claude Code quota state from quota-state.json.

Usage:
  python3 read-quota.py              # human readable
  python3 read-quota.py --json       # machine readable
  python3 read-quota.py --gate       # exit 1 if exhausted
"""

import json
import sys
from datetime import datetime
from pathlib import Path

# Canonical home is <workspace>/state/quota-state.json (written by the
# credential proxy). status_read_path also tries the legacy workspace-root
# path; the skill-dir / cwd entries below stay as last-resort fallbacks for
# one release while older proxy installs roll over.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
try:
    from workspace_default import status_read_path  # noqa: E402
    _canonical = status_read_path("quota-state.json")
except Exception:
    _canonical = None

for candidate in [
    _canonical,
    Path(__file__).parent.parent / "quota-state.json",
    Path.cwd() / "quota-state.json",
]:
    if candidate is not None and candidate.exists():
        QUOTA_FILE = candidate
        break
else:
    print("No quota-state.json found. Is the credential proxy running?")
    sys.exit(1)


def main():
    data = json.loads(QUOTA_FILE.read_text())
    headers = data.get("headers", {})

    status = headers.get("anthropic-ratelimit-unified-status", "unknown")
    util_5h = float(headers.get("anthropic-ratelimit-unified-5h-utilization", 0))
    util_7d = float(headers.get("anthropic-ratelimit-unified-7d-utilization", 0))
    reset_5h = headers.get("anthropic-ratelimit-unified-5h-reset", "")
    reset_7d = headers.get("anthropic-ratelimit-unified-7d-reset", "")

    result = {
        "status": status,
        "available": status == "allowed",
        "utilization_5h": util_5h,
        "utilization_7d": util_7d,
        "remaining_5h_pct": round((1 - util_5h) * 100),
        "remaining_7d_pct": round((1 - util_7d) * 100),
    }

    if reset_5h:
        result["reset_5h"] = datetime.fromtimestamp(int(reset_5h)).isoformat()
    if reset_7d:
        result["reset_7d"] = datetime.fromtimestamp(int(reset_7d)).isoformat()

    if "--json" in sys.argv:
        print(json.dumps(result, indent=2))
        return

    if "--gate" in sys.argv:
        sys.exit(0 if result["available"] else 1)

    # Human readable
    print(f"Status: {status}")
    print(f"5h window: {int(util_5h * 100)}% used, {result['remaining_5h_pct']}% remaining")
    if reset_5h:
        print(f"  Resets: {datetime.fromtimestamp(int(reset_5h)).strftime('%H:%M %b %d')}")
    print(f"7d window: {int(util_7d * 100)}% used, {result['remaining_7d_pct']}% remaining")
    if reset_7d:
        print(f"  Resets: {datetime.fromtimestamp(int(reset_7d)).strftime('%H:%M %b %d')}")


if __name__ == "__main__":
    main()
