"""What an HTML page shows, as a voice agent needs it: slides and what can be pointed at.

A deck (elements whose class includes `slide`) is listed slide by slide: its
number, its title (first h1–h3), and its `data-topic` keys with the words they
label. Any other page is listed by its headings. The page is parsed, never run,
so text a script adds at runtime is not here.
"""
from __future__ import annotations

from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
SKIP = {"script", "style", "svg", "template", "noscript"}
TEXT_MAX = 80


class _Outline(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[dict] = []
        self.slides: list[dict] = []
        self.headings: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            return
        a = dict(attrs)
        node = {"tag": tag, "text": [], "slide": None, "topic": a.get("data-topic"), "heading": tag in ("h1", "h2", "h3")}
        if "slide" in (a.get("class") or "").split() and not any(n["slide"] is not None for n in self.stack):
            node["slide"] = {"n": len(self.slides) + 1, "id": a.get("id"), "title": None, "topics": []}
            self.slides.append(node["slide"])
        if tag in SKIP:
            self.skip += 1
        self.stack.append(node)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        while self.stack:
            node = self.stack.pop()
            if node["tag"] in SKIP:
                self.skip = max(0, self.skip - 1)
            words = " ".join(" ".join(node["text"]).split())[:TEXT_MAX]
            if self.stack:
                self.stack[-1]["text"].append(" ".join(node["text"]))
            slide = next((n["slide"] for n in reversed(self.stack) if n["slide"]), None) or node["slide"]
            if node["heading"] and words:
                if slide is not None and slide["title"] is None:
                    slide["title"] = words
                elif slide is None:
                    self.headings.append(words)
            if node["topic"] and slide is not None and "'" not in node["topic"]:
                slide["topics"].append({"topic": node["topic"], "text": words})
            if node["tag"] == tag:
                break

    def handle_data(self, data):
        if self.stack and not self.skip:
            self.stack[-1]["text"].append(data)


def outline(html: str) -> dict:
    p = _Outline()
    p.feed(html)
    p.close()
    if p.slides:
        return {"kind": "deck", "slides": p.slides}
    return {"kind": "page", "headings": p.headings}
