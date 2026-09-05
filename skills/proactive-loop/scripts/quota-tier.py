#!/usr/bin/env python3
"""Tier each quota window by ITS OWN rule and return the MOST restrictive.

Built 2026-09-01 after I hand-selected the tier and inverted the comparison —
printed FULL when 5h=FULL and 7d=MEDIUM, which would have authorised subagents
on a MEDIUM budget. The two windows are scored differently and the selection is
a max over an ordered scale, both of which are easy to get wrong in prose.

Reads `read-quota.py` output on stdin, or --used5/--rem5/--reset5 etc. for tests.
"""
import argparse
import datetime
import re
import sys

ORDER = ["FULL", "MEDIUM", "LIGHT", "MINIMAL"]   # least -> MOST restrictive


def tier_5h(remaining_pct: float, minutes_to_reset: float) -> str:
    """Retained absolute budget per 5-minute pass. Calibrated for 5h ONLY."""
    if remaining_pct <= 0:
        return "MINIMAL"
    if minutes_to_reset <= 0:
        return "FULL"                             # window just reset
    v = remaining_pct / (minutes_to_reset / 5)
    return "FULL" if v > 3 else "MEDIUM" if v >= 1 else "LIGHT"


def tier_7d(remaining_pct: float, elapsed_frac: float) -> str:
    """Headroom = remaining / (1 - elapsed). The 5h bands are a CONSTANT here."""
    if remaining_pct <= 0:
        return "MINIMAL"
    if elapsed_frac >= 1:
        return "FULL"
    head = (remaining_pct / 100) / (1 - elapsed_frac)
    return "FULL" if head >= 1.5 else "MEDIUM" if head >= 0.8 else "LIGHT"


def most_restrictive(*tiers: str) -> str:
    """max over ORDER. min() returns the LEAST restrictive — the inversion that
    caused this file to exist."""
    return max(tiers, key=ORDER.index)


def parse_reset(s: str, now: datetime.datetime, max_ahead_h: float) -> datetime.datetime:
    """'03:10 Sep 01' -> a datetime, or raise. The year is absent from the input,
    so it is inferred and then BOUNDS-CHECKED: a reset must be in the future and
    within this window's own horizon. Guessing an unchecked year is how a 5h
    window silently becomes a 364-day one."""
    m = re.match(r"\s*(\d{1,2}):(\d{2})\s+([A-Z][a-z]{2})\s+(\d{1,2})", s)
    if not m:
        raise ValueError(f"unparseable reset {s!r}")
    hh, mm, mon, dd = int(m.group(1)), int(m.group(2)), m.group(3), int(m.group(4))
    month = ["Jan","Feb","Mar","Apr","May","Jun",
             "Jul","Aug","Sep","Oct","Nov","Dec"].index(mon) + 1
    for year in (now.year, now.year + 1):        # a Dec->Jan reset rolls the year
        try:
            cand = datetime.datetime(year, month, dd, hh, mm)
        except ValueError:
            continue
        ahead = (cand - now).total_seconds() / 3600
        if 0 <= ahead <= max_ahead_h:
            return cand
    raise ValueError(f"reset {s!r} is not within {max_ahead_h}h of now — refusing to guess")


