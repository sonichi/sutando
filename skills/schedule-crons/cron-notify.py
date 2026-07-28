#!/usr/bin/env python3
"""cron-notify — pure logic for the cron-room → owner-active-channel notification bridge.

Track 13 (typed rooms as management surfaces), owner ask 2026-07-11 23:23Z:
> "Dedicated cron-rooms declutter but hurt discoverability. Each cron should, ONLY for
>  attention-worthy updates, post a ONE-LINE summary + deep-link into the owner's
>  currently-active channel, rate-limited. Full detail stays in the cron-room; the
>  active channel gets only the ping."

This module is the DECISION + FORMAT half — pure, side-effect-free, unit-tested:
  - is_attention_worthy(kind, summary) — should this update ping at all?
  - format_ping(cron, summary, room_id, event_id) — the one-line + matrix.to deep-link
  - should_ping_now(state, cron, now, min_interval_s) — per-cron rate-limit

The DELIVERY half (which owner channel, and HOW to hand off) is deliberately NOT wired
here: `results/proactive-*.txt` has a 9-duplicate-delivery history (see
src/proactive_routing.py — the module that exists to make that path single-delivery),
so the hand-off is a landmine that needs owner-reviewed care + live E2E. Ship the tested
core now; gate delivery. Mirrors how the attention primitive shipped (analysis half
first, act half owner-shaped).

The routing target, once delivery is wired, reuses proactive_routing.should_claim_proactive:
the owner's active bridge channel (discord/telegram) is where the ping lands, so the
matrix.to deep-link is clickable there. If the active channel is a non-bridge surface
(voice/ag2space/github-commits), that module already defaults to discord.
"""
from __future__ import annotations

# Update kinds a cron can emit. Only a subset is worth pulling the owner's attention
# to their active channel; routine "nothing changed" ticks stay silent (in-room only).
#   owner_action — the cron needs an owner decision/input  → PING
#   error        — the cron failed / hit a blocker          → PING
#   digest       — a periodic roll-up worth surfacing       → PING
#   routine      — a normal tick, nothing owner-actionable  → SILENT (in-room only)
_ATTENTION_KINDS = frozenset({"owner_action", "error", "digest"})

# Phrases that betray a "nothing happened" update even if mislabeled as attention-worthy.
# Defense against a cron that tags every tick `digest` — a digest with no news is noise.
_EMPTY_SIGNALS = (
    "nothing new", "no news", "no change", "no changes", "no updates",
    "nothing to report", "nothing changed", "silent", "no-op", "queue empty",
)


def is_attention_worthy(kind: str, summary: str = "") -> bool:
    """True iff this cron update should ping the owner's active channel.

    kind must be one of owner_action/error/digest to qualify; anything else
    (routine, unknown) stays in-room only. As a second gate, a qualifying kind
    whose summary reads as "nothing happened" is downgraded to silent — a
    digest/roundup with no actual news is exactly the noise the owner wanted
    to avoid. `error` is never downgraded (an error IS the news).
    """
    if not isinstance(kind, str) or kind not in _ATTENTION_KINDS:
        return False
    if kind == "error":
        return True
    s = (summary or "").lower()
    if any(sig in s for sig in _EMPTY_SIGNALS):
        return False
    return True


def deep_link(room_id: str, event_id: str = "", via: str = "ag2.space") -> str:
    """Build a matrix.to deep-link to a room (and optional event).

    Format per roadmap Track 13a: matrix.to/#/<room>/<event>?via=<homeserver>.
    A room-only link (no event) omits the event segment. `via` is the routing
    hint homeservers need to find the room; empty `via` drops the query.
    """
    frag = room_id if not event_id else f"{room_id}/{event_id}"
    base = f"https://matrix.to/#/{frag}"
    return f"{base}?via={via}" if via else base


def format_ping(cron: str, summary: str, room_id: str, event_id: str = "",
                via: str = "ag2.space", max_summary: int = 140) -> str:
    """One-line ping for the owner's active channel: `⏰ <cron>: <summary> → <deep-link>`.

    Summary is collapsed to a single line and truncated (the full detail lives in the
    cron-room the link points to). The deep-link targets the specific event when given,
    else the room.
    """
    one_line = " ".join((summary or "").split())
    if len(one_line) > max_summary:
        one_line = one_line[: max_summary - 1].rstrip() + "…"
    link = deep_link(room_id, event_id, via)
    return f"⏰ {cron}: {one_line} → {link}"


