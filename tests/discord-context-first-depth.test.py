#!/usr/bin/env python3
"""CONTEXT-FIRST must state its own read depth and its continuation (#4148).

The Discord instruction named a reader whose default is ONE page of ten, and
said nothing about going deeper. Measured: a fact buried under nine or more
messages was reported as absent, every run, 475 commits apart. The depth
policy belongs to the instruction, not to a CLI default the instruction never
mentions, so this pins three things the bridge text must carry and ties each
to the mechanism the reader actually offers:

  1. an explicit `--limit N` on the command, with N past the measured miss
     boundary — and the reader's own default still BELOW it (control: if the
     default ever rises to N the pin stops carrying information);
  2. a continuation the reader accepts (`--until`), so "read until the message
     stands on its own" names a way to do that;
  3. truncation honesty — a read that stops short must be reported as a read
     that stopped, never as the fact being absent.

Run: python3 tests/discord-context-first-depth.test.py
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"

# Deepest burial the report measured as a MISS (38 under a 10-page).
MEASURED_MISS_DEPTH = 38

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok: {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL: {name} {detail}", file=sys.stderr)


def context_first_step(bridge_src: str) -> str:
    """The CONTEXT-FIRST step's source text: from its literal to the next step."""
    start = bridge_src.index("CONTEXT-FIRST (unconditional)")
    end = bridge_src.index("step += 1", start)
    return bridge_src[start:end]


def main() -> int:
    bridge = (SRC / "discord-bridge.py").read_text()
    reader = (SRC / "discord-read.py").read_text()
    skill = (REPO / "skills" / "context-reconstruct" / "SKILL.md").read_text()
    step = context_first_step(bridge)

    # 1. explicit depth, past the measured miss, and the reader default below it
    m = re.search(r"discord-read\.py \{channel_id_str\} --serving \{channel_id_str\} --limit (\d+)", step)
    check("instruction command carries an explicit --limit", m is not None, step[:200])
    limit = int(m.group(1)) if m else 0
    check(f"explicit limit ({limit}) exceeds the measured miss depth ({MEASURED_MISS_DEPTH})",
          limit > MEASURED_MISS_DEPTH)
    d = re.search(r'"--limit",\s*type=int,\s*default=(\d+)', reader)
    check("reader still has its own default (the pin is about the instruction, not the CLI)",
          d is not None)
    check("CONTROL: reader default is below the instruction's limit, so the instruction is load-bearing",
          d is not None and int(d.group(1)) < limit,
          f"default={d.group(1) if d else '?'} limit={limit}")
    check("reader caps a page at 100, so the requested limit is honoured in one call",
          limit <= 100 and "min(max(args.limit, 1), 100)" in reader)

    # 2. a continuation the reader accepts
    check("instruction names --until as the way to read older", "`--until" in step)
    check("reader accepts --until", 'add_argument("--until"' in reader)
    check("instruction says the first call is ONE page", "ONE page" in step)

    # 3. truncation honesty
    check("instruction forbids reporting absence from unread messages",
          "never report a fact as absent from messages you did not read" in step)
    check("instruction asks for the oldest timestamp read when stopping short",
          "oldest timestamp you read" in step)

    # the sibling pin (discord-reader-merge) must still hold: --serving stays first
    check("--serving pin intact", "src/discord-read.py {channel_id_str} --serving {channel_id_str}" in step)

    # the skill doc teaches the same depth + continuation
    check("context-reconstruct doc carries --limit", "--serving <task channel_id> --limit" in skill)
    check("context-reconstruct doc carries --until", "--until <ISO time or message id>" in skill)

    if FAILS:
        print(f"\nFAILED {len(FAILS)}: {FAILS}", file=sys.stderr)
        return 1
    print("\nPASS: CONTEXT-FIRST states its depth, its continuation, and truncation honesty")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