def parse(text: str) -> dict:
    def grab(win, what):
        m = re.search(rf"{win} window: (\d+)% used, (\d+)% remaining", text)
        if not m:
            raise ValueError(f"no {win} window line in input")
        return int(m.group(1)) if what == "used" else int(m.group(2))
    # Each `Resets:` belongs to the window line printed just above it, so the
    # top-tier lane keeps ITS reset and never borrows the ordinary week's.
    resets, by_win, cur = [], {}, None
    for line in text.splitlines():
        w = re.match(r"\s*(5h|7d-oi|7d) window", line)
        if w:
            cur = w.group(1); continue
        r = re.match(r"\s*Resets: (.+)", line)
        if r:
            resets.append(r.group(1))
            if cur and cur not in by_win: by_win[cur] = r.group(1)
    out = {"used5": grab("5h", "used"), "rem5": grab("5h", "rem"),
           "used7": grab("7d", "used"), "rem7": grab("7d", "rem"),
           "resets": resets, "resets_by_window": by_win}
    m = re.search(r"7d-oi window[^:]*: (\d+)% used, (\d+)% remaining", text)
    if m:
        out["used7oi"], out["rem7oi"] = int(m.group(1)), int(m.group(2))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset5", help="5h reset, ISO or 'HH:MM Mon DD'")
    ap.add_argument("--reset7", help="7d reset, ISO")
    ap.add_argument("--reset7oi", help="top-tier (7d_oi) weekly reset, ISO")
    a = ap.parse_args(argv)
    q = parse(sys.stdin.read())
    now = datetime.datetime.now()
    r5 = datetime.datetime.fromisoformat(a.reset5) if a.reset5 else None
    r7 = datetime.datetime.fromisoformat(a.reset7) if a.reset7 else None
    if not (r5 and r7):
        # Fall back to the printed reset lines, year inferred and bounds-checked.
        try:
            r5 = r5 or parse_reset(q["resets"][0], now, 6)      # 5h: <=6h ahead
            r7 = r7 or parse_reset(q["resets"][1], now, 8 * 24)  # 7d: <=8d ahead
        except (ValueError, IndexError) as e:
            print(f"quota-tier: cannot resolve reset times ({e}); pass "
                  f"--reset5/--reset7. Nothing guessed.", file=sys.stderr)
            return 2
    m5 = (r5 - now).total_seconds() / 60
    s7 = r7 - datetime.timedelta(days=7)
    el = (now - s7).total_seconds() / (r7 - s7).total_seconds()
    el_oi = None
    if "rem7oi" in q:
        # The top-tier lane is tiered against ITS OWN reset. Present utilization
        # with no usable reset is a refusal, never a borrowed week.
        try:
            r7oi = (datetime.datetime.fromisoformat(a.reset7oi) if a.reset7oi
                    else parse_reset(q["resets_by_window"]["7d-oi"], now, 8 * 24))
        except (ValueError, KeyError) as e:
            print(f"quota-tier: 7d-oi utilization is present but its reset is missing or invalid ({e}); "
                  f"pass --reset7oi. Refusing to tier the top-tier lane against another window's week.",
                  file=sys.stderr)
            return 2
        s7oi = r7oi - datetime.timedelta(days=7)
        el_oi = (now - s7oi).total_seconds() / (r7oi - s7oi).total_seconds()
    t5, t7 = tier_5h(q["rem5"], m5), tier_7d(q["rem7"], el)
    burn = (q["used7"] / 100) / el if el > 0 else float("inf")
    head = (q["rem7"] / 100) / (1 - el) if el < 1 else float("inf")
    tiers = {"5h": t5, "7d": t7}
    if "rem7oi" in q:
        tiers["7d-oi"] = tier_7d(q["rem7oi"], el_oi)
    sel = most_restrictive(*tiers.values())
    bound = [w for w, t in tiers.items() if t == sel]
    binding = "both" if len(bound) == len(tiers) == 2 else "+".join(bound)
    print(f"5h  {q['rem5']}% rem, {m5:.0f} min -> retained "
          f"{q['rem5']/(m5/5):.2f} %/pass -> {t5}")
    print(f"7d  {q['rem7']}% rem, elapsed {el:.4f}, burn {burn:.2f}, "
          f"headroom {head:.3f} -> {t7}")
    if "rem7oi" in q:
        head_oi = (q["rem7oi"] / 100) / (1 - el_oi) if el_oi < 1 else float("inf")
        print(f"7d-oi (top-tier models) {q['rem7oi']}% rem, elapsed {el_oi:.4f}, "
              f"headroom {head_oi:.3f} -> {tiers['7d-oi']}")
    # burn guards its own denominator above and then BECOMES one here. A
    # just-reset window has used7 == 0, so the ratio is unbounded, not a number.
    ratio = (f"{head/burn:.2f}x CURRENT pace" if burn > 0
             else "unbounded vs CURRENT pace (no usage recorded yet)")
    print(f"TIER {sel} (bound by {binding})   "
          f"sustainable {ratio} ({head:.2f}x even pace)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
