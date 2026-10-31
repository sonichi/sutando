#!/usr/bin/env python3
"""The dashboard's Answer form must say it means an answer.

POST /answer keeps an item open for resolve=false (a Reply, or a question back) and
resolves for resolve=true; its default is true only because this form used to send no
flag. Sending the flag explicitly is the first condition for flipping that default to
the recoverable side (keep open). See PR #4066's "When the default flips".

Run: python3 tests/web-client-answer-sends-resolve-true.test.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE = (REPO / "src" / "web-client.ts").read_text()

failures = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


calls = [m for m in re.finditer(r"fetch\(apiBase \+ '/answer',(.*?)\}\)\.then", SOURCE, re.S)]
check("exactly one /answer call in the client", len(calls) == 1, f"found {len(calls)}")
body = calls[0].group(1) if calls else ""
check("the body carries resolve: true", "resolve: true" in body, body[:200])
check("the body still carries the id and the trimmed answer",
      "id: qid" in body and "answer: answer.trim()" in body, body[:200])
check("no other /answer caller omits the flag",
      all("resolve" in m.group(1) for m in calls))

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
