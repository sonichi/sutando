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

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable



def newest_archived(directory: Path, task_id: str) -> Path | None:
    """Newest record for task_id in one directory — collision suffix included.
    A repeat lands as `<id>.txt.1`, so plain `<id>.txt` is the OLDEST, not current."""
    base = directory / f"{task_id}.txt"
    if not base.exists():
        return None          # `.N` is only minted once `.txt` is taken
    # Probe exact names, never glob: this runs in agent-api's per-poll loop over an
    # archive dir that reached 5,716 entries, where a glob measured 442x an exists().
    best, n = base, 1
    while True:
        nxt = base.with_name(f"{base.name}.{n}")
        if not nxt.exists():
            return best
        best, n = nxt, n + 1


# ── Schema constants ─────────────────────────────────────────────────────────

# Interaction-plane vocabulary (step 1, PR #1953). Producers stamp exactly one
# of these next to `source:`; the remote-gateway bridge whitelists inbound
# values against this set (unknown → "message").
INTERACTION_TYPES = frozenset({
    "message", "realtime_audio", "realtime_video",
    "tool_initiated", "system_event", "self_reflective",
})

# Content-modality vocabulary (interaction-model 4D, step 1.5). The modalities
# a task's payload spans — orthogonal to interaction_type (which names the
# *mode*, not the media). A text-only DM is {"text"}; a photo with a caption is
# {"text", "image"}; a voice note is {"audio"}. Producers stamp a comma-joined
# subset next to `interaction_type`; readers whitelist against this set
# (unknown token dropped), the same discipline as the interaction_type gateway
# whitelist.
CONTENT_MODALITIES = frozenset({"text", "image", "audio", "video", "file"})

# Media-form vocabulary — the load-bearing routing axis of the 4D model:
# `attachment` = a discrete, already-bounded object delivered alongside a
# message (a photo, a PDF, a voice note) → lands in the task file as an
# AttachmentRef the Core fetches + analyzes. `live_stream` = a continuous
# real-time media plane (a call's audio, a screen share) → does NOT belong in
# a task file; it is owned by the LiveAgentRuntime. A missing header defaults
# to `attachment`: messaging is the default plane, and `live_stream` is the
# explicit opt-out that routes a payload away from the task-file path.
MEDIA_FORMS = frozenset({"attachment", "live_stream"})

# Task priority enum (src/task_priority.py is the behavior owner; these are
# the schema names). Consumer semantics: highest first, mtime FIFO tiebreak.
PRIORITIES = ("urgent", "normal", "low")

# ── Durable Work Model ───────────────────────────────────────────────────────

# pending = file in tasks/; result_written = same-id result file (the
# canonical completion marker); archived = under tasks/archive/.
LIFECYCLE_STATES = ("pending", "result_written", "archived")

# `owner` is full, `team` is workspace-write sandboxed, `guest`/`other` are
# read-only, and `ambient` is sandboxed observation — never instructions.
ACCESS_TIERS = ("owner", "team", "guest", "other", "ambient")

# The header vocabulary: every key observed in the real archive corpus
# (3,401 files, 2026-07-06) plus the live writers' full sets. This list is
# ENFORCED in two places that must stay in lockstep (Codex P2 on PR #1954):
# - the parsers below promote ONLY these keys to headers (an unknown
#   `key: value` line stays in the body — so junk like transcript dialogue
#   `Caller: hi` never becomes metadata), and
# - task_body_guard.confine_user_content defangs exactly these keys in
#   untrusted bodies (it imports this list), so no key a parser would trust
#   can survive undefanged in user-supplied content.
# Adding a producer header = add it here; the guard follows automatically.
KNOWN_HEADER_KEYS = (
    "id", "timestamp", "session_scope", "task", "source", "access_tier", "user_id",
    "channel_id", "priority", "interaction_type", "source_message_id",
    "channel_name", "guild_name", "attempts", "sender_name", "room_name",
    "parent_message_id", "reply_chain_ids", "reminder", "author_name",
    "author_id", "chat_id",
    # Reply addressing: header status means only the trusted bridge writes
    # them, and the guard defangs forged body copies of the same names.
    "thread_ts", "reply_to_event", "reply_to_me", "reply_to_sender",
    "addressed_to", "callSid", "caller",
    # Which instance took delivery. Same namespace as the addressee in the body, so a
    # non-addressed core can tell; header status defangs a forged body copy.
    "receiving_instance",
    "from", "call_sid", "hint", "instructions", "transcript",
    # Durable schedule identity (#2723): the codex scheduler stamps which
    # schedule and which slot produced the task.
    "schedule_name", "schedule_slot",
    # interaction-model 4D, step 1.5 — structured media metadata. Listing them
    # here promotes them to headers AND (via the guard's shared import) defangs
    # them in untrusted bodies, so a forged `attachments:` body line can never
    # smuggle a fetch locator past the parser.
    "content_modalities", "media_form", "attachments",
    # Platform-signed metadata pointer (one-line JSON object): a signed
    # reference to the delivering platform's canonical agent operating card,
    # verifiable offline-of-the-task against the platform's well-known key
    # (skills/agent-room-ops/verify_platform_card.py). Header status means a
    # trusted bridge wrote it; the guard defangs a forged `platform_card:`
    # body line the same as `attachments:`.
    "platform_card",
)
_KNOWN_KEY_SET = frozenset(KNOWN_HEADER_KEYS)

