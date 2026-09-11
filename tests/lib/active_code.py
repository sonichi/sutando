#!/usr/bin/env python3
"""The one active-code/comment predicate these guards share.

Four private copies drifted apart; a guard that reads its own comment rule wrong
passes on a commented-out caller.
"""


def active_lines(text: str) -> list[str]:
    """Each line's code part. Comment-only lines are dropped entirely."""
    out = []
    for ln in text.splitlines():
        if ln.lstrip().startswith("#"):
            continue
        out.append(ln.split(" #", 1)[0])
    return out


def active_text(text: str) -> str:
    return "\n".join(active_lines(text))
