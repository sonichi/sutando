#!/usr/bin/env python3
"""Pure meeting-scheduler policy — time parsing, conflicts, dedup, name matching.

Extracted from `schedule_meeting.py` so the scheduling rules are testable
without Google Workspace access. This module performs no IO: no `gws` calls, no
filesystem reads, no network, no printing, and it never creates or sends an
event. Account selection, timezone detection (reads the host), Gmail identity
lookup, Calendar reads, the irreversible create/send, CLI parsing and terminal
output all stay in `schedule_meeting.py`.

Feature-specific by design — stays inside skills/meeting-scheduler/ and is not
promoted into src/.

Public API:
    parse_when(s, now=None)                 -> datetime   (naive, wall-clock)
    compute_end(start, duration_min)        -> datetime
    find_conflicts(events, start, end)      -> list[dict]
    find_duplicates(events, title)          -> list[dict]
    pick_email_for_name(headers, name)      -> dict

Timezone contract worth knowing before you touch it: `_event_bounds` strips
tzinfo and compares WALL CLOCK, and all-day (date-only) events return None so
they never block a timed slot. `find_conflicts` therefore expects NAIVE start
and end; passing offset-aware datetimes raises TypeError. That is deliberate,
not an oversight.
"""
from __future__ import annotations

import datetime as dt
import re


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")



def parse_when(s: str, now: dt.datetime | None = None) -> dt.datetime:
    """Parse the meeting start.

    Supported (intentionally small — the invoking agent should pre-resolve
    anything fancier into ISO 8601):
      * ISO 8601:                 2026-07-25T15:00  /  2026-07-25 15:00
      * 'today' / 'tomorrow' + a clock time: 'tomorrow 3pm', 'today 14:00'

    Returns a NAIVE datetime (wall-clock); the caller pairs it with an IANA
    timeZone for the Calendar API, matching how Google interprets local times.
    Raises ValueError on anything it can't confidently parse.
    """
    s = s.strip()
    now = now or dt.datetime.now()

    # 1) ISO 8601 first (most reliable).
    iso = s.replace(" ", "T", 1) if (" " in s and "T" not in s) else s
    try:
        return dt.datetime.fromisoformat(iso)
    except ValueError:
        pass

    # 2) A tiny natural-language surface: today/tomorrow + a clock time.
    m = re.match(
        r"^(today|tomorrow)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$",
        s,
        re.IGNORECASE,
    )
    if m:
        day_word, hh, mm, ampm = m.groups()
        hour = int(hh)
        minute = int(mm) if mm else 0
        if ampm:
            ampm = ampm.lower()
            if ampm == "pm" and hour != 12:
                hour += 12
            if ampm == "am" and hour == 12:
                hour = 0
        base = now.date()
        if day_word.lower() == "tomorrow":
            base = base + dt.timedelta(days=1)
        return dt.datetime(base.year, base.month, base.day, hour, minute)

    raise ValueError(
        f"could not parse --when {s!r}. Pass ISO 8601 (2026-07-25T15:00) or "
        "'today/tomorrow HH[:MM][am/pm]'."
    )


def compute_end(start: dt.datetime, duration_min: int) -> dt.datetime:
    return start + dt.timedelta(minutes=duration_min)


def _event_bounds(ev: dict) -> tuple[dt.datetime, dt.datetime] | None:
    """Return (start, end) naive datetimes for a Calendar event, or None if it's
    an all-day event / unparseable (all-day events don't block a timed slot)."""
    start = (ev.get("start") or {}).get("dateTime")
    end = (ev.get("end") or {}).get("dateTime")
    if not start or not end:
        return None  # all-day (date-only) or malformed — ignore for slot conflicts
    try:
        # Normalize trailing Z and drop tz for a wall-clock comparison; Google
        # returns the calendar's local offset which we compare like-for-like.
        s = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
        e = dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
        return s.replace(tzinfo=None), e.replace(tzinfo=None)
    except ValueError:
        return None


