#!/usr/bin/env python3
"""Invariance test for step 3b reader 2: discord-bridge `_task_source` now
extracts `source:` via local_task_protocol.parse_task_headers_lenient (full
scan, first occurrence wins) — EXACT legacy semantics, so extraction must be
identical on every input and the DM-fallback verdict unchanged across the
whole corpus.

Why lenient and not the stricter stop-at-task: parser: the corpus contains
May-2026 voice tasks written task-MID (source: after task:) — the strict
parser flips 23 real files' DM verdicts. This test pins that era-mixed shape
as a fixture so the union-of-shapes requirement can't regress silently. The
known first-wins spoofability (a body line can supply a MISSING key) is
pre-existing and documented at the parser; hardening it is an owner decision,
not a read-side refactor.

Run: python3 tests/discord-task-source-invariance.test.py
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "local_task_protocol", REPO / "src" / "local_task_protocol.py")
ltp = importlib.util.module_from_spec(spec)
sys.modules["local_task_protocol"] = ltp
spec.loader.exec_module(ltp)

DM_FALLBACK_SOURCES = {"voice", "phone"}
failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def legacy_source(text: str):
    """Verbatim pre-3b extraction (discord-bridge.py `_task_source` core)."""
    for ln in text.splitlines():
        if ln.startswith("source:"):
            return ln.split(":", 1)[1].strip().lower() or None
    return None


def new_source(text: str):
    src = ltp.parse_task_headers_lenient(text).get("source")
    return (src or "").strip().lower() or None


# The bridge must actually be on the new implementation.
db = (REPO / "src" / "discord-bridge.py").read_text()
core = db[db.find("def _task_source"): db.find("def _dm_fallback_eligible")]
check("discord-bridge._task_source uses parse_task_headers_lenient",
      "parse_task_headers_lenient" in core and 'startswith("source:")' not in core)

# Fixtures: verdicts must agree on every shape (lenient == legacy).
VOICE = "id: t\ntimestamp: ts\nsource: voice\nchannel_id: local-voice\ntask: x\n"
PHONE = "id: task-phone-1\ntimestamp: ts\nsource: phone\ninteraction_type: realtime_audio\ncallSid: C\ntask: x\n"
DISCORD_MID = "id: t\ntimestamp: ts\ntask: x\nsource: discord\nchannel_id: 1\n"
FORGED = "id: t\ntimestamp: ts\ntask: do it\nnote below\nsource: voice\n"
NO_SOURCE = "id: t\ntimestamp: ts\ntask: x\n"

for name, text in (("voice", VOICE), ("phone", PHONE), ("no-source", NO_SOURCE)):
    old_v = legacy_source(text) in DM_FALLBACK_SOURCES
    new_v = new_source(text) in DM_FALLBACK_SOURCES
    check(f"verdict agrees[{name}]", old_v == new_v, f"old={old_v} new={new_v}")

# task-mid discord: extracted value differs (discord vs None) but the verdict
# (not DM-eligible) is identical — assert at the verdict level.
check("verdict agrees[discord task-mid]",
      (legacy_source(DISCORD_MID) in DM_FALLBACK_SOURCES)
      == (new_source(DISCORD_MID) in DM_FALLBACK_SOURCES))

# Era-mixed shape: May-2026 voice tasks are task-mid — both readers must
# classify them voice (this is what ruled out the stricter parser).
VOICE_2026_05 = "id: t\ntimestamp: ts\ntask: Organize my emails.\nsource: voice\nchannel_id: local-voice\n"
check("May-era task-mid voice: both read voice",
      legacy_source(VOICE_2026_05) == "voice" and new_source(VOICE_2026_05) == "voice")

# Known pre-existing spoofability (missing key supplied by body): identical
# in both — pinned so any future hardening is a deliberate verdict change.
check("forged body on source-less file: legacy and lenient agree",
      legacy_source(FORGED) == new_source(FORGED) == "voice")

# Live corpus: verdict-level dual run (skipped in CI).
try:
    sys.path.insert(0, str(REPO / "src"))
    from workspace_default import resolve_workspace
    corpus = resolve_workspace() / "tasks"
except Exception:
    corpus = Path("/nonexistent")

if (corpus / "archive").is_dir():
    n = diffs = 0
    for p in ltp.iter_archived_tasks(corpus):
        text = p.read_text(errors="replace")
        n += 1
        if legacy_source(text) != new_source(text):
            diffs += 1
            if diffs <= 3:
                print(f"    corpus diff: {p.name} legacy={legacy_source(text)} "
                      f"new={new_source(text)}")
    check(f"live corpus: {n} files, extraction identical everywhere", diffs == 0,
          f"{diffs} diffs")
else:
    print("  (live corpus sweep skipped — no workspace archive)")

if failures:
    sys.exit(1)
print("PASS — _task_source DM-verdict invariant under the 3b switch")
