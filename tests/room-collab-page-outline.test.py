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
reveal = outline('<div class="reveal"><div class="slides"><section><h2>One</h2><section><h3>nested</h3></section>'
                 '</section><section><h1>Two</h1><p data-topic="k">key</p></section></div></div>')
assert [s["title"] for s in reveal["slides"]] == ["One", "Two"], reveal
assert reveal["slides"][1]["topics"] == [{"topic": "k", "text": "key"}], reveal
page = outline("<h1>Report</h1><p>x</p><h2>Findings</h2>")
assert page == {"kind": "page", "headings": ["Report", "Findings"]}, page
# Element anchors: the ids the web client pins comments to, data-id before data-topic.
from page_outline import anchors  # noqa: E402
found = anchors('<section class="slide" data-topic="intro"><h1>Hi</h1><div data-id="cta" data-topic="x">Buy '
                '<b>now</b></div><p data-id="bad value">z</p><img data-id="logo"></section>'
                '<p data-topic="k">tail</p><script>var s=\'<p data-id="q">\'</script><p data-id="%s">long</p>' % ("x" * 65))
assert [(a["anchor"], a["slide"], a["text"]) for a in found] == [
    ("el:data-topic=intro", 1, "Hi Buy now z"), ("el:data-id=cta", 1, "Buy now"),
    ("el:data-id=logo", 1, ""), ("el:data-topic=k", None, "tail")], found
import room_collab  # noqa: E402
assert "el:data-id=cta" in room_collab.render_anchors('<div data-id="cta">Buy</div>', False)
assert "no data-id" in room_collab.render_anchors("<p>x</p>", False)
assert room_collab.build_parser().parse_args(["--kind", "html", "anchors", "!r:x"]).command == "anchors"
print("room-collab page outline: ok")
