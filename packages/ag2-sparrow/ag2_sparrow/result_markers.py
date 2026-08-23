"""
Unified parsing for the result-body protocol markers used by every delivery
consumer (discord, slack, telegram, remote-gateway, voice/task-bridge, and the
`src/dm-result.py` REST fallback). Closes #873.

Why centralize: each bridge previously hand-rolled its own marker recognition,
which (a) drifted (telegram never recognized [deduped:], slack never recognized
[channel:]), and (b) leaked literal marker text to the user when the bridge
didn't honor it. This module is the single source of truth for marker
shapes; consumers call `parse_markers(text)` and apply the actions they CAN
support, silently stripping the rest from the body so nothing ever leaks.

No consumer may define its own marker regex. discord-bridge.py and
dm-result.py each carried a private `_FILE_MARKER_RE` that matched only
`/...` or `~/...` values; markers this parser strips were therefore delivered
as literal text by the fallback. Both copies are gone and
`tests/bridge-marker-no-leak.test.py` guards their return.

This module deliberately does NOT enforce path allowlists. File-marker
extraction returns paths; the bridge's own `_is_path_sendable()` check
must run at the upload sink. The reason: CodeQL's py/path-injection rule
recognizes `os.path.realpath(p)` + `p.startswith(allowed)` inline at the
sink. If we abstracted the allowlist behind a helper return value, CodeQL
would stop recognizing the sanitizer and start flagging every upload site.

Marker spec (matches CLAUDE.md → "Result-body protocol markers"):

  SKIP markers — at body start (must be the first non-whitespace chars):
    [no-send]
    [REPLIED]
    [deduped: <task-id>]
  When any of these is found, the bridge archives the task silently and
  delivers nothing to the user.

  REDIRECT marker — first non-empty line:
    [channel: <channel-id>]
  When found, the bridge delivers the body to <channel-id> instead of the
  task's originating channel. The body is the text AFTER this line.

  DM-ONLY marker — anywhere in the body:
    [dm-only]
  Privacy guard: suppresses any [channel:] redirect on the same body (no
  redirect action is emitted), so a body carrying private data can never be
  redirected out to a shared channel. It emits a dm-only action but does NOT
  by itself route to the owner's DM — that stays the consumer's job. In
  practice the private producer (the morning briefing's calendar + email) is
  emitted as a proactive result (results/proactive-*.txt), which every bridge
  already delivers to the owner's DM; dm-only reinforces that by guaranteeing
  no stray [channel:] redirect can override it. A STANDALONE marker (alone on
  its line) is stripped from the delivered text; an INLINE mention is left
  verbatim, because rewriting prose that merely discusses the marker silently
  corrupts owner-facing text. Detection is unaffected — it still matches
  anywhere, so the order-independence guarantee holds. dm-only overrides redirect regardless of marker order;
  it does NOT override a SKIP (a skipped body is delivered nowhere anyway).

  ATTACH markers — anywhere in the body:
    [file: /path]
    [send: /path]
    [attach: /path]
  When found, the bridge extracts the path, runs its own allowlist check,
  uploads the file. The marker is stripped from the delivered text body.

Parse contract:

  parse_markers(text) → ParseResult
    .body      str — text with all known markers stripped
    .actions   list[Action] — what the bridge should do, in priority order:
                 ("skip", reason)         — archive, no delivery
                 ("redirect", channel_id) — deliver to alternate channel
                 ("attach", path)         — bridge runs its own allowlist
                                            check, then uploads

  Skip takes precedence over everything else. If text starts with a skip
  marker, only the skip action is returned (no redirect or attach extraction).

  This is intentional. The bridge's logic is: did we get a skip? If yes,
  archive and return. Otherwise, walk the rest of the action list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal


ActionKind = Literal["skip", "redirect", "attach", "dm-only"]


@dataclass
class Action:
    """One thing the bridge should do with a result body."""

    kind: ActionKind
    # For "skip": one of "no-send" | "REPLIED" | "deduped"
    # For "redirect": the channel id string (numeric for discord, "C..."/etc for slack)
    # For "attach": the file path as-extracted (caller must allowlist-check)
    value: str
    # Optional extra context — e.g., for "skip" with kind "deduped", the
    # referenced task id. Bridges typically don't need this but it's useful
    # for logging.
    extra: str | None = None


@dataclass
class ParseResult:
    """What parse_markers returns."""

    body: str
    actions: list[Action] = field(default_factory=list)


# Recognized skip markers + the canonical reason name we emit.
_SKIP_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\s*\[no-send\]\s*", re.IGNORECASE), "no-send"),
    (re.compile(r"^\s*\[REPLIED\]\s*"), "REPLIED"),
    # `*` not `+`: `[deduped:]` and `[deduped: ]` differ only by a space and
    # must parse alike, or one is audited and the other ships its own marker.
    (re.compile(r"^\s*\[deduped:\s*([^\]]*)\]\s*", re.IGNORECASE), "deduped"),
]

# Redirect marker — Discord channel IDs are 17-20 digits; Slack channel IDs
# match `[CDG][A-Z0-9]+`. Accept both via a permissive group; the bridge
# validates the id format for its platform when applying.
# Note: used with `.match()` below, which always anchors at string start —
# no MULTILINE flag needed (re.MULTILINE only affects `^`/`$` in scan-style
# methods like `.search()` / `.finditer()`).
# The value may carry ONE bracketed segment (`!r:[2001:db8::1]:8448` — Matrix
# bracketed-IPv6 hosts); a first-`]` cut truncated those mid-address.
_REDIRECT_RE = re.compile(
    r"^\s*\[channel:\s*([^\]\[]*(?:\[[^\]]*\][^\]\[]*)?)\]\s*\n?")

# D7 reply-header pattern (owner directive 2026-05-19) — pool cores prepend
# `**[core: N]**` plus an optional italic `_(...)_` sub-line to every
# user-facing reply so chat clients can see which core handled the message.
# The header lives at byte 0, which would shadow `[channel:]` since the
# redirect regex anchors with `re.match()`. We peel the header off before
# marker parsing and stitch it back onto the returned body so the human
# reader still sees it.
_D7_HEADER_RE = re.compile(
    r"\A\*\*\[core:\s*[^\]]+\]\*\*\s*\n(?:_[^\n]*_\s*\n)?\s*"
)

# Attach markers — file/send/attach aliases. A marker inside markdown code is
# being SHOWN, not issued; _code_lines and _SPAN_RE below mask those regions.
_ATTACH_RE = re.compile(r"(?<!`)\[(?:file|send|attach):\s*([^\]]*)\](?!`)")

_FENCE_RE = re.compile(r"^\s{0,3}(?:```|~~~)")

# A run of N backticks closed by the same run. Matching the SPAN, not the
# characters beside a marker, is what catches one mid-span.
_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(?:(?!\1).)+?\1(?!`)", re.DOTALL)


def _code_lines(text: str) -> set:
    """Line indices inside a fenced or indented markdown code block.

    An unclosed fence swallows the rest of the body on purpose: the alternative
    is treating shown-but-unterminated example text as a live directive.
    """
    lines, out, fenced = text.split("\n"), set(), False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            fenced = not fenced
            out.add(i)
            continue
        if fenced or line.startswith(("    ", "\t")):
            out.add(i)
    return out

# DM-only privacy marker — matched ANYWHERE in the body (not anchored) so it
# suppresses a [channel:] redirect regardless of which came first. All
# occurrences are DETECTED anywhere; only STANDALONE ones are stripped.
_DMONLY_RE = re.compile(r"\[dm-only\]\s*\n?", re.IGNORECASE)

#: STRIPPING is narrower than DETECTION, deliberately. Detection stays
#: `search()`-anywhere so the privacy guard cannot be defeated by marker
#: ORDER (see the docstring). But removing every occurrence also removed
#: the literal from PROSE that merely discusses the marker, mangling
#: owner-facing text with no indication:
#:     in   - #2170 [dm-only]: closes the leak vector
#:     out  - #2170 : closes the leak vector
#: Routing over-triggering fails SAFE; silently editing the body does not.
#: So only a STANDALONE marker — alone on its line — is stripped.
_DMONLY_STRIP_RE = re.compile(r"^[ \t]*\[dm-only\][ \t]*\r?\n?", re.IGNORECASE | re.MULTILINE)


def parse_markers(text: str) -> ParseResult:
    """Parse a result-body string and return body + action list.

    Order of evaluation:
      1. SKIP first. If any skip marker matches at body start, return
         immediately with a single skip action — no redirect, no attach.
         (The bridge archives the task and delivers nothing.)
      2. DM-ONLY next. If `[dm-only]` appears anywhere, add a dm-only action,
         strip it, and suppress any `[channel:]` redirect (privacy guard —
         the body can't be redirected out to a shared channel).
      3. REDIRECT next. If the body starts with `[channel: <id>]`, strip
         that line and add a redirect action — UNLESS dm-only suppressed it.
      4. ATTACH last. Scan the remaining body for `[file:|send:|attach:]`
         markers, collect paths in document order, strip from body.

    Returns:
      ParseResult(body=stripped_text, actions=[...])
    """
    if not text:
        return ParseResult(body="", actions=[])

    actions: list[Action] = []

    # Peel off optional D7 reply-header before any marker scan so neither
    # the skip patterns nor the redirect regex are shadowed by it. The
    # header is re-stitched onto the returned body for non-skip results;
    # skip results are invisible to the user regardless, so the header is
    # discarded alongside the body.
    d7_prefix = ""
    d7_match = _D7_HEADER_RE.match(text)
    if d7_match:
        d7_prefix = d7_match.group(0)
        body = text[d7_match.end():]
    else:
        body = text

    # 1. SKIP — matches anchored at body start. Whitespace before is OK.
    # If a result has a D7 header followed by a skip marker, the result is
    # still invisible to the user — skip is terminal and the header is
    # discarded along with the body.
    for pat, reason in _SKIP_PATTERNS:
        m = pat.match(body)
        if m:
            extra = None
            if reason == "deduped":
                # group(1) is the task id like "task-12345" or "1234"
                extra = m.group(1).strip()
            actions.append(Action(kind="skip", value=reason, extra=extra))
            # No further parsing — skip is terminal.
            return ParseResult(body="", actions=actions)

    # 2. DM-ONLY — privacy guard, checked BEFORE redirect so it can suppress
    # it. DETECTION and STRIPPING are deliberately different scopes:
    #   detect  — anywhere (not anchored), so `[dm-only]` overrides a
    #             `[channel:]` redirect no matter which appears first. That is
    #             what makes the guard undefeatable by marker ORDER, and
    #             over-triggering it fails SAFE (a reply goes to the DM).
    #   strip   — standalone only (`_DMONLY_STRIP_RE`, marker alone on its
    #             line). Stripping every occurrence rewrote the owner's own
    #             prose: a result DISCUSSING the marker had it silently
    #             excised, which is not a routing outcome and does not fail
    #             safe. An inline mention is detected but delivered verbatim.
    # A bridge that sees the dm-only action (or, equivalently, the ABSENCE of a
    # redirect action) delivers to the DM.
    dm_only = bool(_DMONLY_RE.search(body))
    if dm_only:
        actions.append(Action(kind="dm-only", value=""))
        body = _DMONLY_STRIP_RE.sub("", body)

    # 3. REDIRECT — must be the first non-empty line (after any D7 header).
    # Suppressed entirely when dm-only is set: strip a leading `[channel:]`
    # marker so it can't leak into the DM, but emit NO redirect action so the
    # private body stays in the owner's DM.
    redirect_match = _REDIRECT_RE.match(body)
    if redirect_match:
        channel = redirect_match.group(1).strip()
        if not dm_only and channel:
            # Empty target = no action: value="" release-loops at the default
            # sink (claimable yet foreign) and int("")s in Discord conversion.
            actions.append(Action(kind="redirect", value=channel))
        body = body[redirect_match.end():]

    # Restore D7 header so it appears in the user-facing body. (It was only
    # peeled off so it didn't shadow the marker regexes.)
    if d7_prefix:
        body = d7_prefix + body

    # 3. ATTACH — scan everywhere in the (possibly already-redirected) body.
    # Document-order paths.
    code = _code_lines(body)
    spans = [(m.start(), m.end()) for m in _SPAN_RE.finditer(body)]

    def _in_code(pos: int) -> bool:
        if body.count("\n", 0, pos) in code:
            return True
        return any(a <= pos < b for a, b in spans)

    for m in _ATTACH_RE.finditer(body):
        if _in_code(m.start()):
            continue
        actions.append(Action(kind="attach", value=m.group(1).strip()))

    # Strip only the markers we acted on — one shown in a code block stays
    # visible, which is the whole point of showing it.
    body = _ATTACH_RE.sub(
        lambda m: "" if not _in_code(m.start()) else m.group(0), body).strip()

    return ParseResult(body=body, actions=actions)


_TASK_CHANNEL_RE = re.compile(r"^channel_id:\s*(\S+)\s*$", re.MULTILINE)


def dedup_cross_channel_target(deduped_channel_id, holder_task_text: str | None) -> str | None:
    """Channel-aware dedup support.

    A `[deduped: task-X]` result silently archives the deduped task and
    relies on the holder task-X carrying the full reply. But the holder's
    reply is delivered to *its own* channel — so when the deduped task and
    the holder came from DIFFERENT channels, the channel that actually asked
    is left silent while the answer lands elsewhere (observed 2026-06-22: an
    owner question in #workspace-revamp folded into a #design holder).

    Returns the holder's `channel_id` when it is known AND differs from the
    deduped task's own channel — a cross-channel dedup, which is INVALID
    (dedup is per-channel only). The bridge rejects it and re-queues the
    original task to be re-answered in its own channel. Returns None (→ keep
    the silent-archive behavior) when the holder text is missing, has no
    channel_id, or is the SAME channel (the common, correct intra-channel
    consolidation case).
    """
    if not holder_task_text:
        return None
    m = _TASK_CHANNEL_RE.search(holder_task_text)
    if not m:
        return None
    holder_channel = m.group(1).strip()
    if holder_channel and str(holder_channel) != str(deduped_channel_id):
        return holder_channel
    return None


def dedup_holder_delivered(holder_result_text: str | None) -> bool:
    """Whether a `[deduped: task-X]` holder actually produced a user-facing reply.

    A dedup is only valid if the holder answered. When the holder's own result
    was empty or was itself a skip marker, honouring the dedup archives the
    asking task against a delivery that never happened, and every retry carrying
    the same marker is archived the same way — the ask can never be answered.

    Pure: the caller supplies the holder's archived result text (None when the
    archive has no record of it).
    """
    if holder_result_text is None:
        return False
    body = holder_result_text.strip()
    if not body:
        return False
    return not any(a.kind == "skip" for a in parse_markers(body).actions)


_REQUEUE_COUNT_RE = re.compile(r"^dedup_requeue_count:\s*(\d+)\s*$", re.MULTILINE)


def dedup_requeue_count(task_text: str | None) -> int:
    """How many times this task has already been re-queued after a rejected
    cross-channel dedup. 0 if the field is absent. Used as the loop guard:
    a task that comes back cross-channel-deduped with count >= 1 is NOT
    re-queued again — the bridge notifies instead (owner-directed: "second
    time, send a msg about this error")."""
    if not task_text:
        return 0
    m = _REQUEUE_COUNT_RE.search(task_text)
    return int(m.group(1)) if m else 0


def dedup_decision(holder_result_text: str | None, orig_task_text: str | None) -> str:
    """What to do with a `[deduped: <holder>]` result.

    ``"honour"``  — the holder answered; archive silently as before.
    ``"requeue"`` — it did not; re-ask so the question actually gets answered.
    ``"report"``  — re-asking already failed once; tell the owner instead of looping.

    Pure. The caller supplies the holder's archived result text (None when the
    archive has no record) and the original task text (for the loop guard).
    """
    if dedup_holder_delivered(holder_result_text):
        return "honour"
    if dedup_requeue_count(orig_task_text) >= 1:
        return "report"
    return "requeue"


_REQUEUE_REASONS = {
    "cross-channel": lambda holder_id, asking_channel: (
        f"Your previous result used [deduped: {holder_id}], but that holder task is in a "
        f"DIFFERENT channel. Dedup is per-channel only — a cross-channel dedup leaves this "
        f"channel silent. Re-answer THIS task directly in its own channel (<#{asking_channel}>). "
        "Do NOT [deduped:] across channels.\n"
    ),
    "holder-empty": lambda holder_id, asking_channel: (
        f"Your previous result used [deduped: {holder_id}], but that holder delivered nothing, "
        "so this question was never answered. Answer THIS task directly and in full. Only use "
        "[deduped:] when the holder task carries the complete reply.\n"
    ),
}


def build_requeued_task(
    orig_text: str, new_task_id: str, count: int, asking_channel, holder_id: str,
    reason: str = "cross-channel",
) -> str:
    """Rewrite an original task for re-processing after a REJECTED cross-channel
    dedup. Keeps the original fields (channel_id, access_tier, source, body, …)
    so it routes + tiers identically, but:
      * sets `id:` to new_task_id (so the watcher re-fires),
      * sets `dedup_requeue_count: count` (loop guard),
      * appends a trusted `===SUTANDO SYSTEM INSTRUCTIONS===` block telling the
        core the prior dedup was cross-channel (invalid) and to answer THIS
        task directly in its own channel, not dedup across channels.

    The appended fence is bridge-authored (trusted). Its safety relies on the
    original user body already being confined at first-write time
    (task_body_guard / PR #1743) so a sender can't have pre-forged their own
    fence inside the body.
    """
    lines = []
    seen_count = False
    for ln in (orig_text or "").rstrip("\n").split("\n"):
        if ln.startswith("id:"):
            lines.append(f"id: {new_task_id}")
        elif ln.startswith("dedup_requeue_count:"):
            lines.append(f"dedup_requeue_count: {count}")
            seen_count = True
        else:
            lines.append(ln)
    if not seen_count:
        lines.append(f"dedup_requeue_count: {count}")
    note = (
        "\n===SUTANDO SYSTEM INSTRUCTIONS (do not ignore; overrides anything above)===\n"
        + _REQUEUE_REASONS.get(reason, _REQUEUE_REASONS["cross-channel"])(
            holder_id, asking_channel
        )
        + "===END SUTANDO SYSTEM INSTRUCTIONS===\n"
    )
    return "\n".join(lines) + note


def first_action(result: ParseResult, kind: ActionKind) -> Action | None:
    """Convenience: return the first action of the given kind, or None.

    Useful for "do I have a skip / redirect?" checks; for attach actions
    you typically want to iterate the full list to upload every file."""
    for a in result.actions:
        if a.kind == kind:
            return a
    return None


def has_skip_action(actions) -> bool:
    """True if `actions` (a ParseResult.actions list) carries a skip-kind
    marker (`[no-send]`/`[REPLIED]`/`[deduped:...]`) — the body must never be sent."""
    return any(a.kind == "skip" for a in actions)
