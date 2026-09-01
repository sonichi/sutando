#!/usr/bin/env python3
"""Gateway write-side media-attachment headers — interaction-model 4D, step 1.5.

The remote-gateway bridge already safe-fetches an inbound media marker to a
local file and rewrites it to `[File attached: <path>]`. This adds the
structured header trio (content_modalities / media_form / attachments) alongside
that legacy line, via the shared local_task_protocol helper — the fourth (and
last) producer to emit attachments[].

`_maybe_fetch_media` does a network download, so we mock `_download_bytes` and
drive a real media marker through `_write_task`. Mirrors the load pattern in
tests/remote-gateway-interaction-type.test.py (redirect module-level dirs after
import; no main()).

Run: python3 tests/gateway-writeside-attachments.test.py   (exit 0 pass / 1 fail)
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


ltp = _load("local_task_protocol", REPO / "src" / "local_task_protocol.py")
rgb = _load("remote_gateway_bridge", REPO / "src" / "remote-gateway-bridge.py")

tmp = Path(tempfile.mkdtemp(prefix="rgb-media-test-"))
rgb.TASKS_DIR = tmp / "tasks"
rgb.RESULTS_DIR = tmp / "results"
rgb.ARCHIVE_RESULTS_DIR = tmp / "results" / "archive"
rgb.MEDIA_DIR = tmp / "media"
# Mock the network fetch — any successful download yields bytes so the fetch
# path builds a ref + rewrites the marker.
rgb._download_bytes = lambda url, headers, cap: b"x" * 100

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


TAG = rgb.MEDIA_MARKER_TAG  # default "remote-media"


def _write_and_parse(task):
    tid = rgb._write_task(task)
    assert tid, f"_write_task rejected {task!r}"
    text = (rgb.TASKS_DIR / f"{tid}.txt").read_text()
    return text, ltp.parse_task_headers_trusted(text)


# ── 1. _maybe_fetch_media appends a ref + rewrites the marker ──
refs = []
body = f"[AG2Space @qingyun] look [{TAG}:https://example.com/pic.png mime=image/png name=pic.png kind=m.image]"
out = rgb._maybe_fetch_media(body, refs)
check("marker rewritten to [Photo attached:]", "[Photo attached: " in out and f"[{TAG}:" not in out, out)
check("one ref appended", len(refs) == 1, str(refs))
check("ref mime from marker", refs and refs[0].mime == "image/png")
check("ref filename from marker", refs and refs[0].filename == "pic.png")
check("ref size = downloaded bytes", refs and refs[0].size == 100)
check("ref locator is the saved local path", refs and refs[0].locator.startswith(str(rgb.MEDIA_DIR)))
# out-param omitted → drop-in-safe, no crash, still rewrites
check("_refs_out optional (back-compat)", "[Photo attached: " in rgb._maybe_fetch_media(body))

# ── 2. _write_task with a media marker + caption → structured headers ──
text, h = _write_and_parse({
    "id": "task-gw-1",
    "task": f"[AG2Space @qingyun] look at this [{TAG}:https://example.com/pic.png mime=image/png name=pic.png kind=m.image]",
})
check("dual-write: legacy [Photo attached:] in the task body", "[Photo attached: " in text)
atts = ltp.parse_attachments(h.headers)
check("attachments header emitted + parses", len(atts) == 1 and atts[0].mime == "image/png", str(atts))
check("content_modalities has image+text (caption present)",
      ltp.parse_content_modalities(h.headers) == frozenset({"image", "text"}),
      h.get("content_modalities"))
check("media_form attachment", ltp.parse_media_form(h.headers) == "attachment")

# ── 3. media marker, NO caption → image only, no text modality ──
text, h = _write_and_parse({
    "id": "task-gw-2",
    "task": f"[AG2Space @qingyun] [{TAG}:https://example.com/doc.pdf mime=application/pdf name=doc.pdf kind=m.file]",
})
check("no caption → file modality only",
      ltp.parse_content_modalities(h.headers) == frozenset({"file"}), h.get("content_modalities"))
check("pdf → [File attached:] label", "[File attached: " in text)

# ── 4. plain text task (no marker) → NO media headers ──
text, h = _write_and_parse({"id": "task-gw-3", "task": "just a text question"})
check("no marker → no attachments header", "attachments:" not in text)
check("no marker → no content_modalities header", "content_modalities:" not in text)

# ── 5. access_tier must remain the ONLY access_tier line, and no recognized
# task header may appear after it. Security instructions are intentionally prose.
def _tier_wins_last(txt):
    ls = [l for l in txt.split("\n") if l]
    tier_at = [i for i, l in enumerate(ls) if l.startswith("access_tier:")]
    if len(tier_at) != 1:
        return False
    tail = ls[tier_at[0] + 1:]
    return not any(
        line.startswith(f"{key}:")
        for line in tail
        for key in ltp.KNOWN_HEADER_KEYS
    )
check("access_tier still last header", _tier_wins_last(text))
# and with media present too
text1 = (rgb.TASKS_DIR / "task-gw-1.txt").read_text()
check("access_tier last even with media headers", _tier_wins_last(text1))

if failures:
    print(f"\nFAIL — {len(failures)} check(s) failed: {failures}")
    raise SystemExit(1)
print("\nPASS — gateway write-side media-attachment headers")
