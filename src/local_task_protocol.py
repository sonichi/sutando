"""
Local Task Protocol — read-side reference implementation.

Interaction-planes refactor, step 3 (read side). The durable local execution
boundary: this module names the schema of `tasks/*.txt` files and provides the
canonical pure functions for reading them. It consolidates parsing that today
is hand-rolled per consumer (task_priority.py, task-bridge's `_isVoiceTask`,
each bridge's header scan) so new code imports ONE definition. Writers are
deliberately untouched in this phase — the write-side switch happens per
bridge, later, with byte-identical golden tests.

The result-body half of the protocol already lives in `src/result_markers.py`
(#873) and stays there; this module is the TASK-file half plus shared schema
constants.

R1 invariant (design doc §6): everything here is stdlib-only, no network, no
daemon, no lock service — the last-resort producers (health-check --emit-task,
Sutando.app context-drop) must keep working under total daemon death, and the
future write side of this module inherits that constraint.

## The two header shapes (and why there are two parsers)

Producers serialize headers in two shapes with three distinct trust
mechanisms — established by surveying 3.4k archived task files plus every
live writer (2026-07-06):

**task-last** (task-bridge.ts chat/voice/context-drop, agent-api `/task`):
every header precedes the `task:` line; the body after it is untrusted
multi-line content. Ordering IS the trust mechanism — consumers stop
header-scanning at the first `task:` line, or a body containing
`\naccess_tier: owner` forges headers (the PR #982 injection, re-flagged in
#1035). Use `parse_task_headers()`.

**task-mid** (all Python bridges: discord/slack/telegram/gateway, plus
health-check and github-webhook): `task:` lands mid-file and real headers
(source, channel_id, …) follow it. Safe only because the writer neutralizes
the body by one of two mechanisms: newline-stripping every value
(`_one_line` in the gateway, the sanitizer in github-webhook) or defanging
header-like body lines with a ZWSP prefix (`task_body_guard.
confine_user_content` in discord/telegram). Use
`parse_task_headers_trusted()` — full scan, LAST occurrence wins, which the
gateway's tier defense depends on (its locally-decided `access_tier` is
written last to beat anything the remote side claimed).

Pick the parser by writer, never by sniffing content. The safe default for
a file of unknown provenance is `parse_task_headers()` — it under-reads
task-mid files rather than over-trusting task-last ones. That under-read is
real: `parse_priority_from_text` (stop-at-`task:`) has never seen a
bridge-written `priority:` field. The write-side phase converges writers on
task-last; until then both parsers exist and are named for their trust model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# ── Schema constants ─────────────────────────────────────────────────────────

# Interaction-plane vocabulary (step 1, PR #1953). Producers stamp exactly one
# of these next to `source:`; the remote-gateway bridge whitelists inbound
# values against this set (unknown → "message").
INTERACTION_TYPES = frozenset({
    "message", "realtime_audio", "realtime_video",
    "tool_initiated", "system_event", "self_reflective",
})

# Task priority enum (src/task_priority.py is the behavior owner; these are
# the schema names). Consumer semantics: highest first, mtime FIFO tiebreak.
PRIORITIES = ("urgent", "normal", "low")

# Access tiers (CLAUDE.md access-control sections). `owner` is full
# processing; team/other are sandboxed. A missing header reads as owner for
# legacy local files — that default belongs to consumers, not this module.
ACCESS_TIERS = ("owner", "team", "other")

# Header keys observed in the real archive corpus (3,401 files, 2026-07-06),
# most-common first. Descriptive, not enforced — producers may add keys, and
# readers must ignore unknown ones (that additivity is what let step 1 ship).
KNOWN_HEADER_KEYS = (
    "id", "timestamp", "task", "source", "access_tier", "user_id",
    "channel_id", "priority", "interaction_type", "source_message_id",
    "channel_name", "guild_name", "attempts", "sender_name", "room_name",
    "parent_message_id", "reminder", "author_name", "author_id", "chat_id",
    "reply_to_event", "reply_to_me", "callSid", "caller", "from", "call_sid",
    "hint", "instructions", "transcript",
)

# Task-id shape: `task-<slug>` where slug is dash-separated [a-z0-9] segments
# (task-1783..., task-chat-1783..., task-phone-..., task-summary-...,
# task-gh-..., task-health-...). Mirrors the gateway bridge's `_valid_tid`
# defense: ids become filenames, so the charset is the path-traversal guard.
TASK_ID_RE = re.compile(r"^task-[A-Za-z0-9][A-Za-z0-9-]{0,120}$")


def valid_task_id(tid: str) -> bool:
    """True iff `tid` is a well-formed task id, safe to embed in a filename.

    Rejects path separators, dots, whitespace, and empty/oversized ids — the
    id is used as `tasks/<tid>.txt` and `results/<tid>.txt`, so this is the
    single traversal gate for readers that accept ids from message content
    (e.g. `[deduped: <tid>]` holders).
    """
    return bool(TASK_ID_RE.match(tid or ""))


# ── Header parsing ───────────────────────────────────────────────────────────

_HEADER_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):[ \t]?(.*)$")


@dataclass
class TaskHeaders:
    """Parsed view of a task file. `headers` preserves the parser's trust
    rule (see module docstring).

    `body` semantics differ BY PARSER — this is deliberate and load-bearing:
    - `parse_task_headers` (task-last): the full work item — the `task:`
      line's content plus every line after it.
    - `parse_task_headers_trusted` / `_lenient` (task-mid): the SCALAR value
      of the first `task:` line ONLY. In task-mid files the lines after
      `task:` are a mix of real headers and continuation content (health
      bullets, phone `hint:`/`transcript:` sections) that a header scan
      cannot losslessly split — so these parsers do not pretend to. A reader
      that needs the complete work item from a file of any shape must use
      `task_body()`, which never drops a line. (Codex review on PR #1954:
      the earlier draft looked like it returned the work item and silently
      lost health-check bullets.)
    """
    headers: dict = field(default_factory=dict)
    body: str = ""

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.headers.get(key, default)


def task_body(text: str) -> str:
    """The complete work item of a task file, shape-independent: everything
    from the first `task:` line onward, verbatim, with only the `task:`
    prefix stripped from that first line. Never drops a line — for task-mid
    files this includes trailing headers, which is the honest trade: a reader
    that wants clean headers uses the parsers; a reader that wants the full
    work item (health bullets, phone transcript, meeting instructions) uses
    this and must tolerate header lines inside it."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("task:"):
            return "\n".join([line[len("task:"):].lstrip()] + lines[i + 1:])
    return ""


