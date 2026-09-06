#!/usr/bin/env python3
"""One 5-field cron evaluation contract for every scheduler and presentation
surface (cron-runner, codex-scheduler, dashboard_schedules, scheduled-panel).

Grammar per field: ``*``, ``*/N``, ``A``, ``A,B``, ``A-B``, ``A-B/N``. Day-of-
week accepts 7 as Sunday (folded to 0 at the SET level, so ``5-7`` and ``0-7``
keep their meaning). Day-of-month and day-of-week are OR-ed when both are
restricted, AND-ed otherwise (Vixie cron). Malformed fields raise ValueError.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# (lo, hi) per field: minute, hour, day-of-month, month, day-of-week (7 = Sunday).
FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def field_values(spec: str, lo: int, hi: int, *, sunday_7: bool = False,
                 clamp: bool = False) -> frozenset[int]:
    """Expand one field to the set of matching ints.

    clamp=True drops out-of-range values and empties an inverted range instead
    of raising (the launchd runner's historical contract); syntax errors raise.
    """
    values: set[int] = set()
    for token in spec.split(","):
        token = token.strip()
        if not token:
            raise ValueError("empty cron field token")
        base, slash, step_text = token.partition("/")
        try:
            step = int(step_text) if slash else 1
        except ValueError as exc:
            raise ValueError(f"cron step {step_text!r} is not an integer") from exc
        if step <= 0:
            raise ValueError("cron step must be positive")
        try:
            if base == "*":
                start, end = lo, hi
            elif "-" in base:
                left, right = base.split("-", 1)
                start, end = int(left), int(right)
            else:
                start = end = int(base)
        except ValueError as exc:
            raise ValueError(f"cron field {base!r} is not numeric") from exc
        if not clamp and start > end:
            raise ValueError(f"cron range {base!r} is inverted")
        if not clamp and (start < lo or end > hi):
            raise ValueError(f"cron value {base!r} outside {lo}-{hi}")
        values.update(v for v in range(start, end + 1, step) if lo <= v <= hi)
    return fold_sunday(values) if sunday_7 else frozenset(values)


def fold_sunday(values) -> frozenset[int]:
    """Fold a day-of-week set expanded over 0-7 so 7 means Sunday (0)."""
    values = set(values)
    if 7 in values:
        values.discard(7)
        values.add(0)
    return frozenset(values)


@dataclass(frozen=True)
class CronSpec:
    minutes: frozenset[int]
    hours: frozenset[int]
    doms: frozenset[int]
    months: frozenset[int]
    dows: frozenset[int]  # 0-6, Sunday = 0
    dom_restricted: bool
    dow_restricted: bool

    def date_matches(self, year: int, month: int, day: int) -> bool:
        if month not in self.months:
            return False
        dom_ok = day in self.doms
        dow_ok = ((datetime(year, month, day).weekday() + 1) % 7) in self.dows
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def matches(self, dt: datetime) -> bool:
        return (dt.minute in self.minutes and dt.hour in self.hours
                and self.date_matches(dt.year, dt.month, dt.day))


def parse(expr: str, *, clamp: bool = False) -> CronSpec:
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron expression must have 5 fields: {expr!r}")
    (mlo, mhi), (hlo, hhi), (dlo, dhi), (molo, mohi), (wlo, whi) = FIELD_BOUNDS
    return CronSpec(
        minutes=field_values(fields[0], mlo, mhi, clamp=clamp),
        hours=field_values(fields[1], hlo, hhi, clamp=clamp),
        doms=field_values(fields[2], dlo, dhi, clamp=clamp),
        months=field_values(fields[3], molo, mohi, clamp=clamp),
        dows=field_values(fields[4], wlo, whi, sunday_7=True, clamp=clamp),
        dom_restricted=fields[2] != "*",
        dow_restricted=fields[4] != "*",
    )


def matches(expr: str, dt: datetime) -> bool:
    """True when ``dt`` (a wall-clock time in the job's own zone) fires ``expr``."""
    return parse(expr).matches(dt)


def next_match(expr: str, after: datetime, horizon_days: int = 8):
    """First minute strictly after ``after`` that fires, or None inside the
    horizon. Whole days that cannot match are skipped, so a multi-year horizon
    (a leap-day job) costs days, not minutes.

    A naive ``after`` is walked as wall-clock minutes. An aware one is walked
    as real instants, each judged by its wall clock in ``after``'s zone — the
    predicate both schedulers apply to their minute slots — so a spring-forward
    minute that never exists is never returned and a fall-back minute that
    exists twice is visited twice.
    """
    spec = parse(expr)
    tz = after.tzinfo
    if tz is None:
        t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        end = after + timedelta(days=horizon_days)
        while t <= end:
            if not spec.date_matches(t.year, t.month, t.day):
                t = (t + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if t.minute in spec.minutes and t.hour in spec.hours:
                return t
            t += timedelta(minutes=1)
        return None
    utc = timezone.utc
    t = after.astimezone(utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = after.astimezone(utc) + timedelta(days=horizon_days)
    while t <= end:
        local = t.astimezone(tz)
        if not spec.date_matches(local.year, local.month, local.day):
            midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            jump = midnight.astimezone(utc)
            t = jump if jump > t else t + timedelta(minutes=1)
            continue
        if local.minute in spec.minutes and local.hour in spec.hours:
            return local
        t += timedelta(minutes=1)
    return None
