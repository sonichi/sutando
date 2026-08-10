#!/usr/bin/env python3
"""
Meeting scheduler — the MECHANICAL core of "set up a small meeting".

Given a title, a time slot, a duration, and either explicit attendee emails or
names to resolve, this:

  1. (optional) resolves names -> emails via a Gmail search (`gws gmail ...`),
  2. checks the OWNER's calendar for conflicts in the proposed slot,
  3. dedup-checks for an existing same-title event that day,
  4. (only with --send) creates the event with sendUpdates=all so Google emails
     the invites, and prints the event htmlLink.

Scope is intentionally TIGHT: it does NOT negotiate cross-person availability
(that stays an interactive conversation). It resolves, checks the owner's own
calendar, dedups, and — on explicit owner action — creates + sends.

FAIL-SAFE by design:
  * Defaults to --dry-run. Nothing is created or emailed unless --send is given.
  * On a conflict or a same-title duplicate, it REFUSES to create even with
    --send, unless --force is also given (the owner's explicit override).

Everything network-touching shells out to the `gws` CLI (Google Workspace).
The dedup / conflict / when-parsing logic is pure and unit-tested offline
(see --self-check and tests/meeting-scheduler.test.py).

Examples:
    # dry-run (default) — resolve names, check the slot, dedup, report only:
    python3 schedule_meeting.py --title "Sync" --when 2026-07-25T15:00 \
        --duration-min 30 --resolve "Alice, Bob"

    # actually create + email invites:
    python3 schedule_meeting.py --title "Sync" --when 2026-07-25T15:00 \
        --duration-min 30 --attendees "a@x.com,b@y.com" --send

    # offline logic self-check (no network):
    python3 schedule_meeting.py --self-check
"""
# PEP 604 (`str | None`) annotations are evaluated lazily so the module still
# imports on Python 3.9 hosts.
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys

# Pure scheduling policy lives in policy.py; this module keeps gws access,
# account/timezone resolution, CLI, output and the irreversible create/send.
sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
from policy import (_EMAIL_RE, parse_when, compute_end, _event_bounds,  # noqa: E402
                    _is_blocking, find_conflicts, find_duplicates,
                    pick_email_for_name)
from pathlib import Path

# Owner is Pacific; used only if we can't detect the host tz and none is given.
DEFAULT_TZ = "America/Los_Angeles"
DEFAULT_DURATION_MIN = 30
GMAIL_LOOKBACK_DEFAULT = 365  # days of Gmail history to scan when resolving a name


# --------------------------------------------------------------------------- #
# gws plumbing                                                                 #
# --------------------------------------------------------------------------- #
def _gws_env(account: str) -> dict:
    """Build the environment for a gws call.

    The ag2.ai account keeps its own config dir (per memory: the ag2.ai account
    uses GOOGLE_WORKSPACE_CLI_CONFIG_DIR=$HOME/.config/gws-ag2). The default
    account uses gws's own default config dir (no override).
    """
    env = dict(os.environ)
    if account == "ag2":
        env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] = str(
            Path.home() / ".config" / "gws-ag2"
        )
    return env


def _strip_keyring(text: str) -> str:
    """gws prints noisy 'keyring' lines to stdout on some hosts; drop them so
    the remainder parses as clean JSON."""
    return "\n".join(
        line for line in text.splitlines() if "keyring" not in line.lower()
    )


def run_gws(args: list[str], account: str, timeout: int = 60) -> dict | list:
    """Run `gws <args>` and return parsed JSON. Raises RuntimeError on failure."""
    cmd = ["gws", *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=_gws_env(account),
            timeout=timeout,
        )
    except FileNotFoundError:
        raise RuntimeError("`gws` CLI not found on PATH — is Google Workspace CLI installed?")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"gws timed out after {timeout}s: {' '.join(args)}")
    out = _strip_keyring(proc.stdout or "")
    if proc.returncode != 0:
        raise RuntimeError(
            f"gws failed (exit {proc.returncode}): {' '.join(args)}\n"
            f"{(proc.stderr or '').strip()}\n{out.strip()}"
        )
    out = out.strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"could not parse gws JSON output: {e}\n{out[:500]}")


