#!/usr/bin/env python3
"""Model names the Claude CLI's `--model` and `/model` accept: an alias or a `claude-*` id.

One owner for the grammar, so a recorded worker model and a live switch refuse the same
names. A family alias or an id may carry a `[1m]` context tag. Stdlib only.

    python3 claude_model_name.py <name>    exit 0 when valid, 2 otherwise
"""
from __future__ import annotations

import re
import sys

PATTERN = re.compile(r"(default|(opus|sonnet|haiku|fable)(\[1m\])?|claude-[a-z0-9.-]+(\[1m\])?)")


def is_valid(name) -> bool:
    return isinstance(name, str) and PATTERN.fullmatch(name) is not None


if __name__ == "__main__":
    sys.exit(0 if len(sys.argv) == 2 and is_valid(sys.argv[1]) else 2)
