#!/usr/bin/env python3
"""Hold a summoned agent in the surfaces it was called into.

A summon asks an agent to *be* somewhere, but a task is short-lived and the
presence it establishes is not: `watch` is a foreground child of whichever
session ran it, so the agent drops out the moment that session ends. This is
the host that outlives it.

It decides nothing. `presence_policy.plan` says what to connect and what to
drop; `presence_store` holds the two records; this module owns only sockets,
the clock and the process. Keeping it that thin is what makes the rules
testable without a server.

Run under a supervisor, not once: a surface session ends on its own (a clean
close, a refusal, a service restart), and a launcher that does not restart
leaves the agent quietly absent.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import presence_policy as policy  # noqa: E402
import presence_store as store  # noqa: E402

# Long enough that a reconnect storm cannot spin, short enough that a summon
# read from the record is acted on while the person who sent it is still there.
RECONCILE_SECONDS = 5.0


def desired_path(workspace: Path) -> Path:
    return workspace / "state" / "room-collab-presence.json"


def live_path(workspace: Path) -> Path:
    return workspace / "state" / "room-collab-presence-live.json"


class Held:
    """One surface this daemon is holding open, and when it last saw activity."""

    def __init__(self, entry: dict, now: float) -> None:
        self.entry = entry
        self.since = now
        self.last_activity = now
        self.task: asyncio.Task | None = None
        self.connected = False

    @property
    def key(self) -> tuple[str, str]:
        return policy.key_of(self.entry)

    def row(self, state: str) -> dict:
        # `state` is whether this DAEMON holds the surface; `transport` is
        # whether the socket is up. A reconnecting surface is held and down.
        row = {"room": self.entry.get("room"), "kind": self.entry.get("kind"),
               "state": state, "since": self.since, "last_activity": self.last_activity}
        if state == policy.CONNECTED:
            row["transport"] = "up" if self.connected else "down"
        return row


class Daemon:
    def __init__(self, workspace: Path, url: str, token: str, *,
                 idle_seconds: float = policy.IDLE_SECONDS,
                 cap: int = policy.MAX_CONNECTIONS,
                 insecure: bool = False, clock=time.time,
                 max_backoff: float = 30.0) -> None:
        self.ws = workspace
        self.url = url
        self.token = token
        self.idle_seconds = idle_seconds
        self.cap = cap
        self.insecure = insecure
        # ONE clock: `hold` stamping time.time() against a `reconcile(now)` is
        # two clocks that agree only by luck, and an untestable idle rule.
        self.clock = clock
        self.max_backoff = max_backoff
        self.held: dict[tuple[str, str], Held] = {}
        # A surface the daemon stopped holding, and why: `plan` reads this to
        # decide whether it may come back without a new summon.
        self.retired: dict[tuple[str, str], dict] = {}
        self.resumed = False

    def resume(self) -> None:
        """Take back what the last run remembered. Idempotent; the first
        reconcile calls it, so no caller has to remember to.

        Only `idle` and `capped` survive a restart: a CONNECTED row describes a
        socket that died with the process, and `plan` will reopen it. Without
        this the 30-minute rule resets on every restart — the record was
        written and never read, which is not durable, only written down.
        """
        if self.resumed:
            return
        self.resumed = True
        for row in store.read_entries(live_path(self.ws)):
            key = policy.key_of(row)
            if all(key) and row.get("state") in (policy.IDLE, policy.CAPPED):
                self.retired[key] = dict(row)
        if self.retired:
            print(f"presence: resumed {len(self.retired)} remembered surface(s)", flush=True)

    # --- the record the policy reads, assembled from what is actually held
    def live_rows(self) -> list[dict]:
        rows = [h.row(policy.CONNECTED) for h in self.held.values()]
        rows.extend(self.retired.values())
        return rows

    def publish(self) -> None:
        store.write_entries(live_path(self.ws), self.live_rows())

    async def hold(self, held: Held) -> None:
        """Keep one surface open, and put it back when the transport dies.

        Any change on the surface counts as activity, not only what names this
        agent: a document someone else is editing is when presence matters.
        """
        from room_collab_client import open_room_collab

        entry = held.entry
        failures = 0
        while True:
            try:
                async with open_room_collab(self.url, entry["room"], self.token,
                                            kind=entry["kind"], insecure=self.insecure) as doc:
                    name = entry.get("name") or entry.get("identity")
                    if name:
                        await doc.set_presence(str(name), user_id=entry.get("identity"))
                    held.connected = True
                    held.last_activity = self.clock()
                    failures = 0

                    def touched() -> None:
                        held.last_activity = self.clock()

                    stop = doc.on_activity(touched)
                    try:
                        # Awaited, not slept through: the read loop records the
                        # end, it never raises into a holder that only sleeps.
                        raise await doc.closed()
                    finally:
                        stop()
            except asyncio.CancelledError:
                held.connected = False
                raise
            except Exception as exc:  # noqa: BLE001 - one surface must not end the daemon
                held.connected = False
                failures += 1
                wait = min(2 ** failures, self.max_backoff)
                print(f"presence: {entry.get('room')} ({entry.get('kind')}) lost: {exc}; "
                      f"reconnecting in {wait:.0f}s", flush=True)
                await asyncio.sleep(wait)

    def start(self, entry: dict, now: float) -> None:
        held = Held(entry, now)
        held.task = asyncio.ensure_future(self.hold(held))
        self.held[held.key] = held
        self.retired.pop(held.key, None)
        print(f"presence: joined {entry.get('room')} ({entry.get('kind')})", flush=True)

    async def stop(self, key: tuple[str, str], reason: str, now: float) -> None:
        held = self.held.pop(key, None)
        if held is None:
            return
        if held.task is not None:
            held.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await held.task
        # `left` is forgotten; idle and capped must not be, or the next pass
        # rejoins what this one dropped.
        if reason != "left":
            state = policy.IDLE if reason == "idle" else policy.CAPPED
            self.retired[key] = {"room": key[0], "kind": key[1], "state": state,
                                 "since": now, "last_activity": held.last_activity}
        print(f"presence: left {key[0]} ({key[1]}): {reason}", flush=True)

    async def reconcile(self, now: float) -> None:
        self.resume()
        try:
            desired = store.read_entries(desired_path(self.ws))
        except store.RecordUnreadable as exc:
            # Keep holding what we hold. An unreadable record is not a request
            # to leave, and evicting on one drops the agent everywhere.
            print(f"presence: desired record unreadable, holding: {exc}", flush=True)
            return
        want = {policy.key_of(e) for e in desired}
        # A surface nobody asks for any more has nothing left to remember.
        for key in [k for k in self.retired if k not in want]:
            self.retired.pop(key, None)
        step = policy.plan(desired, self.live_rows(), now,
                           idle_seconds=self.idle_seconds, cap=self.cap)
        for key, reason in step["drop"]:
            await self.stop(key, reason, now)
        for entry in step["connect"]:
            self.start(entry, now)
        self.publish()

    async def run(self) -> int:
        print(f"presence daemon: watching {desired_path(self.ws)} "
              f"(idle {self.idle_seconds:.0f}s, cap {self.cap})", flush=True)
        while True:
            try:
                await self.reconcile(self.clock())
            except Exception as exc:  # noqa: BLE001 - a bad pass must not end the daemon
                print(f"presence: reconcile failed: {exc}", flush=True)
            await asyncio.sleep(RECONCILE_SECONDS)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="hold a summoned agent in its surfaces")
    ap.add_argument("--workspace")
    ap.add_argument("--url")
    ap.add_argument("--token")
    ap.add_argument("--idle-seconds", type=float, default=policy.IDLE_SECONDS)
    ap.add_argument("--cap", type=int, default=policy.MAX_CONNECTIONS)
    ap.add_argument("--insecure", action="store_true")
    a = ap.parse_args(argv)

    import room_collab  # the one place credentials and the workspace resolve

    ws = Path(a.workspace) if a.workspace else room_collab._workspace(None)
    try:
        url = room_collab.resolve_url(a.url)
        token = room_collab.resolve_token(a.token)
    except Exception as exc:  # noqa: BLE001 - a missing credential is a message, not a trace
        print(f"presence daemon: {exc}", file=sys.stderr)
        return 2
    d = Daemon(ws, url, token, idle_seconds=a.idle_seconds, cap=a.cap, insecure=a.insecure)
    try:
        return asyncio.run(d.run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