def should_ping_now(state: dict, cron: str, now: int, min_interval_s: int = 1800) -> bool:
    """Per-cron rate-limit: True iff `cron` hasn't pinged within the last min_interval_s.

    `state` maps cron name → last-ping epoch seconds (the caller persists it). A cron
    absent from state (never pinged) always passes. Non-numeric/negative stored values
    are treated as "never pinged" (fail-open toward delivering an attention-worthy ping
    rather than swallowing it). Default cooldown 30 min bounds a flapping cron to at
    most 2 pings/hour into the active channel.
    """
    if not isinstance(state, dict):
        return True
    last = state.get(cron)
    try:
        last = int(last)
    except (TypeError, ValueError):
        return True
    if last < 0:
        return True
    return (now - last) >= min_interval_s


def record_ping(state: dict, cron: str, now: int) -> dict:
    """Return `state` with `cron`'s last-ping stamped to `now` (mutates + returns)."""
    if not isinstance(state, dict):
        state = {}
    state[cron] = int(now)
    return state


# ─────────────────────────────────────────────────────────────────────────────
# CLI — room delivery (the SAFE delivery path: gateway op:message, single-delivery).
#
# This wires the pure logic above to an actual send, but ONLY to an explicit target
# room (`--room`), via the same op:message call every cron already uses to post its
# output. It does NOT touch `results/proactive-*.txt` (the dupe-prone owner-DM path,
# still gated on owner review). A cron calls this to emit an attention-filtered,
# rate-limited, deep-linked ping into a room the owner monitors.
# ─────────────────────────────────────────────────────────────────────────────
def _load_gateway(env_path=".env"):
    """Return (base_url, secret) from AG2_REMOTE_TOKEN='url|secret' in .env, or (None, None)."""
    import os
    try:
        for line in open(env_path):
            if line.startswith("AG2_REMOTE_TOKEN="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if "|" in v:
                    base, secret = v.split("|", 1)
                    return base.strip().strip("'").strip('"'), secret.strip()
    except OSError:
        pass
    return None, None


def _post_to_room(room_id, body, env_path=".env"):
    """Send `body` to `room_id` via gateway op:message. Returns event_id or None."""
    import json
    import urllib.error
    import urllib.request
    base, secret = _load_gateway(env_path)
    if not base:
        return None
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/room",
        data=json.dumps({"op": "message", "room_id": room_id, "body": body}).encode(),
        headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json",
                 "User-Agent": "sutando-core/1.0"})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return (json.loads(r.read().decode() or "{}")).get("event_id")
    except (urllib.error.URLError, urllib.error.HTTPError):
        return None


def _load_state(path):
    import json
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return {}


def main(argv=None):
    """cron-notify CLI. Decide (attention + rate-limit) → format → post to --room.

    Exit 0 = posted; exit 3 = suppressed (not attention-worthy / rate-limited);
    exit 2 = usage/gateway error. --dry-run prints the decision + would-be body
    without sending (and without stamping the rate-limit state)."""
    import argparse
    import time
    ap = argparse.ArgumentParser(description="Emit an attention-filtered cron ping to a room.")
    ap.add_argument("--cron", required=True, help="cron name (shown in the ping)")
    ap.add_argument("--summary", required=True, help="one-line update text")
    ap.add_argument("--kind", default="digest", help="owner_action|error|digest|routine")
    ap.add_argument("--room", required=True, help="target room id (the ping destination)")
    ap.add_argument("--event-id", default="", help="cron-room event to deep-link to")
    ap.add_argument("--via", default="ag2.space")
    ap.add_argument("--state-file", default="", help="JSON rate-limit state (cron→last-ping epoch)")
    ap.add_argument("--min-interval", type=int, default=1800)
    ap.add_argument("--now", type=int, default=0, help="override epoch (testing); 0=real time")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    now = args.now or int(time.time())

    if not is_attention_worthy(args.kind, args.summary):
        print(f"suppressed: not attention-worthy (kind={args.kind})")
        return 3

    state = _load_state(args.state_file) if args.state_file else {}
    if not should_ping_now(state, args.cron, now, args.min_interval):
        print(f"suppressed: rate-limited (last ping < {args.min_interval}s ago)")
        return 3

    body = format_ping(args.cron, args.summary, args.room, args.event_id, args.via)

    if args.dry_run:
        print("DRY-RUN would post:")
        print(body)
        return 0

    eid = _post_to_room(args.room, body)
    if not eid:
        print("error: post failed (no gateway / send error)")
        return 2
    if args.state_file:
        import json
        record_ping(state, args.cron, now)
        try:
            json.dump(state, open(args.state_file, "w"), indent=2)
        except OSError:
            pass
    print(f"posted: {eid}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
