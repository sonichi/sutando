"""Triage-queue policy for pending-questions.md: ranking, re-check verdict, dismissal.

Policy only — no file reads, no subprocesses. The HTTP adapter owns the IO (reading
the markdown, probing GitHub, serving rows) and calls in here for every decision, so
the queue's behaviour is testable without a workspace, a network, or a browser.

The queue shows ONE question at a time, so the order is the feature: whatever sorts
first is what the owner is asked about, and everything else is invisible until they
act. Two signals decide it — how long a question has waited, and how much is blocked
behind it.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from typing import Iterable, Optional

# A reference carrying its own repository: `owner/repo#123`, or a github.com pull/
# issue URL. These are the only ones whose repository is known from the text alone.
QUALIFIED_REF_RE = re.compile(
    r'(?:https?://github\.com/)?'
    r'([A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*)'
    r'(?:/(?:pull|issues)/|#)(\d{1,7})(?!\w)'
)

# `src/agent-api.py#3` is a file anchor, not `owner/repo#3`.
SOURCE_FILE_RE = re.compile(r'\.(py|ts|tsx|js|mjs|json|md|sh|txt|ya?ml|toml|cfg|ini)$', re.I)

# `#123` as a standalone token. Requires a non-word char (or string start) before the
# `#` so `foo#3` and colour literals like `#3fa9c1` are not read as issue references.
REF_RE = re.compile(r'(?:(?<=\W)|^)#(\d{1,7})(?!\w)')

# Days of waiting one blocked reference is worth when ranking. Chosen, not derived.
BLOCKED_REF_DAYS = 7

# Re-check verdicts. `stale` is advisory — a stale row is still shown and still
# answerable; nothing is ever dropped on the strength of a probe.
RECHECK_STALE = "stale"
RECHECK_BLOCKING = "blocking"

_OPEN_STATES = {"OPEN"}
_RESOLVED_STATES = {"MERGED", "CLOSED"}


def extract_refs(*texts: Optional[str]) -> list[tuple]:
    """References a question makes, as `(repo, number)` pairs in first-seen order.

    `repo` is None when the text does not say which repository the number belongs
    to. That distinction is the whole point: these questions discuss several repos
    in one prose file, so a bare `#292` resolved against whichever checkout happens
    to be running answers about a different pull request that merged long ago.
    """
    blob = "\n".join(t or "" for t in texts)
    refs: list[tuple] = []
    for match in QUALIFIED_REF_RE.finditer(blob):
        if SOURCE_FILE_RE.search(match.group(1)):
            continue
        ref = (match.group(1), int(match.group(2)))
        if ref not in refs:
            refs.append(ref)
    # A question whose qualified references all name ONE repo has said which repo it
    # is about; its bare numbers inherit that. With none or several, they stay unbound.
    named = {repo for repo, _ in refs}
    inherited = next(iter(named)) if len(named) == 1 else None
    # A qualified ref's '#' always follows a word character, which REF_RE's lookbehind
    # rejects — so the qualified matches above cannot be counted a second time here.
    for match in REF_RE.finditer(blob):
        ref = (inherited, int(match.group(1)))
        if ref not in refs:
            refs.append(ref)
    return refs


def apply_recheck(rows: list[dict], ref_states: Optional[dict[int, str]] = None) -> list[dict]:
    """Annotate each row with `refs`, `blocks` and a `recheck` verdict, in place.

    `ref_states` maps a reference number to the state the probe observed; a reference
    the probe could not decide is simply absent. Absence is never treated as resolved
    — an undecidable reference still counts as blocking, so a failed or empty probe
    leaves the ranking exactly as it was rather than silently demoting the row.
    """
    states = ref_states or {}
    for row in rows:
        refs = extract_refs(row.get("text"), row.get("detail"))
        row["refs"] = [ref_label(ref) for ref in refs]
        known = {ref: states[ref] for ref in refs if ref in states}
        resolved = [ref for ref, state in known.items() if state in _RESOLVED_STATES]
        # Unknown counts as open: the probe failing must not look like "nothing is blocked".
        row["blocks"] = len(refs) - len(resolved)
        if refs and len(resolved) == len(refs):
            row["recheck"] = {
                "status": RECHECK_STALE,
                "note": _stale_note(known, resolved),
                "refs": [ref_label(ref) for ref in resolved],
            }
        elif resolved:
            row["recheck"] = {
                "status": RECHECK_BLOCKING,
                "note": _stale_note(known, resolved),
                "refs": [ref_label(ref) for ref in resolved],
            }
        else:
            row["recheck"] = None
    return rows


def ref_label(ref: tuple) -> str:
    """How a reference is written on the card: qualified only when its repo is known."""
    repo, number = ref
    return f"{repo}#{number}" if repo else f"#{number}"


def probeable(refs: Iterable[tuple]) -> list[tuple]:
    """The references whose repository is known, and so can actually be looked up."""
    out: list[tuple] = []
    for ref in refs:
        if ref[0] and ref not in out:
            out.append(ref)
    return out


def _stale_note(known: dict, resolved: list) -> str:
    return ", ".join(f"{ref_label(ref)} {known[ref].lower()}" for ref in resolved)


def rank_key(row: dict) -> tuple:
    """Sort key: undated last, then most-waited-and-most-blocking first.

    An undated heading cannot be ranked by age and must sort LAST rather than
    default to 0, which would put the one unrankable question ahead of everything
    genuinely waiting.
    """
    age = row.get("age_days")
    blocks = row.get("blocks") or 0
    score = (age or 0) + BLOCKED_REF_DAYS * blocks
    return (age is None, -score, -(age or 0))


def rank(rows: list[dict]) -> list[dict]:
    """Return `rows` ordered for the queue. Never adds or removes a row."""
    return sorted(rows, key=rank_key)


# --- dismissal ---------------------------------------------------------------

# Permanent by design: a changed situation is written as a NEW section, which
# hashes to a new id. So this stores ids, never "until" state.

def load_dismissed(path) -> set[str]:
    """Dismissed question ids. A missing or unreadable store means none."""
    try:
        with open(path, "r") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return set()
    ids = data.get("dismissed") if isinstance(data, dict) else data
    if not isinstance(ids, list):
        return set()
    return {str(qid) for qid in ids if isinstance(qid, (str, int))}


def save_dismissed(path, ids: Iterable[str]) -> None:
    """Replace the store atomically; a reader never observes a truncated file."""
    path = str(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = json.dumps({"dismissed": sorted(set(ids))}, indent=1)
    handle, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def dismiss(path, qid: str) -> set[str]:
    """Record `qid` as dismissed forever and return the resulting id set."""
    ids = load_dismissed(path)
    ids.add(str(qid))
    save_dismissed(path, ids)
    return ids


def without_dismissed(rows: list[dict], dismissed: set[str]) -> list[dict]:
    return [row for row in rows if row.get("id") not in dismissed]
