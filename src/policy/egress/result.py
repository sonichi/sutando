#!/usr/bin/env python3
"""Final scan applied to a Team-tier result before any router reads its markers.

A Team result is produced under a prompt its sender can influence, so the last
thing between that text and a marker-interpreting router must be code, not the
producer's compliance with an instruction.

Ownership: this module is the single implementation of the policy — the control
marker set, the secret scan, and what a withheld result says. Adapters bind
their own tier lookup and delivery; they must not restate any of it.

Suppression markers are deliberately NOT controls here. `[channel:]` and
`[file:]` move data somewhere the sender should not reach; `[no-send]`,
`[REPLIED]` and `[deduped:]` move nothing, and the party left without an answer
is the same non-owner who asked. What they can hide is that a task was handled,
which is an accountability property — so they are honoured on every tier and
journaled, rather than refused.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

# A marker is a control only where the shared parser EXECUTES it, so the guard
# derives its classification from parse_markers rather than a parallel grammar.
try:  # pragma: no cover - the packaged twin exercises the relative imports
    from .result_markers import parse_markers  # packaged sibling (ag2-sparrow)
    from .local_task_protocol import canonical_access_tier, find_archived_task, parse_task_headers
    from .task_archive import find_task_file
    from .file_lock import lock_fd, unlock_fd
except ImportError:
    from result_markers import parse_markers  # monorepo src/ on sys.path
    from local_task_protocol import canonical_access_tier, find_archived_task, parse_task_headers
    from task_archive import find_task_file
    from file_lock import lock_fd, unlock_fd

TEAM_LEAK_RESULT = (
    "I completed the Team task, but the response was withheld because it may "
    "contain sensitive information. The owner can review the work locally."
)

# Safe to name: the marker is the sender's own construct. Content/secret
# classes stay generic — naming those would confirm probe hits.
TEAM_LEAK_RESULT_MARKER = (
    "I completed the Team task, but the response was withheld because it "
    "carried a delivery-control marker, which non-owner results may not use. "
    "The owner can review the work locally."
)

TEAM_LEAK_RESULT_UNSAVED = (
    "I completed the Team task, but the response was withheld because it may "
    "contain sensitive information or delivery-control markers. Preparing the "
    "private owner review failed; the result remains withheld and will retry."
)

# Reached ONLY when the record cannot be written: a close must never be both
# silent and untraceable. Unreachable in normal operation.
TEAM_SUPPRESS_RESULT = (
    "Task handled. The agent marked this reply as not-for-delivery, but the "
    "record of that suppression could not be written, so this notice is "
    "delivered in its place. Nothing was withheld for content reasons."
)

# Only `owner` is exempt — markers are a feature for the owner and a capability
# for everyone else. Stated as an exemption so an unknown tier guards by default.
OWNER_TIER = "owner"


def is_guarded_tier(tier) -> bool:
    """Every tier except `owner` is guarded, including empty and unrecognised."""
    return (tier or "").strip().lower() != OWNER_TIER


# Not a tier: an unreadable file, kept distinct from a malformed one (`guest`).
# Outside the legal set, so it guards like every non-owner value and cannot be forged.
TIER_UNREADABLE = "unreadable"

class TeamResultLeakError(RuntimeError):
    """A Team result carried a delivery-control marker or a likely secret."""


# An internal error carries this host's paths and state ids; it is the owner's to read, never a
# guarded reader's. Paths match only under per-user and temp roots (POSIX, Windows), never `/x/y`.
INTERNAL_ERROR_DETAIL = "internal error detail"
_INTERNAL_ERROR_RE = re.compile(
    r"\[(?:Errno|WinError) \d+\]"
    r"|Traceback \(most recent call last\)"
    r"|(?<![\w.~/-])(?:/(?:Users|home|root|tmp|var/tmp|private/(?:var|tmp)|var/folders)|~/Library)/[^\s'\"`]+"
    r"|(?<![\w.~/-])[A-Za-z]:[\\/]Users[\\/][^\s'\"`]+")


def carries_internal_error_detail(body) -> bool:
    """True when the text a reader would see quotes a raw OS error, a traceback, or a local path.
    Marker values are excluded: an admitted `[file:]` path is consumed by the transport, never shown."""
    return bool(_INTERNAL_ERROR_RE.search(parse_markers(body or "").body or ""))


def resolve_access_tier(task_file) -> str:
    """Read a task's effective tier without letting a task-last body escalate.

    Task-last writers put the trusted tier before ``task:``; prefer that value.
    The remote gateway is task-mid and newline-confines every wire value, so if
    no pre-task tier exists its final tier line is the trusted value.  Missing
    legacy tiers remain owner; malformed explicit tiers fail closed to guest, and an
    UNREADABLE file answers `TIER_UNREADABLE` so a transient I/O failure is never
    reported as a tier the file does not carry.
    """
    try:
        content = Path(task_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return TIER_UNREADABLE
    # LF-only split: the writer strips only \r/\n, so a Unicode line
    # boundary in a field must not forge a header. str.splitlines() would.
    def _tiers(text):
        return [
            line.partition(":")[2].strip().lower()
            for line in text.split("\n")
            if line.startswith("access_tier:")
        ]
    before_task = content.split("\ntask:", 1)[0]
    candidates = _tiers(before_task) or _tiers(content)
    if not candidates:
        return "owner"
    # Conflicting explicit tiers can only come from injection — fail closed.
    normed = {canonical_access_tier(t) for t in candidates}
    if len(normed) > 1:
        return "guest"
    tier = normed.pop()
    return tier if tier in {"owner", "team", "guest"} else "guest"


def resolve_route_tier(tier, tasks_dir: Path, task_id: str) -> str:
    """The tier a reply is guarded with: the route's own tier when it carries one,
    else the task file's (live, claimed, or archived); no task file anywhere is guest."""
    if tier and tier != "unknown":
        return tier
    task_file = find_task_file(tasks_dir, task_id) or find_archived_task(tasks_dir, task_id)
    return resolve_access_tier(task_file) if task_file else "guest"


