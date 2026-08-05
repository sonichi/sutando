#!/usr/bin/env python3
"""Events-plane boundary guard (docs/architecture-boundaries.md, 2026-08-05).

sparrow owns the RESIDENT event plane (durable inbox, SSE pump, cursor
persistence); agent-room-ops owns the ON-DEMAND surface (subscribe/pull/
debug-stream). This test pins the boundary from both sides so neither
component regrows the other's half:

  1. The durable event store stays sparrow-only: no sqlite usage appears in
     the room-ops skill (positive control: sparrow's EventInbox IS sqlite-
     backed, proving the probe detects what it guards).
  2. No cross-dependency: sparrow never imports the room-ops skill and the
     room-ops skill never imports sparrow — both are gateway clients, not
     each other's clients.
  3. The grandfathered debt is exactly `stream_with_resume` in room-ops
     events.py: the resident-lifecycle allowlist there is FROZEN — this test
     fails if a new `while True` loop is added outside it (the "reduce the
     allowlist monotonically" rule from the migration principles).

Run: python3 tests/events-plane-boundary.test.py
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROOM_OPS = REPO / "skills" / "agent-room-ops"
SPARROW = REPO / "packages" / "ag2-sparrow" / "ag2_sparrow"

passed = failed = 0


def check(name: str, cond: bool) -> None:
    global passed, failed
    print(("  ok  " if cond else "FAIL  ") + name)
    passed += bool(cond)
    failed += not cond


def _py_sources(root: Path) -> "list[Path]":
    return [p for p in root.rglob("*.py") if "test" not in p.name]


# 1. Durable event store is sparrow-only.
roomops_sqlite = [p.name for p in _py_sources(ROOM_OPS)
                  if "sqlite" in p.read_text(errors="replace")]
check("room-ops skill has no sqlite-backed store (durable inbox = sparrow-only)",
      roomops_sqlite == [])
inbox_src = (SPARROW / "event_inbox.py").read_text(errors="replace")
check("positive control: sparrow EventInbox IS sqlite-backed (probe detects)",
      "sqlite" in inbox_src)

# 2. No cross-IMPORT in either direction. Prose/comment references are fine —
# the boundary is about code dependency. Matches real import statements only.
_IMPORT = r"^\s*(from|import)\s+"
sparrow_imports_roomops = [
    p.name for p in _py_sources(SPARROW)
    if any(re.match(_IMPORT, ln) and re.search(r"agent[-_]?room[-_]?ops", ln)
           for ln in p.read_text(errors="replace").splitlines())]
check("sparrow never IMPORTS the room-ops skill", sparrow_imports_roomops == [])
# Grandfathered: events_acceptance.py runs the taskify promotion client on
# sparrow's consumer machinery (see architecture-boundaries.md). Frozen set —
# any NEW file importing ag2_sparrow from the skill fails here.
ALLOWED_SPARROW_IMPORTS = {"events_acceptance.py"}
roomops_imports_sparrow = {
    p.name for p in _py_sources(ROOM_OPS)
    if any(re.match(_IMPORT, ln) and "ag2_sparrow" in ln
           for ln in p.read_text(errors="replace").splitlines())}
check(f"room-ops ag2_sparrow imports frozen to {sorted(ALLOWED_SPARROW_IMPORTS)} (found {sorted(roomops_imports_sparrow)})",
      roomops_imports_sparrow <= ALLOWED_SPARROW_IMPORTS)
check("positive control: the grandfathered import still exists (allowlist not stale)",
      roomops_imports_sparrow >= ALLOWED_SPARROW_IMPORTS)

# 3. Resident-loop allowlist in room-ops is frozen at the grandfathered debt.
# events.py:stream_with_resume = the resident-lifecycle debt (see doc);
# _gateway.py:_token_from_vault = a BOUNDED ancestor-directory walk, listed so
# the freeze stays a strict "no new while-True anywhere in the skill" gate.
ALLOWED_RESIDENT_LOOPS = {("events.py", "stream_with_resume"),
                          ("_gateway.py", "_token_from_vault")}
found = set()
for p in _py_sources(ROOM_OPS):
    src = p.read_text(errors="replace")
    current_fn = None
    for line in src.splitlines():
        m = re.match(r"def\s+(\w+)", line)
        if m:
            current_fn = m.group(1)
        if re.search(r"\bwhile\s+True\b", line):
            found.add((p.name, current_fn or "<module>"))
check(f"room-ops resident loops frozen to grandfathered set {sorted(ALLOWED_RESIDENT_LOOPS)} (found {sorted(found)})",
      found <= ALLOWED_RESIDENT_LOOPS)
check("positive control: the grandfathered loop still exists (allowlist not stale)",
      found >= ALLOWED_RESIDENT_LOOPS)


# 4. Durable-cursor ownership + taskification are sparrow-plane (frozen debt).
# save_cursor/durable-cursor machinery in the skill is confined to events.py
# (the grandfathered stream_with_resume support); no NEW file may own cursors.
# The complete grandfathered durable-cursor surface: events.py implements
# stream_with_resume/save_cursor; room_ops.py is its CLI wiring (which already
# offers bounded modes --once/--max-events — the unbounded default is the
# debt); events_acceptance.py is the promotion client riding the same wrapper.
cursor_owners = {p.name for p in _py_sources(ROOM_OPS)
                 if re.search(r"\bsave_cursor\b|cursor[_-]file", p.read_text(errors="replace"))}
ALLOWED_CURSOR_OWNERS = {"events.py", "room_ops.py", "events_acceptance.py"}
check(f"durable-cursor ownership frozen to {sorted(ALLOWED_CURSOR_OWNERS)} (found {sorted(cursor_owners)})",
      cursor_owners <= ALLOWED_CURSOR_OWNERS)
# Taskification (writing task files from events) is confined to the
# grandfathered events_acceptance.py promotion client.
# Tripwire, not proof: matches the promotion contract's provenance stamp and
# the taskify mode registration — the signals any compliant promotion writer
# must carry — rather than prose mentions of the word.
taskifiers = {p.name for p in _py_sources(ROOM_OPS)
              if re.search(r"promotion_reason|mode.{0,12}taskify", p.read_text(errors="replace"))}
ALLOWED_TASKIFIERS = {"events_acceptance.py"}
check(f"event taskification frozen to {sorted(ALLOWED_TASKIFIERS)} (found {sorted(taskifiers)})",
      taskifiers <= ALLOWED_TASKIFIERS)

print(f"\n{passed}/{passed + failed} passed")
raise SystemExit(0 if failed == 0 else 1)
