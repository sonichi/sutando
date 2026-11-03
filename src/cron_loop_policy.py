"""Core loop identity and deliberate parking shared by schedule adapters."""
from __future__ import annotations

from typing import Optional
from cron_ownership import CORE, entry_owner


_MONTH_MAX_DAYS = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30,
                   7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}


def _cron_field_values(field: str, lo: int, hi: int) -> "set[int] | None":
    """Expand numeric fields conservatively; unsupported syntax returns None."""
    out: "set[int]" = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            return None
        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            if not raw_step.isdigit() or int(raw_step) < 1:
                return None
            step = int(raw_step)
        if part in ("*", "?"):
            start, end = lo, hi
        elif "-" in part.lstrip("-"):
            start_s, _, end_s = part.partition("-")
            if not (start_s.isdigit() and end_s.isdigit()):
                return None
            start, end = int(start_s), int(end_s)
        elif part.isdigit():
            start = end = int(part)
        else:
            return None  # names (JAN/MON), L, W, # — not our business
        if start > end or start < lo or end > hi:
            return None
        out.update(range(start, end + 1, step))
    return out or None


def _cron_can_never_fire(expr: str) -> bool:
    """Prove an impossible date only with unrestricted day-of-week (cron uses OR)."""
    parts = expr.split()
    if len(parts) != 5:
        return False
    _, _, dom_f, month_f, dow_f = parts
    if dow_f.strip() not in ("*", "?"):
        return False  # OR-semantics with day-of-week — it can still fire
    doms = _cron_field_values(dom_f, 1, 31)
    months = _cron_field_values(month_f, 1, 12)
    if doms is None or months is None:
        return False
    return not any(d <= _MONTH_MAX_DAYS[m] for m in months for d in doms)


def _entry_marked_parked(entry: dict) -> bool:
    """Require an explicit disabled field or the established DISABLED name marker."""
    if entry.get("disabled") is True:
        return True
    name = entry.get("name")
    return isinstance(name, str) and "DISABLED" in name


def is_core_main_loop(entry: dict) -> bool:
    return (
        entry_owner(entry) == CORE
        and entry.get("name") == "main-loop"
        and entry.get("prompt_skill") == "proactive-loop"
        and not entry.get("launchd")
    )


def _core_loop_candidates(entries: list) -> list:
    return [e for e in entries if isinstance(e, dict) and entry_owner(e) == CORE
            and (e.get("name") == "main-loop" or e.get("prompt_skill") == "proactive-loop")]


def core_loop_is_parked(entries: list) -> bool:
    candidates = _core_loop_candidates(entries)
    return bool(candidates) and all(
        _entry_marked_parked(e) and _cron_can_never_fire(str(e.get("cron", "")))
        for e in candidates
    )


def main_loop_problem(entries: list, runtime: str) -> Optional[str]:
    if runtime != "codex":
        return None
    candidates = _core_loop_candidates(entries)
    if core_loop_is_parked(entries):
        return None
    loops = [e for e in candidates if is_core_main_loop(e)]
    if len(loops) != 1 or len(candidates) != 1:
        return "expected exactly one core main-loop with prompt_skill proactive-loop"
    loop = loops[0]
    cron = loop.get("cron")
    if not isinstance(cron, str) or len(cron.split()) != 5 or _cron_can_never_fire(cron):
        return "core main-loop has no usable cron schedule"
    if any(_cron_field_values(field, lo, hi) is None
           for field, (lo, hi) in zip(cron.split(), ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7)))):
        return "core main-loop has an invalid cron schedule"
    if loop.get("execution") not in (None, "codex-task"):
        return "core main-loop has no durable Codex owner"
    return None
