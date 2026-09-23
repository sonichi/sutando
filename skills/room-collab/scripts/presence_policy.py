"""Which surfaces an agent should be connected to right now.

Pure decision rules: no sockets, no files, no clock of its own — `now` is
passed in. The daemon does the IO and asks this module what to do, so the
policy that decides an agent's visible presence is testable without a server.

Two records feed it, each with one writer:

  desired — the surfaces an agent was summoned into, written by the CLI when
            an agent reads a summon. The daemon never edits it: an eviction is
            "not connected right now", not "no longer wanted", so load dropping
            brings the surface back without a second summon.

  live    — what the daemon last observed, written only by the daemon. It must
            be durable: without it a restart re-joins everything, and the idle
            rule evaporates every time the daemon bounces.
"""
from __future__ import annotations

# Owner 2026-09-22: drop after 30 minutes with no activity on the surface.
IDLE_SECONDS = 1800.0
# A fuse, not a capacity plan: with the idle rule doing the real work the cap
# is not normally reached. 3 rooms x 3 surfaces = 9 is a full day's use.
MAX_CONNECTIONS = 16

CONNECTED = "connected"
CAPPED_REASON = "capped"
IDLE = "idle"
CAPPED = "capped"


def key_of(entry: dict) -> tuple[str, str]:
    """A surface is a (room, kind) pair. Both records key on this, so the
    daemon and the CLI cannot disagree about what "the same surface" is."""
    return (str(entry.get("room") or ""), str(entry.get("kind") or ""))


def _num(value: object, fallback: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float)) else fallback


def plan(
    desired: list[dict],
    live: list[dict],
    now: float,
    *,
    idle_seconds: float = IDLE_SECONDS,
    cap: int = MAX_CONNECTIONS,
) -> dict:
    """`{"connect": [entry...], "drop": [(key, reason)...]}` — what to change.

    Reasons are `left` (no longer summoned), `idle` (quiet past the timeout)
    and `capped` (over the fuse). They are distinct because only `idle` and
    `capped` leave the surface eligible to come back.
    """
    want = {key_of(e): e for e in desired if all(key_of(e))}
    seen = {key_of(e): e for e in live if all(key_of(e))}

    drop: list[tuple[tuple[str, str], str]] = []
    holding: dict[tuple[str, str], dict] = {}
    for key, entry in seen.items():
        if entry.get("state") != CONNECTED:
            continue
        if key not in want:
            drop.append((key, "left"))
        elif now - _num(entry.get("last_activity")) >= idle_seconds:
            drop.append((key, "idle"))
        else:
            holding[key] = entry

    # A surface idled out stays out until it is summoned again — otherwise the
    # next reconcile pass rejoins it and the timeout means nothing.
    def eligible(key: tuple[str, str]) -> bool:
        was = seen.get(key)
        if was is None or was.get("state") == CONNECTED:
            return True
        if was.get("state") == CAPPED:
            return True
        return _num(want[key].get("summoned_at")) > _num(was.get("since"))

    # A surface dropped in THIS pass is not a candidate in it: `seen` still
    # says CONNECTED, so `eligible` would wave the timed-out entry back in.
    dropped = {k for k, _ in drop}

    # Ranked newest-summon-first so a fresh summon wins the last slot over one
    # that has been waiting since yesterday.
    queue = sorted(
        (k for k in want if k not in holding and k not in dropped and eligible(k)),
        key=lambda k: (-_num(want[k].get("summoned_at")), k),
    )

    # LRU ADMISSION, not merely a ceiling. Shedding only when the cap is
    # LOWERED leaves a fresh summon queued behind 16 idle surfaces forever.
    quietest = sorted(holding, key=lambda k: (_num(holding[k].get("last_activity")), k))
    room = max(0, cap) - len(holding)

    def evict(key: tuple[str, str]) -> None:
        drop.append((key, CAPPED_REASON))
        holding.pop(key)

    while room < 0 and quietest:          # already over the cap
        evict(quietest.pop(0))
        room += 1
    # A candidate with no slot takes the quietest holder's if it is newer than
    # that holder's last activity; the queue is newest-first, so one loss ends it.
    i = 0
    while i < len(queue) and quietest and room <= i:
        if _num(want[queue[i]].get("summoned_at")) <= _num(holding[quietest[0]].get("last_activity")):
            break
        evict(quietest.pop(0))
        room += 1
        i += 1
    return {"connect": [want[k] for k in queue[:max(0, room)]], "drop": drop}
