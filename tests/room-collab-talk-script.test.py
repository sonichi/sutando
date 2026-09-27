#!/usr/bin/env python3
"""A talk script in the room's Doc becomes ordered steps of words and cues.

The failures worth pinning: a cue fired out of order with the words around it,
a bracket that is not a cue swallowed, and a script that runs past its section
into the rest of the Doc.

Run: python3 tests/room-collab-talk-script.test.py  (exit 0 pass / 1 fail)
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

from talk_script import extract, parse  # noqa: E402

DOC = """# Notes
ignore me [next]

## Talk script

Welcome. [highlight: Sutando] This is one agent.

That closes the loop. [next] Here is what we learned [laughs] [highlight: trust]
[pause 1.5] [slide 12] [prev] [clear]

## Appendix
not part of it [next]
"""

steps = parse(extract(DOC))
assert len(steps) == 2, steps
assert steps[0] == [{"say": "Welcome."}, {"cue": "highlight", "topic": "sutando"}, {"say": "This is one agent."}], steps[0]
assert steps[1] == [
    {"say": "That closes the loop."},
    {"cue": "slide", "move": "next"},
    {"say": "Here is what we learned [laughs]"},
    {"cue": "highlight", "topic": "trust"},
    {"cue": "pause", "seconds": 1.5},
    {"cue": "slide", "move": 12},
    {"cue": "slide", "move": "prev"},
    {"cue": "highlight", "topic": "clear"},
], steps[1]
assert extract("# Just notes\nno script") is None
assert parse(extract("### talk script\n\n[next]")) == [[{"cue": "slide", "move": "next"}]]
print("room-collab talk script: ok")
