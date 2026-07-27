#!/usr/bin/env python3
"""Tests for src/reply_chain.py — the pure reply-context formatter.

Regression + design test for the owner-reported truncation loss (Chi 2026-07-25:
"Remove the truncation", then "I'm not sure about full chain inline" → lean).

Lean contract:
  * inline the FULL immediate parent (no 400-char cap; size-clipped only for a
    pathological multi-KB body, and clipped LOUDLY);
  * do NOT inline deeper ancestors — those are referenced by reply_chain_ids;
  * reply_chain_ids carries the full walked spine, root-first.

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
check("attachment-only parent (blank content) -> ''",
      rc.format_reply_chain([E("a", "t", "   ")]) == "")

# --- immediate parent: full content, backward-compatible shape, no truncation ---
out = rc.format_reply_chain([E("Sutando-Pro#8185", "2026-07-25 17:42", "the full answer")])
check("immediate parent shape",
      out == "\n\n[Replying to Sutando-Pro#8185 (2026-07-25 17:42): the full answer]")

# The old 400-char cap is GONE: a 900-char body survives whole.
long_body = "x" * 900
out = rc.format_reply_chain([E("a", "t", long_body)])
check("no 400-char truncation", long_body in out and "…[" not in out)

# --- LEAN: a deep chain inlines ONLY the immediate parent ---
chain = [
    E("Sutando-Pro", "17:42", "the immediate reply I am responding to"),   # parent
    E("sonichi", "17:41", "GRANDPARENT should NOT be inlined"),
    E("sonichi", "17:38", "ROOT should NOT be inlined"),
]
out = rc.format_reply_chain(chain)
check("deep chain: immediate parent inlined", "the immediate reply I am responding to" in out)
check("deep chain: grandparent NOT inlined", "GRANDPARENT should NOT be inlined" not in out)
check("deep chain: root NOT inlined", "ROOT should NOT be inlined" not in out)
check("deep chain: single-line block (no 'Reply chain')", "Reply chain" not in out)

# --- pathological immediate parent: clipped LOUDLY, never silent ---
huge = "y" * 5000
out = rc.format_reply_chain([E("a", "t", huge)], max_msg_chars=2000)
check("pathological parent clipped", "…[+3000 chars" in out and "re-fetch" in out)
check("pathological parent length bounded", len(out) < 2200)

# --- format_reply_chain_ids: full root-first spine for thread reconstruction ---
check("no chain (<2 ids) -> ''", rc.format_reply_chain_ids([111]) == "")
check("empty ids -> ''", rc.format_reply_chain_ids([]) == "")
check("Nones filtered to <2 -> ''", rc.format_reply_chain_ids([None, 5, None]) == "")
# ids arrive immediate-parent-first; emitted root-first (reversed) with trailing \n
out = rc.format_reply_chain_ids([300, 200, 100])   # parent=300 … root=100
check("ids root-first line", out == "reply_chain_ids: 100,200,300\n")
check("ids line has trailing newline", out.endswith("\n"))
# the id spine keeps EVERY ancestor even though only the parent was inlined
out = rc.format_reply_chain_ids([1530634946949943497, None, 1530631339764875396])
check("id spine spans full chain, None dropped",
      out == "reply_chain_ids: 1530631339764875396,1530634946949943497\n")

# --- format_reply_chain_truncation: deep-thread id spine is never silently cut ---
# reached_root=True → the spine is complete → no marker
check("reached root -> no marker", rc.format_reply_chain_truncation(True, 999) == "")
check("reached root, no id -> no marker", rc.format_reply_chain_truncation(True, None) == "")
# not reached_root → visible marker anchored on the oldest captured id
mk = rc.format_reply_chain_truncation(False, 1530631339764875396)
check("truncated -> visible marker", "truncated" in mk and "1530631339764875396" in mk)
check("truncated marker tells how to recover", "reply to an older message" in mk)
check("truncated marker is a single leading-newline line",
      mk.startswith("\n") and mk.count("\n") == 1)
# defensive: no oldest id (empty walk) → nothing to anchor → no marker
check("truncated but no id -> no marker", rc.format_reply_chain_truncation(False, None) == "")

print()
if _fails:
    print(f"{len(_fails)} test(s) FAILED: {_fails}")
    sys.exit(1)
print("all tests passed")
