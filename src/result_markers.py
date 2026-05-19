"""
Unified parsing for the result-body protocol markers used by every bridge
(discord, slack, telegram, voice/task-bridge). Closes #873.

Why centralize: each bridge previously hand-rolled its own marker recognition,
which (a) drifted (telegram never recognized [deduped:], slack never recognized
[channel:]), and (b) leaked literal marker text to the user when the bridge
didn't honor it. This module is the single source of truth for marker
shapes; bridges call `parse_markers(text)` and apply the actions they CAN
support, silently stripping the rest from the body so nothing ever leaks.

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


ActionKind = Literal["skip", "redirect", "attach"]


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
    (re.compile(r"^\s*\[deduped:\s*([^\]]+)\]\s*", re.IGNORECASE), "deduped"),
]

# Redirect marker — Discord channel IDs are 17-20 digits; Slack channel IDs
# match `[CDG][A-Z0-9]+`. Accept both via a permissive group; the bridge
# validates the id format for its platform when applying.
# Note: used with `.match()` below, which always anchors at string start —
# no MULTILINE flag needed (re.MULTILINE only affects `^`/`$` in scan-style
# methods like `.search()` / `.finditer()`).
_REDIRECT_RE = re.compile(r"^\s*\[channel:\s*([^\]]+)\]\s*\n?")

# Attach markers — file/send/attach are aliases.
_ATTACH_RE = re.compile(r"\[(?:file|send|attach):\s*([^\]]+)\]")


def parse_markers(text: str) -> ParseResult:
    """Parse a result-body string and return body + action list.

    Order of evaluation:
      1. SKIP first. If any skip marker matches at body start, return
         immediately with a single skip action — no redirect, no attach.
         (The bridge archives the task and delivers nothing.)
      2. REDIRECT next. If the body starts with `[channel: <id>]`, strip
         that line and add a redirect action.
      3. ATTACH last. Scan the remaining body for `[file:|send:|attach:]`
         markers, collect paths in document order, strip from body.

    Returns:
      ParseResult(body=stripped_text, actions=[...])
    """
    if not text:
        return ParseResult(body="", actions=[])

    actions: list[Action] = []

    # 1. SKIP — matches anchored at body start. Whitespace before is OK.
    for pat, reason in _SKIP_PATTERNS:
        m = pat.match(text)
        if m:
            extra = None
            if reason == "deduped":
                # group(1) is the task id like "task-12345" or "1234"
                extra = m.group(1).strip()
            actions.append(Action(kind="skip", value=reason, extra=extra))
            # No further parsing — skip is terminal.
            return ParseResult(body="", actions=actions)

    body = text

    # 2. REDIRECT — must be the first non-empty line. The regex uses
    # MULTILINE so ^ anchors at any newline, but we restrict to the first
    # such match.
    redirect_match = _REDIRECT_RE.match(body)
    if redirect_match:
        channel = redirect_match.group(1).strip()
        actions.append(Action(kind="redirect", value=channel))
        body = body[redirect_match.end():]

    # 3. ATTACH — scan everywhere in the (possibly already-redirected) body.
    # Document-order paths.
    for m in _ATTACH_RE.finditer(body):
        path = m.group(1).strip()
        actions.append(Action(kind="attach", value=path))

    # Strip the attach markers from body so the user never sees them.
    body = _ATTACH_RE.sub("", body).strip()

    return ParseResult(body=body, actions=actions)


def first_action(result: ParseResult, kind: ActionKind) -> Action | None:
    """Convenience: return the first action of the given kind, or None.

    Useful for "do I have a skip / redirect?" checks; for attach actions
    you typically want to iterate the full list to upload every file."""
    for a in result.actions:
        if a.kind == kind:
            return a
    return None
