"""Reply-context formatting (pure) — companion to ``discord-bridge.py``.

When the owner replies to a message, the bridge inlines the referenced message
into the task file so the core agent sees WHAT is being replied to without
having to re-fetch. This module owns the *formatting* of that block so the
truth table is unit-tested directly (same shape as ``discord_addressee.py`` /
``result_markers.py``); the bridge does the async Discord fetches and hands the
resolved primitives in here.

Why this exists (Chi 2026-07-25): the old bridge truncated the referenced
message to a 400-char snippet (``ref_content[:400]``), which silently dropped
context — including, in a deep thread, the root question ("you lost the original
question").

Lean design (Chi 2026-07-25, after weighing the alternative): inline the **full
immediate parent** (no truncation, size-clipped only for a pathological
multi-KB body), and emit the walked ancestor **ids** separately
(``reply_chain_ids``) so any deeper ancestor can be fetched precisely on demand.
We do NOT inline the whole chain's content: injecting the entire thread into
every reply's task file bloats it and can *bury* the actual message under a wall
of mostly-irrelevant quoted ancestors — a different failure than the one being
fixed. The full immediate parent is the direct referent (the 95% case); the id
spine is the reconstruction handle for the rest.
"""

from __future__ import annotations

from typing import Sequence

# Far above a normal reply, so the guard only fires on a genuinely pathological
# immediate-parent body — never on ordinary owner context.
DEFAULT_MSG_MAX_CHARS = 2000


def _clip(text: str, max_chars: int) -> str:
    """Clip a single message body, appending a visible ``…[+N chars]`` marker.

    Never silent: the marker tells the agent content was cut and how much, so
    it can re-fetch by id if the tail actually matters.
    """
    text = (text or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    dropped = len(text) - max_chars
    return text[:max_chars].rstrip() + f" …[+{dropped} chars, reply id to re-fetch]"


def format_reply_chain_ids(ids: Sequence) -> str:
    """Format the ``reply_chain_ids:`` task-file metadata line (Chi 2026-07-25:
    "list of msg ids for thread reconstruction").

    ``ids`` is the walked ancestor chain **immediate-parent-first** (the same
    order the walk produces). The emitted line is **root-first** so it reads as
    the thread's chronological id spine: ``reply_chain_ids: <root>,…,<parent>``.
    Combined with ``source_message_id`` (the current message) this gives the
    precise handles to re-fetch any ancestor beyond the inlined immediate parent
    — the deeper thread is referenced, not inlined.

    Returns ``""`` when there is no real chain to reconstruct (fewer than two
    ids): a single id is already covered by ``parent_message_id``, so emitting
    it here would be redundant noise.
    """
    clean = [str(i) for i in ids if i]
    if len(clean) < 2:
        return ""
    return "reply_chain_ids: " + ",".join(reversed(clean)) + "\n"


def format_reply_chain_truncation(reached_root: bool, oldest_walked_id) -> str:
    """Visible marker for when the id walk stopped BEFORE the thread's root.

    The bridge walks the ancestor chain toward the root within a bounded depth
    (``REPLY_CHAIN_IDS_MAX_DEPTH``) so ``reply_chain_ids`` normally reaches the
    root even for deep threads. On a pathologically deep (or unfetchable) thread
    the walk stops before the root — previously that dropped the oldest
    ancestors (incl. the root question) *silently*, with no inline content and
    no id handle. This marker makes the truncation VISIBLE (same never-silent
    principle as ``_clip``) so the agent knows older context exists and can
    reply to an older message directly to pull it.

    ``reached_root`` True → the spine is complete → no marker. Otherwise emit a
    one-line marker anchored on the oldest id we DID capture.
    """
    if reached_root or not oldest_walked_id:
        return ""
    return (
        f"\n[reply chain truncated: ancestors older than id {oldest_walked_id} "
        "were not walked — reply to an older message directly to pull it]"
    )


def format_reply_chain(
    chain: Sequence[dict],
    *,
    max_msg_chars: int = DEFAULT_MSG_MAX_CHARS,
) -> str:
    """Format the inline reply-context for the **immediate parent only**.

    ``chain`` is the walked ancestor list, **immediate-parent-first** (index 0 =
    the message directly replied to). Only ``chain[0]`` is inlined — the full
    content, with no truncation (size-clipped only for a pathological body).
    Deeper ancestors are intentionally NOT inlined; they are referenced by
    ``reply_chain_ids`` and fetched on demand. Each entry is a dict with keys
    ``author`` (str), ``ts`` (preformatted timestamp str), ``content`` (str).

    Returns ``\\n\\n[Replying to <author> (<ts>): <content>]`` (leading blank
    line included) — the same shape as before, minus the 400-char cap — or
    ``""`` when the immediate parent has no text to inline (e.g. an
    attachment-only parent, whose attachment is handled separately and whose id
    still appears in ``reply_chain_ids``).
    """
    if not chain:
        return ""
    parent = chain[0]
    content = _clip(str(parent.get("content", "")), max_msg_chars)
    if not content:
        return ""
    author = str(parent.get("author", "unknown"))
    ts = str(parent.get("ts", ""))
    return f"\n\n[Replying to {author} ({ts}): {content}]"
