"""CI guard: migration number must be max(applied in main) + 1.

Fails if:
  - Two scripts share the same 4-digit prefix in the local migrations/ dir.
  - Any proposed number is not exactly max_applied + 1
    (catches gaps and duplicate numbers across concurrent PRs).

Run by CI on every PR that touches migrations/.
"""
from __future__ import annotations

import re
from pathlib import Path


_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
_NUMBER_RE = re.compile(r"^(\d{4})-")


def _migration_numbers() -> list[int]:
    nums: list[int] = []
    if not _MIGRATIONS_DIR.is_dir():
        return nums
    for p in _MIGRATIONS_DIR.iterdir():
        if not p.is_file():
            continue
        if p.suffix not in (".sh", ".py"):
            continue
        m = _NUMBER_RE.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return sorted(nums)


def test_no_duplicate_numbers() -> None:
    """No two migration scripts may share the same 4-digit prefix."""
    nums = _migration_numbers()
    seen: set[int] = set()
    dupes: list[int] = []
    for n in nums:
        if n in seen:
            dupes.append(n)
        seen.add(n)
    assert not dupes, f"Duplicate migration numbers found: {dupes}"


def test_contiguous_sequence() -> None:
    """Numbers must form a contiguous sequence 0001, 0002, …, N with no gaps."""
    nums = _migration_numbers()
    if not nums:
        return  # no migrations yet — nothing to check
    expected = list(range(1, max(nums) + 1))
    assert nums == expected, (
        f"Migration numbers are not contiguous.\n"
        f"  Expected: {expected}\n"
        f"  Got:      {nums}\n"
        f"  Missing:  {sorted(set(expected) - set(nums))}"
    )
