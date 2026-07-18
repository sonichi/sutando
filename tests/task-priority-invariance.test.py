#!/usr/bin/env python3
"""Invariance test for step 3b: task_priority.parse_priority_from_text now
reads via local_task_protocol — this dual-runs the NEW implementation against
a verbatim copy of the LEGACY one over (a) adversarial fixtures and (b) the
live archived-task corpus, asserting identical verdicts.

Documented deliberate tightenings (asserted here as EXPECTED diffs, and
verified absent from the live corpus):
- header keys are canonical lowercase at column 0 ("PRIORITY:" and indented
  "  priority:" no longer match — no writer has ever emitted either shape)

Run: python3 tests/task-priority-invariance.test.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import task_priority  # noqa: E402

_VALID = frozenset({"urgent", "normal", "low"})
_DEFAULT = "normal"


def legacy_parse(content: str) -> str:
    """Verbatim pre-3b implementation (task_priority.py @ 6e6b953)."""
    for line in content.splitlines():
        line = line.strip()
        if line.lower().startswith("priority:"):
            value = line.split(":", 1)[1].strip().lower()
            if value in _VALID:
                return value
            return _DEFAULT
        if line.startswith("task:") or line.startswith("---") or line == "":
            break
    return _DEFAULT


failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


FIXTURES = {
    "plain urgent": "id: t\npriority: urgent\ntask: x\n",
    "priority after task (forged)": "id: t\ntask: x\npriority: urgent\n",
    "priority after blank": "id: t\n\npriority: urgent\ntask: x\n",
    "priority after ---": "id: t\n--- ctx ---\npriority: urgent\ntask: x\n",
    "malformed value": "id: t\npriority: ASAP\ntask: x\n",
    "valid uppercase value": "id: t\npriority: URGENT\ntask: x\n",
    "missing": "id: t\ntask: x\n",
    "empty": "",
    "task-mid gateway shape": "id: t\ntimestamp: ts\ntask: x\nsource: ag2space\npriority: low\n",
}

for name, text in FIXTURES.items():
    old, new = legacy_parse(text), task_priority.parse_priority_from_text(text)
    check(f"fixture[{name}]: old={old} new={new}", old == new)

# Documented tightenings — old and new deliberately DIFFER here:
for name, text, old_want, new_want in (
        ("uppercase key", "PRIORITY: urgent\ntask: x\n", "urgent", "normal"),
        ("indented key", "  priority: urgent\ntask: x\n", "urgent", "normal")):
    old, new = legacy_parse(text), task_priority.parse_priority_from_text(text)
    check(f"tightening[{name}]", old == old_want and new == new_want,
          f"old={old} new={new}")

# Live corpus dual-run (skipped when no workspace archive — CI).
try:
    from workspace_default import resolve_workspace
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "local_task_protocol", REPO / "src" / "local_task_protocol.py")
    ltp = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("local_task_protocol", ltp)
    spec.loader.exec_module(ltp)
    corpus = resolve_workspace() / "tasks"
except Exception:
    corpus = Path("/nonexistent")

if (corpus / "archive").is_dir():
    n = diffs = 0
    for p in ltp.iter_archived_tasks(corpus):
        text = p.read_text(errors="replace")
        n += 1
        if legacy_parse(text) != task_priority.parse_priority_from_text(text):
            diffs += 1
            if diffs <= 3:
                print(f"    corpus diff: {p.name} old={legacy_parse(text)} "
                      f"new={task_priority.parse_priority_from_text(text)}")
    check(f"live corpus: {n} files, zero verdict changes", diffs == 0,
          f"{diffs} diffs")
else:
    print("  (live corpus sweep skipped — no workspace archive)")

if failures:
    sys.exit(1)
print("PASS — parse_priority_from_text invariant under the 3b switch")
