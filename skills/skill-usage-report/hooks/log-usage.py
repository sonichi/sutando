#!/usr/bin/env python3
"""PostToolUse hook: append a one-line usage record when a skill is invoked.

Registered for the Skill tool only (matcher "Skill" in settings.json). Reads
the hook payload from stdin, appends {"slug", "ts"} to
<workspace>/state/skill-usage-log.jsonl, and always exits 0 — usage logging
must never block or fail a skill invocation. The log is drained by
scripts/report-usage.py (batched POST /api/skills/usage — AU#93).
"""

import json
import os
import sys
import time
from pathlib import Path


def repo_root() -> Path:
    # This file lives at <repo>/skills/skill-usage-report/hooks/log-usage.py.
    # realpath() first: the skill is symlinked into ~/.claude/skills/.
    return Path(os.path.realpath(__file__)).parents[3]


def workspace() -> Path:
    root = repo_root()
    sys.path.insert(0, str(root / "src"))
    try:
        from workspace_default import resolve_workspace  # type: ignore

        return Path(resolve_workspace())
    except Exception:
        return root / "workspace"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if payload.get("tool_name") != "Skill":
        return 0
    slug = (payload.get("tool_input") or {}).get("skill")
    if not slug or not isinstance(slug, str):
        return 0
    # Directory-scoped ("apps/web:deploy") and plugin ("plugin:skill") forms
    # report the bare skill name — AU skills are keyed by plain slug.
    slug = slug.split(":")[-1].strip()
    if not slug:
        return 0
    try:
        log = workspace() / "state" / "skill-usage-log.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"slug": slug, "ts": int(time.time())}) + "\n")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
