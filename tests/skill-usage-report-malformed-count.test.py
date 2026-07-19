#!/usr/bin/env python3
"""Regression: a malformed persisted `count` must not strand the reporter.

The fold-back path writes count-carrying records ({slug, ts, count}) into the
active log. Review finding on #2180: `int(rec.get("count", 1))` was parsed
OUTSIDE the malformed-record guard, so a record like
{"slug":"x","ts":1,"count":"not-int"} raised ValueError AFTER
log.rename(pending) — reporter exits nonzero, only the .reporting claim file
left behind: the exact stuck-reporter/data-retention class this PR closes.

Proves, through main() with stubbed vault/workspace/urlopen:
  - malformed count → exit 0 (fail-open contract holds)
  - the record is KEPT with default count=1 (not dropped)
  - well-formed counts still aggregate (2+3 → 5)
  - the .reporting claim is released and the active log is drained

Run: python3 tests/skill-usage-report-malformed-count.test.py  (exit 0/1)
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


ws = Path(tempfile.mkdtemp())
(ws / "state").mkdir()
log = ws / "state" / "skill-usage-log.jsonl"
log.write_text(
    json.dumps({"slug": "bad-count", "ts": 100, "count": "not-int"}) + "\n"
    + json.dumps({"slug": "good", "ts": 200, "count": 2}) + "\n"
    + json.dumps({"slug": "good", "ts": 300, "count": 3}) + "\n"
)

# Stub the two modules main() imports from src/ so the test is hermetic.
fake_ws = types.ModuleType("workspace_default")
fake_ws.resolve_workspace = lambda: str(ws)
sys.modules["workspace_default"] = fake_ws
fake_vault = types.ModuleType("vault_intercept")
fake_vault.get_vault_key = lambda key: "test-token"
sys.modules["vault_intercept"] = fake_vault
os.environ["AGENT_MXID"] = "@test.agent:ag2.space"

spec = importlib.util.spec_from_file_location(
    "report_usage", REPO / "skills" / "skill-usage-report" / "scripts" / "report-usage.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

posted = []


def fake_urlopen(req, timeout=None):
    posted.append(json.loads(req.data))
    resp = io.BytesIO(json.dumps({"accepted": 99, "skipped": 0}).encode())
    resp.__enter__ = lambda *a: resp
    resp.__exit__ = lambda *a: False
    return resp


mod.urllib.request.urlopen = fake_urlopen

rc = mod.main()

check("exit 0 despite malformed count (fail-open contract)", rc == 0, f"rc={rc}")
events = {e["slug"]: e for chunk in posted for e in chunk["events"]}
check("malformed-count record KEPT with default count=1",
      events.get("bad-count", {}).get("count") == 1, f"got {events.get('bad-count')}")
check("well-formed counts aggregate (2+3=5)",
      events.get("good", {}).get("count") == 5, f"got {events.get('good')}")
check(".reporting claim released", not (ws / "state" / "skill-usage-log.jsonl.reporting").exists())
check("active log drained", not log.exists())

print()
if failures:
    print(f"FAIL — {len(failures)} check(s): {failures}")
    sys.exit(1)
print("PASS — malformed count no longer strands the reporter")
