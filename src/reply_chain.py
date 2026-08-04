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


def should_fetch_reply_context(has_reference: bool, has_message_id: bool,
                               is_forward: bool) -> bool:
    """Whether the bridge should fetch the referenced message for reply context.

    A FORWARD sets ``message.reference`` too, but the referenced message lives in
    the SOURCE channel, so ``channel.fetch_message(reference.message_id)`` on the
    receiving channel is guaranteed to 404 — one wasted network round trip and an
    alarming ``[reply-context] fetch failed: 404 ... Unknown Message`` on every
    forward the owner sends.

    The forward's own body is NOT reply context and is not lost by skipping this:
    it lives in ``message.message_snapshots`` and is extracted by the dedicated
    forward handler earlier in the same path. Reply context answers "which
    earlier message in THIS channel is being replied to", which a forward has no
    answer to.

    Split out as a predicate because the bridge is not unit-importable, so the
    only way to prove the activated path is gated is to test the condition it
    branches on (john-the-dev + bassilkhilo-ag2 on #2633: the first version
    re-keyed the header and left this fetch executing, so the reported 404 was
    still live).
    """
    return bool(has_reference and has_message_id and not is_forward)


def format_parent_reference(message_id, is_forward: bool, source_channel_id=None) -> str:
    """Format the reference header for a message that carries a ``reference``.

    Discord sets ``message.reference`` for two different features, and only one
    of them is a reply:

      * a **reply** — the referenced message is in THIS channel, and
        ``parent_message_id`` is a handle the agent can re-fetch. That is what
        the key has always meant.
      * a **forward** — the reference points at the original in its **source**
        channel. Emitting that under ``parent_message_id`` claimed a
        relationship that does not exist and produced an id that cannot be
        resolved from the channel the task was written in. Observed
        2026-08-04: a forward into ``#echo`` recorded
        ``parent_message_id: 1534196303205105849``; that id 404s in ``#echo``
        and resolves only in the channel it was forwarded FROM, so the bridge
        also logged ``[reply-context] fetch failed: 404 Unknown Message``.

    So a forward is re-keyed to ``forwarded_from_message_id`` and, when known,
    ``forwarded_from_channel_id`` — the provenance is kept (Chi chose re-key
    over dropping it) and made resolvable, since an id without its channel is
    not a handle a consumer can act on.

    ``is_forward`` is decided by the CALLER from ``message.message_snapshots``,
    the payload a forward carries. That is deliberately not inferred from
    ``reference.type`` here: the snapshot IS the forward, whereas the reference
    type is a second-hand signal for the same fact.

    Returns ``""`` when there is no id to emit.
    """
    if not message_id:
        return ""
    if not is_forward:
        return f"parent_message_id: {message_id}\n"
    line = f"forwarded_from_message_id: {message_id}\n"
    if source_channel_id:
        line += f"forwarded_from_channel_id: {source_channel_id}\n"
    return line


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


async def walk_reply_chain(
    seed,
    fetch_message,
    *,
    max_content_depth: int,
    max_ids_depth: int,
    strip_mention: str = "",
):
    """Walk a reply chain from ``seed`` toward the thread root.

    Extracted from ``discord-bridge.py`` (PR #2310 review 2) so the two failure
    modes reviewers flagged are reachable by a test. Inside the bridge handler
    this walk sat behind ``# pragma: no cover`` — meaning the depth cap and the
    unfetchable-ancestor path, the exact cases where context is silently lost,
    were the only ones never exercised. Assertions could not fix that; the walk
    had to become callable.

    ``fetch_message`` is an async ``id -> message | None`` (the bridge passes
    ``channel.fetch_message``); raising is treated the same as returning None,
    because an unfetchable ancestor and a failing fetch are the same event to
    the caller.

    Returns ``(chain, chain_ids, reached_root)``:
      * ``chain``      — content dicts, immediate-parent-first, capped at
        ``max_content_depth``. Only ``chain[0]`` is inlined by the bridge.
      * ``chain_ids``  — id spine, immediate-parent-first, walked past the
        content cap to ``max_ids_depth`` so the spine can still reach the root
        question when the content cap has already stopped collecting bodies.
      * ``reached_root`` — True ONLY on a clean stop (an ancestor with no
        further reference). Depth exhaustion and an unfetchable ancestor both
        leave it False, so the caller emits the truncation marker. Defaulting
        this to False on any non-clean stop is deliberate: an unmarked partial
        chain is the original bug (a reply whose root question vanished with no
        visible sign), so "not proven complete" must never render as complete.
    """
    chain: list = []
    chain_ids: list = []
    cur = seed
    depth = 0
    reached_root = False

    while cur is not None and depth < max_ids_depth:
        cid = getattr(cur, "id", None)
        if depth < max_content_depth:
            content = (getattr(cur, "content", "") or "").strip()
            if strip_mention:
                content = content.replace(strip_mention, "")
            created = getattr(cur, "created_at", None)
            chain.append(
                {
                    "id": cid,
                    "author": str(getattr(cur, "author", "")),
                    "ts": created.strftime("%Y-%m-%d %H:%M") if created else "",
                    "content": content,
                }
            )
        if cid is not None:
            chain_ids.append(cid)

        nref = getattr(cur, "reference", None)
        if not (nref and getattr(nref, "message_id", None)):
            reached_root = True
            break

        nxt = getattr(nref, "resolved", None)
        if nxt is None:
            try:
                nxt = await fetch_message(nref.message_id)
            except Exception:
                nxt = None
        if nxt is None:
            # Unfetchable ancestor: older context exists but is unreachable.
            # Leave reached_root False so the marker names the oldest id we DID
            # reach, giving the agent a precise fetch handle instead of silence.
            break
        cur = nxt
        depth += 1

    return chain, chain_ids, reached_root