# Canonical live task-id shape: `task-<slug>` where slug is dash-separated
# [a-z0-9] segments (task-1783..., task-chat-1783..., task-phone-...,
# task-summary-..., task-gh-..., task-health-...). This stays narrower than
# the archive lookup gate below: live API/task-result routes still key off
# the canonical `task-*` namespace even though historic archives contain
# additional gateway-safe producer ids like `ask-*`.
TASK_ID_RE = re.compile(r"^task-[A-Za-z0-9][A-Za-z0-9-]{0,120}$")
# `~` and 128 chars cover the gateway's named-instance ids
# (`task-<inst>~<broker-id>`); neither is a traversal character.
ARCHIVE_LOOKUP_ID_RE = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")


def valid_task_id(tid: str) -> bool:
    """True iff `tid` is a canonical live task id.

    Live task/result routes still expect the `task-*` namespace, so this is
    intentionally stricter than the archive lookup gate.
    """
    return bool(TASK_ID_RE.match(tid or ""))


def valid_archive_lookup_id(tid: str) -> bool:
    """True iff `tid` is safe to look up as an archived filename stem.

    Archive corpora include historic non-`task-*` producer ids (`ask-*`,
    `sc-ask-*`, `reco-skill-*`). The lookup gate therefore mirrors the
    gateway bridge's filename-safe charset while still rejecting traversal
    sentinels and path separators.
    """
    return bool(ARCHIVE_LOOKUP_ID_RE.match(tid or "")) and tid not in (".", "..")


# ── Header parsing ───────────────────────────────────────────────────────────

_HEADER_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):[ \t]?(.*)$")


