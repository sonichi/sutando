#!/usr/bin/env python3
"""Tiny CLI over tmux_probe.has_session(), for callers (start-cli.sh's relay
loop) that cannot import Python but must not duplicate its ABSENT_SIGNATURES.

Exit 0 = PRESENT, 1 = confirmed ABSENT, 2 = UNKNOWN (includes a timed-out or
refused client -- has_session()'s own subprocess timeout bounds the wait, so
the caller never blocks on a wedged tmux server).

Usage: tmux-probe-cli.py <socket> <session-target>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tmux_probe import has_session  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: tmux-probe-cli.py <socket> <session-target>", file=sys.stderr)
        return 2
    result = has_session(argv[1], argv[2])
    return 0 if result is True else (1 if result is False else 2)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
