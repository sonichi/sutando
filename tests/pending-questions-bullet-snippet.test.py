#!/usr/bin/env python3
"""A bullet question must deliver its ASK, not just its bracketed label.

The proactive DM renders `snippet`; the bullet parser hardcoded it empty, so a
question carrying options and a default arrived as a slug and a date.
"""
import importlib.util
import tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "cpq", Path(__file__).resolve().parent.parent / "src" / "check-pending-questions.py")
cpq = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(cpq)
except SystemExit:
    pass

fails = []


def check(name, cond):
    print(("  ok  " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


def load(text):
    d = Path(tempfile.mkdtemp())
    f = d / "pending-questions.md"
    f.write_text(text)
    cpq.PQ_FILE = f
    return cpq.get_waiting_questions()


ASK = "One word: `handler` (land it -- rec) / `ungate`. Silence takes the recommendation."
q = load(f"# Pending\n\n- **[demo-slug, 2026-08-26]** {ASK}\n\n# Resolved\n")
check("the bullet is counted", len(q) == 1)
check("snippet carries the ask, not the empty string", bool(q and q[0]["snippet"]))
check("the options reach the rendered snippet", bool(q) and "One word" in q[0]["snippet"])
check("the label is not repeated into the snippet", bool(q) and "demo-slug" not in q[0]["snippet"])

# The control that can fail: a label-only bullet has no ask, and must not
# manufacture one out of its own title.
q2 = load("# Pending\n\n- **[bare-label, 2026-08-26]**\n\n# Resolved\n")
check("a bullet with no ask yields an empty snippet", len(q2) == 1 and q2[0]["snippet"] == "")

# Section-format questions keep their existing snippet behaviour.
q3 = load("# Pending\n\n## A section question\n\nBody line one.\n\n# Resolved\n")
check("section format still populates its snippet", len(q3) == 1 and "Body line one" in q3[0]["snippet"])

print(("FAILED: " + ", ".join(fails)) if fails else "ALL PASS")
raise SystemExit(1 if fails else 0)
