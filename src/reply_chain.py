"""Reply-context formatting (pure) — companion to ``discord-bridge.py``.

When the owner replies to a message, the bridge inlines the referenced
message(s) into the task file so the core agent sees WHAT is being replied to
without having to re-fetch. This module owns the *formatting* of that block so
the truth table is unit-tested directly (same shape as ``discord_addressee.py``
/ ``result_markers.py``); the bridge does the async Discord fetches and hands
the resolved primitives in here.

Why this exists (Chi 2026-07-25): the old bridge truncated the referenced
message to a 400-char single-level snippet (``ref_content[:400]``). In a deep
reply thread that silently dropped the ROOT question — the recurring
"you lost the original question" failure. The fix is structural: inline the
**full** replied-to message, and walk the reply chain to the root so the whole
ancestor context lands in the task file as DATA (not something the agent must
remember to re-fetch via ``parent_message_id``). An id is only a fetch-handle;
inlining the walked chain is what makes the context unavoidable.

Guards keep a pathological multi-KB message or a very deep chain from bloating
the task file: each message is capped at ``max_msg_chars`` and the whole block
at ``max_total_chars``, with an explicit ``…[+N chars]`` marker so truncation
is never silent (the exact failure mode being fixed).
"""

from __future__ import annotations

from typing import Sequence

# Defaults: far above a normal reply, so the guard only ever fires on a
# genuinely pathological message/chain — never on ordinary owner context.
DEFAULT_MSG_MAX_CHARS = 2000
DEFAULT_TOTAL_MAX_CHARS = 6000
DEFAULT_MAX_DEPTH = 8


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
    precise handles to re-fetch any ancestor whose inlined content was
    size-clipped, edited, or dropped past the depth/size guard — the inlined
    text is the guarantee, these ids are the reconstruction handles.

    Returns ``""`` when there is no real chain to reconstruct (fewer than two
    ids): a single id is already covered by ``parent_message_id``, so emitting
    it here would be redundant noise.
    """
    clean = [str(i) for i in ids if i]
    if len(clean) < 2:
        return ""
    return "reply_chain_ids: " + ",".join(reversed(clean)) + "\n"


def format_reply_chain(
    chain: Sequence[dict],
    *,
    max_msg_chars: int = DEFAULT_MSG_MAX_CHARS,
    max_total_chars: int = DEFAULT_TOTAL_MAX_CHARS,
) -> str:
    """Format an inline reply-context block from a resolved ancestor chain.

    ``chain`` is ordered **immediate-parent-first** (index 0 = the message
    directly replied to, higher indices = older ancestors toward the root).
    Each entry is a dict with keys ``author`` (str), ``ts`` (preformatted
    timestamp str), ``content`` (str). Entries with empty content after
    stripping are skipped (e.g. an attachment-only parent) but do not break the
    walk.

    Returns the block to append to the task body (leading ``\\n\\n`` included),
    or ``""`` when there is nothing worth inlining. Backward-compatible shape:
    a single-level reply renders exactly ``[Replying to <author> (<ts>): <content>]``
    (no truncation), matching the pre-fix format minus the 400-char cap.
    """
    cleaned = []
    for entry in chain:
        content = _clip(str(entry.get("content", "")), max_msg_chars)
        if not content:
            continue
        cleaned.append(
            {
                "author": str(entry.get("author", "unknown")),
                "ts": str(entry.get("ts", "")),
                "content": content,
            }
        )
    if not cleaned:
        return ""

    # Enforce the total-size guard from the immediate parent outward: the
    # nearest context is the most relevant, so if the budget is exhausted we
    # drop the OLDEST ancestors (tail), never the immediate parent.
    kept = []
    total = 0
    dropped_ancestors = 0
    for i, entry in enumerate(cleaned):
        cost = len(entry["content"])
        if kept and total + cost > max_total_chars:
            dropped_ancestors = len(cleaned) - i
            break
        kept.append(entry)
        total += cost

    if len(kept) == 1 and dropped_ancestors == 0:
        e = kept[0]
        return f"\n\n[Replying to {e['author']} ({e['ts']}): {e['content']}]"

    # Multi-level: render root-first (oldest → immediate parent) so the agent
    # reads the thread in chronological order and the ROOT question leads.
    lines = ["\n\n[Reply chain (root first → the message you replied to last):"]
    ordered = list(reversed(kept))
    if dropped_ancestors:
        lines.append(f"  …[{dropped_ancestors} older ancestor(s) omitted for size]")
    for e in ordered:
        lines.append(f"  • {e['author']} ({e['ts']}): {e['content']}")
    lines.append("]")
    return "\n".join(lines)
