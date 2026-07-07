"""Shared message chunking — one fence-aware chunker for every outbound surface.

Result Router v1, slice **S3**. The per-message length caps differ per surface
(Discord 2000, Slack/Telegram 4000+), but the *hard* part — splitting long text
without shredding a Markdown code fence across a chunk boundary — is identical.
Before this module it lived only in the Discord bridge (`_chunk_for_discord`);
Slack and Telegram each hand-rolled a naive `range(0, len, N)` byte-slice, so:

- **Slack** (`chat_postMessage` defaults to `mrkdwn`): a result >4000 chars that
  contains a ```-fenced block was split mid-fence → the first message rendered a
  half-open code block and the second leaked the trailing backticks. **This is
  the user-visible bug S3 fixes** — Slack now shares this chunker.
- **Telegram** sends plain text (no `parse_mode`), so a mid-fence split is only
  cosmetic there; it is intentionally NOT wired here (nothing to render). If
  Telegram ever adopts MarkdownV2, route it through `chunk_message` too — the
  same fence-safety then becomes correctness (unbalanced entities would 400).

Pure functions only — no I/O, no bridge state — so any delivery path can adopt
it without coupling. `chunk_message` is the single source of truth; the Discord
bridge keeps its `_chunk_for_discord(text)` name as a thin `max_len=1900` alias
so its call sites and byte-for-byte output are unchanged.
"""

from __future__ import annotations

import re

# A real Markdown block-fence line: stripped content is just a backtick/tilde
# run of >=3, optionally followed by a language/info string. Anchored so inline
# backticks (`print("```")`, `use ```js`) never match.
_FENCE_LINE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([^\s`~][^`~]*)?\s*$")


def _is_fence_open_line(line: str):
    """Return the fence opener string if `line` is a real Markdown block-fence line.

    A fence line is one whose stripped content is just a backtick/tilde run of >=3
    optionally followed by a language/info string. Lines like `print("```")`,
    shell heredocs, or `use ```js inline` do NOT match — they have non-fence
    content before the fence chars on the same line.

    Returns the full fence opener (e.g. "```python", "~~~", "````markdown")
    so the chunker can reopen the SAME opener after a chunk boundary, preserving
    the language tag and the fence-token kind/length.

    Returns None if the line is not a fence line.
    """
    m = _FENCE_LINE.match(line)
    if not m:
        return None
    return line.strip()


def chunk_message(text: str, max_len: int = 1900):
    """Yield chunks <= max_len chars, preserving Markdown code fences.

    The naive `range(0, len, max_len)` chunker breaks code blocks: if a fence
    opens before the chunk boundary and closes after, the first chunk renders as
    a half-open code block and the second chunk leaks the literal trailing
    backticks as plain text.

    This chunker walks line-by-line, tracks fence state (the exact opener string
    when inside a fence; None when outside). When a new line would push the
    buffer past max_len, it closes the current fence (if open) with a matching
    closer, yields the buffer, and reopens the SAME opener in the next chunk —
    preserving language tags and fence-token length.

    Fence detection only matches real block-fence lines (regex-anchored). Inline
    backticks in code or prose (`print("```")`, `use ```js`) do NOT toggle state.

    Single-line content longer than max_len is hard-split mid-line; fence state
    is preserved across the split.
    """
    if not text:
        return
    fence_opener = None  # full opener string when inside a fence; None when outside
    buf = []
    buf_len = 0

    def fence_closer(opener):
        # Match the fence-token kind (` or ~) and use 3 of them. Discord's
        # parser closes on >=3 matching chars, so a 3-char closer suffices
        # even if opener was 4+ chars (the literal opener length doesn't have
        # to match for closure, only the char kind).
        return opener[0] * 3 if opener else "```"

    def flush():
        nonlocal buf, buf_len
        if not buf:
            return None
        chunk = "\n".join(buf)
        # If we're mid-fence at chunk boundary, close it so it renders cleanly
        if fence_opener:
            chunk = chunk + "\n" + fence_closer(fence_opener)
        buf = []
        buf_len = 0
        return chunk

    for line in text.split("\n"):
        # Real fence-line detection (only at start of stripped line, not anywhere)
        opener_on_line = _is_fence_open_line(line)
        # If we're outside a fence and this line is a fence-open, treat as opening.
        # If we're inside a fence and this line matches the fence-token kind,
        # treat as closing (we don't require exact length match for close).

        line_overhead = len(line) + 1  # +1 for newline
        # Reserve space for closing fence if we'd cut mid-fence
        reserve = (len(fence_closer(fence_opener)) + 1) if fence_opener else 0

        if buf_len + line_overhead + reserve > max_len and buf:
            chunk = flush()
            if chunk is not None:
                yield chunk
            # Reopen fence in next chunk if we were inside one
            if fence_opener:
                buf.append(fence_opener)
                buf_len = len(fence_opener) + 1

        # Single line longer than max_len → hard-split
        if line_overhead + reserve > max_len:
            remaining = line
            while len(remaining) + reserve > max_len:
                take = max_len - reserve - buf_len - 1
                if take <= 0:
                    chunk = flush()
                    if chunk is not None:
                        yield chunk
                    if fence_opener:
                        buf.append(fence_opener)
                        buf_len = len(fence_opener) + 1
                    take = max_len - reserve - buf_len - 1
                buf.append(remaining[:take])
                buf_len += take + 1
                remaining = remaining[take:]
                chunk = flush()
                if chunk is not None:
                    yield chunk
                if fence_opener:
                    buf.append(fence_opener)
                    buf_len = len(fence_opener) + 1
            buf.append(remaining)
            buf_len += len(remaining) + 1
        else:
            buf.append(line)
            buf_len += line_overhead

        # Update fence state AFTER placing the line (the line itself is intact)
        if opener_on_line is not None:
            if fence_opener is None:
                fence_opener = opener_on_line
            else:
                # Fence-line at this position closes the active fence
                # (Discord/CommonMark allows any close-fence of the same kind to close)
                fence_opener = None

    chunk = flush()
    if chunk is not None:
        yield chunk
