#!/usr/bin/env python3
"""Tests for src/reply_chain.py — the pure reply-context formatter.

Regression for the owner-reported truncation loss (Chi 2026-07-25:
"Remove the truncation"): the bridge used to inline only a 400-char
single-level snippet of the replied-to message, silently dropping the root
question in a deep thread. The formatter now inlines the FULL text and, for a
multi-level reply, the whole ancestor chain root-first — with visible size
guards so a pathological message/chain is trimmed loudly, never silently.

Run: python3 tests/reply-chain.test.py   (exit 0 pass / non-zero on failure)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import reply_chain as rc  # noqa: E402

_fails = []


def check(name, cond):
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}")
        _fails.append(name)


def E(author, ts, content):
    return {"author": author, "ts": ts, "content": content}


# --- empty / nothing to inline ---
check("empty chain -> ''", rc.format_reply_chain([]) == "")
check("all-blank content -> ''", rc.format_reply_chain([E("a", "t", "   ")]) == "")

# --- single level: backward-compatible shape, no truncation ---
out = rc.format_reply_chain([E("Sutando-Pro#8185", "2026-07-25 17:42", "the full answer")])
check("single level shape", out == "\n\n[Replying to Sutando-Pro#8185 (2026-07-25 17:42): the full answer]")

# The old 400-char cap is GONE: a 900-char body survives whole (well under the
# 2000-char pathological guard).
long_body = "x" * 900
out = rc.format_reply_chain([E("a", "t", long_body)])
check("no 400-char truncation", long_body in out and "…[" not in out)

# --- pathological single message: clipped LOUDLY, never silent ---
huge = "y" * 5000
out = rc.format_reply_chain([E("a", "t", huge)], max_msg_chars=2000)
check("pathological msg clipped", "…[+3000 chars" in out and "re-fetch" in out)
check("pathological msg length bounded", len(out) < 2200)

# --- multi-level: root-first ordering, full text of each ---
chain = [
    E("Sutando-Pro", "17:42", "the minimal fix answer"),   # immediate parent
    E("sonichi", "17:41", "is there no pr removing the truncation?"),
    E("sonichi", "17:38", "what's it like? show me the task file"),   # root-ish
]
out = rc.format_reply_chain(chain)
check("multi-level uses chain block", "[Reply chain" in out)
# root must appear BEFORE the immediate parent (chronological, root leads)
root_i = out.index("show me the task file")
parent_i = out.index("the minimal fix answer")
check("root ordered before parent", root_i < parent_i)
check("all three levels present",
      all(s in out for s in ["the minimal fix answer",
                             "is there no pr removing the truncation?",
                             "show me the task file"]))

# --- blank ancestor is skipped, walk not broken ---
chain = [E("a", "t1", "reply body"), E("b", "t2", "   "), E("c", "t3", "root body")]
out = rc.format_reply_chain(chain)
check("blank ancestor skipped", "root body" in out and "reply body" in out)

# --- total-size guard drops OLDEST ancestors, keeps immediate parent ---
big = [E(f"u{i}", f"t{i}", "z" * 400) for i in range(10)]  # 10 × 400 = 4000
out = rc.format_reply_chain(big, max_total_chars=1000)
check("size guard notes omitted ancestors", "older ancestor(s) omitted" in out)
check("size guard keeps immediate parent", "u0" in out)   # index 0 = immediate parent
check("size guard bounded total", len(out) < 1600)

# --- format_reply_chain_ids: root-first id spine for thread reconstruction ---
check("no chain (<2 ids) -> ''", rc.format_reply_chain_ids([111]) == "")
check("empty ids -> ''", rc.format_reply_chain_ids([]) == "")
check("Nones filtered to <2 -> ''", rc.format_reply_chain_ids([None, 5, None]) == "")
# ids arrive immediate-parent-first; emitted root-first (reversed) with trailing \n
out = rc.format_reply_chain_ids([300, 200, 100])   # parent=300 … root=100
check("ids root-first line", out == "reply_chain_ids: 100,200,300\n")
check("ids line has trailing newline", out.endswith("\n"))
# real discord snowflakes (ints) stringify cleanly, None ancestors dropped
out = rc.format_reply_chain_ids([1530634946949943497, None, 1530631339764875396])
check("snowflakes stringified, None dropped",
      out == "reply_chain_ids: 1530631339764875396,1530634946949943497\n")

print()
if _fails:
    print(f"{len(_fails)} test(s) FAILED: {_fails}")
    sys.exit(1)
print("all tests passed")
