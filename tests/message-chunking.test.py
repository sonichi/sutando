#!/usr/bin/env python3
"""Golden tests for src/message_chunking.py — Result Router slice S3.

`chunk_message` is the verbatim extraction of the Discord bridge's
`_chunk_for_discord` (now a thin `max_len=1900` alias), promoted to a shared
module so Slack reuses the same fence-aware logic instead of its old naive
`range(0, len, 4000)` byte-slice. These tests pin the invariants that matter
for every surface:

  1. every chunk is <= max_len
  2. every chunk is fence-balanced (opens outside a fence, ends outside one) —
     THE fix: a code block never renders half-open across a chunk boundary
  3. content that fits in one chunk comes back byte-identical (no behaviour drift)
  4. inline backticks never toggle fence state
  5. a long single line hard-splits and reassembles losslessly
  6. real content lines survive, in order

Run: python3 tests/message-chunking.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("message_chunking", REPO / "src" / "message_chunking.py")
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)
chunk_message = mc.chunk_message

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([^\s`~][^`~]*)?\s*$")


def ends_outside_fence(chunk: str) -> bool:
    """CommonMark-aware: True if `chunk` ends outside any code fence.

    Uses the module's own close rule (`mc._closes_fence`) so a nested opener
    (```python or a shorter run inside a ````markdown block) does NOT toggle the
    outer fence — a naive any-fence-line toggle would falsely report balance.
    """
    opener = None
    for line in chunk.split("\n"):
        if not _FENCE.match(line):
            continue
        fl = line.strip()
        if opener is None:
            opener = fl
        elif mc._closes_fence(fl, opener):
            opener = None
    return opener is None


# 1. Empty / falsy input yields nothing.
check("empty string → no chunks", list(chunk_message("", 100)) == [])
check("empty at default max_len → no chunks", list(chunk_message("", 1900)) == [])

# 2. Short text (no fence) → single, byte-identical chunk.
short = "hello world\nsecond line"
check("short text → single byte-identical chunk", list(chunk_message(short, 1900)) == [short])

# 3. A fenced block that fits → unchanged single chunk.
fenced_small = "before\n```python\nprint('hi')\n```\nafter"
check("small fenced block → single unchanged chunk",
      list(chunk_message(fenced_small, 1900)) == [fenced_small])

# 4. Inline backticks / info-string-in-prose do NOT toggle fence state → no spurious close.
inline = 'talk about `print("```")` and ```js inline usage without a real block'
out_inline = list(chunk_message(inline, 1900))
check("inline backticks stay in one chunk", out_inline == [inline])
check("inline backticks don't inject a closer",
      all("\n```" not in c[len(inline):] for c in out_inline) and out_inline == [inline])

# 5. THE FIX — a code block that spans a chunk boundary stays fence-balanced.
# 30 code lines inside one ```python fence, small max_len forces several splits.
body = "\n".join(f"line_{i} = {i}" for i in range(30))
big_fenced = f"intro paragraph\n```python\n{body}\n```\noutro paragraph"
chunks = list(chunk_message(big_fenced, 120))
check("fenced block splits into multiple chunks", len(chunks) > 1, f"got {len(chunks)}")
check("every chunk <= max_len", all(len(c) <= 120 for c in chunks),
      "lens=" + str([len(c) for c in chunks]))
check("every chunk is fence-balanced (no half-open code block)",
      all(ends_outside_fence(c) for c in chunks),
      "unbalanced=" + str([c for c in chunks if not ends_outside_fence(c)][:1]))
# The reopened fence must carry the SAME opener (language tag preserved).
reopened = [c for c in chunks if c.lstrip().startswith("```python")]
check("continuation chunks reopen with the same ```python opener", len(reopened) >= 1)

# 6. Content survival — every real (non-fence) line appears, in order.
def content_lines(text):
    return [ln for ln in text.split("\n") if not _FENCE.match(ln)]

orig_content = content_lines(big_fenced)
chunk_content = []
for c in chunks:
    chunk_content.extend(content_lines(c))
check("all real content lines survive in order", chunk_content == orig_content,
      f"orig={len(orig_content)} got={len(chunk_content)}")

# 7. Slack bug scenario — >4000 chars with a fence, chunked at 4000.
slack_body = "\n".join(f"row {i}: " + "x" * 60 for i in range(120))  # ~7.5k inside a fence
slack_text = f"here is a big log:\n```\n{slack_body}\n```\ndone"
slack_chunks = list(chunk_message(slack_text, 4000))
check("slack: splits at 4000", len(slack_chunks) > 1)
check("slack: every chunk <= 4000", all(len(c) <= 4000 for c in slack_chunks))
check("slack: every chunk fence-balanced (the bug that broke rendering)",
      all(ends_outside_fence(c) for c in slack_chunks))

# 8. Long single line hard-splits and reassembles losslessly (plain text, no fence).
longline = "z" * 10000
ll_chunks = list(chunk_message(longline, 4000))
check("long line hard-splits", len(ll_chunks) > 1)
check("long line each chunk <= max_len", all(len(c) <= 4000 for c in ll_chunks))
check("long line reassembles losslessly", "".join(ll_chunks) == longline,
      f"reassembled {len('' .join(ll_chunks))} vs {len(longline)}")

# 9. Tilde fences and 4+ backtick fences are handled (kind + length preserved on close).
tilde = "a\n~~~\n" + "\n".join(f"t{i}" for i in range(30)) + "\n~~~\nb"
tilde_chunks = list(chunk_message(tilde, 100))
check("tilde-fence chunks balanced", all(ends_outside_fence(c) for c in tilde_chunks))
check("tilde-fence closer uses ~ not backtick",
      all("`" not in c for c in tilde_chunks))

# 10. Mid-fence hard-split — a single very long line INSIDE a fence forces the
# hard-split loop to close+reopen the fence around each flushed piece (exercises
# the fence-reopen branch inside the while-split, not just the between-line one).
midfence = "```python\n" + ("y" * 300) + "\n```"
mf = list(chunk_message(midfence, 40))
check("mid-fence long line: splits into multiple chunks", len(mf) > 1)
check("mid-fence long line: each chunk <= max_len", all(len(c) <= 40 for c in mf))
check("mid-fence long line: each chunk fence-balanced", all(ends_outside_fence(c) for c in mf))
ypayload = "".join(ch for c in mf for line in c.split("\n") if line and set(line) <= {"y"} for ch in line)
check("mid-fence long line: y-payload preserved across splits", ypayload == "y" * 300,
      f"got {len(ypayload)}")

# 11. Nested fences (CommonMark) — an inner ```python / ``` inside an outer
# ````markdown block must NOT close the outer fence, and a boundary split must
# close+reopen the OUTER fence with a matching run (````), not ```. (P2 fix,
# reported by wu-air's Codex + john-the-dev.)
nested = "````markdown\n" + "a" * 3000 + "\n```python\nx = 1\n```\n" + "b" * 3000 + "\n````"
nc = list(chunk_message(nested, 4000))
check("nested-fence: splits into multiple chunks", len(nc) > 1, f"got {len(nc)}")
check("nested-fence: each chunk <= max_len", all(len(c) <= 4000 for c in nc))
check("nested-fence: each chunk CommonMark-balanced (outer fence not broken)",
      all(ends_outside_fence(c) for c in nc),
      "unbalanced=" + str([c[:40] for c in nc if not ends_outside_fence(c)][:1]))
check("nested-fence: continuation reopens the OUTER ````markdown (not ```)",
      any(c.lstrip().startswith("````markdown") for c in nc[1:]))

# Direct unit-checks of the CommonMark close rule (covers _closes_fence/_fence_run).
check("_closes_fence: ``` does NOT close ````markdown", not mc._closes_fence("```", "````markdown"))
check("_closes_fence: ```python does NOT close ````markdown", not mc._closes_fence("```python", "````markdown"))
check("_closes_fence: ```` closes ````markdown", mc._closes_fence("````", "````markdown"))
check("_closes_fence: ``` closes ```python", mc._closes_fence("```", "```python"))
check("_closes_fence: ~~~ does NOT close ```", not mc._closes_fence("~~~", "```"))
check("_fence_run: counts leading run", mc._fence_run("````md") == 4 and mc._fence_run("~~~") == 3)

# chunk_plain_text: the plain-transport contract is byte-identity — nothing
# inserted (no synthetic fences), nothing dropped, every chunk bounded.
run9k = "x" * 9000
pc = mc.chunk_plain_text(run9k, 4000)
check("plain: unbreakable 9k run hard-cuts within bound",
      "".join(pc) == run9k and max(map(len, pc)) <= 4000, str(list(map(len, pc))))
body = "intro\n```python\n" + "\n".join(f"line {i} of a long listing" for i in range(300)) + "\n```\ntail"
pc2 = mc.chunk_plain_text(body, 4000)
check("plain: fenced body reassembles byte-identical (no fence bytes added)",
      "".join(pc2) == body and len(pc2) > 1, f"{sum(map(len, pc2))} vs {len(body)}")
check("plain: splits land after newlines when available",
      all(c.endswith("\n") for c in pc2[:-1]))
check("plain: empty input -> no chunks", mc.chunk_plain_text("", 4000) == [])
check("plain: exact-limit input is one chunk",
      mc.chunk_plain_text("y" * 4000, 4000) == ["y" * 4000])

# --- paragraph-aware splits + fence keep-whole (2026-08-19, "wider" gates) ---

# Paragraph preference: paragraphs of ~400 chars; a forced split must land at a
# blank line (chunk ends on a paragraph's last line), not mid-paragraph.
paras = ["para %d line one is quite long %s\npara %d line two %s" % (i, "w" * 150, i, "v" * 150)
         for i in range(8)]
para_text = "\n\n".join(paras)
pchunks = list(chunk_message(para_text, 1900))
check("para: multiple chunks generated", len(pchunks) > 1)
# Each non-final chunk's last line is a paragraph's SECOND line (ends with the
# v-run), never a first line (w-run) — a mid-paragraph cut.
check("para: no chunk ends mid-paragraph",
      all(c.split("\n")[-1].endswith("v" * 10) for c in pchunks[:-1]),
      str([c.split("\n")[-1][-20:] for c in pchunks[:-1]]))

# Fence keep-whole: a block that fits alone must land intact in one chunk —
# no synthetic close/reopen (the parent chunker's behavior).
prose = "\n".join("prose line %d %s" % (i, "p" * 80) for i in range(18))
fence_block = "```python\n" + "\n".join("code line %d" % i for i in range(30)) + "\n```"
ftext = prose + "\n" + fence_block
fchunks = list(chunk_message(ftext, 1900))
intact = ["```python" in c and c.rstrip().endswith("```") and c.count("```") == 2
          for c in fchunks if "code line" in c]
check("fence: whole block lands intact in one chunk",
      len(intact) == 1 and intact[0], str([c[:40] for c in fchunks]))

# Invariant 1 of this file's docstring, asserted across every fixture above.
for _label, _chunks, _cap in [
    ("inline", out_inline, 1900),
    ("big_fenced", chunks, 120),
    ("slack", slack_chunks, 4000),
    ("longline", ll_chunks, 4000),
    ("tilde", tilde_chunks, 100),
    ("midfence", mf, 40),
    ("nested", nc, 4000),
    ("paragraph", pchunks, 1900),
    ("fence_whole", fchunks, 1900),
]:
    _over = [len(c) for c in _chunks if len(c) > _cap]
    check("len<=max_len: %s" % _label, not _over, "over-limit: %s (cap %d)" % (_over, _cap))

# A cut retains a tail, so the line that follows must not overflow the buffer.
# Two caps: the overflow scales with max_len//4.
for _cap in (1900, 4000):
    _a = "\n".join("A" * 99 for _ in range(14))
    _b = "\n".join("B" * 99 for _ in range(4))
    _tail_line = "L" * (_cap - 50)
    _cut_chunks = list(chunk_message(_a + "\n\n" + _b + "\n" + _tail_line, _cap))
    _over = [len(c) for c in _cut_chunks if len(c) > _cap]
    check("para-cut then long line stays within cap %d" % _cap, not _over,
          "over-limit: %s" % _over)

# Lookback discriminator: several SHORT lines after the last blank, so the
# buffer grows past the blank before overflowing. The parent cuts mid-B here.
_a16 = "\n".join("A" * 99 for _ in range(16))
_b5 = "\n".join("B" * 80 for _ in range(5))
_lb = list(chunk_message(_a16 + "\n\n" + _b5, 1900))
check("lookback: paragraph B is never split across chunks",
      len(_lb) == 2 and "B" not in _lb[0] and _lb[1].count("B" * 80) == 5,
      str([len(c) for c in _lb]))

# fits_one_message: the compose-time half of the cap.
check("fits: short body is one message", mc.fits_one_message("hello"))
check("fits: 3k body is not", not mc.fits_one_message("z\n" * 1500))
check("fits: empty body is one message", mc.fits_one_message(""))

if failures:
    print(f"\nFAIL — {len(failures)} check(s) failed: {failures}")
    raise SystemExit(1)
print("\nPASS — message_chunking golden tests")
