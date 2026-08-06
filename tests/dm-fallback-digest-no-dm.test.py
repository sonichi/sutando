#!/usr/bin/env python3
"""Regression guard for the 2026-08-06 DM digest flood.

## The incident

check-pending-questions writes a `question-{ts}.txt` digest every 30 min.
dm-result.py defers all DM delivery while sse-status reports voiceConnected —
that gate stayed true for ~24h, so ~20 near-identical digests accumulated in
results/. The moment the owner left VC the gate flipped and poll_dm_fallback
flushed every copy into her DM in one burst, retrying through HTTP 429s.

## The fix (two halves, owner-approved 2026-08-06 「提交 pr」)

1. poll_dm_fallback: digest artifacts (question-/insight-) are web-UI/voice
   surfaces, never DM material. Past the 90s voice first-dibs grace window
   they are archived, not DM'd. briefing-/friction-/task- are unchanged.
2. check-pending-questions.notify_voice: a new digest supersedes older
   undelivered ones (「新清单作废旧清单」), so no consumer can ever drain a
   backlog of near-identical copies.
"""

import re
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGE_SRC = (REPO / "src" / "discord-bridge.py").read_text()
CPQ_SRC = (REPO / "src" / "check-pending-questions.py").read_text()

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        failures.append(name)


def poll_dm_fallback_body() -> str:
    m = re.search(r"async def poll_dm_fallback\(\):.*?(?=^(?:async )?def |\Z)",
                  BRIDGE_SRC, re.MULTILINE | re.DOTALL)
    assert m, "poll_dm_fallback not found"
    return m.group(0)


# --- Half 1: bridge never DMs digest artifacts --------------------------------
body = poll_dm_fallback_body()

digest_branch = re.search(
    r'if f\.name\.startswith\(\("question-", "insight-"\)\):'
    r'.*?archive_file\(f, "results", f\.stem\).*?continue',
    body, re.DOTALL)
check("digest branch exists (question-/insight- -> archive, no DM)",
      digest_branch is not None)

if digest_branch:
    grace_pos = body.find("if age < GRACE_SECONDS:")
    send_pos = body.rfind("dm-result.py")  # rfind: first hit is the docstring
    check("digest branch sits AFTER the voice first-dibs grace check",
          grace_pos != -1 and digest_branch.start() > grace_pos,
          f"(grace at {grace_pos}, branch at {digest_branch.start()})")
    check("digest branch sits BEFORE the dm-result send",
          send_pos != -1 and digest_branch.start() < send_pos,
          f"(branch at {digest_branch.start()}, send at {send_pos})")

check("briefing-/friction- delivery NOT swept into the digest branch",
      'startswith(("question-", "insight-"))' in body
      and 'startswith(("question-", "insight-", "briefing-"' not in body)


# --- Half 2: notify_voice supersedes older digests ----------------------------
m = re.search(r"def notify_voice\(questions\):.*?(?=^def |\Z)",
              CPQ_SRC, re.MULTILINE | re.DOTALL)
assert m, "notify_voice not found"
nv_src = m.group(0)

check("notify_voice unlinks older question-*.txt before writing",
      re.search(r'glob\("question-\*\.txt"\).*?unlink', nv_src, re.DOTALL) is not None)

# Exercise it for real: two stale digests + one write -> exactly one file left.
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    (tmp / "question-111.txt").write_text("old wall 1")
    (tmp / "question-222.txt").write_text("old wall 2")
    (tmp / "task-333.txt").write_text("unrelated result")
    ns = {"RESULTS_DIR": tmp, "time": __import__("time"), "Path": Path}
    exec(compile(nv_src, "notify_voice", "exec"), ns)
    ns["notify_voice"]([{"title": "q1"}, {"title": "q2"}])
    remaining = sorted(p.name for p in tmp.glob("question-*.txt"))
    check("exactly one digest remains after a new write",
          len(remaining) == 1, f"(got {remaining})")
    check("the survivor is the NEW digest",
          bool(remaining) and remaining[0] not in ("question-111.txt", "question-222.txt"))
    check("non-digest results untouched", (tmp / "task-333.txt").exists())

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    sys.exit(1)
print("all checks passed")