# --------------------------------------------------------------------------- #
# Pure logic (unit-tested offline)                                            #
# --------------------------------------------------------------------------- #
def detect_timezone() -> str:
    """Best-effort IANA tz of the host (from /etc/localtime symlink); falls back
    to the owner's Pacific default."""
    try:
        target = os.readlink("/etc/localtime")
        # e.g. /var/db/timezone/zoneinfo/America/Los_Angeles
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return DEFAULT_TZ




# --------------------------------------------------------------------------- #
# Network steps (thin wrappers over run_gws)                                   #
# --------------------------------------------------------------------------- #
def resolve_names(names: list[str], account: str, lookback_days: int,
                  verbose: bool = False) -> list[dict]:
    """Resolve each display name to an email via a Gmail search over recent mail."""
    resolved = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        q = f'"{name}" newer_than:{lookback_days}d'
        try:
            listing = run_gws(
                ["gmail", "users", "messages", "list",
                 "--params", json.dumps({"userId": "me", "q": q, "maxResults": 10})],
                account,
            )
        except RuntimeError as e:
            if verbose:
                print(f"  ! name lookup failed for {name!r}: {e}", file=sys.stderr)
            resolved.append({"name": name, "email": None, "alternates": [], "error": str(e)})
            continue
        ids = [m.get("id") for m in (listing.get("messages") or []) if m.get("id")]
        headers = []
        for mid in ids[:8]:
            try:
                msg = run_gws(
                    ["gmail", "users", "messages", "get",
                     "--params", json.dumps(
                         {"userId": "me", "id": mid, "format": "metadata",
                          "metadataHeaders": ["From", "To", "Cc"]})],
                    account,
                )
            except RuntimeError:
                continue
            hd = {}
            for h in ((msg.get("payload") or {}).get("headers") or []):
                hd[h.get("name", "").lower()] = h.get("value", "")
            headers.append(hd)
        resolved.append(pick_email_for_name(headers, name))
    return resolved


def list_events_for_day(start: dt.datetime, account: str, calendar: str,
                        tz: str, query: str | None = None) -> list[dict]:
    """Return the owner's events across the calendar day of `start` (so one call
    serves BOTH the slot-conflict check and the same-day dedup check)."""
    day = start.date()
    day_start = dt.datetime(day.year, day.month, day.day, 0, 0)
    next_day = day_start + dt.timedelta(days=1)
    # Each boundary's UTC offset is computed at ITS OWN date, not at 'now', so a
    # cross-DST-season query stays correct — a December (PST, -08:00) day emits
    # -08:00 even when this runs in July (PDT). day_start and next_day are
    # localized independently in case the day itself straddles a DST switch;
    # otherwise the window could start an hour late and MISS a real conflict.
    params = {
        "calendarId": calendar,
        "timeMin": day_start.isoformat() + _tz_offset(tz, day_start),
        "timeMax": next_day.isoformat() + _tz_offset(tz, next_day),
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": 250,
    }
    if query:
        params["q"] = query
    data = run_gws(["calendar", "events", "list", "--params", json.dumps(params)], account)
    return data.get("items") or []


def _tz_offset(tz: str, at: dt.datetime) -> str:
    """RFC3339 offset (e.g. -07:00) for an IANA tz AT the wall-clock moment `at`
    — NOT at 'now'. Computing it at the target date is what keeps a December
    (PST, -08:00) query correct even when the process runs in July (PDT). Falls
    back to 'Z' (UTC) if the zoneinfo isn't available — Calendar still interprets
    timeMin/timeMax correctly, this only widens the window slightly."""
    try:
        from zoneinfo import ZoneInfo  # py3.9+
        off = at.replace(tzinfo=ZoneInfo(tz)).utcoffset()
        if off is None:
            return "Z"
        total = int(off.total_seconds())
        sign = "+" if total >= 0 else "-"
        total = abs(total)
        return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"
    except Exception:
        return "Z"


