#!/usr/bin/env python3
"""Write-side convergence acceptance for the health-check task writer
(interaction-planes follow-up, bridge 1/N: task-mid → task-last).

Asserts, against a synthetic old-shape fixture and the writer's NEW shape:
1. The new shape is task-last: every header precedes `task:` and is visible
   to the safe (stop-at-task:) parser.
2. Activated behavior, named: `parse_priority_from_text` now reads
   `priority: low` (it returned "normal" for the old shape — dead config).
3. Nothing regresses for full-scan readers: the lenient parser extracts the
   same headers from both shapes.
4. Body fidelity: the failure bullets survive in the body under every parser
   and are never promoted to headers.
5. The writer's actual source code emits the new order (guards the reorder).

Run: python3 tests/health-check-task-shape.test.py
"""
import importlib.util
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import task_priority  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "local_task_protocol", REPO / "src" / "local_task_protocol.py")
ltp = importlib.util.module_from_spec(spec)
sys.modules.setdefault("local_task_protocol", ltp)
spec.loader.exec_module(ltp)

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


TASK_LINE = ("task: Health check found issues. Decide whether to restart, "
             "DM owner, or treat as transient:")
BULLET = "- memory: warn (swap 900M in use)"
HEADERS = ("source: health-check\ninteraction_type: system_event\n"
           "user_id: health-check\naccess_tier: owner\npriority: low\n")

OLD = f"id: task-health-1\ntimestamp: ts\n{TASK_LINE}\n{BULLET}\n{HEADERS}"
NEW = f"id: task-health-1\ntimestamp: ts\n{HEADERS}{TASK_LINE}\n{BULLET}\n"

# 1+2. Safe parser + priority activation.
h_old, h_new = ltp.parse_task_headers(OLD), ltp.parse_task_headers(NEW)
check("old shape: safe parser blind to priority (the dead config)",
      h_old.get("priority") is None)
check("new shape: safe parser sees every header",
      h_new.get("priority") == "low" and h_new.get("source") == "health-check"
      and h_new.get("interaction_type") == "system_event")
check("activated: parse_priority_from_text old=normal new=low",
      task_priority.parse_priority_from_text(OLD) == "normal"
      and task_priority.parse_priority_from_text(NEW) == "low")

# 3. Lenient (full-scan) readers see identical headers either way.
l_old, l_new = ltp.parse_task_headers_lenient(OLD), ltp.parse_task_headers_lenient(NEW)
check("lenient parser: header set unchanged across shapes",
      l_old.headers == l_new.headers)

# 4. Body fidelity: bullets stay body, never metadata ("memory" is not a
# vocabulary key, but assert the exact line survives everywhere).
for label, parsed in (("safe/new", h_new), ("lenient/new", l_new), ("lenient/old", l_old)):
    check(f"bullet stays in body [{label}]", BULLET in parsed.body)
check("bullet never promoted to headers",
      "memory" not in l_new.headers and "-" not in l_new.headers)

# 5. The writer's source emits task-last: priority: precedes task: in the
# emit-task body construction.
src = (REPO / "src" / "health-check.py").read_text()
seg = src[src.find("id: task-health-{now_ms}"):]
seg = seg[:seg.find(")")]
check("writer source: priority: before task: (task-last order)",
      0 < seg.find('priority: low') < seg.find('task: Health check found issues'))

if failures:
    sys.exit(1)
print("PASS — health-check writer task-last convergence")
