"""A result's Activity labels: the `<name>.meta.json` side file beside `<name>.txt` in results/.

`{"title": ..., "outcome": ...}` — the title says what the task is (imperative, at most
TITLE_MAX characters), the outcome what came of it (past tense, asked at most OUTCOME_ASK,
accepted up to OUTCOME_MAX). Both are one line with no control characters. The writer publishes
the side file before the result, so the bridge that reads the result finds its labels; a bridge
without this module ignores them.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import unicodedata
from pathlib import Path

TITLE_MAX = 60
OUTCOME_MAX = 150
# What agents are asked for; OUTCOME_MAX is the hard limit, with headroom above it.
OUTCOME_ASK = 120
SUFFIX = ".meta.json"
LIMITS = {"title": TITLE_MAX, "outcome": OUTCOME_MAX}
# Far above two bounded labels: a larger file is not one this writer made.
_READ_MAX = 4096


def side_path(result_file: Path) -> Path:
    """`results/<name>.meta.json` for `results/<name>.txt`."""
    result_file = Path(result_file)
    return result_file.with_name(result_file.stem + SUFFIX)


def clean(value) -> str:
    """One line: control and separator characters become spaces, runs of space collapse."""
    if not isinstance(value, str):
        return ""
    chars = (" " if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") else c for c in value)
    return " ".join("".join(chars).split())


def fit(value, limit: int) -> str:
    """`clean(value)`, cut to `limit` characters with a trailing ellipsis when longer."""
    text = clean(value)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def check(title=None, outcome=None) -> "tuple[dict, str]":
    """(labels, "") with the empty ones left out, or ({}, why) when one is over its limit."""
    labels = {}
    for key, value in (("title", title), ("outcome", outcome)):
        text = clean(value)
        if len(text) > LIMITS[key]:
            return {}, f"--{key} over {LIMITS[key]} characters"
        if text:
            labels[key] = text
    return labels, ""


def write(result_file: Path, labels: dict) -> "Path | None":
    """Atomically publish `labels` beside `result_file`. With none, remove any side file left
    there, so a retried result never carries an earlier attempt's labels; then None."""
    labels = {k: clean(labels.get(k)) for k in LIMITS if clean(labels.get(k))}
    target = side_path(result_file)
    if not labels:
        target.unlink(missing_ok=True)
        return None
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(labels, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(OSError):
            Path(tmp).unlink(missing_ok=True)
    return target


def archive(results_dir: Path, stem: str, archive_dir: Path, now: float) -> None:
    """Move `<stem>.meta.json` to `archive_dir/<stem>-<now>.meta.json`; absent is done.
    Raises OSError when it could not be moved."""
    side = Path(results_dir) / f"{stem}{SUFFIX}"
    Path(archive_dir).mkdir(parents=True, exist_ok=True)
    try:
        os.replace(side, Path(archive_dir) / f"{stem}-{int(now)}{SUFFIX}")
    except FileNotFoundError:
        pass


def read(path: Path) -> dict:
    """The labels at a side file, each cleaned and fitted to its limit; {} when absent or unreadable."""
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
    if not isinstance(doc, dict):
        return {}
    return {k: fit(doc.get(k), limit) for k, limit in LIMITS.items() if clean(doc.get(k))}