def create_event(title: str, start: dt.datetime, end: dt.datetime, tz: str,
                 emails: list[str], location: str | None, description: str | None,
                 account: str, calendar: str) -> dict:
    """Create the event and let Google email invites (sendUpdates=all)."""
    body: dict = {
        "summary": title,
        "start": {"dateTime": start.isoformat(timespec="seconds"), "timeZone": tz},
        "end": {"dateTime": end.isoformat(timespec="seconds"), "timeZone": tz},
        "attendees": [{"email": e} for e in emails],
    }
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    return run_gws(
        ["calendar", "events", "insert",
         "--params", json.dumps({"calendarId": calendar, "sendUpdates": "all"}),
         "--json", json.dumps(body)],
        account,
    )


# --------------------------------------------------------------------------- #
# Offline self-check (no network)                                              #
# --------------------------------------------------------------------------- #
def self_check() -> int:
    """Exercise the pure logic without touching the network."""
    ok = True

    def check(cond, msg):
        nonlocal ok
        status = "PASS" if cond else "FAIL"
        if not cond:
            ok = False
        print(f"  [{status}] {msg}")

    # when-parsing
    start = parse_when("2026-07-25T15:00")
    check(start == dt.datetime(2026, 7, 25, 15, 0), "parse ISO 2026-07-25T15:00")
    check(parse_when("2026-07-25 09:30") == dt.datetime(2026, 7, 25, 9, 30),
          "parse ISO with space separator")
    now = dt.datetime(2026, 7, 24, 8, 0)
    check(parse_when("tomorrow 3pm", now=now) == dt.datetime(2026, 7, 25, 15, 0),
          "parse 'tomorrow 3pm'")
    check(parse_when("today 14:00", now=now) == dt.datetime(2026, 7, 24, 14, 0),
          "parse 'today 14:00'")
    try:
        parse_when("next tuesday-ish")
        check(False, "reject unparseable when")
    except ValueError:
        check(True, "reject unparseable when")

    end = compute_end(start, 30)
    check(end == dt.datetime(2026, 7, 25, 15, 30), "compute_end +30min")

    # conflict detection
    events = [
        {"summary": "Standup",
         "start": {"dateTime": "2026-07-25T15:15:00-07:00"},
         "end": {"dateTime": "2026-07-25T15:45:00-07:00"}},          # overlaps
        {"summary": "Lunch",
         "start": {"dateTime": "2026-07-25T12:00:00-07:00"},
         "end": {"dateTime": "2026-07-25T13:00:00-07:00"}},          # no overlap
        {"summary": "Focus", "transparency": "transparent",
         "start": {"dateTime": "2026-07-25T15:10:00-07:00"},
         "end": {"dateTime": "2026-07-25T15:20:00-07:00"}},          # free → ignored
        {"summary": "All day thing", "start": {"date": "2026-07-25"},
         "end": {"date": "2026-07-26"}},                             # all-day → ignored
    ]
    conflicts = find_conflicts(events, start, end)
    check(len(conflicts) == 1 and conflicts[0]["summary"] == "Standup",
          "find_conflicts flags only the overlapping busy event")

    # adjacency is NOT a conflict (half-open)
    adj = [{"summary": "Prev",
            "start": {"dateTime": "2026-07-25T14:30:00-07:00"},
            "end": {"dateTime": "2026-07-25T15:00:00-07:00"}}]
    check(find_conflicts(adj, start, end) == [], "back-to-back meeting is not a conflict")

    # dedup
    day_events = [
        {"summary": "Sync", "start": {"dateTime": "2026-07-25T09:00:00-07:00"}},
        {"summary": "  sync ", "status": "cancelled"},  # cancelled → ignored
        {"summary": "Other"},
    ]
    dups = find_duplicates(day_events, "Sync")
    check(len(dups) == 1, "find_duplicates matches same title (case/space-insensitive), skips cancelled")
    check(find_duplicates(day_events, "Brand New") == [], "no false dup for a fresh title")

    # email resolution heuristic
    headers = [
        {"from": "Alice Adams <alice@example.com>", "to": "Dana <dana@x.com>"},
        {"from": "Someone Else <nobody@example.com>", "to": "alice@example.com"},
    ]
    picked = pick_email_for_name(headers, "Alice")
    check(picked["email"] == "alice@example.com",
          "pick_email_for_name resolves display-name match")
    check(pick_email_for_name([], "Nobody")["email"] is None,
          "pick_email_for_name returns None when nothing found")
    # FAIL CLOSED (a): headers exist but NONE match the name → None, not a guess.
    nonmatch = [{"from": "Carol Manager <carol@example.com>"}]
    check(pick_email_for_name(nonmatch, "Alice Example")["email"] is None,
          "pick_email_for_name fails closed when no display name matches")
    # FAIL CLOSED (b): a top-score tie is ambiguous → None (never auto-picked).
    tie = [{"from": "Dana Green <dana.green@example.com>",
            "to": "Dana Blue <dana.blue@example.com>"}]
    picked_tie = pick_email_for_name(tie, "Dana")
    check(picked_tie["email"] is None and picked_tie.get("ambiguous"),
          "pick_email_for_name flags an ambiguous tie instead of guessing")

    # tz offset is DATE-aware (not 'now'): summer→PDT, winter→PST for LA.
    check(_tz_offset("America/Los_Angeles", dt.datetime(2026, 7, 15)) == "-07:00",
          "_tz_offset gives PDT (-07:00) for a summer date")
    check(_tz_offset("America/Los_Angeles", dt.datetime(2026, 12, 15)) == "-08:00",
          "_tz_offset gives PST (-08:00) for a winter date")

    print("\nSELF-CHECK:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="schedule_meeting.py",
        description="Resolve attendees, check the owner's calendar, dedup, and "
                    "(only with --send) create + email a Google Calendar invite. "
                    "Defaults to --dry-run.",
    )
    p.add_argument("--title", help="Event title / summary.")
    p.add_argument("--when", help="Start time: ISO 8601 (2026-07-25T15:00) or "
                                   "'today/tomorrow HH[:MM][am/pm]'.")
    p.add_argument("--duration-min", type=int, default=DEFAULT_DURATION_MIN,
                   help=f"Duration in minutes (default {DEFAULT_DURATION_MIN}).")
    p.add_argument("--attendees", default="",
                   help="Comma-separated explicit emails.")
    p.add_argument("--resolve", default="",
                   help="Comma-separated names to resolve to emails via Gmail.")
    p.add_argument("--location", help="Optional location / video link.")
    p.add_argument("--description", help="Optional event description / agenda.")
    p.add_argument("--calendar", default="primary", help="Calendar id (default primary).")
    p.add_argument("--timezone", help="IANA tz (default: detect host, else Pacific).")
    p.add_argument("--account", default="default", choices=["default", "ag2"],
                   help="gws account: 'default' or 'ag2' (ag2.ai config dir).")
    p.add_argument("--gmail-lookback", type=int, default=GMAIL_LOOKBACK_DEFAULT,
                   help="Days of Gmail history to scan when resolving names.")
    # Safety switches — dry-run is the default.
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--send", action="store_true",
                      help="Actually create the event + email invites. Without "
                           "this, the run is a dry-run (no writes).")
    mode.add_argument("--dry-run", action="store_true",
                      help="Explicit dry-run (the default anyway).")
    p.add_argument("--force", action="store_true",
                   help="Create even when a conflict or duplicate is detected "
                        "(owner override). Ignored in dry-run.")
    p.add_argument("--self-check", action="store_true",
                   help="Run the offline logic self-check and exit (no network).")
    p.add_argument("--verbose", action="store_true", help="Verbose diagnostics.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.self_check:
        return self_check()

    # Required-for-a-real-run args (kept out of argparse `required=` so that
    # --self-check / --help stay side-effect- and arg-free).
    missing = [n for n in ("title", "when") if not getattr(args, n)]
    if missing:
        print(f"error: missing required arg(s): {', '.join('--' + m for m in missing)}",
              file=sys.stderr)
        return 2

    tz = args.timezone or detect_timezone()
    try:
        start = parse_when(args.when)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    end = compute_end(start, args.duration_min)

    is_send = args.send  # dry-run is the default whenever --send is absent

    print(f"Meeting:  {args.title}")
    print(f"When:     {start.isoformat()} → {end.isoformat()}  ({tz})")
    if args.location:
        print(f"Location: {args.location}")
    print(f"Mode:     {'SEND (will create + email invites)' if is_send else 'DRY-RUN (no changes)'}")

    # 1) attendees --------------------------------------------------------- #
    emails: list[str] = [e.strip() for e in args.attendees.split(",") if e.strip()]
    unresolved: list[str] = []
    if args.resolve.strip():
        names = [n for n in args.resolve.split(",") if n.strip()]
        print(f"\nResolving {len(names)} name(s) via Gmail…")
        for r in resolve_names(names, args.account, args.gmail_lookback, args.verbose):
            if r.get("email"):
                emails.append(r["email"])
                alt = ""
                if r.get("alternates"):
                    alt = "  (alts: " + ", ".join(a["email"] for a in r["alternates"]) + ")"
                print(f"  {r['name']!r} → {r['email']}{alt}")
            else:
                unresolved.append(r["name"])
                print(f"  {r['name']!r} → (unresolved){' — ' + r['error'] if r.get('error') else ''}")
    # de-dup + preserve order
    emails = list(dict.fromkeys(emails))
    if not emails:
        print("\nerror: no attendee emails (none given, none resolved).", file=sys.stderr)
        return 2
    print(f"\nAttendees ({len(emails)}): {', '.join(emails)}")
    if unresolved:
        print(f"Unresolved names: {', '.join(unresolved)} "
              "— pass their emails via --attendees or drop them.")

    # 2 + 3) conflict + dedup (one calendar read serves both) -------------- #
    try:
        day_events = list_events_for_day(start, args.account, args.calendar, tz)
    except RuntimeError as e:
        print(f"\nerror: calendar read failed: {e}", file=sys.stderr)
        return 1
    conflicts = find_conflicts(day_events, start, end)
    duplicates = find_duplicates(day_events, args.title)

    if conflicts:
        print(f"\n⚠ CONFLICT: {len(conflicts)} overlapping event(s) on the owner's calendar:")
        for ev in conflicts:
            print(f"    - {ev.get('summary','(no title)')}  "
                  f"{(ev.get('start') or {}).get('dateTime','?')}")
    else:
        print("\n✓ Slot is free on the owner's calendar.")

    if duplicates:
        print("⚠ DUPLICATE: a same-title event already exists that day:")
        for ev in duplicates:
            print(f"    - {ev.get('summary')}  {(ev.get('start') or {}).get('dateTime','?')}  "
                  f"{ev.get('htmlLink','')}")
    else:
        print("✓ No same-title event that day (not a duplicate).")

    # An unresolved name (no Gmail match, or an ambiguous tie we refused to
    # guess) is ALSO a send-blocker: better to refuse than email an invite to
    # the wrong person. --force (owner's explicit call) or supplying the address
    # via --attendees is the way past it.
    blocked = bool(conflicts or duplicates or unresolved)

    # 4) create ------------------------------------------------------------ #
    if not is_send:
        print("\nDRY-RUN: not creating. Re-run with --send to create + email invites"
              + (" (add --force to override the warnings above)." if blocked else "."))
        return 0

    if blocked and not args.force:
        reasons = []
        if conflicts or duplicates:
            reasons.append("conflict/duplicate detected")
        if unresolved:
            reasons.append(f"unresolved name(s): {', '.join(unresolved)}")
        print("\nREFUSING to create: " + "; ".join(reasons) + ". "
              "Re-run with --send --force to override (owner's explicit call), "
              "supply the email via --attendees, or pick a different slot/title.",
              file=sys.stderr)
        return 3

    print("\nCreating event + sending invites (sendUpdates=all)…")
    try:
        ev = create_event(args.title, start, end, tz, emails, args.location,
                           args.description, args.account, args.calendar)
    except RuntimeError as e:
        print(f"error: event creation failed: {e}", file=sys.stderr)
        return 1
    link = ev.get("htmlLink", "(no link returned)")
    print("✓ Created.")
    print(f"Event link: {link}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
