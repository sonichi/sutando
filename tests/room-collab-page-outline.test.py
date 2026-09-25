#!/usr/bin/env python3
"""The page outline a voice agent navigates by: slides, titles, and pointable topics.

Run: python3 tests/room-collab-page-outline.test.py  (exit 0 pass / 1 fail)
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

from page_outline import outline  # noqa: E402

deck = outline("""<main><section class="slide active" id="s1"><h1>My <b>AI</b> Stand</h1>
<div data-topic="ag2">AG2<br>framework</div><img src=x></section>
<section class="slide"><h2>Loop</h2><p data-topic="step1">Observe</p><script>var t='<h1>no</h1>'</script></section></main>""")
assert deck["kind"] == "deck", deck
assert deck["slides"][0] == {"n": 1, "id": "s1", "title": "My AI Stand", "topics": [{"topic": "ag2", "text": "AG2 framework"}]}, deck
assert deck["slides"][1]["title"] == "Loop" and deck["slides"][1]["topics"] == [{"topic": "step1", "text": "Observe"}]
page = outline("<h1>Report</h1><p>x</p><h2>Findings</h2>")
assert page == {"kind": "page", "headings": ["Report", "Findings"]}, page
print("room-collab page outline: ok")
