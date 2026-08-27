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
- **Telegram** sends plain text (no `parse_mode`): there is nothing to render,
  so it must NOT use `chunk_message` — the synthetic close/re-open fences that
  transport inserts are formatting for Discord/Slack but literal extra bytes on
  a plain-text surface. Telegram uses `chunk_plain_text`, whose contract is
  byte-identity: the concatenation of the chunks IS the input. If Telegram ever
  adopts MarkdownV2, switch it to `chunk_message` + real escaping — fence
  safety then becomes correctness (unbalanced entities would 400).

Pure functions only — no I/O, no bridge state — so any delivery path can adopt
it without coupling. `chunk_message` is the single source of truth; the Discord
bridge uses it beneath its network-facing delivery budget while retaining a
lossless private alias for golden reassembly tests.
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


def _fence_run(fence_line: str) -> int:
    """Length of the leading backtick/tilde run of a (stripped) fence line."""
    c = fence_line[0]
    n = 0
    for ch in fence_line:
        if ch != c:
            break
        n += 1
    return n


def _closes_fence(closer: str, opener: str) -> bool:
    """CommonMark close rule: a fence closes only on a *bare* fence line of the
    same char kind whose run length >= the opener's run, with no info string.

    So inside a ````markdown block (run 4), an inner ```python line (info string)
    or a ``` line (run 3 < 4) is a NESTED opener, NOT a closer — it must leave the
    outer fence open. Both `closer` and `opener` are the stripped fence strings.
    """
    run = _fence_run(closer)
    return (
        closer[0] == opener[0]              # same fence char kind (` vs ~)
        and run >= _fence_run(opener)       # closing run at least as long
        and closer == closer[0] * run       # bare closer — no info/language string
    )


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
        # Close with a run matching the opener's length. CommonMark requires the
        # closing run >= the opening run, so a ````markdown fence (run 4) must be
        # closed with ```` — a 3-backtick closer would NOT close it, leaving the
        # continuation chunk still inside the outer fence.
        return opener[0] * _fence_run(opener)

    def flush():
        nonlocal buf, buf_len
        if not buf:
            return None  # pragma: no cover  (defensive: callers guard `and buf`)
        chunk = "\n".join(buf)
        # If we're mid-fence at chunk boundary, close it so it renders cleanly
        if fence_opener:
            chunk = chunk + "\n" + fence_closer(fence_opener)
        buf = []
        buf_len = 0
        return chunk

    lines = text.split("\n")
    for idx, line in enumerate(lines):
        # Real fence-line detection (only at start of stripped line, not anywhere)
        opener_on_line = _is_fence_open_line(line)

        # A fence block that fits a chunk alone must not be entered near the
        # cap — that forces a close/reopen split inside the block.
        if opener_on_line is not None and fence_opener is None and buf:
            block_len = len(line) + 1
            for look in lines[idx + 1:]:
                block_len += len(look) + 1
                if _is_fence_open_line(look) is not None \
                        and _closes_fence(look.strip(), opener_on_line):
                    break
            else:
                block_len = None  # unterminated fence — no lookahead call
            if block_len is not None and block_len <= max_len \
                    and buf_len + block_len > max_len:
                chunk = flush()
                if chunk is not None:
                    yield chunk
        # If we're outside a fence and this line is a fence-open, treat as opening.
        # If we're inside a fence and this line matches the fence-token kind,
        # treat as closing (we don't require exact length match for close).

        line_overhead = len(line) + 1  # +1 for newline
        # Reserve space for closing fence if we'd cut mid-fence
        reserve = (len(fence_closer(fence_opener)) + 1) if fence_opener else 0

        if buf_len + line_overhead + reserve > max_len and buf:
            # Outside a fence, prefer the last blank line in the lookback
            # window: the paragraph is what a reader loses to a mid-cut.
            cut = None
            if fence_opener is None:
                scanned = 0
                for j in range(len(buf) - 1, 0, -1):
                    scanned += len(buf[j]) + 1
                    # Lookback is a quarter of the budget so chunks stay near
                    # max_len (no half-empty sends) at ANY cap, not only 1900.
                    if scanned > max_len // 4:
                        break
                    if buf[j] == "":
                        cut = j
                        break
            tail_len = 0
            if cut is not None:
                tail_len = sum(len(t) + 1 for t in buf[cut + 1:])
                # A cut must leave room for this line, or it trades a clean
                # boundary for a chunk over max_len.
                if tail_len + line_overhead + reserve > max_len:
                    cut = None
            if cut is not None:
                head, tail = buf[:cut], buf[cut + 1:]
                yield "\n".join(head)
                buf = tail
                buf_len = tail_len
            else:
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
                if take <= 0:  # pragma: no cover
                    # Defensive: only reachable when the fence opener + reserve
                    # alone exceed max_len (max_len < ~opener_len+5). Real caps
                    # (1900 Discord / 4000 Slack) make this unreachable, and
                    # reopening the same opener can't shrink buf_len, so this
                    # guard cannot make progress anyway — kept verbatim from the
                    # original _chunk_for_discord for byte-for-byte parity.
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
            elif _closes_fence(opener_on_line, fence_opener):
                fence_opener = None
            # else: a NESTED opener line — an info-string fence (```python) or a
            # shorter run of the same char inside a longer fence. CommonMark keeps
            # the outer fence open, so we must NOT clear fence_opener here.

    chunk = flush()
    if chunk is not None:
        yield chunk


def chunk_plain_text(text: str, max_len: int) -> list[str]:
    """Split for a plain-text transport. Contract: ``"".join(result) == text``
    (nothing inserted, nothing dropped), every chunk is 1..max_len chars, and
    splits prefer the last newline in the window, then the last space, then a
    hard cut for unbreakable runs."""
    if max_len < 1:
        raise ValueError("max_len must be >= 1")
    chunks: list[str] = []
    rest = text
    while len(rest) > max_len:
        window = rest[:max_len]
        cut = window.rfind("\n")
        if cut <= 0:
            cut = window.rfind(" ")
        cut = max_len if cut <= 0 else cut + 1   # boundary char stays left
        chunks.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        chunks.append(rest)
    return chunks


def fits_one_message(text: str, max_len: int = 1900) -> bool:
    """True when chunk_message would deliver `text` as ONE message.

    The compose-time half of the delivery cap: gate a body on this before
    writing it, instead of learning from the delivered thread that it split.
    """
    gen = chunk_message(text, max_len)
    if next(gen, None) is None:
        return True
    return next(gen, None) is None
