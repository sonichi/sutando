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
    import re

    # Downgrade only when the summary reads as "nothing happened" AS A WHOLE —
    # i.e. every clause is just an empty-signal phrase — not when it merely
    # contains one as an incidental aside. Match signals against whole clauses
    # (equality), not `sig in s` (substring): the substring form silently
    # swallowed real owner_action/digest pings whose summary carried an
    # unrelated empty clause, e.g. "approve #2446; no changes needed elsewhere".
    s = " ".join((summary or "").split()).lower()
    if not s:
        return True
    for clause in re.split(r"[;,.]|—", s):
        c = clause.strip().strip(".!?,;:").strip()
        # tolerate a trailing temporal qualifier: "nothing new this pass"
        c = re.sub(
            r"\s+(?:this|last|next)?\s*"
            r"(?:pass|tick|run|cycle|time|today|tonight|now)$",
            "", c).strip()
        if c and c not in _EMPTY_SIGNALS:
            return True   # a substantive clause → attention-worthy
    return False          # every clause is an empty signal → downgrade


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


def save_state_atomic(path: str, state: dict) -> bool:
    """Write `state` to `path` atomically. True on success, False on any failure.

    temp-file + os.replace so a crash mid-write cannot leave a truncated or
    half-written cooldown file — a corrupt state file reads as "never pinged"
    and would let every cron ping on every fire. The boolean is the point: the
    caller must be able to distinguish "cooldown recorded" from "not recorded",
    because silently treating a failed write as success is what allows
    duplicate notifications.
    """
    import json
    import os
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


# ─────────────────────────────────────────────────────────────────────────────
# CLI — room delivery (the SAFE delivery path: gateway op:message, single-delivery).
#
# This wires the pure logic above to an actual send, but ONLY to an explicit target
# room (`--room`), via the same op:message call every cron already uses to post its
# output. It does NOT touch `results/proactive-*.txt` (the dupe-prone owner-DM path,
# still gated on owner review). A cron calls this to emit an attention-filtered,
# rate-limited, deep-linked ping into a room the owner monitors.
# ─────────────────────────────────────────────────────────────────────────────
_RESOLVE_TOKEN = None


def _canonical_resolver():
    """Lazily load resolve_token() from the sibling ensure-cron-room.py.

    Its main() is `__main__`-guarded, so importing the module only defines its
    functions (no side effects). Reusing that one resolver keeps a SINGLE
    gateway-cred contract across the schedule-crons skill instead of a second,
    drift-prone copy — the file has a hyphen so it can't be a plain `import`."""
    global _RESOLVE_TOKEN
    if _RESOLVE_TOKEN is None:
        import importlib.util
        from pathlib import Path
        sib = Path(__file__).resolve().parent / "ensure-cron-room.py"
        spec = importlib.util.spec_from_file_location("_ensure_cron_room", sib)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _RESOLVE_TOKEN = mod.resolve_token
    return _RESOLVE_TOKEN


def _repo_root():
    from pathlib import Path
    return str(Path(__file__).resolve().parents[2])


def _load_gateway(repo=None):
    """Return (base_url, secret) for the gateway, or (None, None) if unconfigured.

    Delegates to the canonical resolver (ensure-cron-room.py:resolve_token) so
    cron-notify honors the SAME cred contract as every other client: process env
    AND <repo>/.env (process env wins), all alias keys
    (GATEWAY_*/RELAY_*/REMOTE_TASK_*/AG2_REMOTE_*), and both a combined
    "url|secret" token and a split URL+token. Repo-root anchored, so a cron
    invoked from any cwd still resolves — the prior cwd-relative, `.env`-only,
    `AG2_REMOTE_TOKEN`-only reader returned (None, None) for a supported
    split-token install and from a non-repo cwd (#2346 review)."""
    return _canonical_resolver()(repo if repo is not None else _repo_root())


def _post_to_room(room_id, body, repo=None):
    """Send `body` to `room_id` via gateway op:message. Returns event_id or None."""
    import json
    import urllib.error
    import urllib.request
    base, secret = _load_gateway(repo)
    if not base:
        return None
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/room",
        data=json.dumps({"op": "message", "room_id": room_id, "body": body}).encode(),
        headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json",
                 "User-Agent": "sutando-core/1.0"})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        body_json = json.loads(r.read().decode() or "{}")
    except (urllib.error.URLError, urllib.error.HTTPError):
        return None
    # A 2xx IS delivery: the gateway may answer {"ok": true} with no event_id,
    # so keying the return on that field reports every success as a failure.
    return body_json.get("event_id") or body_json.get("ok") or True


