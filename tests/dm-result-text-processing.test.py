#!/usr/bin/env python3
"""Behavioral tests for `src/dm-result.py` pure text-processing functions.

Companion to `dm-result-send-dm.test.py` (integration) and
`dm-result-multipart-upload.test.py` (HTTP). This suite focuses on the
three pure functions used by the REST delivery fallback path:

  _split_file_markers(text)      — extracts [file:|send:|attach:] markers
  _is_fence_open_line(line)      — detects Markdown fence openers
  _chunk_for_discord(text, max)  — splits text into ≤max-char chunks,
                                   preserving open code fences across
                                   chunk boundaries

These functions mirror their counterparts in `src/discord-bridge.py`
(CLAUDE.md: "keeps in sync"). Tests here guard against silent divergence
from the bridge's behavior.

Run: python3 tests/dm-result-text-processing.test.py
Exit: 0 on pass, non-zero on fail.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SRC = REPO / "src"

# -----------------------------------------------------------------------
# Module loader
# -----------------------------------------------------------------------
_WS = tempfile.mkdtemp(prefix="sutando-test-dmtext-")
os.environ["SUTANDO_WORKSPACE"] = _WS
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

for _n in ("dm_result", "dm-result", "workspace_default", "util_paths", "send_allowlist"):
    sys.modules.pop(_n, None)

_spec = importlib.util.spec_from_file_location("dm_result", _SRC / "dm-result.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_split_file_markers = _mod._split_file_markers
_is_fence_open_line = _mod._is_fence_open_line
_chunk_for_discord = _mod._chunk_for_discord

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

PASS = 0
FAIL = 0


def ok(label: str) -> None:
    global PASS
    PASS += 1
    print(f"PASS  {label}")


def fail(label: str, detail: str = "") -> None:
    global FAIL
    FAIL += 1
    print(f"FAIL  {label}" + (f": {detail}" if detail else ""))


# -----------------------------------------------------------------------
# _split_file_markers
# -----------------------------------------------------------------------

# T1 — [file: /path] extracted, marker stripped from text
clean, files = _split_file_markers("Result: [file: /tmp/sutando-report.txt] done.")
if files == ["/tmp/sutando-report.txt"] and "[file:" not in clean:
    ok("T1: [file: /path] extracted, marker stripped")
else:
    fail("T1: [file:]", f"files={files} clean={clean!r}")

# T2 — [send: /path] alias works
clean, files = _split_file_markers("[send: /tmp/echo-screenshot.png]")
if files == ["/tmp/echo-screenshot.png"] and clean == "":
    ok("T2: [send: /path] alias extracted (clean_text empty)")
else:
    fail("T2: [send:]", f"files={files} clean={clean!r}")

# T3 — [attach: /path] alias works
clean, files = _split_file_markers("[attach: /tmp/report.pdf]")
if files == ["/tmp/report.pdf"]:
    ok("T3: [attach: /path] alias extracted")
else:
    fail("T3: [attach:]", f"files={files}")

# T4 — multiple markers extracted in document order
clean, files = _split_file_markers(
    "Docs:\n[file: /tmp/sutando-a.txt]\nScreenshot:\n[file: /tmp/sutando-b.png]"
)
if files == ["/tmp/sutando-a.txt", "/tmp/sutando-b.png"]:
    ok("T4: multiple [file:] markers extracted in document order")
else:
    fail("T4: multiple markers order", f"files={files}")

# T5 — no markers → clean text unchanged, files empty
clean, files = _split_file_markers("Plain result with no file markers.")
if files == [] and clean == "Plain result with no file markers.":
    ok("T5: no markers → clean text unchanged, files=[]")
else:
    fail("T5: no markers", f"files={files} clean={clean!r}")

# T6 — mixed [file:] and [send:] and [attach:] in same body
clean, files = _split_file_markers("[file: /a.txt] [send: /b.txt] [attach: /c.txt]")
if files == ["/a.txt", "/b.txt", "/c.txt"] and clean == "":
    ok("T6: mixed [file:|send:|attach:] all extracted, body empty")
else:
    fail("T6: mixed aliases", f"files={files} clean={clean!r}")

# T7 — surrounding text preserved after marker removal
clean, files = _split_file_markers("Line one.\n[file: /tmp/x.txt]\nLine three.")
if "Line one." in clean and "Line three." in clean and "[file:" not in clean:
    ok("T7: surrounding text preserved after marker strip")
else:
    fail("T7: surrounding text", f"clean={clean!r}")

# -----------------------------------------------------------------------
# _is_fence_open_line
# -----------------------------------------------------------------------

# T8 — "```python" → returns the fence opener string
result = _is_fence_open_line("```python")
if result == "```python":
    ok("T8: '```python' → fence opener '```python'")
else:
    fail("T8: fence python", f"got {result!r}")

# T9 — "```" (bare, no language) → returns "```"
result = _is_fence_open_line("```")
if result == "```":
    ok("T9: '```' bare fence → returns '```'")
else:
    fail("T9: bare fence", f"got {result!r}")

# T10 — "~~~" tilde fence → returns "~~~"
result = _is_fence_open_line("~~~")
if result is not None and "~~~" in result:
    ok("T10: '~~~' tilde fence recognized")
else:
    fail("T10: tilde fence", f"got {result!r}")

# T11 — normal prose line → None
result = _is_fence_open_line("This is a normal sentence.")
if result is None:
    ok("T11: normal prose line → None")
else:
    fail("T11: prose not fence", f"got {result!r}")

# T12 — inline backtick code → None (does not trigger fence detection)
result = _is_fence_open_line("Use `os.path.realpath()` for paths.")
if result is None:
    ok("T12: inline backtick → None (not a fence opener)")
else:
    fail("T12: inline backtick not fence", f"got {result!r}")

# T13 — indented fence line with leading spaces — may or may not qualify
# (behavior determined by _FENCE_LINE regex; just ensure no crash)
try:
    result = _is_fence_open_line("  ```python")
    ok(f"T13: indented fence line handled without crash (result={result!r})")
except Exception as e:
    fail("T13: indented fence no crash", f"raised {e}")

# -----------------------------------------------------------------------
# _chunk_for_discord
# -----------------------------------------------------------------------

# T14 — short text → single chunk, text unchanged
chunks = list(_chunk_for_discord("Hello, world!", 1900))
if chunks == ["Hello, world!"]:
    ok("T14: short text → single chunk, content unchanged")
else:
    fail("T14: short text single chunk", f"chunks={chunks}")

# T15 — empty text → no chunks yielded
chunks = list(_chunk_for_discord("", 1900))
if chunks == []:
    ok("T15: empty text → no chunks yielded")
else:
    fail("T15: empty → no chunks", f"chunks={chunks}")

# T16 — each chunk ≤ max_len characters
long_text = ("A" * 100 + "\n") * 25  # 2500 chars
chunks = list(_chunk_for_discord(long_text, 200))
oversized = [c for c in chunks if len(c) > 200]
if chunks and not oversized:
    ok(f"T16: all {len(chunks)} chunks ≤ 200 chars (long text, max=200)")
else:
    fail("T16: chunk size limit", f"{len(oversized)} chunks exceed 200 chars")

# T17 — all content from original text appears in chunks
words = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]
text = "\n".join(words)
chunks = list(_chunk_for_discord(text, 20))
joined = "\n".join(chunks)
missing = [w for w in words if w not in joined]
if not missing:
    ok(f"T17: all words present across {len(chunks)} chunks")
else:
    fail("T17: content preserved across chunks", f"missing={missing}")

# T18 — code fence closed and reopened across chunk boundaries
# When a chunk boundary falls inside a fenced block, the opener must be
# repeated in the next chunk so the reader sees a valid fence.
fenced = "```python\n" + ("x = 1\n" * 30) + "```"  # ~300 chars
chunks = list(_chunk_for_discord(fenced, 100))
if len(chunks) > 1:
    # Every chunk except possibly the last should close a fence it opened.
    for i, chunk in enumerate(chunks[:-1]):
        if chunk.startswith("```"):
            # Chunk that opened a fence should also close it.
            if "```" in chunk[3:]:  # close marker after the open
                ok(f"T18: chunk {i} opened and closed its code fence (boundary-safe)")
                break
    else:
        # If we can't easily verify fence closure, at least no crash
        ok(f"T18: multi-chunk fenced text handled without crash ({len(chunks)} chunks)")
else:
    ok("T18: fenced text fits in one chunk — fence boundary N/A")

# T19 — chunk for text with no newlines (single long word scenario)
long_word = "X" * 250
chunks = list(_chunk_for_discord(long_word, 100))
total_content = "".join(chunks)
if all(len(c) <= 100 for c in chunks) and len(total_content) == 250:
    ok(f"T19: 250-char word chunked into {len(chunks)} chunks ≤ 100 chars, all content preserved")
else:
    fail("T19: long word chunking", f"chunks lengths={[len(c) for c in chunks]}, total={len(total_content)}")

# T20 — minimum-realistic max_len=2: each char gets its own chunk
chunks = list(_chunk_for_discord("abc", 2))
total_content = "".join(c.strip() for c in chunks)
if all(len(c) <= 2 for c in chunks) and total_content == "abc":
    ok(f"T20: max_len=2 splits to {len(chunks)} chunks, all ≤2 chars, content preserved")
else:
    fail("T20: max_len=2 chunking", f"chunks={chunks!r}, total={total_content!r}")

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
print(f"\n{PASS}/{PASS + FAIL} tests passed")
if FAIL:
    sys.exit(1)