@dataclass
class TaskHeaders:
    """Parsed view of a task file. `headers` preserves the parser's trust
    rule (see module docstring).

    `body` semantics differ BY PARSER — this is deliberate and load-bearing:
    - `parse_task_headers` (task-last): the full work item — the `task:`
      line's content plus every line after it, verbatim.
    - `parse_task_headers_trusted` / `_lenient` (task-mid): the `task:`
      line's content plus every subsequent NON-vocabulary line. Vocabulary
      header lines (KNOWN_HEADER_KEYS) after `task:` are promoted to
      `headers` and excluded from body; everything else — health-check
      failure bullets, phone transcript dialogue, unknown `key:`-shaped
      lines — stays in body. Every post-`task:` line lands in exactly one
      of headers/body (no silent loss; Codex P2s on PR #1954). For the
      byte-verbatim work item including trailing headers, use `task_body()`.
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
        if m and m.group(1) in _KNOWN_KEY_SET:
            headers.setdefault(m.group(1), m.group(2))
        # Unknown-key and non-matching lines before task: are tolerated and
        # skipped — real archive files contain blank lines and free-text
        # hint blocks; only vocabulary keys become metadata.
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
    return _parse_task_mid(text, last_wins=False)


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
    return _parse_task_mid(text, last_wins=True)


def _parse_task_mid(text: str, last_wins: bool) -> TaskHeaders:
    """Shared task-mid scan. Headers = vocabulary keys only
    (KNOWN_HEADER_KEYS); body = the `task:` line's content plus every
    subsequent NON-vocabulary line — so continuation content (health-check
    failure bullets, phone transcript dialogue, skill-hint blocks) is
    preserved losslessly in body while trailing real headers are excluded.
    Every input line after `task:` lands in exactly one of headers/body
    (fidelity is asserted over the full archive corpus in the golden test).
    An unknown `key: value` shape (`Caller: hi`) is body, never metadata."""
    headers: dict = {}
    body_lines: list[str] = []
    in_body = False
    for line in text.split("\n"):
        if not in_body and line.startswith("task:"):
            in_body = True
            body_lines.append(line[len("task:"):].lstrip())
            continue
        m = _HEADER_LINE_RE.match(line)
        if m and m.group(1) in _KNOWN_KEY_SET:
            if last_wins:
                headers[m.group(1)] = m.group(2)
            else:
                headers.setdefault(m.group(1), m.group(2))
        elif in_body:
            body_lines.append(line)
    # Trailing blank lines are file-termination artifacts, not content —
    # interior blank lines are preserved.
    while body_lines and body_lines[-1] == "":
        body_lines.pop()
    return TaskHeaders(headers=headers, body="\n".join(body_lines))


# ── Media attachments (interaction-model 4D, step 1.5) ───────────────────────


@dataclass
class AttachmentRef:
    """One discrete media object carried by a task, source-independent.

    The unified descriptor every bridge normalizes its native attachment into
    (Discord CDN url, Slack file, Telegram file_id, Matrix mxc://, or an
    already-downloaded local path) so the Core sees ONE shape regardless of
    surface. `locator` is the source-specific pointer to the bytes; the
    safe-fetch policy (bridge/relay side, a later slice) resolves a remote
    locator to a local path and may rewrite `locator` in place once fetched.

    Deliberately metadata-only: no bytes, no file handles (R1 stdlib-only, and
    a task file must stay a small text record). `size`/`sha256` let a consumer
    enforce a limit and dedupe *before* fetching. Every field but `locator` is
    optional — a ref with nothing to point at is meaningless, so an element
    lacking a locator is dropped on both encode and decode.
    """
    locator: str
    id: str = ""
    mime: str = ""
    filename: str = ""
    size: int = 0
    sha256: str = ""
    expiry: str = ""  # ISO-8601 UTC; "" = no expiry / already-local

    def as_dict(self) -> dict:
        """Compact dict for JSON serialization — omits empty/zero fields so the
        header line stays small and round-trips back to an equal ref."""
        d: dict = {"locator": self.locator}
        for k in ("id", "mime", "filename", "sha256", "expiry"):
            v = getattr(self, k)
            if v:
                d[k] = v
        if self.size:
            d["size"] = self.size
        return d


def parse_content_modalities(headers: dict) -> frozenset:
    """The whitelisted set of content modalities from a parsed header dict.

    Reads the comma-joined `content_modalities` value, lower-cases + trims each
    token, and drops anything outside CONTENT_MODALITIES — an unknown token is
    noise, never a new modality (same rule as the interaction_type whitelist).
    Missing/empty → empty set; a text-only task need not stamp `{"text"}`."""
    raw = (headers or {}).get("content_modalities") or ""
    return frozenset(
        t for t in (tok.strip().lower() for tok in raw.split(",")) if t in CONTENT_MODALITIES
    )


def parse_media_form(headers: dict) -> str:
    """The task's media form: `attachment` (default) or `live_stream`.

    Whitelists the `media_form` value; anything unknown or missing collapses to
    `attachment`, the safe default that routes the payload through the normal
    task-file path rather than mis-claiming a LiveAgentRuntime stream."""
    v = ((headers or {}).get("media_form") or "").strip().lower()
    return v if v in MEDIA_FORMS else "attachment"


def parse_attachments(headers: dict) -> list["AttachmentRef"]:
    """Decode the `attachments:` header — a one-line JSON array of objects —
    into AttachmentRefs.

    Tolerant by contract: a malformed value, a non-list payload, a non-object
    element, or an element with no `locator` is skipped, never raised. A bad
    attachments header must not block reading the rest of the task (the same
    never-block principle as the audit sink). Missing header → []."""
    raw = (headers or {}).get("attachments")
    if not raw:
        return []
    try:
        arr = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(arr, list):
        return []
    refs: list[AttachmentRef] = []
    for el in arr:
        if not isinstance(el, dict):
            continue
        locator = el.get("locator")
        if not locator or not isinstance(locator, str):
            continue
        size = el.get("size", 0)
        if isinstance(size, bool):
            size = 0  # a JSON bool is not a byte count (int(True) == 1 would lie)
        elif not isinstance(size, int):
            try:
                size = int(size)
            except (ValueError, TypeError):
                size = 0
        if size < 0:
            size = 0  # a negative byte count is nonsense and would slip past a
            #            `ref.size and ref.size > max_bytes` cap check — normalize
            #            it to 0 (unknown) so limit enforcement stays sound.
        refs.append(AttachmentRef(
            locator=locator,
            id=str(el.get("id", "")),
            mime=str(el.get("mime", "")),
            filename=str(el.get("filename", "")),
            size=size,
            sha256=str(el.get("sha256", "")),
            expiry=str(el.get("expiry", "")),
        ))
    return refs


def format_attachments(refs: Iterable["AttachmentRef"]) -> str:
    """Encode AttachmentRefs into the one-line JSON value of an `attachments:`
    header — the write-side inverse of parse_attachments.

    Guaranteed single-line (json escapes any embedded newline in a field), so
    it is safe for both task-last single-line values and the task-mid
    newline-stripping writers. Refs without a locator are dropped (they'd be
    unparseable on read, so encoding them would not round-trip)."""
    payload = [r.as_dict() for r in refs if r.locator]
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def modality_for_mime(mime: str) -> str:
    """Map an attachment MIME type to a content modality (CONTENT_MODALITIES):
    image/audio/video by top-level type, everything else (pdf, zip, docx,
    unknown) → `file`. The shared home for the mapping each bridge needs when it
    stamps `content_modalities` — promoted here once a third bridge required it
    (discord/telegram carried private copies first)."""
    m = (mime or "").lower()
    if m.startswith("image/"):
        return "image"
    if m.startswith("audio/"):
        return "audio"
    if m.startswith("video/"):
        return "video"
    return "file"


def media_attachment_headers(attachment_refs: Iterable["AttachmentRef"], has_text: bool) -> str:
    """Build the `content_modalities`/`media_form`/`attachments` header block a
    bridge stamps on a task file when a message carried attachments (step 1.5).
    Returns "" when there are no refs — so a text-only task's headers are
    unchanged.

    `content_modalities` = `text` (iff `has_text`) plus one modality per ref
    mime; `media_form` = `attachment` (these are discrete objects — a live
    stream is never emitted through the messaging task path); `attachments` =
    the one-line JSON from `format_attachments`. The block is newline-terminated
    so it composes directly into a writer's header f-string. Pure — a bridge can
    unit-test its write path without a live message object."""
    refs = [r for r in attachment_refs if r.locator]
    if not refs:
        return ""
    mods = set()
    if has_text:
        mods.add("text")
    for r in refs:
        mods.add(modality_for_mime(r.mime))
    return (
        f"content_modalities: {','.join(sorted(mods))}\n"
        f"media_form: attachment\n"
        f"attachments: {format_attachments(refs)}\n"
    )


# ── Archive rules ────────────────────────────────────────────────────────────

_MONTH_DIR_RE = re.compile(r"^\d{4}-\d{2}$")


def archive_month_dir(base: Path, iso_timestamp: str) -> Path:
    """Month-partitioned archive dir for a given ISO timestamp: the layout
    task-bridge.ts introduced in PR #591 (`archive/YYYY-MM/`). The month
    comes from the supplied timestamp, not the wall clock, so writers and
    tests are deterministic around month boundaries."""
    return base / "archive" / iso_timestamp[:7]


def find_archived_result(results_dir: Path, task_id: str) -> Path | None:
    """Locate an archived result across BOTH layouts in use.

    The messaging bridges archive as `archive/<YYYY-MM>/<id>.txt` via
    `archive_path`; the gateway archives flat as `archive/<id>-<epoch>.txt`.
    A locator that knows only one silently returns None for the other, which
    reads as "this task never delivered" — the wrong answer for any caller
    deciding whether a delivery happened.

    Month scan mirrors `find_archived_task`: scandir, filter on NAME before
    asking is_dir, newest month first. Rejects malformed ids rather than
    globbing with them (traversal gate).
    """
    if not valid_archive_lookup_id(task_id):
        return None
    archive = Path(results_dir) / "archive"
    fname = f"{task_id}.txt"

    direct = archive / fname
    if direct.is_file():
        return direct

    try:
        with os.scandir(archive) as entries:
            months = sorted((e.name for e in entries
                             if _MONTH_DIR_RE.match(e.name) and e.is_dir()),
                            reverse=True)
    except (OSError, ValueError):
        months = []
    for month in months:
        candidate = archive / month / fname
        if candidate.is_file():
            return candidate

    # glob on a missing or non-directory path yields nothing rather than
    # raising, so no guard is needed here.
    flat = sorted(archive.glob(f"{task_id}-*.txt"))
    return flat[-1] if flat else None


def find_result(results_dir: Path, task_id: str) -> Path | None:
    """Locate a task's result: live dir first, then archive. Archival trails
    delivery, so an archive-only lookup reads a fresh result as never delivered."""
    if not valid_archive_lookup_id(task_id):
        return None
    live = Path(results_dir) / f"{task_id}.txt"
    if live.is_file():
        return live
    return find_archived_result(results_dir, task_id)


def find_archived_task(tasks_dir: Path, task_id: str) -> Path | None:
    """Locate a task file across the live dir, the legacy flat archive, and
    the month-partitioned archive — the same candidate set task-bridge's
    `_isVoiceTask` walks. Returns the first existing path or None. Rejects
    malformed ids rather than globbing with them (traversal gate).

    The month scan uses `os.scandir` and filters on the NAME before asking
    whether the entry is a directory. The archive root holds one file per
    archived task and only a handful of `YYYY-MM/` dirs, so it grows without
    bound while the thing being looked for stays tiny — on a live host it was
    5,716 entries to find 3 month dirs, and this lookup cost 182 ms. Measured
    there, per call:

        sorted(iterdir())                 121 ms   <- Path.__lt__ on 5,716 objects
        iterdir() + is_dir() on every one  88 ms   <- one stat syscall each
        scandir() + is_dir() on every one  11 ms   <- dirent type is already cached

    Both halves of the old cost were avoidable: `sorted()` paid to order 5,716
    Path objects when only the matching month names need ordering, and
    `Path.is_dir()` stat'd every entry when `os.DirEntry.is_dir()` reads the
    type the kernel already returned. Sorting the handful of matched NAMES
    preserves the previous candidate order exactly.

    This is not micro-optimisation for its own sake: `agent-api.py` calls this
    once per result file in a loop of up to 10 (`_remember_done_result_file`),
    so the old cost showed up as ~1.8 s of directory scanning on a single
    dashboard poll.
    """
    if not valid_archive_lookup_id(task_id):
        return None
    dirs = [tasks_dir, tasks_dir / "processed", tasks_dir / "archive"]
    archive_root = tasks_dir / "archive"
    try:
        with os.scandir(archive_root) as entries:
            months = sorted(e.name for e in entries
                            if _MONTH_DIR_RE.match(e.name) and e.is_dir())
    except (OSError, ValueError):
        months = []          # missing/unreadable archive is "no months", not an error
    dirs.extend(archive_root / m for m in months)
    for d in dirs:
        # Shared owner picks among collision suffixes: `<id>.txt` is the OLDEST
        # record once `<id>.txt.1` exists, so `.exists()` here returned stale.
        hit = newest_archived(d, task_id)
        if hit is not None:
            return hit
    return None


def iter_archived_tasks(tasks_dir: Path, *,
                        newest_first: bool = False) -> Iterable[Path]:
    """Yield every archived task file (flat legacy + month-partitioned),
    for corpus sweeps and golden tests. Skips non-task artefacts (files
    without a `task:` line) that may accumulate in the archive directory
    (e.g. `answer-Q*` files from the pending-questions flow).

    `newest_first` reverses the traversal for callers that stop early: the
    default order puts the oldest month first, so a bounded consumer sees
    only the least recent tasks. Both orders stay lazy — a caller that stops
    at N never stats the rest of the archive.

    Either order is a HEURISTIC about where a corpus keeps its recent tasks,
    never a guarantee: `newest_first` assumes the flat legacy files are older
    than every month partition, and a host whose flat tail holds recent tasks
    defeats it (measured: first non-owner task at index 590 under
    `newest_first` vs index 1 under the default). A bounded caller must
    therefore treat exhausting its cap as UNKNOWN rather than absence — the
    cap is a bound on work, not a tuning knob for accuracy.
    """
    archive_root = tasks_dir / "archive"
    if not archive_root.is_dir():
        return
    flat = sorted(archive_root.glob("*.txt"), reverse=newest_first)
    months = [e for e in sorted(archive_root.iterdir(), reverse=newest_first)
              if e.is_dir() and _MONTH_DIR_RE.match(e.name)]

    def _months() -> Iterable[Path]:
        for entry in months:
            for q in sorted(entry.glob("*.txt"), reverse=newest_first):
                if _has_task_line(q):
                    yield q

    def _flat() -> Iterable[Path]:
        for q in flat:
            if _has_task_line(q):
                yield q

    # Month partitions hold the recent tasks; flat files are the legacy tail.
    groups = (_months(), _flat()) if newest_first else (_flat(), _months())
    for group in groups:
        for q in group:
            yield q


def _has_task_line(path: Path) -> bool:
    """True iff `path` contains a `task:` header line — the structural marker
    that distinguishes a real task file from a non-task artefact that has
    accumulated in the archive directory."""
    try:
        return any(line.startswith("task:") for line in
                   path.read_text(errors="replace").split("\n"))
    except OSError:
        return False


# ── Write side (task-last, the convergence shape) ────────────────────────────

def serialize_task_last(headers: "Iterable[tuple[str, str]]", task_body: str) -> str:
    """Serialize a task file in the task-last trust shape: every header
    precedes the single `task:` line; only the body after it may span lines.

    Header values must be single-line — a newline in a value could forge a
    header, so it raises instead of writing a corrupt file.
    """
    lines = []
    for key, value in headers:
        if key == "task":
            raise ValueError("pass the body via task_body, not as a header")
        if key not in _KNOWN_KEY_SET:
            raise ValueError(f"unknown header key {key!r} — add it to KNOWN_HEADER_KEYS first")
        if "\n" in value or "\r" in value:
            raise ValueError(f"header {key!r} value contains a newline")
        lines.append(f"{key}: {value}")
    lines.append(f"task: {task_body}")
    return "\n".join(lines) + "\n"


_TASK_STAMPER = None


def apply_task_stamper(text: str) -> str:
    """Run the host-injected stamper over serialized task text; fail-open —
    a raising stamper must never lose the task. EVERY producer that persists
    task text calls this (write_task_file AND the live gateway _write_task)."""
    if _TASK_STAMPER is None:
        return text
    try:
        return _TASK_STAMPER(text)
    except Exception:
        return text


def set_task_stamper(fn) -> None:
    """Host-injected transform applied to the serialized task text just
    before persist (e.g. Sutando's HMAC envelope stamp). Provider-neutral
    seam: sparrow never names a concrete stamper; the adapter edge does.
    Fail-open by contract — a raising stamper must not lose the task."""
    global _TASK_STAMPER
    _TASK_STAMPER = fn


def write_task_file(tasks_dir: "Path | str", task_id: str,
                    headers: "Iterable[tuple[str, str]]", task_body: str) -> Path:
    """Write `<tasks_dir>/<task_id>.txt` in the task-last shape. The task
    enters the Durable Work Model's `pending` state the moment this returns."""
    if not valid_task_id(task_id):
        raise ValueError(f"not a canonical task id: {task_id!r}")
    hdrs = list(headers)
    supplied = [v for k, v in hdrs if k == "id"]
    if any(v != task_id for v in supplied):
        raise ValueError(
            f"id header {supplied!r} disagrees with task_id {task_id!r} — "
            "the filesystem key and the in-band id must be the same identity")
    if not supplied:
        hdrs.insert(0, ("id", task_id))
    d = Path(tasks_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{task_id}.txt"
    path.write_text(apply_task_stamper(serialize_task_last(hdrs, task_body)))
    return path
