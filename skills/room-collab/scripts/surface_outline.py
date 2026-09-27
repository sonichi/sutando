"""What the board and the Doc show, as a voice agent needs it: the parts a move steps through.

The board is listed frame by frame, in the order the web client presents them
(boardPresent.ts `slideFrames`): rows top to bottom, each row left to right, a
frame starting above the row's lowest edge joining that row. Each frame has its
number, its name and the texts drawn in it. The Doc is listed by its `#`
headings outside code fences — the same rule as the client's docStage.ts — so
"heading N" here is heading N on every screen.
"""
from __future__ import annotations

import math
import re

TEXT_MAX = 80
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _box(f: dict) -> tuple[float, float, float, float]:
    x2, y2 = f["x"] + f["width"], f["y"] + f["height"]
    return min(f["x"], x2), min(f["y"], y2), max(f["x"], x2), max(f["y"], y2)


def slide_frames(elements: list[dict]) -> list[dict]:
    """The live frames in reading order."""
    frames = [e for e in elements
              if e.get("type") == "frame" and not e.get("isDeleted")
              and all(_finite(e.get(k)) for k in ("x", "y", "width", "height"))
              and e["width"] != 0 and e["height"] != 0]
    by_top = sorted(frames, key=lambda f: (_box(f)[1], _box(f)[0], f["id"]))
    rows: list[list[dict]] = []
    row_bottom = -math.inf
    for f in by_top:
        _, top, _, bottom = _box(f)
        if rows and top < row_bottom:
            rows[-1].append(f)
            row_bottom = max(row_bottom, bottom)
        else:
            rows.append([f])
            row_bottom = bottom
    return [f for row in rows for f in sorted(row, key=lambda f: (_box(f)[0], _box(f)[1]))]


def element_words(e: dict) -> str:
    """The words an element shows: a text's text, a frame's name."""
    if e.get("type") == "text":
        t = e.get("originalText") if isinstance(e.get("originalText"), str) else e.get("text")
        return t if isinstance(t, str) else ""
    if e.get("type") == "frame":
        return e["name"] if isinstance(e.get("name"), str) else ""
    return ""


def _words(s: str) -> str:
    return " ".join(s.split())


def board_outline(elements: list[dict]) -> dict:
    live = [e for e in elements if not e.get("isDeleted")]
    by_id = {e["id"]: e for e in live}
    slides = []
    for n, f in enumerate(slide_frames(live), 1):
        texts = []
        for e in live:
            if e.get("type") != "text":
                continue
            container = by_id.get(e.get("containerId")) if isinstance(e.get("containerId"), str) else None
            if f["id"] in (e.get("frameId"), (container or {}).get("frameId")):
                words = _words(element_words(e))[:TEXT_MAX]
                if words:
                    texts.append(words)
        slides.append({"n": n, "title": _words(element_words(f))[:TEXT_MAX] or None, "texts": texts})
    return {"kind": "board", "slides": slides}


def doc_headings(text: str) -> list[dict]:
    """The `#` headings with words, outside fenced code, in order."""
    out, fence = [], None
    for i, raw in enumerate(text.split("\n")):
        line = raw.rstrip("\r")
        f = FENCE.match(line)
        if f:
            mark = f.group(1)[0]
            fence = mark if fence is None else (None if mark == fence else fence)
            continue
        if fence is None:
            m = ATX.match(line)
            title = (m.group(2) or "").strip() if m else ""
            if title:
                out.append({"n": len(out) + 1, "level": len(m.group(1)), "title": title[:TEXT_MAX],
                            "line": i + 1})
    return out


def doc_outline(text: str) -> dict:
    return {"kind": "doc", "headings": doc_headings(text)}


def board_words(elements: list[dict]) -> str:
    """Everything the board says, lower-cased and single-spaced, one element per line."""
    return "\n".join(" ".join(element_words(e).lower().split())
                     for e in elements if not e.get("isDeleted"))
