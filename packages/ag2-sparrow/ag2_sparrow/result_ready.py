#!/usr/bin/env python3
"""Readiness of a `results/<task-id>.txt` file, for every delivery consumer.

The single owner of "is this result file ready to send?". Adapters bind their
own resolved results directory and keep only provider-specific delivery; they
must not re-implement the check.

A result path can exist before it holds an answer. The core writes
temp-file-then-rename, but it is an LLM driving a shell and will create the
destination for unrelated reasons, and a partial write can be observed
mid-content. File existence is therefore not readiness: a consumer that treats
it as readiness delivers an empty message and archives the task as done, which
strands the real answer written moments later.

A deliberately empty reply is expressed with the `[no-send]` marker, parsed by
`result_markers`, not by writing an empty file.
"""
from __future__ import annotations

import importlib.util
import os
import re
import tempfile
import time

from pathlib import Path

try:
    from .publication import publish_result
except ImportError:
    # Loaded by file path (tests, standalone tools): bind the sibling directly.
    _spec = importlib.util.spec_from_file_location("publication", Path(__file__).with_name("publication.py"))
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    publish_result = _mod.publish_result

try:
    from .result_markers import parse_markers
except ImportError:
    try:
        from result_markers import parse_markers
    except ImportError:
        # Loaded by file path from src/delivery/: the marker owner is one level up.
        _mspec = importlib.util.spec_from_file_location(
            "result_markers", Path(__file__).resolve().parent.parent / "result_markers.py")
        _mmod = importlib.util.module_from_spec(_mspec)
        _mspec.loader.exec_module(_mmod)
        parse_markers = _mmod.parse_markers

__all__ = ["read_ready_result", "is_ready_body", "retire_claim_if_unchanged",
           "sweep_retired"]


def is_ready_body(text: str | None) -> bool:
    """True when `text` is a deliverable body (non-empty after stripping)."""
    return bool(text and text.strip())


def read_ready_result(path: str | Path) -> str | None:
    """Return the stripped body of `path`, or None when it is not ready.

    None covers missing, unreadable and empty-or-whitespace-only files. Callers
    skip on None and retry on a later pass — the file is not consumed, so a
    result that lands between passes is still delivered.
    """
    p = Path(path)
    try:
        body = p.read_text()
    except (OSError, UnicodeDecodeError):
        # Missing, unreadable, or a partial write mid-character. Never
        # deliverable, and readable again on a later pass.
        return None
    body = body.strip()
    return body if body else None


def retire_claim_if_unchanged(claim: str | Path, delivered: str) -> bool:
    """Retire `claim` only while it still holds exactly the body that was sent.

    A claim is a hard link, so a producer holding the original fd keeps
    appending to THIS inode after the consumer read it. A destructive retire
    (unlink) turns any append that lands after the last check into bytes that
    were never guarded and never delivered — and no check-then-unlink closes
    that window, because the producer is not party to the check.

    So retirement never destroys: the claim is MOVED (atomic rename) into a
    sibling `retired/` directory, out of every consumer's glob, and the moved
    inode is re-read. If the body grew in the meantime the move is undone and
    False is returned: the caller releases the claim and a later pass sends it
    whole. An append that lands after that re-read is preserved in `retired/`
    rather than lost. False also covers unreadable, partial or vanished
    claims — never retire what cannot be verified.
    """
    p = Path(claim)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return True
    except OSError:
        # Unreadable: never destroy bytes whose content cannot be verified.
        return False
    try:
        body = raw.decode()
    except UnicodeDecodeError:
        # A partial write mid-character. Bytes EXIST and are undelivered, so
        # this is the opposite of "nothing to retire" — keep the claim.
        return False
    stripped = body.strip()
    if not stripped:
        # Emptied under us: nothing left to retire or resend.
        p.unlink(missing_ok=True)
        return True
    if stripped != delivered:
        return False
    try:
        if p.stat().st_size != len(raw):
            return False
    except OSError:
        return False
    retired = _retired_path(p)
    try:
        retired.parent.mkdir(parents=True, exist_ok=True)
        p.replace(retired)
    except OSError:
        # Vanished or unmovable: release rather than act on a path we cannot verify.
        return False
    try:
        final = retired.read_bytes()
        if final.decode().strip() != delivered:
            retired.replace(p)   # grew between the check and the move: undo, resend whole
            return False
    except (OSError, UnicodeDecodeError):
        try:
            retired.replace(p)
        except OSError:
            pass
        return False
    # Record how much of the inode was delivered: bytes a stale descriptor
    # appends after this point are republished by sweep_retired, not marooned.
    try:
        _write_atomic(_delivered_marker(retired), str(len(final)))
    except OSError:
        # Without the marker late bytes are indistinguishable: undo instead.
        try:
            retired.replace(p)
        except OSError:
            pass
        return False
    return True


