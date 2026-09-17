"""Pick an existing workstream for a task, or refuse.

The classifier is a model, not a script, so the ranking used to be re-derived
each pass. A re-derived rule cannot degrade gracefully: on a tie it silently
falls back to whatever order the candidates arrived in, which is the arbitrary
pick that scoring was supposed to remove — and the printed shortlist makes the
result look deliberate.

So the margin rule lives here instead of in a habit.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence


def candidates_from_snapshot(snapshot: dict) -> list[tuple[str, str]]:
    """Build ``best_match`` candidates from a classifier snapshot.

    The store keys a workstream's label as ``title``, but every snapshot layer
    re-exports it as ``name``. A caller that assembles these pairs by hand and
    reaches for the wrong key gets empty text rather than an error, every score
    collapses to zero, and ``best_match`` then refuses everything -- which is
    indistinguishable from a correct low-confidence refusal. Reading both keys
    here is what stops that guess from being made at each call site.
    """
    rows = (snapshot or {}).get("existing_workstreams") or []
    out: list[tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("id") or "")
        if not cid:
            continue
        label = str(row.get("name") or row.get("title") or "")
        out.append((cid, f'{label} {row.get("summary") or ""}'.strip()))
    return out


def keywords_from_text(text: str, *, min_length: int = 4) -> list[str]:
    """Build ``best_match`` keywords from one task's own text.

    Splits on every non-letter. Keeping ``-`` inside the token class merges a
    compound such as ``morning-briefing.py`` into one token that appears in no
    workstream's label, so a workstream named "Daily morning briefing" scores on
    neither word; an unrelated candidate then wins by a sub-margin count and
    ``best_match`` refuses -- indistinguishable from a correct low-confidence
    refusal. Deriving the list here is what stops that regex from being
    re-invented at each call site, as ``candidates_from_snapshot`` does for the
    other argument.
    """
    if not text:
        return []
    floor = max(1, int(min_length))
    return sorted({w for w in re.findall(r"[a-z]+", str(text).lower()) if len(w) >= floor})


def score(text: str, keywords: Sequence[str]) -> int:
    """Occurrences of any keyword in text. Case-insensitive, substring-based."""
    low = text.lower()
    return sum(low.count(k.lower()) for k in keywords)


def best_match(
    candidates: Iterable[tuple[str, str]],
    keywords: Sequence[str],
    *,
    min_margin: int = 2,
) -> Optional[str]:
    """Return the id of the best-scoring candidate, or None.

    ``candidates`` is (id, searchable_text). None is returned when the field is
    empty, when nothing scores, or when the top two are within ``min_margin`` of
    each other. None means OMIT — leave the task unassigned, which the skill
    treats as a valid answer — never "take the first one".
    """
    candidates = list(candidates)
    # A 2-key dict unpacks to its KEY STRINGS and scores those, so every
    # materialized candidate is checked, not only the first.
    if any(isinstance(c, dict) for c in candidates):
        raise TypeError("best_match takes (id, searchable_text) tuples, not dicts")
    scored = sorted(
        ((score(text, keywords), cid) for cid, text in candidates),
        key=lambda pair: (-pair[0], pair[1]),
    )
    if not scored:
        return None
    top_score, top_id = scored[0]
    if top_score <= 0:
        return None
    runner_up = scored[1][0] if len(scored) > 1 else 0
    if top_score - runner_up < min_margin:
        return None
    return top_id
