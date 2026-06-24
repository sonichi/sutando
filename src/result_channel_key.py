"""Per-channel pull path for task-result files in `results/`.

REGULAR task results stay at ``results/task-{id}.txt`` — the default. The
existing consumers (discord-bridge / telegram-bridge / slack-bridge /
task-bridge / agent-api) all key off that name (specific task_id or
``task-*`` glob) and are NOT modified by this scoping.

NEW namespace — ``results/<channel-key>.task-{id}.txt`` — is used ONLY
when a task result needs to reach a non-delegating pull consumer (today:
phone, plus any pull-side plugin surface). A ``.``-prefixed filename slides
past the existing consumers' patterns because none of their startswith / glob /
pending-id lookups match the channel-key prefix.

Twin of ``src/result-channel-key.ts`` — keep in sync if a TS writer changes.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

# Filename-safe alphabet. Any char outside it is collapsed to `-` so a
# stray channel id can never inject a path separator or a regex special.
_KEY_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")

# Scoped filename shape: `<channel-key>.task-{id}` (with or without .txt).
# The key must NOT contain `.` (so `task-foo.txt` itself never matches).
_SCOPED_RESULT_RE = re.compile(r"^([A-Za-z0-9_-]+)\.(task-.+)$")


def sanitize_key(raw: Optional[str]) -> str:
    """Collapse `raw` to the filename-safe key alphabet.

    Empty / falsy input → ``'unknown'`` sentinel so the produced filename
    always has a non-empty prefix and stays distinct from the legacy
    ``task-...`` form.
    """
    if not raw:
        return "unknown"
    cleaned = _KEY_SAFE_RE.sub("-", str(raw).strip())
    return cleaned or "unknown"


def result_filename(channel_key: str, task_id: str) -> str:
    """Return the scoped task-result filename for a given channel + task id."""
    return f"{sanitize_key(channel_key)}.{task_id}.txt"


def parse_result_filename(filename: str) -> Tuple[Optional[str], str]:
    """Split a `results/` filename into ``(channel_key, task_id)``.

    Scoped form ``<key>.task-{id}[.txt]`` → ``(<key>, task-{id})``.
    Anything else (legacy flat ``task-{id}.txt``, ``voice-...``,
    ``proactive-...``) → ``(None, base_name)``. The ``.txt`` suffix is
    optional on input.
    """
    name = filename[:-4] if filename.endswith(".txt") else filename
    m = _SCOPED_RESULT_RE.match(name)
    if m:
        return m.group(1), m.group(2)
    return None, name


def result_belongs_to(filename: str, channel_key: str) -> bool:
    """True iff a result ``filename`` is the scoped form claimed by ``channel_key``.

    Legacy flat ``task-{id}.txt`` files return False — they're owned by
    their delegating consumer (discord-bridge / task-bridge / etc), NOT by
    a per-channel scan.

    Requires an EXACT ``.txt`` suffix. Atomic-write temps like
    ``<key>.task-X.txt.tmp``, ``.sending``, ``.partial`` etc. must NOT
    match — reading/unlinking a writer's in-flight temp before the rename
    completes would inject a half-written body and orphan the rename
    target. The scan loops also gate on ``endswith('.txt')`` as belt-and-
    suspenders.
    """
    if not filename.endswith(".txt"):
        return False
    key, task_id = parse_result_filename(filename)
    if key is None:
        return False
    if not task_id.startswith("task-"):
        return False
    return key == sanitize_key(channel_key)


# ── Typed channel-key constructors ────────────────────────────────────────
# Per-consumer prefixes make key-namespace collisions impossible across
# future consumer types: a hypothetical new pull surface whose ID format
# happens to be pure digits (like a chat snowflake) cannot accidentally claim
# another consumer's prefixed filename. Writer and consumer MUST go through the
# same constructor so the keys agree. Keep symmetric with ``src/result-channel-key.ts``.


def phone_call_key(call_sid: Optional[str]) -> str:
    """Channel key for the phone-conversation consumer. Wraps a Twilio call SID."""
    return f"phone-{sanitize_key(call_sid)}"
