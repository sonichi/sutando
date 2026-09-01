#!/usr/bin/env python3
"""The briefing's pending-question count must come from ONE predicate.

Regression test for a drift found 2026-07-28: check-pending-questions.py counted
33 waiting questions while morning-briefing.py counted 32, because the briefing
re-implemented the predicate. The entry it dropped was a live owner ask titled

    "/observe MVP: design fully resolved, build on your nod"

skipped by a `'RESOLVED' in title.upper()` substring test — the word appears in
the prose describing the *design*, not as a status marker. The same substring
test also fires on "NOT self-resolved", which says the opposite of resolved.

An open question that goes uncounted goes unsurfaced, so this asserts both that
the specific title survives AND that the two modules agree on a shared corpus.
Test 3 is the control: it fails against the old duplicated implementation.
"""
import importlib.util
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

CORPUS = """# Pending questions

## 2026-07-24T10:45Z — /observe MVP: design fully resolved, build on your nod
001 answered all 4 open design questions. **Non-blocking ask: nod and I build it.**

## 2026-07-20 — symlinks: NOT self-resolved
Still open despite the word appearing in the title.

## 2026-07-26T22:05Z — a plain open question
- **Status:** unanswered

## 2026-07-27T01:0xZ — an entry explicitly marked done
- **Status:** resolved

## ACTIVE — 2026-07-05 [organizer header, not a question]
Grouping shell.

## [RESOLVED 2026-07-03] shipped already
An inline resolution MARKER in the ACTIVE region — above the divider, no Status
field. Both consumers must agree it is not waiting.

## [RESOLVED?] Did this actually ship?
Question punctuation is NOT a resolution. This is an open uncertainty and must
survive on both surfaces (review [P1] 2026-07-28: `(?![\\w-])` let `?` through,
so an open ask was classified resolved and vanished from notifier AND briefing).

## [DONE?] Still waiting for confirmation
Same shape, second keyword.

## HELD deployment until the owner approves the migration
- **Status:** open

A real ask whose title merely BEGINS with an organizer keyword. `^HELD\\b` treated
it as a section shell and deleted it from both surfaces (review 2026-07-28).
An organizer shell is a keyword plus a SEPARATOR, never a sentence.

## Confirm whether the UI should render a [DONE] badge
- **Status:** open

A question ABOUT a marker. Searching for `[DONE]` anywhere in the title matched
it; a resolution marker must LEAD the title or it is not a marker.

# Resolved

## 2026-07-20T13:14Z — ✅ RESOLVED — this lives below the divider
Must never be counted.
"""

# Titles that MUST be counted as waiting. The first two are the bug: both carry
# the word "resolved" in prose while being open.
# NB: the briefing truncates titles to 60 chars for display, so these needles are
# kept short enough to survive it — a longer needle fails on truncation and reads
# as a dropped question, which cost one debugging cycle here.
MUST_COUNT = [
    "/observe MVP",
    "NOT self-resolved",
    "a plain open question",
    # Negative controls for the marker grammar: an interrogative marker is an
    # OPEN question, never a resolution. A guard that only rejects word chars and
    # hyphens lets `?` through and silently deletes these from both surfaces.
    "[RESOLVED?] Did this actually ship?",
    "[DONE?] Still waiting",
    # Shape controls: an organizer keyword that opens a real sentence, and a
    # marker mentioned mid-title. Both are explicitly Status: open.
    "HELD deployment until the owner",
    "render a [DONE] badge",
]
# Must NOT be counted: explicit status, organizer shell, below-divider.
MUST_NOT_COUNT = ["an entry explicitly marked done", "organizer header",
                  "below the divider", "shipped already"]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as td:
        pq = Path(td) / "pending-questions.md"
        pq.write_text(CORPUS)

        cpq = load("cpq_t", REPO / "src" / "check-pending-questions.py")
        cpq.PQ_FILE = pq

        # Exercise the SHIPPED function, not a re-implementation of it. The first
        # version of this test rebuilt the delegation inline and therefore never
        # ran the briefing's own presentation filter — it passed a regression
        # straight through. mb._CPQ is the single notifier instance the shipped
        # code calls, so pointing its PQ_FILE at the fixture is enough.
        mb = load("mb_t", REPO / "src" / "morning-briefing.py")
        if not hasattr(mb, "_CPQ"):
            # The pre-fix module re-implements the predicate and exposes no
            # notifier handle. Report that plainly instead of dying on an
            # AttributeError, so the control run says WHY it failed.
            print("  FAIL briefing does not delegate: no _CPQ notifier handle")
            print("Results: 1 failed")
            return 1
        # Patch the same seam the sibling regression tests use: the briefing
        # resolves its own file via personal_path and hands it to the predicate.
        from unittest.mock import patch as _patch
        _p = _patch.object(mb, "personal_path", return_value=pq); _p.start()

        notifier = [
            ((q.get("title") or q.get("id") or "") if isinstance(q, dict) else str(q))
            for q in cpq.get_waiting_questions()
        ]
        briefing = mb.get_pending_questions()
        _p.stop()

        # 1. RAW parity — same file, same count, no normalization. The previous
        #    version filtered organizer shells off the notifier side before
        #    comparing, which silently excused exactly the divergence this is
        #    supposed to catch: a test that pre-removes the difference cannot
        #    fail on it. Both classifications now live in the shared parser, so
        #    the two consumers must agree on the raw number.
        if len(notifier) != len(briefing):
            # Compare on the briefing's own truncation width. Comparing raw
            # strings makes any title longer than 60 chars look notifier-only,
            # which blamed an innocent entry in the first control run.
            seen = set(briefing)
            only = sorted(x for x in notifier if x[:60] not in seen)
            failures.append(
                f"count drift: notifier={len(notifier)} briefing={len(briefing)} "
                f"(notifier-only: {only})")

        # 2. every open entry is present, including the prose-"resolved" ones
        for needle in MUST_COUNT:
            if not any(needle in t for t in briefing):
                failures.append(f"open question dropped from briefing: {needle!r}")

        # 3. genuinely-resolved / structural entries stay out of BOTH consumers.
        #    Checking the notifier too is the point of the change: if only the
        #    briefing rejects them, the predicate has forked again.
        for needle in MUST_NOT_COUNT:
            if any(needle in x for x in briefing):
                failures.append(f"non-question counted as pending (briefing): {needle!r}")
            if any(needle in x for x in notifier):
                failures.append(f"non-question counted as pending (notifier): {needle!r}")

        # 4. Structural: the briefing must DELEGATE, not re-implement. Asserted on
        #    the function body rather than the whole file, because the module
        #    docstring quotes the retired rule and a naive file-wide grep matches
        #    its own explanation (it did, on the first run of this test).
        import inspect
        mb_mod = load("mb_struct", REPO / "src" / "morning-briefing.py")
        body = inspect.getsource(mb_mod.get_pending_questions)
        if "get_waiting_questions" not in body:
            failures.append("get_pending_questions no longer delegates to the notifier predicate")
        code = body.split('"""')[-1]  # skip the docstring; look only at executable lines
        if "RESOLVED" in code.upper() and "org_header" not in code:
            failures.append("a substring RESOLVED status test reappeared in the briefing")

    for f in failures:
        print(f"  FAIL {f}")
    if failures:
        print(f"Results: {len(failures)} failed")
        return 1
    print("  ok  briefing and notifier share one predicate; prose-'resolved' titles survive")
    print("Results: all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
