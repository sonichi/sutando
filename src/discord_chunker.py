"""Discord message chunking — fence-aware split for the 2000-char limit.

Canonical implementation. Imported by `src/discord-bridge.py` and
`src/dm-result.py`. Replaces the previous arrangement of two copies that
had already drifted on the long-line hard-split branch (the dm-result
copy correctly accounted for `buf_len` and the `+1` newline reservation
inside the while-loop condition; the discord-bridge copy did not). The
drift was invisible to `tests/discord-chunker.test.py` because that
test, ironically, tested BOTH copies separately precisely to catch
drift — yet none of its cases exercised the edge where the conditions
disagreed.

Behavior notes (verbatim from the previous in-file docstrings):

- The naive `range(0, len, max_len)` chunker breaks code blocks: if a
  fence opens before the chunk boundary and closes after, the first
  chunk renders as a half-open code block on Discord and the second
  chunk leaks the literal trailing backticks as plain text.
- This chunker walks line-by-line, tracks fence state (the exact
  opener string when inside a fence; None when outside). When a new
  line would push the buffer past max_len, it closes the current
  fence (if open) with a matching closer, yields the buffer, and
  reopens the SAME opener in the next chunk — preserving language
  tags and fence-token length.
- Fence detection only matches real block-fence lines (regex-anchored).
  Inline backticks in code or prose (`print("```")`, `use ```js`) do
  NOT toggle state.
- Single-line content longer than max_len is hard-split mid-line;
  fence state is preserved across the split.
"""
import re


_FENCE_LINE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([^\s`~][^`~]*)?\s*$")


def _is_fence_open_line(line: str):
    """Return the fence opener string if `line` is a real Markdown
    block-fence line, else None.

    A fence line is one whose stripped content is just a backtick/tilde
    run of >=3 optionally followed by a language/info string. Lines like
    `print("```")`, shell heredocs, or `use ```js inline` do NOT match
    — they have non-fence content adjacent to the backticks.
    """
    if not _FENCE_LINE.match(line):
        return None
    return line.strip()


def _chunk_for_discord(text: str, max_len: int = 1900):
    """Yield Discord-safe chunks <= max_len chars, preserving Markdown
    code fences across chunk boundaries.

    `max_len` defaults to 1900 (Discord's hard limit is 2000; the 100-
    char headroom covers fence closer + reopener insertion).
    """
    if not text:
        return
    fence_opener = None  # full opener string when inside a fence; None when outside
    buf = []
    buf_len = 0

    def fence_closer(opener):
        # Tilde fences close with tildes, backtick fences close with
        # backticks. Token-kind preservation.
        return opener[0] * 3 if opener else "```"

    def flush():
        nonlocal buf, buf_len
        if not buf:
            return None
        chunk = "\n".join(buf)
        if fence_opener:
            chunk = chunk + "\n" + fence_closer(fence_opener)
        buf = []
        buf_len = 0
        return chunk

    for line in text.split("\n"):
        opener_on_line = _is_fence_open_line(line)
        line_overhead = len(line) + 1  # +1 for newline
        reserve = (len(fence_closer(fence_opener)) + 1) if fence_opener else 0

        if buf_len + line_overhead + reserve > max_len and buf:
            chunk = flush()
            if chunk is not None:
                yield chunk
            if fence_opener:
                buf.append(fence_opener)
                buf_len = len(fence_opener) + 1

        if line_overhead + reserve > max_len:
            # Long single line — hard-split. While-condition accounts
            # for both `buf_len` (existing content) AND the `+1`
            # newline reservation that `"\n".join(buf)` will add. The
            # discord-bridge.py pre-extract copy dropped both terms;
            # the dm-result.py pre-extract copy kept them. This (dm-
            # result-derived) version is the conservative one.
            remaining = line
            while len(remaining) + 1 + reserve > max_len - buf_len:
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

        # Update fence state AFTER placing the line (the line itself is
        # intact). Fence-line at this position closes the active fence
        # (Discord/CommonMark allows any same-kind close-fence to close).
        if opener_on_line is not None:
            if fence_opener is None:
                fence_opener = opener_on_line
            else:
                fence_opener = None

    chunk = flush()
    if chunk is not None:
        yield chunk
