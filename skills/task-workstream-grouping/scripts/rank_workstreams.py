"""Pick an existing workstream for a task, or refuse.

The classifier is a model, not a script, so the ranking used to be re-derived
each pass. A re-derived rule cannot degrade gracefully: on a tie it silently
falls back to whatever order the candidates arrived in, which is the arbitrary
pick that scoring was supposed to remove — and the printed shortlist makes the
result look deliberate.

So the margin rule lives here instead of in a habit.
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence


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
    # A 2-key dict silently unpacks to its KEY STRINGS here, scoring 0 for
    # every candidate — measured 2026-09-02: a whole night of None verdicts.
    if candidates and isinstance(candidates[0], dict):
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
