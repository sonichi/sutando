"""A talk script with cues, kept in the room's Doc where people can review it.

Under a heading named "Talk script", each paragraph is one step: words to say,
with cues in brackets where an action should happen:

    …and that closes the loop. [next] Here is what we learned. [highlight: trust]

Cues: [next] [prev] [slide 5] [highlight: <topic>] [clear] [pause 2].
Any other bracketed text is left in the words: the author meant it.
"""
from __future__ import annotations

import re

HEADING_RE = re.compile(r"^(#{1,6})\s*talk script\b.*$", re.I | re.M)
CUE_RE = re.compile(
    r"\[\s*(next|prev(?:ious)?|slide\s+(\d{1,3})|highlight\s*:\s*([A-Za-z0-9][A-Za-z0-9._-]{0,63})"
    r"|clear|pause\s+(\d{1,2}(?:\.\d+)?))\s*\]",
    re.I,
)


def extract(doc: str) -> str | None:
    """The text under the first "Talk script" heading, up to the next heading at
    the same or a higher level; None when the Doc has no such heading."""
    m = HEADING_RE.search(doc)
    if not m:
        return None
    level = len(m.group(1))
    rest = doc[m.end():]
    end = re.search(rf"^#{{1,{level}}}\s", rest, re.M)
    return rest[: end.start()] if end else rest


def cue_of(m: re.Match) -> dict:
    word = m.group(1).lower()
    if word == "next":
        return {"cue": "slide", "move": "next"}
    if word.startswith("prev"):
        return {"cue": "slide", "move": "prev"}
    if m.group(2):
        return {"cue": "slide", "move": int(m.group(2))}
    if m.group(3):
        return {"cue": "highlight", "topic": m.group(3).lower()}
    if word == "clear":
        return {"cue": "highlight", "topic": "clear"}
    return {"cue": "pause", "seconds": float(m.group(4))}


def parse(script: str) -> list[list[dict]]:
    """Steps in order; each step is its items in order, {"say": …} or a cue."""
    steps: list[list[dict]] = []
    for para in re.split(r"\n\s*\n", script):
        para = " ".join(para.split())
        if not para or para.startswith(("<!--", "---")):
            continue
        items: list[dict] = []
        at = 0
        for m in CUE_RE.finditer(para):
            words = para[at:m.start()].strip()
            if words:
                items.append({"say": words})
            items.append(cue_of(m))
            at = m.end()
        words = para[at:].strip()
        if words:
            items.append({"say": words})
        steps.append(items)
    return steps
