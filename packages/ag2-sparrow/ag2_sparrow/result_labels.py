"""A result's Activity labels: the `<name>.meta.json` side file beside `<name>.txt` in results/.

`{"title": ..., "outcome": ...}` — the title says what the task is (imperative, at most
TITLE_MAX characters), the outcome what came of it (past tense, asked at most OUTCOME_ASK,
accepted up to OUTCOME_MAX). Both are one line with no control characters. The writer publishes
the side file before the result, so the bridge that reads the result finds its labels; a bridge
without this module ignores them.

Labels belong to one result body: the side file carries the SHA-256 of the bytes it was written for,
and a reader given another body's digest gets none. So two publishers of one name can lose labels
but never swap them, and no writer deletes a side file: one that matches no body is inert.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
import unicodedata
from pathlib import Path

TITLE_MAX = 60
OUTCOME_MAX = 150
# What agents are asked for; OUTCOME_MAX is the hard limit, with headroom above it.
OUTCOME_ASK = 120
SUFFIX = ".meta.json"
DIGEST_KEY = "result_sha256"
LIMITS = {"title": TITLE_MAX, "outcome": OUTCOME_MAX}
# Far above two bounded labels: a larger file is not one this writer made.
_READ_MAX = 4096


def side_path(result_file: Path) -> Path:
    """`results/<name>.meta.json` for `results/<name>.txt`."""
    result_file = Path(result_file)
    return result_file.with_name(result_file.stem + SUFFIX)


_ZWNJ, _ZWJ = "\u200c", "\u200d"


def clean(value) -> str:
    """One line: control, invisible-format (bidi included) and line/paragraph separator characters
    become spaces and runs of space collapse. ZWJ and ZWNJ stay: emoji sequences and scripts need them."""
    if not isinstance(value, str):
        return ""
    chars = (" " if c not in (_ZWNJ, _ZWJ) and unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp")
             else c for c in value)
    return " ".join("".join(chars).split())


def _joins_previous(c: str) -> bool:
    """Whether `c` belongs to the grapheme before it: a combining mark, a joiner or a variation selector."""
    return (unicodedata.category(c) in ("Mn", "Mc", "Me") or c in (_ZWNJ, _ZWJ)
            or "\ufe00" <= c <= "\ufe0f" or "\U0001f3fb" <= c <= "\U0001f3ff")


def fit(value, limit: int) -> str:
    """`clean(value)` in at most `limit` code points (the server's count), cut with a trailing
    ellipsis when longer. The cut never splits a base from its marks or a ZWJ sequence."""
    text = clean(value)
    if len(text) <= limit:
        return text
    cut = limit - 1
    while cut > 0 and (_joins_previous(text[cut]) or text[cut - 1] == _ZWJ):
        cut -= 1
    return text[:cut].rstrip() + "…"


def check(title=None, outcome=None) -> "tuple[dict, list]":
    """(labels, notes): each label cleaned and fitted to its limit, empty ones left out; a note
    for each one that was cut."""
    labels, notes = {}, []
    for key, value in (("title", title), ("outcome", outcome)):
        text = fit(value, LIMITS[key])
        if text != clean(value):
            notes.append(f"--{key} was over {LIMITS[key]} characters and was cut to fit")
        if text:
            labels[key] = text
    return labels, notes


def digest(body: str) -> str:
    """The SHA-256 of `body` as a text-mode UTF-8 writer publishes it, newlines translated to the
    platform's line ending: the bytes a reader of the result file hashes."""
    return hashlib.sha256(body.replace("\n", os.linesep).encode("utf-8")).hexdigest()


def write(result_file: Path, labels: dict, body: str) -> "Path | None":
    """Atomically publish `labels` beside `result_file`, bound to `body`; None when there are none.
    The results directory is made if missing, as publishing the result would."""
    labels = {k: clean(labels.get(k)) for k in LIMITS if clean(labels.get(k))}
    if not labels:
        return None
    labels[DIGEST_KEY] = digest(body)
    target = side_path(result_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    try:
        # mkstemp already made it 0600 where modes apply; os.fchmod is missing on older Windows.
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(labels, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(OSError):
            Path(tmp).unlink(missing_ok=True)
    return target


def archive(results_dir: Path, stem: str, archive_dir: Path) -> None:
    """Move `<stem>.meta.json` to `archive_dir/<stem>-<ns>.meta.json`, a name no earlier archive
    holds; absent is done. Raises OSError when it could not be moved."""
    side = Path(results_dir) / f"{stem}{SUFFIX}"
    Path(archive_dir).mkdir(parents=True, exist_ok=True)
    ns = time.time_ns()
    while (Path(archive_dir) / f"{stem}-{ns}{SUFFIX}").exists():
        ns += 1
    try:
        os.replace(side, Path(archive_dir) / f"{stem}-{ns}{SUFFIX}")
    except FileNotFoundError:
        pass


def read(path: Path, result_digest: "str | None") -> dict:
    """The labels at a side file for the result whose bytes hash to `result_digest`, each cleaned
    and fitted to its limit; {} when absent, unreadable, or written for another body."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(_READ_MAX + 1)
    except OSError:
        return {}
    if len(raw) > _READ_MAX:
        return {}
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(doc, dict) or not result_digest or doc.get(DIGEST_KEY) != result_digest:
        return {}
    return {k: fit(doc.get(k), limit) for k, limit in LIMITS.items() if clean(doc.get(k))}