def _default_state_file():
    """Canonical workspace-backed rate-limit state path, used when --state-file
    is omitted. Without this the default was empty → every process started from
    {} and the cooldown never applied, so the default invocation posted on every
    fire with no rate limit (#2346 review). Anchored to the resolved workspace so
    the cooldown persists across processes regardless of cwd."""
    import os
    import sys
    from pathlib import Path
    src = str(Path(__file__).resolve().parents[2] / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from workspace_default import resolve_workspace
    path = resolve_workspace() / "state" / "cron-notify-cooldown.json"
    # Create the managed default's parent so a CLEAN install (no state/ yet) can
    # acquire the sidecar lock — otherwise fail-closed refuses every default ping
    # until some unrelated service happens to create state/ (#2346 review). This
    # applies ONLY to the managed default: an explicit --state-file with a missing
    # parent stays fail-closed by design (an unwritable target refuses to post).
    # Best-effort — if the mkdir fails the lock still fails closed, never posts.
    try:
        os.makedirs(path.parent, exist_ok=True)
    except OSError:
        pass
    return str(path)


def _load_state(path):
    import json
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return {}


class _StateLock:
    """Cross-process exclusive lock over the load→check→reserve transaction.

    save_state_atomic() prevents torn JSON but not overlapping reservations:
    without a lock, two same-cron fires both _load_state() before either saves,
    both pass the cooldown, and both post — defeating the per-cron noise bound —
    and two DIFFERENT crons race the shared file's read-modify-write, clobbering
    each other's stamp (#2346 review). flock serializes that whole transaction.

    The lock is a DEDICATED sidecar `<state_file>.lock`, never the state file
    itself: save_state_atomic() does os.replace(), which swaps the state file's
    inode on every write, so a lock held on the old inode would not exclude a
    process that opened the new one. The sidecar is created once and never
    replaced, so its inode is stable. flock is per-open-file-description, so even
    two threads/processes each opening their own fd contend correctly. Held
    across load→check→reserve and released BEFORE network I/O, so a slow/hung
    send never blocks another cron's reservation.

    FAIL-CLOSED: acquisition failure PROPAGATES (OSError) — it is never silently
    downgraded to running unlocked. A sidecar that can't be opened does NOT imply
    the save will fail: the sidecar can be unopenable (e.g. a directory sits at
    its path, or a filesystem fault) while the state file's parent stays writable,
    so a "best-effort no-lock" would reserve+post WITHOUT exclusivity and restore
    the exact duplicate-notification race this lock exists to prevent (#2346
    review, john's directory-at-`.lock` repro). The caller must treat an
    acquisition failure as "refuse to reserve/post", not "continue".
    """

    def __init__(self, lock_path):
        self._lock_path = lock_path
        self._fd = None

    def __enter__(self):
        import fcntl
        import os
        # os.open raising OSError propagates with no fd yet → caller fails closed.
        # If flock raises AFTER open, __enter__ does not return so __exit__ never
        # runs — we must close the just-opened fd here or it leaks (one per faulted
        # invocation). Publish self._fd only once the lock is fully held.
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, *exc):
        import fcntl
        import os
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
        return False


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
    ap.add_argument(
        "--link-room",
        default="",
        help="room the deep-link points at (default: --room). Set this when the "
             "ping is delivered to one room but the detail event lives in another "
             "— e.g. cron-room detail linked into the owner's active channel.",
    )
    ap.add_argument("--event-id", default="", help="cron-room event to deep-link to")
    ap.add_argument("--via", default="ag2.space")
    ap.add_argument("--state-file", default="",
                    help="JSON rate-limit state (cron→last-ping epoch). Default: "
                         "<workspace>/state/cron-notify-cooldown.json — omitting it "
                         "still rate-limits, it no longer means 'no persistence'.")
    ap.add_argument("--min-interval", type=int, default=1800)
    ap.add_argument("--now", type=int, default=0, help="override epoch (testing); 0=real time")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    now = args.now or int(time.time())

    # Empty --state-file no longer disables persistence — it resolves to the
    # canonical workspace-backed path so the DEFAULT invocation is rate-limited
    # too (the empty default previously started every process from {}, so the
    # cooldown never applied and the default posted on every fire — #2346).
    state_file = args.state_file or _default_state_file()

    if not is_attention_worthy(args.kind, args.summary):
        print(f"suppressed: not attention-worthy (kind={args.kind})")
        return 3

    # Destination and deep-link target are DIFFERENT identities. --event-id is a
    # cron-room event; linking it under the destination room yields a matrix.to
    # URL that cannot resolve the event. Default to --room so room-local
    # delivery is unchanged.
    link_room = args.link_room or args.room
    body = format_ping(args.cron, args.summary, link_room, args.event_id, args.via)

    if args.dry_run:
        # Read-only: honor the rate-limit for an accurate decision, but never
        # reserve — a dry-run must not stamp the cooldown, so no lock is needed.
        state = _load_state(state_file)
        if not should_ping_now(state, args.cron, now, args.min_interval):
            print(f"suppressed: rate-limited (last ping < {args.min_interval}s ago)")
            return 3
        print("DRY-RUN would post:")
        print(body)
        return 0

    # load → rate-limit check → reserve MUST be ONE cross-process-exclusive
    # transaction. Atomic writes stop torn JSON but not overlapping reservations:
    # without the lock, two same-cron fires both _load_state() before either
    # saves, both pass the cooldown, both post — defeating the noise bound — and
    # two DIFFERENT crons race the shared file's read-modify-write, clobbering
    # each other's stamp (#2346 review). The lock is released BEFORE network I/O
    # so a slow/hung send never blocks another cron's reservation.
    lock_path = state_file + ".lock"
    try:
        with _StateLock(lock_path):
            state = _load_state(state_file)
            if not should_ping_now(state, args.cron, now, args.min_interval):
                print(f"suppressed: rate-limited (last ping < {args.min_interval}s ago)")
                return 3
            # Reserve the cooldown BEFORE delivering. Persisting after a successful
            # post means an unwritable state path returns "posted" with no cooldown
            # recorded, and the next fire duplicates the notification. Reserving
            # first inverts the failure: the worst case becomes one SUPPRESSED ping
            # (visible, self-heals on the next fire) instead of an unbounded
            # duplicate stream.
            prior = state.get(args.cron) if isinstance(state, dict) else None
            record_ping(state, args.cron, now)
            if not save_state_atomic(state_file, state):
                print(
                    "error: could not persist rate-limit state — refusing to post "
                    "(posting without a cooldown risks duplicate notifications)"
                )
                return 2
    except OSError:
        # FAIL CLOSED: the exclusive lock could not be acquired (unopenable
        # sidecar — a directory at its path, a filesystem fault, a permission
        # error). Do NOT post: without exclusivity, overlapping fires duplicate
        # pings and clobber other crons' stamps — the exact race the lock guards
        # (#2346). The only OSError that reaches here is from _StateLock.__enter__;
        # _load_state and save_state_atomic swallow their own OSErrors.
        print(
            "error: could not acquire the exclusive cooldown lock — refusing to "
            "post (posting without exclusivity risks duplicate notifications)"
        )
        return 2

    eid = _post_to_room(args.room, body)
    if not eid:
        # Delivery failed, so the reservation is a lie — release it, or a
        # transient send error would silently mute this cron for the whole
        # cooldown window. Re-acquire the lock and roll back ONLY if the stamp is
        # still the one WE wrote (== now): if a later fire already reserved and
        # posted after our cooldown elapsed, our stale rollback must not erase
        # that newer, valid reservation. Best-effort — a failed rollback write
        # keeps the reservation, which suppresses rather than duplicates.
        try:
            with _StateLock(lock_path):
                cur = _load_state(state_file)
                if isinstance(cur, dict) and cur.get(args.cron) == now:
                    if prior is None:
                        cur.pop(args.cron, None)
                    else:
                        cur[args.cron] = prior
                    save_state_atomic(state_file, cur)
        except OSError:
            # Can't acquire the lock to roll back — keep the reservation. That
            # suppresses this cron's next fire (self-heals), never duplicates, so
            # a lost rollback is the safe failure here (unlike the reserve path).
            pass
        print("error: post failed (no gateway / send error)")
        return 2

    print(f"posted: {eid}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