def _delivered_marker(retired: Path) -> Path:
    return retired.with_name(retired.name + ".delivered")


def _write_atomic(path: Path, text: str) -> None:
    """Whole-or-nothing text write: a torn marker reads as a bad int and the
    inode is then aged out with its late bytes unpublished."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


_SWEEP_SUFFIX = ".sweeping"
_CLAIM_SUFFIXES = (".txt", ".sending")
_TYPED_STEM_RE = re.compile(r"^(?P<base>.+?)(?P<dest>\.to-[a-z0-9_-]+)?\Z")
_SCRATCH_RE = re.compile(r"^\.(?P<inode>.+)\.late-(?P<offset>\d+)\.tmp$")


def _late_name(inode: Path, now: float) -> str:
    """The republished remainder keeps the typed destination where the
    filename grammar reads it: at the end, before .txt."""
    stem = inode.name
    for suffix in _CLAIM_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    m = _TYPED_STEM_RE.match(stem)
    base, dest = (m.group("base"), m.group("dest") or "") if m else (stem, "")
    if base.startswith("proactive-"):
        base = base[len("proactive-"):]
    return f"proactive-late-{base}-{int(now)}{dest}.txt"


def _carried_redirect(delivered_prefix: bytes) -> str:
    """The routing the delivered head carried, read by the marker owner so a
    remainder can never widen it: dm-only stays dm-only, a redirect stays."""
    parsed = parse_markers(delivered_prefix.decode(errors="replace"))
    kinds = {a.kind: a.value for a in parsed.actions}
    if "dm-only" in kinds:
        return "[dm-only]\n"
    if kinds.get("redirect"):
        return f"[channel: {kinds['redirect']}]\n"
    return ""


def _scratch_path(inode: Path, results: Path, delivered: int) -> Path:
    """The staged remainder is the journal: dotted (no consumer globs it) and
    keyed by the cursor it starts at, so a replay finds the same entry."""
    return results / f".{inode.name}.late-{delivered}.tmp"


def _lease_age(claim: Path, now: float) -> float | None:
    try:
        return now - float(claim.read_text())
    except (OSError, ValueError):
        try:
            return now - claim.stat().st_mtime
        except OSError:
            return None


def _claim_for_sweep(inode: Path, now: float, quiesce_s: float) -> Path | None:
    """One sweeper per inode: an exclusive stamped lease, so its age is the
    claim's own and not the inode's. A stale lease is renamed away before it
    is replaced — only one breaker wins the rename, so two cannot both sweep."""
    claim = inode.with_name(inode.name + _SWEEP_SUFFIX)
    age = _lease_age(claim, now)
    if age is not None and age >= quiesce_s:
        broken = claim.with_name(f"{claim.name}.broken-{os.getpid()}-{int(now)}")
        try:
            os.rename(claim, broken)
            broken.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(repr(now))
    except OSError:
        claim.unlink(missing_ok=True)
        return None
    if not inode.exists():
        claim.unlink(missing_ok=True)
        return None
    return claim


def _recover_staged(inode: Path, results: Path, marker: Path, now: float) -> list[Path]:
    """Finish or discard a staged remainder a crashed sweeper left behind.
    Marker past the stage's cursor means the bytes are accounted for and only
    publication is owed; otherwise the stage predates its cursor and is redone."""
    published: list[Path] = []
    try:
        delivered = int(marker.read_text()) if marker.exists() else None
    except (OSError, ValueError):
        return published
    for stage in sorted(results.glob(f".{inode.name}.late-*.tmp")):
        m = _SCRATCH_RE.match(stage.name)
        if not m or m.group("inode") != inode.name:
            continue
        offset = int(m.group("offset"))
        if delivered is not None and delivered > offset:
            target = results / _late_name(inode, now)
            try:
                os.replace(stage, target)
            except OSError:
                continue
            published.append(target)
        else:
            stage.unlink(missing_ok=True)
    return published


def sweep_retired(results_dir: str | Path, quiesce_s: float = 600.0,
                  now: float | None = None) -> list[Path]:
    """Managed lifecycle for retired inodes: republish any bytes appended
    after retirement as a new proactive file, and drop an inode only once it
    has been quiescent for `quiesce_s`. Returns the republished paths.

    A producer holding the original descriptor can append after the retire
    re-read; the claim protocol cannot prove writer completion, so the
    remainder is delivered as its own message instead of being preserved in
    a directory no consumer reads. A retired file with no marker predates
    this lifecycle: its delivered length is unknown, so it is never
    republished (a duplicate is worse than the loss) and only aged out.

    One sweeper per inode. A remainder is staged under a dotted name keyed by
    its cursor, the marker advances, then the stage is published: a crash at
    any point is replayed by `_recover_staged` — never lost, never twice.
    """
    results = Path(results_dir)
    retired_dir = results / "retired"
    if not retired_dir.is_dir():
        return []
    now = time.time() if now is None else now
    published: list[Path] = []
    for inode in sorted(retired_dir.iterdir()):
        name = inode.name
        if name.startswith(".") or not name.endswith(_CLAIM_SUFFIXES):
            continue
        claim = _claim_for_sweep(inode, now, quiesce_s)
        if claim is None:
            continue
        try:
            marker = _delivered_marker(inode)
            published.extend(_recover_staged(inode, results, marker, now))
            try:
                raw = inode.read_bytes()
                delivered = int(marker.read_text()) if marker.exists() else None
                mtime = inode.stat().st_mtime
            except (OSError, ValueError):
                continue
            if delivered is not None and len(raw) > delivered:
                # A split multibyte character is a still-growing write: leave
                # the cursor where it is and read the whole character later.
                try:
                    remainder = raw[delivered:].decode("utf-8").strip()
                except UnicodeDecodeError:
                    continue
                if remainder:
                    body = _carried_redirect(raw[:delivered]) + remainder + "\n"
                    stage = _scratch_path(inode, results, delivered)
                    target = results / _late_name(inode, now)
                    try:
                        _write_atomic(stage, body)
                        _write_atomic(marker, str(len(raw)))
                    except OSError:
                        stage.unlink(missing_ok=True)
                        continue
                    try:
                        os.replace(stage, target)
                    except OSError:
                        continue  # the stage + advanced marker replay next sweep
                    published.append(target)
                    continue
                try:
                    _write_atomic(marker, str(len(raw)))
                except OSError:
                    pass
                continue
            if now - mtime >= quiesce_s:
                inode.unlink(missing_ok=True)
                marker.unlink(missing_ok=True)
        finally:
            claim.unlink(missing_ok=True)
    return published


def _retired_path(claim: Path) -> Path:
    """Where a retired claim's inode is kept: a sibling dir no consumer globs."""
    return claim.parent / "retired" / claim.name