def _is_blocking(ev: dict) -> bool:
    """An event blocks the slot unless it's cancelled or explicitly free/transparent."""
    if ev.get("status") == "cancelled":
        return False
    if ev.get("transparency") == "transparent":  # "free" busy-status
        return False
    return True


def find_conflicts(events: list[dict], start: dt.datetime, end: dt.datetime) -> list[dict]:
    """Events whose busy time overlaps [start, end)."""
    hits = []
    for ev in events:
        if not _is_blocking(ev):
            continue
        bounds = _event_bounds(ev)
        if bounds is None:
            continue
        s, e = bounds
        if s < end and start < e:  # half-open overlap
            hits.append(ev)
    return hits


def find_duplicates(events: list[dict], title: str) -> list[dict]:
    """Existing events on the day whose summary matches the title (case/space
    insensitive) and that aren't cancelled — the dedup guard."""
    want = " ".join(title.split()).casefold()
    dups = []
    for ev in events:
        if ev.get("status") == "cancelled":
            continue
        summary = " ".join((ev.get("summary") or "").split()).casefold()
        if summary and summary == want:
            dups.append(ev)
    return dups


def pick_email_for_name(messages_headers: list[dict], name: str) -> dict:
    """Given a list of {'from': 'Display <e@x>', 'to': '...'} header dicts and a
    query name, return {'name', 'email', 'alternates'} — the best-matching
    address plus any other candidates seen.

    Heuristic: collect (display, email) pairs from From/To headers; rank an
    address higher when the display name contains a query token. Pure so it can
    be tested offline against fixture headers.

    FAILS CLOSED (the contract is "returns None — never a guess — on no match"):
      * If NO candidate's display name actually matches a query token (top
        score 0), return email=None. A guessed address emails the invite to the
        WRONG person, which is worse than an unresolved name.
      * If two+ addresses TIE at the top score, the match is genuinely
        ambiguous — return email=None with 'ambiguous': True and the tied
        'candidates'. The caller must disambiguate with an explicit --attendees
        email (or --force) before --send; we never auto-pick.
    """
    tokens = [t for t in re.split(r"\s+", name.strip().casefold()) if t]
    seen: dict[str, str] = {}  # email -> best display seen
    scores: dict[str, int] = {}
    for h in messages_headers:
        for field in ("from", "to", "cc"):
            raw = h.get(field) or ""
            for chunk in raw.split(","):
                emails = _EMAIL_RE.findall(chunk)
                if not emails:
                    continue
                email = emails[0].lower()
                display = chunk.split("<")[0].strip().strip('"').strip()
                disp_cf = (display or email).casefold()
                score = sum(1 for t in tokens if t and t in disp_cf)
                if email not in seen or score > scores.get(email, -1):
                    seen[email] = display or email
                if score > scores.get(email, -1):
                    scores[email] = score
    if not seen:
        return {"name": name, "email": None, "alternates": []}
    ranked = sorted(seen, key=lambda e: (scores.get(e, 0), e), reverse=True)
    top = scores.get(ranked[0], 0)
    # Fail closed (a): nobody's display name matched the requested name.
    if top <= 0:
        return {"name": name, "email": None, "alternates": [],
                "candidates": [{"email": e, "display": seen[e]} for e in ranked[:5]]}
    # Fail closed (b): a tie at the top score is ambiguous — do NOT auto-pick.
    tied = [e for e in ranked if scores.get(e, 0) == top]
    if len(tied) > 1:
        return {"name": name, "email": None, "ambiguous": True, "alternates": [],
                "candidates": [{"email": e, "display": seen[e]} for e in tied[:5]]}
    best = ranked[0]
    return {
        "name": name,
        "email": best,
        "display": seen[best],
        "alternates": [{"email": e, "display": seen[e]} for e in ranked[1:5]],
    }
