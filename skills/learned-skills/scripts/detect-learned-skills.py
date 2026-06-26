"""Scan recent task archives for repeated workflow patterns and propose candidate
learned skills. Gated on state/learned-skills-enabled.sentinel (toggled from
Settings → Skills → Behavior); exits early when the gate is off.

Run: python3 skills/learned-skills/scripts/detect-learned-skills.py
Exit: 0 always (gate-off, no candidates, or proposals written to stdout).
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Resolve src/ (scripts/ → learned-skills/ → skills/ → repo root → src/)
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from workspace_default import resolve_workspace  # noqa: E402

WORKSPACE = resolve_workspace()
STATE_DIR = WORKSPACE / "state"
TASKS_DIR = WORKSPACE / "tasks"
SENTINEL = STATE_DIR / "learned-skills-enabled.sentinel"

# Minimum times a pattern must appear before it's proposed as a candidate.
MIN_OCCURRENCES = 3
# How many candidate proposals to emit.
MAX_PROPOSALS = 5
# Scan subdirs of tasks/archive/ recursively.
ARCHIVE_DIRS = ["archive", "processed"]


def _gate_on() -> bool:
    return SENTINEL.exists()


def _collect_tasks() -> list[dict]:
    """Read all archived task files and return parsed dicts."""
    tasks: list[dict] = []
    for subdir in ARCHIVE_DIRS:
        base = TASKS_DIR / subdir
        if not base.exists():
            continue
        for p in sorted(base.rglob("task-*.txt")):
            try:
                raw = p.read_text(errors="ignore")
            except OSError:
                continue
            entry: dict = {"path": str(p)}
            for line in raw.splitlines():
                if ":" in line:
                    key, _, val = line.partition(":")
                    entry[key.strip()] = val.strip()
                elif not entry.get("task") and line.strip():
                    entry.setdefault("body", line.strip())
            tasks.append(entry)
    return tasks


def _normalize(text: str) -> str:
    """Strip task-specific identifiers to make similar tasks comparable."""
    text = text.lower()
    # Remove URLs
    text = re.sub(r"https?://\S+", "<url>", text)
    # Remove numbers (ids, timestamps, line numbers)
    text = re.sub(r"\b\d[\d.,:/\-]*\b", "<n>", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _leading_verb_phrase(text: str) -> str:
    """Extract the first ~5 words as a rough intent fingerprint."""
    words = text.split()
    return " ".join(words[:5])


def _mine_patterns(tasks: list[dict]) -> list[tuple[str, int, list[str]]]:
    """Return list of (pattern, count, example_tasks) sorted by count desc."""
    verb_counter: Counter = Counter()
    verb_examples: dict[str, list[str]] = defaultdict(list)

    for t in tasks:
        raw = t.get("task", t.get("body", ""))
        if not raw:
            continue
        # Skip cancel / dedup / system tasks
        if raw.startswith("CANCEL_INSTRUCTION") or raw.startswith("[deduped"):
            continue
        norm = _normalize(raw)
        vp = _leading_verb_phrase(norm)
        if len(vp.split()) < 2:
            continue
        verb_counter[vp] += 1
        if len(verb_examples[vp]) < 3:
            verb_examples[vp].append(raw[:120])

    results = [
        (vp, count, verb_examples[vp])
        for vp, count in verb_counter.items()
        if count >= MIN_OCCURRENCES
    ]
    results.sort(key=lambda x: x[1], reverse=True)
    return results[:MAX_PROPOSALS]


def _format_proposal(pattern: str, count: int, examples: list[str]) -> str:
    lines = [
        f"## Candidate: \"{pattern}\"",
        f"Seen {count} times. Examples:",
    ]
    for ex in examples:
        lines.append(f"  - {ex}")
    lines.append(
        f"\nSuggested skill name: {pattern.replace('<n>', '').replace(' ', '-').strip('-')[:40]}"
    )
    return "\n".join(lines)


def main() -> None:
    if not _gate_on():
        print(
            "detect-learned-skills: gate off "
            f"(sentinel absent: {SENTINEL}). Skipping."
        )
        return

    tasks = _collect_tasks()
    if not tasks:
        print("detect-learned-skills: no archived tasks found.")
        return

    patterns = _mine_patterns(tasks)
    if not patterns:
        print(
            f"detect-learned-skills: scanned {len(tasks)} tasks, "
            f"no pattern appeared ≥{MIN_OCCURRENCES} times."
        )
        return

    print(
        f"detect-learned-skills: scanned {len(tasks)} tasks, "
        f"found {len(patterns)} candidate(s):\n"
    )
    for pattern, count, examples in patterns:
        print(_format_proposal(pattern, count, examples))
        print()


if __name__ == "__main__":
    main()