def sensitive_data_filter_enabled(task_file, tier=None) -> bool:
    """Disable only for paired Team collaborator and filter-off stamps."""
    if tier != "team":
        return True
    try:
        content = Path(task_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    # LF-only split (see resolve_access_tier): a Unicode line boundary in a
    # field must not forge a filter-off / collaborator stamp.
    before_task = content.split("\ntask:", 1)[0].split("\n")
    filter_values = [
        line.partition(":")[2].strip()
        for line in before_task
        if line.startswith("sensitive_data_filter:")
    ]
    collaborator_values = [
        line.partition(":")[2].strip()
        for line in before_task
        if line.startswith("collaborator:")
    ]
    return filter_values != ["false"] or collaborator_values != ["true"]


def trusted_attach_root(root) -> str | None:
    """Return a real, non-symlink task root or None."""
    try:
        absolute = os.path.abspath(os.fspath(root))
        metadata = os.lstat(absolute)
    except (OSError, TypeError, ValueError):
        return None
    if not stat.S_ISDIR(metadata.st_mode):
        return None
    real = os.path.realpath(absolute)
    expected = os.path.join(
        os.path.realpath(os.path.dirname(absolute)), os.path.basename(absolute))
    return real if real == expected else None


def open_confined_attachment(path, root) -> int:
    """Open a regular file below a trusted root without following symlinks."""
    trusted_root = trusted_attach_root(root)
    if trusted_root is None:
        raise ValueError("untrusted task media directory")
    try:
        absolute_path = os.path.normpath(os.path.abspath(os.fspath(path)))
        absolute_root = os.path.normpath(os.path.abspath(os.fspath(root)))
        relative = os.path.relpath(absolute_path, absolute_root)
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError("invalid attachment path") from exc
    if (os.path.isabs(relative) or relative in ("", ".", "..")
            or relative.startswith(".." + os.sep)):
        raise ValueError("attachment is outside the task media directory")
    components = relative.split(os.sep)
    if any(part in ("", ".", "..") for part in components):
        raise ValueError("invalid attachment path")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(trusted_root, directory_flags)
    current_fd = root_fd
    try:
        for component in components[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
        file_fd = os.open(
            components[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current_fd)
        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise OSError("attachment is not a regular file")
        except BaseException:
            os.close(file_fd)
            raise
        return file_fd
    finally:
        if current_fd != root_fd:
            os.close(current_fd)
        os.close(root_fd)


def load_team_result_scanner(repo: Path):
    """Load and warm the full scanner graph before Team-controlled execution."""
    source_dir = str((Path(repo) / "src").resolve())
    if source_dir not in sys.path:
        sys.path.insert(0, source_dir)
    try:
        # Warm the full scanner graph before Team runs; later source rewrites
        # cannot replace the retained parent-process module objects.
        from chat_secret_filter import filter_chat_secrets
        try:
            from secret_scanner import scan_and_redact as retained_scan_and_redact
        except Exception:
            # Optional detector dependency; the curated fallback stays valid.
            retained_scan_and_redact = None
        warmup = filter_chat_secrets("Sutando Team result scanner warmup")
        if not hasattr(warmup, "detected") or (
            retained_scan_and_redact is not None
            and not callable(retained_scan_and_redact)
        ):
            raise TypeError("invalid Team result scanner contract")
    except Exception as exc:
        raise RuntimeError("Team result secret scanner is unavailable") from exc
    return filter_chat_secrets


def attach_markers_confined(body: str, attach_roots) -> bool:
    """True iff the body carries at least one attach marker and EVERY one names an
    ABSOLUTE path whose realpath sits under one of `attach_roots`.

    The task-scoped output allowance (Signal Room, design 5G ⑤a-cap): a Team
    result may carry `[file:]` markers for files the task itself produced under
    its own output directory -- and nothing else. Realpath on both sides, so a
    symlink planted inside the root that points out of it is caught the way
    send_allowlist catches it. A relative or `~`-prefixed path is not confined:
    it would resolve against a cwd or a home directory this policy cannot
    vouch for. An empty `attach_roots` confines nothing.
    """
    roots = [trusted for r in (attach_roots or ())
             if str(r).strip() and (trusted := trusted_attach_root(r))]
    if not roots:
        return False
    values = [a.value for a in parse_markers(body or "").actions if a.kind == "attach"]
    if not values:
        return False
    for raw in values:
        candidate = (raw or "").strip()
        if not candidate or not os.path.isabs(candidate):
            return False
        real = os.path.realpath(candidate)
        if not any(real == root or real.startswith(root + os.sep) for root in roots):
            return False
    return True


def scan_team_result(body: str, repo: Path, secret_filter=None,
                     scan_sensitive_data: bool = True,
                     allow_attach: bool = False,
                     attach_roots=()) -> str:
    """Return `body` unchanged, or raise TeamResultLeakError if it must be withheld."""
    kinds = {action.kind for action in parse_markers(body or "").actions}
    # dm-only only suppresses a redirect the guard already withholds, and a
    # redirect it suppressed never executes -- neither is a control here.
    if kinds & {"redirect"}:
        raise TeamResultLeakError("result delivery control marker")
    if ("attach" in kinds and not allow_attach
            and not attach_markers_confined(body, attach_roots)):
        # Verdict-only exemption: the adapter says whether THIS channel+sender
        # may attach; path authorization stays with the transport allowlist.
        # The task-scoped allowance is narrower than allow_attach: only files
        # under the task's own output root (attach_markers_confined) pass.
        raise TeamResultLeakError("result delivery control marker")
    # Suppression is deliberately absent: redirect and attach move data
    # somewhere the sender should not reach, a skip marker moves nothing.
    # Marker checks stay above this narrow scanner opt-out.
    if not scan_sensitive_data:
        return body
    if carries_internal_error_detail(body):
        raise TeamResultLeakError(INTERNAL_ERROR_DETAIL)
    filter_chat_secrets = secret_filter or load_team_result_scanner(repo)
    try:
        result = filter_chat_secrets(body)
    except Exception as exc:
        raise RuntimeError("Team result secret scan failed") from exc
    if result.detected:
        raise TeamResultLeakError(", ".join(result.secret_types))
    return body


def is_suppression_only(body: str, skip_after_channel: bool = False) -> bool:
    """True when every marker the shared parser executes is suppressive.

    Classification, not validation: the guard does not inspect a dedup target,
    because the consumer that dereferences one already rejects an id it cannot
    look up safely. Deciding here would be a second, drifting copy of that rule.
    skip_after_channel: the parse_markers flag of the delivery verdict to classify.
    """
    actions = parse_markers(body or "", skip_after_channel=skip_after_channel).actions
    return bool(actions) and all(action.kind == "skip" for action in actions)


OWNER_MENTION_HEADER = "owner_mentioned"


def is_owner_mention_task(task_text) -> bool:
    """True when the task's writer attested an owner mention above `task:`.

    The strict parse stops at `task:`, so a body line can never claim it.
    """
    headers = parse_task_headers(task_text or "")
    return (headers.get(OWNER_MENTION_HEADER) or "").strip() == "true"


def owner_mention_result_refused_by_room(task_text, body: str) -> bool:
    """An owner-mention task may close its room turn silently and nothing more:
    any other result goes to the owner's DM, never to the room that mentioned him."""
    return is_owner_mention_task(task_text) and not is_suppression_only(body)


VERDICT_DELIVER = "deliver"
VERDICT_LEAK = "leak"
VERDICT_SUPPRESS = "suppress"
# Machine-detectable prefix of the fail-closed scanner-outage reason, so a
# consumer that owns its own transport can map outage (not content) to an error.
SCANNER_UNAVAILABLE = "scanner unavailable"
WITHHELD_RESULT_DIR = "withheld-team-results"
SUPPRESSED_RESULT_DIR = "suppressed-team-results"


class TeamResultVerdict(NamedTuple):
    """kind: deliver | leak | suppress. body: what the adapter delivers.
    SUPPRESS now names only bodies the adapter must post in place of the
    result: the pending-review stub, and the fail-closed unrecorded notice."""
    kind: str
    body: str
    reason: "str | None"


def _bounded_context(context) -> dict:
    if not isinstance(context, dict):
        return {}
    keys = (
        "source", "channel_id", "room_name", "reply_to_event",
        "source_message_id", "user_id",
    )
    return {key: str(context.get(key) or "")[:512] for key in keys}


def withheld_review_id(task_id: str, body_digest: str = "") -> str:
    """Task-keyed for a task's first recorded body; a later, different body of the
    same task gets its own id, keyed by the digest of that body."""
    if not task_id:
        identity = os.urandom(32)
    else:
        identity = (task_id + ("\0" + body_digest if body_digest else "")).encode()
    return f"wr_{hashlib.sha256(identity).hexdigest()[:16]}"


def withheld_review_path(state_dir: Path, task_id: str, body_digest: str = "") -> Path:
    return Path(state_dir) / WITHHELD_RESULT_DIR / f"{withheld_review_id(task_id, body_digest)}.json"


def _body_digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _holds(path: Path, field: str, body: str) -> bool:
    """The record at `path` is about exactly `body`; unreadable is not a match."""
    try:
        return read_json_strict(path).get(field) == body
    except (OSError, ValueError, AttributeError):
        return False


# Ids a record may take for one body of a task: the task-keyed one, then ones keyed
# by the body's digest. Every id ever issued is reserved in `issued/`, never released.
_ID_TRIES = 8


def _candidates(path_of, state_dir: Path, task_id: str, body: str):
    digest = _body_digest(body)
    yield path_of(state_dir, task_id)
    yield path_of(state_dir, task_id, digest)
    for n in range(2, _ID_TRIES):
        yield path_of(state_dir, task_id, f"{digest}#{n}")


def _archived(path: Path) -> Path:
    return path.parent / "archive" / path.name


def _reservation(path: Path) -> Path:
    return path.parent / "issued" / f"{path.stem}.issued"


def _reserve(path: Path, digest: str) -> bool:
    """Reserve `path`'s id for the body with `digest`: an exclusive create, so an id
    is issued exactly once and the reservation outlives archive and resolution."""
    _ensure_dir(_reservation(path).parent)
    try:
        fd = os.open(_reservation(path),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(digest)
        handle.flush()
        os.fsync(handle.fileno())
    return True


def _issued_to(path: Path, field: str) -> "str | None":
    """The body digest `path`'s id was issued to; None if never issued. A record
    from before reservations existed is reserved first (seeding), and an unreadable
    one counts as issued to no body this rule can match."""
    try:
        return read_strict(_reservation(path)).strip()
    except FileNotFoundError:
        pass
    for existing in (_archived(path), path):           # a decision owns the id it decided
        if present(existing):
            _reserve(path, _record_digest(existing, field))
            return read_strict(_reservation(path)).strip()
    return None


def _record_digest(path: Path, field: str) -> str:
    """The digest of the body a record holds; "unreadable" matches no body."""
    try:
        body = read_json_strict(path).get(field)
    except (OSError, ValueError, AttributeError):
        return "unreadable"
    return _body_digest(body) if isinstance(body, str) else "unreadable"


def _record_of(path: Path, digest: str, field: str) -> "Path | None":
    """The record of an id issued to `digest`, its decision first: only a record
    holding that very body, never another body's copy under the same id."""
    for existing in (_archived(path), path):
        if present(existing) and _record_digest(existing, field) == digest:
            return existing
    return None


@contextlib.contextmanager
def _ledger_lock(directory: Path):
    """The one lock under which a record directory's ids are issued, its records
    written, and its records archived: no transition interleaves with another."""
    issued = Path(directory) / "issued"
    _ensure_dir(issued)  # raises on a linked or odd issued/
    fd = os.open(issued / ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        lock_fd(fd)
        try:
            yield
        finally:
            unlock_fd(fd)
    finally:
        os.close(fd)


def _record_directory(path: Path) -> Path:
    path = Path(path)
    return path.parent.parent if path.parent.name == "archive" else path.parent


def _owns_its_id(path: Path, field: str) -> bool:
    """A live record may act only while its id is reserved to the body it holds
    and no decision for that id is archived; an archived decision is immutable."""
    return (path.parent.name != "archive" and not present(_archived(path))
            and _issued_to(path, field) == _record_digest(path, field))


def present(path: Path, kind: "int | None" = None) -> bool:
    """True when `path` is there as `kind`; False only when it is genuinely absent (see _walk).
    A symlink, a wrong kind or any other error is unknown and raises OSError, never reads absent."""
    mode = _walk(path)
    if mode is None:
        return False
    if kind is not None and stat.S_IFMT(mode) != kind:
        raise OSError(f"{path}: not a path of its kind")
    return True


def entries(directory: Path, pattern: str) -> list:
    """The names under `directory` matching `pattern`; [] only when it is genuinely absent."""
    if not present(directory, stat.S_IFDIR):
        return []
    directory = directory if isinstance(directory, Path) else Path(directory)
    return sorted(directory / name for name in os.listdir(directory)
                  if fnmatch.fnmatchcase(name, pattern))


def read_strict(path: Path) -> str:
    """The text of a record file, read only through the strict walk and never through a link;
    a missing file raises FileNotFoundError, anything else unknown raises OSError."""
    if not present(path, stat.S_IFREG):
        raise FileNotFoundError(2, "No such file or directory", str(path))
    with os.fdopen(_open_no_follow(path), "r", encoding="utf-8") as handle:
        return handle.read()


def _open_no_follow(path: Path) -> int:
    path = path if isinstance(path, Path) else Path(path)
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_CLOEXEC", 0))
    if not hasattr(os, "O_DIRECTORY"):  # no dir_fd walk on this platform: the leaf alone
        return _regular_only(os.open(path, flags))
    fd = _open_dir(path.parent)
    try:
        return _regular_only(os.open(path.name, flags, dir_fd=fd))
    finally:
        os.close(fd)


def _regular_only(fd: int) -> int:
    # Opened non-blocking, so a FIFO cannot hang the open; anything but a regular file is refused.
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError("not a regular file")
    return fd


def _open_dir(directory: Path) -> int:
    # Each directory from the namespace down is opened relative to the last and never through a
    # link, so a directory swapped for a link after the walk is refused, not followed.
    nofollow, flags = getattr(os, "O_NOFOLLOW", 0), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    root = _namespace_root(directory / "entry")         # the same root the walk of an entry uses
    fd = os.open(root, flags | nofollow)
    try:
        for name in directory.relative_to(root).parts:
            step = os.open(name, flags | nofollow, dir_fd=fd)
            os.close(fd)
            fd = step
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_json_strict(path: Path):
    return json.loads(read_strict(path))


_NAMESPACES = (WITHHELD_RESULT_DIR, SUPPRESSED_RESULT_DIR)


def _namespace_root(path: Path) -> Path:
    return next((a for a in (path, *path.parents) if a.name in _NAMESPACES), path.parent)


def namespace_problems(directory: Path) -> list:
    """Read-only, no lock: each entry of a review namespace that a reader would refuse, as
    (path, reason), across live records, archive/, conflicts/, claims/ and issued/."""
    directory = directory if isinstance(directory, Path) else Path(directory)
    problems = []
    for folder, records in ((directory, True), (directory / "archive", True),
                            (directory / "archive" / "superseded", True),
                            (directory / "conflicts", True), (directory / "claims", False),
                            (directory / "issued", False)):
        try:
            paths = entries(folder, "wr_*.json" if records else "*")
        except OSError:
            problems.append((folder, "a link, an odd entry or no access"))
            continue
        for path in paths:
            try:
                if not records:
                    present(path, stat.S_IFREG)  # a directory, FIFO or link here is unknown
                elif not isinstance(read_json_strict(path), dict):
                    problems.append((path, "not a JSON object"))
            except OSError:
                problems.append((path, "a link, an odd entry or no access"))
            except ValueError:
                problems.append((path, "not valid JSON"))
    return problems


def _walk(path) -> "int | None":
    # lstat each step from the record namespace (or the parent) down: a link is unknown, and so is
    # a file on the way (ENOTDIR); None only when genuinely absent below a real, readable directory.
    path = path if isinstance(path, Path) else Path(path)
    root = _namespace_root(path)
    steps = [root, *reversed([a for a in path.parents if root in a.parents]), path]
    for step in dict.fromkeys(steps):
        try:
            status = os.lstat(step)
        except FileNotFoundError:
            _absent(step)
            return None
        mode = status.st_mode
        if stat.S_ISLNK(mode) or getattr(status, "st_file_attributes", 0) & _REPARSE_POINT:
            raise OSError(f"{step}: a symlink or junction")
    return mode


_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: a Windows junction is not S_ISLNK


def _still_at(fd: int, directory: Path) -> bool:
    """The held directory is still the one at its path in the namespace: no link on the way,
    and the same device and inode as the fd. A rename or rebind after the open fails this."""
    try:
        if not present(directory, stat.S_IFDIR):
            return False
        held, there = os.fstat(fd), os.lstat(directory)
    except OSError:
        return False  # unknown is not "still there"
    return (held.st_dev, held.st_ino) == (there.st_dev, there.st_ino)


def _absent(path: Path) -> None:
    # Missing is absent only below a readable directory. The walk already refused links inside the
    # namespace; above it (a linked workspace or state root) a link to a directory is followed.
    for ancestor in path.parents:
        try:
            os.stat(ancestor)
        except FileNotFoundError:
            continue
        if not os.access(ancestor, os.R_OK | os.X_OK):
            raise OSError(f"{path}: {ancestor} is not a readable directory")
        return
    raise OSError(f"{path}: no existing ancestor")


_VERSION_KEYS = ("status", "decision_claim_id", "generation")


def record_version(record) -> dict:
    """The compare-and-set key of a record snapshot: what a writer read it as."""
    record = record if isinstance(record, dict) else {}
    return {key: record.get(key, 0 if key == "generation" else None) for key in _VERSION_KEYS}


def update_record(path: Path, record: dict, field: str = "withheld_body", *,
                  expect: dict) -> bool:
    """Rewrite a live record under the ledger lock, only while it owns its id and is
    still the version `expect` names: a stale snapshot never overwrites a newer one."""
    path = Path(path)
    with _ledger_lock(_record_directory(path)):
        return _write_owned_record(path, record, field, expect)


def _write_owned_record(path: Path, record: dict, field: str, expect: dict,
                        landed: "list | None" = None) -> bool:
    """The one writer of a live record; the caller holds the ledger lock. Refuses a
    record that does not own its id, would change its body, or changed since read."""
    try:
        if not present(path, stat.S_IFREG) or not _owns_its_id(path, field):
            return False
        current = read_json_strict(path)
    except (OSError, ValueError):
        return False  # ownership that cannot be read is refused, never written
    if (not isinstance(current, dict) or record.get(field) != current.get(field)
            or record_version(current) != expect):
        return False
    record["generation"] = int(current.get("generation") or 0) + 1
    temporary, replaced = None, False
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        replaced = True
        _fsync_directory(path.parent)  # success is reported only once the rename is durable
    except Exception:  # noqa: BLE001 — any failure reports whether the rename happened
        if landed is not None:
            landed.append(replaced)  # whether the new record may be on disk
        return False
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return True


def _ensure_dir(directory: Path) -> Path:
    """`directory` as a real directory, created only when genuinely absent; a link or an odd entry
    on the way raises. Moves use verified directory fds or are refused; other writes check the
    path first, so a concurrent swapper with state-dir write access could redirect those."""
    if not present(directory, stat.S_IFDIR):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not present(directory, stat.S_IFDIR):
            raise FileNotFoundError(2, "No such file or directory", str(directory))
    return directory


def _durable_move(source: Path, target: Path) -> None:
    """Move a record so its only copy is never deleted before the new one is durable:
    link, sync the target directory, then unlink and sync the source directory."""
    if not present(source, stat.S_IFREG):                # a real file, reached through no link
        raise FileNotFoundError(2, "No such file or directory", str(source))
    _ensure_dir(target.parent)
    if not hasattr(os, "O_DIRECTORY"):                   # no fd confinement here: refuse, never by path
        raise OSError(f"{source}: a confined move needs directory fds; the record stays")
    source_fd = _open_dir(source.parent)
    try:
        target_fd = _open_dir(target.parent)             # a folder swapped for a link is refused here
        try:
            os.link(source.name, target.name, src_dir_fd=source_fd, dst_dir_fd=target_fd,
                    follow_symlinks=False)               # no-clobber: an existing file stays
            _fsync_fd(target_fd)                         # the directory linked into, by fd, not name
            if not (_still_at(target_fd, target.parent) and _still_at(source_fd, source.parent)):
                os.unlink(target.name, dir_fd=target_fd)  # rebound after the open: undo, keep the source
                _fsync_fd(target_fd)
                raise OSError(f"{target.parent}: moved after it was opened; the record stays")
            os.unlink(source.name, dir_fd=source_fd)
            _fsync_fd(source_fd)
            # The source is gone now, so only the target's place decides whether the copy escaped.
            if not _still_at(target_fd, target.parent):    # rebound between the check and the unlink
                os.link(target.name, source.name, src_dir_fd=target_fd, dst_dir_fd=source_fd,
                        follow_symlinks=False)
                _fsync_fd(source_fd)                     # the record is back before the copy goes
                os.unlink(target.name, dir_fd=target_fd)
                _fsync_fd(target_fd)
                raise OSError(f"{target.parent}: moved during the move; the record was put back")
        finally:
            os.close(target_fd)
    finally:
        os.close(source_fd)


def archive_record(path: Path, field: str = "withheld_body") -> bool:
    """Move a resolved record into its directory's `archive/`, under the ledger
    lock, never over an existing decision and only for the body its id was
    issued to: the id stays reserved, so it is never issued again."""
    path = Path(path)
    with _ledger_lock(_record_directory(path)):
        try:
            if not present(path, stat.S_IFREG) or not _owns_its_id(path, field):
                return False
        except OSError:
            return False  # stays live; it acts only once its ownership reads again
        archive = path.parent / "archive"
        try:
            os.chmod(_ensure_dir(archive), 0o700)
            _durable_move(path, archive / path.name)
        except OSError:
            return False
    return True


CONFLICTS_DIR = "conflicts"


def actionable_records(directory: Path, field: str = "withheld_body"):
    """The live records of `directory` that may act, under the ledger lock, and the
    ones frozen instead: a record whose id is reserved to another body, or whose
    id already has an archived decision, moves to `conflicts/` for the owner and
    never acts. Returns (actionable [(path, record)], frozen [path])."""
    directory = Path(directory)
    actionable, frozen = [], []
    if not present(directory, stat.S_IFDIR):
        return actionable, frozen
    with _ledger_lock(directory):
        for path in entries(directory, "*.json"):
            try:
                record = read_json_strict(path)
            except OSError:
                continue                                 # unknown (a link, unreadable): never acts
            except ValueError:
                record = None                            # malformed: never acts, frozen below
            if isinstance(record, dict) and _owns_its_id(path, field):
                actionable.append((path, record))
                continue
            if _proven_superseded(path, field):
                continue                                 # reconciled by the caller, never frozen
            target = directory / CONFLICTS_DIR / f"{path.stem}.{_record_digest(path, field)[:16]}.json"
            try:
                _durable_move(path, target)
                frozen.append(target)
            except OSError:
                continue
    return actionable, frozen


def _find_record(path_of, state_dir: Path, task_id: str, body: str, field: str) -> Path:
    """Where the record of this exact body is, without issuing an id."""
    digest, unissued = _body_digest(body), None
    first = path_of(state_dir, task_id)
    if not present(first.parent):
        return first
    with _ledger_lock(first.parent):
        for path in _candidates(path_of, state_dir, task_id, body):
            owner = _issued_to(path, field)
            if owner == digest:
                found = _record_of(path, digest, field)
                if found is not None:
                    return found
            elif owner is None and unissued is None:
                unissued = path                     # where this body's record would be issued
    return unissued or path_of(state_dir, task_id, digest)


def _issue_record(path_of, state_dir: Path, task_id: str, body: str, field: str,
                  make_payload) -> "tuple[Path | None, bool]":
    """The record for this exact body, issuing an id only once, ever: an id
    reserved for this body returns its record (its decision first) and is never
    written again; one reserved for another body is skipped. Returns (path, saved);
    runs under the ledger lock, as does every archive, so no transition interleaves."""
    first = path_of(state_dir, task_id)
    digest = _body_digest(body)
    with _ledger_lock(first.parent):
        for path in _candidates(path_of, state_dir, task_id, body):
            owner = _issued_to(path, field)
            if owner is None and _reserve(path, digest):
                return path, _write_artifact(path, make_payload(path), field)
            if (owner or _issued_to(path, field)) == digest:
                found = _record_of(path, digest, field)
                if found is not None:
                    return found, True
        return None, False


def withheld_review_artifact(state_dir: Path, task_id: str, body: str) -> Path:
    """Where the private review of this exact withheld body lives."""
    return _find_record(withheld_review_path, state_dir, task_id, body, "withheld_body")


def is_attach_only_withhold(body: str) -> bool:
    """True when the ONLY control markers are attachments — the one withhold
    class an owner can release per-instance (redirects are never releasable)."""
    kinds = {action.kind for action in parse_markers(body or "").actions}
    return "attach" in kinds and "redirect" not in kinds


def quarantined_attachment_path(state_dir: Path, task_id: str, body_digest: str = "") -> Path:
    return Path(state_dir) / SUPPRESSED_RESULT_DIR / f"qa_{withheld_review_id(task_id, body_digest)}.json"


def journal_quarantined_attachment(body: str, state_dir: Path, task_id: str,
                                   now=None) -> bool:
    """Quarantine a withheld attach-only result so an owner word can release it.

    Best-effort by design: the withhold already happened fail-closed upstream;
    this record only enables the later release, so an unwritable record costs
    the release option, never the withhold."""
    timestamp = float(time.time() if now is None else now)
    directory = Path(state_dir) / SUPPRESSED_RESULT_DIR
    try:
        os.chmod(_ensure_dir(directory), 0o700)
    except OSError:
        return False
    def payload(record: Path) -> dict:
        return {
            "schema_version": 1,
            "record_id": record.stem[len("qa_"):],
            "task_id": task_id,
            "status": "withheld_attachment_pending",
            "created_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
            "withheld_body": body,
        }
    try:
        return _issue_record(quarantined_attachment_path, state_dir, task_id, body,
                             "withheld_body", payload)[1]
    except OSError:
        # best-effort for real: a failed record costs the release option,
        # never the already-decided withhold and never the delivery loop
        return False


def _write_artifact(path: Path, payload: dict, field: str) -> bool:
    """True only when `path` holds a record of this payload's body: an existing
    record of another body is never claimed as written."""
    if present(path, stat.S_IFREG):
        return _holds(path, field, payload[field])
    fd, temporary = tempfile.mkstemp(prefix=".withheld-", suffix=".tmp", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            if os.name == "nt":
                # Windows rename is atomic and refuses to replace an existing file.
                os.rename(temporary, path)
            else:
                os.link(temporary, path)
            _fsync_directory(path.parent)
        except FileExistsError:
            pass
        except OSError:
            return False
        return _holds(path, field, payload[field])
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def materialize_withheld_verdict(verdict: TeamResultVerdict, body: str,
                                 state_dir: Path, task_id: str, context=None,
                                 agent_id: str = "", now=None) -> TeamResultVerdict:
    """Persist a leak verdict for private owner review and suppress the room post."""
    if verdict.kind != VERDICT_LEAK:
        return verdict
    timestamp = float(time.time() if now is None else now)
    directory = Path(state_dir) / WITHHELD_RESULT_DIR
    bounded = _bounded_context(context)
    try:
        os.chmod(_ensure_dir(directory), 0o700)
    except OSError:
        return TeamResultVerdict(VERDICT_LEAK, TEAM_LEAK_RESULT_UNSAVED, verdict.reason)
    def payload(artifact: Path) -> dict:
        return {
            "schema_version": 2,
            "review_id": artifact.stem,
            "status": "pending_dm",
            "created_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
            "task_id": task_id,
            "agent_id": agent_id,
            "reason": verdict.reason,
            "context": bounded,
            "withheld_body": body,
        }
    try:
        artifact, saved = _issue_record(withheld_review_path, state_dir, task_id, body,
                                        "withheld_body", payload)
    except Exception:  # noqa: BLE001 — storage failure must remain fail-closed
        artifact, saved = None, False
    if not saved:
        return TeamResultVerdict(VERDICT_LEAK, TEAM_LEAK_RESULT_UNSAVED, verdict.reason)
    if artifact.parent.name == "archive":
        return TeamResultVerdict(
            VERDICT_SUPPRESS, "[no-send]", f"{verdict.reason}; already decided in owner review {artifact.stem}")
    return TeamResultVerdict(
        VERDICT_SUPPRESS, "[no-send]", f"{verdict.reason}; pending private owner review")


def withheld_decision_lock(directory: Path):
    """The ledger lock: decision claims and publication checks serialize with every
    id issue, record write and archive in the directory."""
    return _ledger_lock(directory)


def _live_undecided_record(path: Path) -> "dict | None":
    # An archived decision for the same review always wins over a live record.
    if present(_archived(path)):
        return None
    try:
        record = read_json_strict(path)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _issued_claim(path: Path, claim_id: str) -> Path:
    return path.parent / "claims" / f"{path.stem}.{claim_id}"


def withheld_claim_publishable(path: Path, claim_id) -> bool:
    """True only when claim_withheld_decision issued this claim id for this review
    (its ledger entry) and the live record still carries it as a pending release."""
    path = Path(path)
    if not (isinstance(claim_id, str) and len(claim_id) == 32
            and all(c in "0123456789abcdef" for c in claim_id)):
        return False
    with withheld_decision_lock(_record_directory(path)):
        record = _live_undecided_record(path)
        issued = present(_issued_claim(path, claim_id), stat.S_IFREG)
    return (issued and record is not None and record.get("status") == "publish_pending"
            and record.get("decision") == "false_positive"
            and record.get("decision_claim_id") == claim_id and decision_is_durable(record))


def decision_is_durable(record) -> bool:
    """A decided record acts (POST, card edit) only once its decision is durable; a record
    without the flag predates it and was written durably."""
    return isinstance(record, dict) and record.get("decision_durable") is not False


def archived_withheld_decision(path: Path) -> "dict | None":
    """The archived decision that owns this review, or None if there is none; {}
    when it exists but is unreadable. A live copy beside it must never act."""
    path = Path(path)
    archived = _archived(path)
    with withheld_decision_lock(_record_directory(path)):
        try:
            if not present(archived):
                return None
            record = read_json_strict(archived)
        except (OSError, ValueError):
            return {}
    return record if isinstance(record, dict) else {}


# The reconcile table: the only archived (status, decision) pairs a live copy may be
# reconciled to. Any other pair, or a malformed archive, is frozen for the owner.
ARCHIVED_OUTCOMES = {
    ("kept_private", "sensitive"): "kept_private",
    ("published", "false_positive"): "published",
    ("publish_failed", "false_positive"): "publish_failed",
}


def archived_outcome(archived) -> "str | None":
    """The outcome an archived decision proves, or None when it proves none."""
    if not isinstance(archived, dict):
        return None
    key = (archived.get("status"), archived.get("decision"))
    return ARCHIVED_OUTCOMES.get(key) if all(isinstance(k, str) for k in key) else None


def _proven_superseded(path: Path, field: str) -> bool:
    """A live copy of the very body its archived decision holds, and that decision
    proves an outcome: the one case reconciled instead of frozen. Lock held by caller."""
    archived = _archived(path)
    if not (present(path, stat.S_IFREG) and present(archived, stat.S_IFREG)):
        return False
    try:
        decision = read_json_strict(archived)
    except (OSError, ValueError):
        return False
    digest = _record_digest(path, field)
    return (archived_outcome(decision) is not None and digest != "unreadable"
            and digest == _record_digest(archived, field)
            and _issued_to(path, field) == digest)


def proven_superseded_record(path: Path, field: str = "withheld_body") -> bool:
    path = Path(path)
    with _ledger_lock(_record_directory(path)):
        return _proven_superseded(path, field)


def retire_superseded_record(path: Path, field: str = "withheld_body") -> bool:
    """Move a live copy that a proven archived decision outranks into
    archive/superseded/; anything else stays for the freeze. The archive is untouched."""
    path = Path(path)
    with _ledger_lock(_record_directory(path)):
        if not _proven_superseded(path, field):
            return False
        target = path.parent / "archive" / "superseded"
        _durable_move(path, target / f"{path.stem}.{os.urandom(4).hex()}.json")
        return True


def claim_withheld_decision(path: Path, updates: dict) -> "dict | None":
    """Apply the owner's decision only to a record still awaiting it and owning its
    id; None means it was already decided (or is gone), so the reply changes nothing."""
    path = Path(path)
    with _ledger_lock(_record_directory(path)):
        record = _live_undecided_record(path)
        if (record is None or record.get("status") != "awaiting_owner"
                or not _owns_its_id(path, "withheld_body")):
            return None
        if entries(_issued_claim(path, "*").parent, f"{path.stem}.*"):
            return None  # an earlier claim whose decision did not survive: never a second one
        claim_id = os.urandom(16).hex()
        ledger = _issued_claim(path, claim_id)
        _ensure_dir(ledger.parent)
        os.close(os.open(ledger, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                         0o600))
        _fsync_directory(ledger.parent)  # the ledger entry is durable before the record names it
        expect, landed = record_version(record), []
        record.update(updates, decision_claim_id=claim_id, decision_durable=False)
        if _write_owned_record(path, record, "withheld_body", expect, landed):
            # Synced, so the decision is durable; only now may anything act on it.
            durable = dict(record, decision_durable=True)
            return durable if _write_owned_record(path, durable, "withheld_body",
                                                  record_version(record)) else record
        if not any(landed):
            try:
                ledger.unlink(missing_ok=True)  # nothing was written: the owner may answer again
                _fsync_directory(ledger.parent)
            except Exception:  # noqa: BLE001 — a left claim is reported as lost, never silent
                pass
        return None


def establish_decision(path: Path) -> bool:
    """The durability barrier: sync a decided record whose decision is not yet durable and
    mark it so; True when it is durable and may act."""
    path = Path(path)
    with _ledger_lock(_record_directory(path)):
        try:
            current = read_json_strict(path)
        except (OSError, ValueError):
            return False
        if not isinstance(current, dict):
            return False
        if decision_is_durable(current):
            return True
        try:
            _fsync_directory(path.parent)
        except OSError:
            return False
        return _write_owned_record(path, dict(current, decision_durable=True), "withheld_body",
                                   record_version(current))


def issued_claims(path: Path) -> list:
    """The claim ids issued for this review, oldest name first."""
    path = Path(path)
    return sorted(p.name[len(path.stem) + 1:] for p in
                  entries(_issued_claim(path, "*").parent, f"{path.stem}.*"))


def record_release_receipt(path: Path, claim_id, event_id: str) -> bool:
    """Durably note on a pending release the event id that confirmed it, before the outbox
    is told it was delivered; the outbox itself keeps no receipt."""
    path = Path(path)
    with _ledger_lock(_record_directory(path)):
        try:
            current = read_json_strict(path)
        except (OSError, ValueError):
            return False
        if not isinstance(current, dict) or current.get("decision_claim_id") != claim_id:
            return False
        if current.get("status") == "published":
            return True  # already settled by this claim's release
        if current.get("status") != "publish_pending":
            return False
        return _write_owned_record(path, dict(current, release_event_id=event_id),
                                   "withheld_body", record_version(current))


def decision_lost(path: Path) -> bool:
    """True when a claim was issued for this still-undecided review but its decision did
    not survive: no later decision is accepted, and the owner must be told."""
    path = Path(path)
    with _ledger_lock(_record_directory(path)):
        record = _live_undecided_record(path)
        return (record is not None and record.get("status") == "awaiting_owner"
                and bool(entries(_issued_claim(path, "*").parent, f"{path.stem}.*")))


def _fsync_directory(directory: Path) -> None:
    if os.name != "nt":
        fd = os.open(directory, os.O_RDONLY)
        try:
            _fsync_fd(fd)
        finally:
            os.close(fd)


def _fsync_fd(fd: int) -> None:
    os.fsync(fd)


def suppressed_record_path(state_dir: Path, task_id: str, body_digest: str = "") -> Path:
    return Path(state_dir) / SUPPRESSED_RESULT_DIR / f"{withheld_review_id(task_id, body_digest)}.json"


def suppressed_record_for(state_dir: Path, task_id: str, body: str) -> Path:
    """Where the suppression record of this exact body lives."""
    return _find_record(suppressed_record_path, state_dir, task_id, body, "suppressed_body")


def journal_suppressed_result(verdict: TeamResultVerdict, body: str,
                              state_dir: Path, task_id: str, context=None,
                              agent_id: str = "", now=None) -> TeamResultVerdict:
    """Record a guarded-tier suppression, then hand the body back unchanged.

    Accountability, not confidentiality: a skip marker moves no data, so the
    guard honours it on every tier and the durable record is what keeps it
    auditable. Fail-CLOSED -- if the record cannot be written the notice takes
    the body's place, so a close is never both silent and unrecorded.
    """
    if verdict.kind != VERDICT_DELIVER:
        return verdict                     # leak/suppress already decided
    timestamp = float(time.time() if now is None else now)
    directory = Path(state_dir) / SUPPRESSED_RESULT_DIR
    try:
        os.chmod(_ensure_dir(directory), 0o700)
    except OSError:
        return TeamResultVerdict(VERDICT_SUPPRESS, TEAM_SUPPRESS_RESULT,
                                 "suppression record unwritable")
    def payload(record: Path) -> dict:
        return {
            "schema_version": 1,
            "record_id": record.stem,
            "status": "suppressed_silent_close",
            "created_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
            "task_id": task_id,
            "agent_id": agent_id,
            "reason": verdict.reason,
            "context": _bounded_context(context),
            "suppressed_body": body,
        }
    try:
        saved = _issue_record(suppressed_record_path, state_dir, task_id, body,
                              "suppressed_body", payload)[1]
    except Exception:  # noqa: BLE001 -- storage failure must remain fail-closed
        saved = False
    if not saved:
        return TeamResultVerdict(VERDICT_SUPPRESS, TEAM_SUPPRESS_RESULT,
                                 "suppression record unwritable")
    return verdict


def classify_result_for_tier(body: str, tier, repo: Path,
                             secret_filter=None,
                             scan_sensitive_data: bool = True,
                             allow_attach: bool = False,
                             honor_suppressions: bool = True,
                             attach_roots=()) -> TeamResultVerdict:
    """The guard-owned policy verdict. Adapters apply transport mechanics only;
    re-deciding (or bypassing) this classification in a bridge is a boundary
    violation, not an implementation choice."""
    if not is_guarded_tier(tier):
        return TeamResultVerdict(VERDICT_DELIVER, body, None)
    if honor_suppressions and is_suppression_only(body):
        # Above the scan on purpose: an undelivered body has nothing to leak —
        # marker-honouring adapters only (publishers pass honor_suppressions=False).
        return TeamResultVerdict(VERDICT_DELIVER, body, None)
    try:
        return TeamResultVerdict(
            VERDICT_DELIVER,
            scan_team_result(body, repo, secret_filter, scan_sensitive_data,
                         allow_attach=allow_attach, attach_roots=attach_roots),
            None)
    except TeamResultLeakError as exc:
        if str(exc) == "result delivery control marker":
            return TeamResultVerdict(VERDICT_LEAK, TEAM_LEAK_RESULT_MARKER, str(exc))
        return TeamResultVerdict(VERDICT_LEAK, TEAM_LEAK_RESULT, str(exc))
    except Exception as exc:
        # Scanner unavailable is fail-CLOSED: an unscannable guarded result is
        # withheld, never delivered on the assumption that it was probably fine.
        return TeamResultVerdict(VERDICT_LEAK, TEAM_LEAK_RESULT,
                                 f"{SCANNER_UNAVAILABLE}: {exc}")


def guard_result_for_tier(body: str, tier, repo: Path, secret_filter=None,
                          scan_sensitive_data: bool = True, *,
                          suppress_journal=None, allow_attach: bool = False,
                          honor_suppressions: bool = True, attach_roots=()):
    """Consumer-facing gate: returns (safe_body, withheld_reason).

    Returns a body rather than raising, so a caller cannot deliver the raw text
    by catching an exception -- the safe body is the only one it is handed.
    Derived from classify_result_for_tier so the verdict has exactly one owner.

    suppress_journal: `(state_dir, task_id)` from an adapter that can write the
    record. A guarded suppression is journaled there and the marker is honoured;
    an adapter that omits it honours the marker with no record of its own.
    """
    verdict = classify_result_for_tier(
        body, tier, repo, secret_filter, scan_sensitive_data,
        allow_attach=allow_attach, honor_suppressions=honor_suppressions,
        attach_roots=attach_roots)
    # An unreadable tier is not a decision to record: journalling it can only
    # fail the same way the tier read did, and its notice replaces the answer.
    if (suppress_journal is not None and is_guarded_tier(tier)
            and tier != TIER_UNREADABLE and is_suppression_only(body)):
        state_dir, task_id = suppress_journal
        verdict = journal_suppressed_result(verdict, body, state_dir, task_id)
    if (suppress_journal is not None and verdict.kind == VERDICT_LEAK
            and is_attach_only_withhold(body)):
        state_dir, task_id = suppress_journal
        journal_quarantined_attachment(body, state_dir, task_id)
    return verdict.body, verdict.reason