def parse_task_headers(text: str) -> TaskHeaders:
    """Parse a **task-last** file: collect `key: value` lines strictly BEFORE
    the first `task:` line; everything from `task:` onward is untrusted body.

    This is the safe default parser — the delimiter rule that keeps a
    user-supplied body from forging headers (PR #982 / #1035). First
    occurrence of a key wins (a duplicate later is more likely forged than
    corrective). The body includes the `task:` line's own content.
    """
    headers: dict = {}
    body_lines: list[str] = []
    in_body = False
    for line in text.split("\n"):
        if in_body:
            body_lines.append(line)
            continue
        if line.startswith("task:"):
            in_body = True
            body_lines.append(line[len("task:"):].lstrip())
            continue
        m = _HEADER_LINE_RE.match(line)
        if m:
            headers.setdefault(m.group(1), m.group(2))
        # Non-matching lines before task: are tolerated and skipped — real
        # archive files contain blank lines and free-text hint lines.
    return TaskHeaders(headers=headers, body="\n".join(body_lines))


def parse_task_headers_lenient(text: str) -> TaskHeaders:
    """Parse across ALL lines, FIRST occurrence of each key wins.

    The shape-union reader: producers' field order has changed across eras
    (May-2026 voice tasks were task-mid; today's are task-last), so consumers
    that must classify files of any age — e.g. discord-bridge's DM-fallback
    `source:` probe — need a scan that finds headers wherever that era's
    writer put them. First-wins resists trailing-body forgery when the real
    header exists; it does NOT protect a file that legitimately lacks the
    probed key (a body line can then supply it). That spoofability predates
    this module — hardening it means changing verdicts for historical shapes
    and is deliberately out of scope for the read-side refactor.
    """
    headers: dict = {}
    body = ""
    for line in text.split("\n"):
        if line.startswith("task:") and not body:
            body = line[len("task:"):].lstrip()
            continue
        m = _HEADER_LINE_RE.match(line)
        if m:
            headers.setdefault(m.group(1), m.group(2))
    return TaskHeaders(headers=headers, body=body)


def parse_task_headers_trusted(text: str) -> TaskHeaders:
    """Parse a **task-mid** file from a trusted writer (remote-gateway-bridge):
    scan ALL lines for `key: value`, LAST occurrence wins.

    Only valid when the writer is known to newline-strip every value — that
    property is what makes the full scan injection-free, and last-wins is
    load-bearing: the gateway writes its locally-decided `access_tier` last
    precisely so it beats anything the remote side claimed earlier in the
    file. Applying this parser to a task-last file would let the body forge
    headers; pick the parser by writer, not by content sniffing.
    """
    headers: dict = {}
    body = ""
    for line in text.split("\n"):
        if line.startswith("task:") and not body:
            body = line[len("task:"):].lstrip()
            continue
        m = _HEADER_LINE_RE.match(line)
        if m:
            headers[m.group(1)] = m.group(2)  # last occurrence wins
    return TaskHeaders(headers=headers, body=body)


# ── Archive rules ────────────────────────────────────────────────────────────

_MONTH_DIR_RE = re.compile(r"^\d{4}-\d{2}$")


def archive_month_dir(base: Path, iso_timestamp: str) -> Path:
    """Month-partitioned archive dir for a given ISO timestamp: the layout
    task-bridge.ts introduced in PR #591 (`archive/YYYY-MM/`). The month
    comes from the supplied timestamp, not the wall clock, so writers and
    tests are deterministic around month boundaries."""
    return base / "archive" / iso_timestamp[:7]


def find_archived_task(tasks_dir: Path, task_id: str) -> Path | None:
    """Locate a task file across the live dir, the legacy flat archive, and
    the month-partitioned archive — the same candidate set task-bridge's
    `_isVoiceTask` walks. Returns the first existing path or None. Rejects
    malformed ids rather than globbing with them (traversal gate)."""
    if not valid_task_id(task_id):
        return None
    fname = f"{task_id}.txt"
    candidates = [tasks_dir / fname, tasks_dir / "processed" / fname,
                  tasks_dir / "archive" / fname]
    archive_root = tasks_dir / "archive"
    if archive_root.is_dir():
        for entry in sorted(archive_root.iterdir()):
            if entry.is_dir() and _MONTH_DIR_RE.match(entry.name):
                candidates.append(entry / fname)
    for p in candidates:
        if p.exists():
            return p
    return None


def iter_archived_tasks(tasks_dir: Path) -> Iterable[Path]:
    """Yield every archived task file (flat legacy + month-partitioned),
    for corpus sweeps and golden tests."""
    archive_root = tasks_dir / "archive"
    if not archive_root.is_dir():
        return
    for p in sorted(archive_root.glob("*.txt")):
        yield p
    for entry in sorted(archive_root.iterdir()):
        if entry.is_dir() and _MONTH_DIR_RE.match(entry.name):
            yield from sorted(entry.glob("*.txt"))
