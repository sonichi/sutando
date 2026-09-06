#!/usr/bin/env python3
"""An attachment carries the label that preceded it.

The bridge sends the body as ONE message and each file as ANOTHER, so the
composer's interleaved "<label>\n[file: ...]" order is lost on the wire and the
images arrive as an unlabelled stack. The caption travels on the action.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import result_markers as m

fails = []
def ck(name, got, want):
    if got != want:
        fails.append(f"{name}: got {got!r} want {want!r}")

def caps(body):
    return [(a.value, a.extra) for a in m.parse_markers(body).actions if a.kind == "attach"]

# the real composer shape — every image labelled
ck("composer 4-slot",
   caps("1。纯 text diagram\n[file: /a/d.png]\n2。纯 text plain\n[file: /a/p.png]\n"),
   [("/a/d.png", "1。纯 text diagram"), ("/a/p.png", "2。纯 text plain")])

# an absent slot's explanation is a legal caption too
ck("absent-slot line",
   caps("3。原文图 — 未能生成：403\n[file: /a/p.png]\n"),
   [("/a/p.png", "3。原文图 — 未能生成：403")])

# no preceding line -> no caption, not a crash
ck("marker first line", caps("[file: /a/d.png]\n"), [("/a/d.png", None)])

# a long paragraph is prose, not a label
ck("over-long prose", caps(("x" * 130) + "\n[file: /a/d.png]\n"), [("/a/d.png", None)])

# a marker never captions another marker
ck("marker above marker",
   caps("[file: /a/d.png]\n[file: /a/p.png]\n"),
   [("/a/d.png", None), ("/a/p.png", None)])

# the paths themselves are unchanged by captioning
ck("value untouched", [v for v, _ in caps("L\n[file: /a/d.png]\n")], ["/a/d.png"])

print("attachment-caption:", "PASS" if not fails else "FAIL")
for f in fails: print("  -", f)
sys.exit(1 if fails else 0)
