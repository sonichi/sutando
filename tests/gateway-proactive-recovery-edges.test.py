#!/usr/bin/env python3
"""Recovery edges of the proactive outbox lane, each a way a message could post twice or be lost:

  1. two bodies parked under one file name -> the logged command for the first restores and
     sends only the first, once
  2. a restart past the retry deadline     -> a recorded receipt settles DELIVERED, an
     interrupted send parks outcome-unknown; neither is spent as an exhausted retry
  3. a crash right after the final refusal  -> the operator's requeue sends it (no stale mark)
  4. the owner DM changes after a refusal   -> the item is held (parked), never re-routed, and
     the logged operator release sends it to its own stored room, once
  5. A's body cannot be read when it parks, then B parks under the same name -> A gets no
     runnable command; an empty digest, or none at all, is refused (exit 5) before any change
  6. room-upgraded -> DM B (automatic requeue) -> 429 -> DM C  -> nothing is sent to B
  7. a drainer parks for a moved DM while another delivers     -> DELIVERED and its receipt survive
  8. the move to undelivered/ fails while holding               -> the next pass logs the release
  9. a delivered file that cannot be archived                   -> kept hidden, archived later, never resent
 10. restore() itself refuses an empty or malformed digest     -> ValueError, nothing moved
 11. a newer body written while a delivered file's archive fails -> untouched; sent once, A never again
 12. a dead drainer's expired claim, then a moved DM             -> parked with a usable release;
     a live owner's claim is still refused, and the handback is logged
 13. a refused room-upgrade park                                 -> handed back, never held as parked
 14. A refused while a newer B was written, then a second drain  -> A's claim survives, B sent once
 15. a torn claim past its grace, then a moved DM                -> swept, parked, usable release;
     a fresh torn claim is still refused
 16. a malformed claim naming a dead pid                         -> parked, logged with its file's age;
     one naming a live owner is refused
 17. a delivered file kept claimed, then orphan-recovered live   -> archived by its DELIVERED item, not resent

Run: python3 tests/gateway-proactive-recovery-edges.test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import

gb, _IMPORT_READS = isolate_then_import(env={"REMOTE_PROACTIVE_ROOM": ""})
import json
import shlex
import subprocess
import urllib.error

from _helpers import proactive_lane as pl
from _helpers.hermetic_env import isolated_child_env
from ag2_sparrow import outbox as vendored
from ag2_sparrow import undelivered_quarantine

REPO = Path(__file__).resolve().parents[1]
ISO = pl.scratch("gw-proactive-recovery-")
check = pl.Checks()


class Crash(BaseException):
    pass


def timeout(payload):
    raise urllib.error.URLError(TimeoutError("timed out"))


def refused(payload):
    raise urllib.error.HTTPError("https://gw.example/relay/v1/room", 429, "slow down", {}, None)


def run_logged(lane, stem: str, nth: int = 0):
    lines = [m for m in lane.logs if "outbox_cli.py" in m and stem in m]
    cmd = shlex.split(lines[nth].rsplit(": ", 1)[-1]) if len(lines) > nth else []
    if not cmd:
        return None
    return subprocess.run([sys.executable if c == "python3" else c for c in cmd], cwd=REPO,
                          env=isolated_child_env(), capture_output=True, text=True, timeout=60)


def die_as_restart(lane, item_id: str) -> None:
    """The claim's owner is a dead process past its reclaim window, and the file is pending."""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    claim_file = vendored._claim_path(lane.results / ".outbox-ag2space-proactive", item_id)
    if claim_file.exists():
        data = json.loads(claim_file.read_text())
        claim_file.write_text(json.dumps({**data, "pid": dead.pid, "start_usec": None,
                                          "claimed_at": data["claimed_at"] - 3600}))
    for c in lane.results.glob("proactive-*.sending.*"):
        c.rename(lane.results / (c.name.split(".sending.")[0] + ".txt"))
    lane.restart()


def two_generations() -> None:
    lane = pl.Lane(gb, ISO, timeout)
    lane.write("proactive-1.txt", "body A\n")
    lane.drain()
    lane.write("proactive-1.txt", "body B\n")
    lane.drain()
    run = run_logged(lane, "proactive-1", 0)
    check(run is not None and run.returncode == 0, f"1. A's logged command runs ({run and run.stderr[-200:]})")
    restored = lane.results / "proactive-1.txt"
    check(restored.exists() and restored.read_text() == "body A\n",
          f"1. it restores A's body, not the newest ({restored.read_text() if restored.exists() else None!r})")
    lane.respond = lambda payload: {"ok": True, "event_id": "$A"}
    n = len(lane.posts)
    for _ in range(3):
        lane.drain()
    check([p.get("body") for p in lane.posts[n:]] == ["body A"],
          f"1. only A is sent, once ({[p.get('body') for p in lane.posts[n:]]})")
    left = sorted(p.read_text() for p in (lane.results / "undelivered").glob("proactive-1-*.txt"))
    check(left == ["body B\n"], f"1. B stays parked for its own recovery ({left})")


def past_the_deadline(with_receipt: bool) -> None:
    label = "with a receipt" if with_receipt else "interrupted, no receipt"
    lane = pl.Lane(gb, ISO, refused)
    lane.write("proactive-2.txt", "late\n")
    backend = lane.backend()
    now = [1_000_000.0]
    backend.clock = lambda: now[0]
    for t in (0, 2, 6, 14, 30):
        now[0] = 1_000_000.0 + t
        lane.drain()
    now[0] = 1_000_000.0 + 60
    item_id = gb._proactive_item_id("proactive-2", "late")
    real = gb._record_proactive_receipt
    if with_receipt:
        lane.respond = lambda payload: {"ok": True, "event_id": "$landed"}

        def record_then_die(*a):
            real(*a)
            raise Crash()
        gb._record_proactive_receipt = record_then_die
    else:
        def die(payload):
            raise Crash()
        lane.respond = die
    try:
        lane.drain()
    except Crash:
        pass
    finally:
        gb._record_proactive_receipt = real
    posts = len(lane.posts)
    die_as_restart(lane, item_id)
    backend = lane.backend()
    backend.clock = lambda: 1_000_000.0 + 700
    lane.respond = lambda payload: {"ok": True, "event_id": "$again"}
    for _ in range(3):
        lane.drain()
    item = vendored.read_item(lane.results / ".outbox-ag2space-proactive", item_id) or {}
    want = ("DELIVERED", None) if with_receipt else ("PARKED", "outcome-unknown")
    check((item.get("status"), item.get("reason")) == want and len(lane.posts) == posts,
          f"2. {label}: {want} after a restart past the deadline, no new POST "
          f"({item.get('status')}, {item.get('reason')}, {len(lane.posts) - posts} new)")


def crash_after_final_refusal() -> None:
    lane = pl.Lane(gb, ISO, refused)
    lane.write("proactive-3.txt", "three\n")
    backend = lane.backend()
    now = [2_000_000.0]
    backend.clock = lambda: now[0]
    for t in (0, 2, 6, 14):
        now[0] = 2_000_000.0 + t
        lane.drain()
    now[0] = 2_000_000.0 + 601
    real = vendored.clear_dispatching

    def die(*a, **k):
        raise Crash()
    vendored.clear_dispatching = die
    try:
        lane.drain()
    except Crash:
        pass
    finally:
        vendored.clear_dispatching = real
    item_id = gb._proactive_item_id("proactive-3", "three")
    root = lane.results / ".outbox-ag2space-proactive"
    item = vendored.read_item(root, item_id) or {}
    check(item.get("status") == "PARKED" and "dispatching" not in item,
          f"3. the final refusal parks in one write, the mark gone with it ({item.get('reason')}, {'dispatching' in item})")
    vendored.requeue_item_with_epoch(root, item_id, reset_attempts=True, operator="test", reason="operator")
    for c in lane.results.glob("proactive-3.sending.*"):
        c.rename(lane.results / "proactive-3.txt")
    lane.respond = lambda payload: {"ok": True, "event_id": "$ok"}
    n = len(lane.posts)
    lane.drain()
    item = vendored.read_item(root, item_id) or {}
    check(len(lane.posts) == n + 1 and item.get("status") == "DELIVERED",
          f"3. the operator's requeue sends it ({len(lane.posts) - n} POST, {item.get('status')}, {item.get('reason')})")


def dm_changes_after_refusal() -> None:
    lane = pl.Lane(gb, ISO, refused)
    lane.write("proactive-4.txt", "four\n")
    lane.drain()
    lane.owner_dm = "!NewOwnerDm:ag2.space"
    lane.respond = lambda payload: {"ok": True, "event_id": "$ok"}
    backend = lane.backend()
    backend.clock = lambda: gb.time.time() + 120
    n = len(lane.posts)
    lane.drain()
    lane.drain()
    item_id = gb._proactive_item_id("proactive-4", "four")
    item = vendored.read_item(lane.results / ".outbox-ag2space-proactive", item_id) or {}
    check(len(lane.posts) == n and lane.where("proactive-4") == "undelivered"
          and item.get("reason") == "destination-changed",
          f"4. held after the DM moved: not re-routed, parked, not re-claimed each pass "
          f"({len(lane.posts) - n} POSTs, {lane.where('proactive-4')}, {item.get('reason')})")
    check(any("HELD" in m and pl.OWNER_DM in m and "!NewOwnerDm:ag2.space" in m for m in lane.logs),
          "4. the hold names the stored and the new room")
    run = run_logged(lane, "proactive-4", 0)
    check(run is not None and run.returncode == 0, f"4. the logged release runs ({run and run.stdout[-160:]})")
    lane.drain()
    check(len(lane.posts) == n + 1 and lane.posts[-1].get("room_id") == pl.OWNER_DM,
          f"4. the operator's release sends it to its own stored room, once "
          f"({len(lane.posts) - n}, {lane.posts[-1].get('room_id') if lane.posts else None})")


def unreadable_first_generation() -> None:
    lane = pl.Lane(gb, ISO, timeout)
    lane.write("proactive-5.txt", "body A\n")
    real_bytes = Path.read_bytes
    Path.read_bytes = lambda self: (_ for _ in ()).throw(OSError("unreadable")) \
        if ".sending." in self.name else real_bytes(self)
    try:
        lane.drain()
    finally:
        Path.read_bytes = real_bytes
    lane.write("proactive-5.txt", "body B\n")
    lane.drain()
    root = lane.results / ".outbox-ag2space-proactive"
    a_id = gb._proactive_item_id("proactive-5", "body A")
    a_lines = [m for m in lane.logs if a_id in m or ("proactive-5" in m and "NO recovery" in m)]
    check(a_lines and not any("outbox_cli.py" in m for m in a_lines) and any("NO recovery command" in m for m in a_lines),
          f"5. A's park prints no runnable command ({[m[-120:] for m in a_lines]})")
    cmd = ["python3", "src/outbox_cli.py", "--root", str(root), "requeue", a_id,
           "--body-id", "proactive-5", "--body-sha256", ""]
    run = subprocess.run([sys.executable] + cmd[1:], cwd=REPO, env=isolated_child_env(),
                         capture_output=True, text=True, timeout=60)
    item = vendored.read_item(root, a_id) or {}
    check(run.returncode == 5 and item.get("status") == "PARKED" and not (lane.results / "proactive-5.txt").exists(),
          f"5. an empty digest is refused with exit 5 before any change (exit {run.returncode}, {item.get('status')})")
    run = subprocess.run([sys.executable] + cmd[1:-1] + ["abc"], cwd=REPO, env=isolated_child_env(),
                         capture_output=True, text=True, timeout=60)
    item = vendored.read_item(root, a_id) or {}
    check(run.returncode == 5 and item.get("status") == "PARKED",
          f"5. a malformed digest ('abc') is refused with exit 5 too (exit {run.returncode}, {item.get('status')})")
    run = subprocess.run([sys.executable] + cmd[1:-2], cwd=REPO, env=isolated_child_env(),
                         capture_output=True, text=True, timeout=60)
    item = vendored.read_item(root, a_id) or {}
    run = subprocess.run([sys.executable] + cmd[1:4] + ["--json"] + cmd[4:-2], cwd=REPO, env=isolated_child_env(),
                         capture_output=True, text=True, timeout=60)
    item = vendored.read_item(root, a_id) or {}
    want = {str(q): undelivered_quarantine.sha256_of(q)
            for q in (lane.results / "undelivered").glob("proactive-5-*.txt")}
    got = (json.loads(run.stdout or "{}") or {}).get("candidates")
    check(run.returncode == 5 and item.get("status") == "PARKED" and not (lane.results / "proactive-5.txt").exists()
          and len(want) == 2 and got == want,
          f"5. no digest while two bodies are parked: exit 5, nothing changed, both digests listed "
          f"(exit {run.returncode}, {item.get('status')}, {run.stdout[-200:]!r})")
    lane.respond = lambda payload: {"ok": True, "event_id": "$B"}
    n = len(lane.posts)
    for _ in range(2):
        lane.drain()
    left = sorted(p.read_text() for p in (lane.results / "undelivered").glob("proactive-5-*.txt"))
    check(len(lane.posts) == n and "body B\n" in left,
          f"5. B is not restored or sent in A's place ({len(lane.posts) - n} POSTs, parked {left})")


def automatic_requeue_is_not_a_release() -> None:
    upgraded = {"reason": "room_upgraded", "replacement_room": "!Replacement:ag2.space"}
    calls = {"n": 0}

    def respond(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError("https://gw.example/relay/v1/room", 400, "upgraded", {},
                                         __import__("io").BytesIO(json.dumps(upgraded).encode()))
        if calls["n"] == 2:
            raise urllib.error.HTTPError("https://gw.example/relay/v1/room", 429, "slow", {}, None)
        return {"ok": True, "event_id": "$sent"}
    lane = pl.Lane(gb, ISO, respond)
    saved_hook, gb.OWNER_DM_UPGRADED_NOTIFY = gb.OWNER_DM_UPGRADED_NOTIFY, lambda *a: None
    saved_state, gb._STATE = gb._STATE, lane.ws / "state"
    try:
        lane.write("proactive-6.txt", "six\n")
        lane.drain()
        lane.owner_dm = "!DmB:ag2.space"
        lane.drain()
        lane.owner_dm = "!DmC:ag2.space"
        backend = lane.backend()
        backend.clock = lambda: gb.time.time() + 120
        lane.drain()
        lane.drain()
    finally:
        gb.OWNER_DM_UPGRADED_NOTIFY, gb._STATE = saved_hook, saved_state
    to_b = [q for q in lane.posts if q.get("room_id") == "!DmB:ag2.space"]
    check(len(to_b) == 1 and lane.posts[-1].get("room_id") == "!DmB:ag2.space" and calls["n"] == 2,
          f"6. after the automatic requeue to B and a 429, nothing more goes to B once the DM is C "
          f"({[q.get('room_id') for q in lane.posts]})")


def park_races_a_delivery() -> None:
    lane = pl.Lane(gb, ISO, refused)
    lane.write("proactive-7.txt", "seven\n")
    lane.drain()
    item_id = gb._proactive_item_id("proactive-7", "seven")
    root = lane.results / ".outbox-ag2space-proactive"
    backend = lane.backend()
    backend.clock = lambda: gb.time.time() + 120
    lane.respond = lambda payload: {"ok": True, "event_id": "$won"}
    real = gb._payload_room
    fired = []

    def other_drainer_delivers(payload):
        room = real(payload)
        if not fired:
            fired.append(True)
            core = gb._delivery_core("proactive")
            saved = gb.RESULTS_DIR, gb._req
            gb.RESULTS_DIR, gb._req = lane.results, lane._req
            try:
                core.deliver_one(item_id, vendored.read_item(root, item_id)["payload"].encode())
            finally:
                gb.RESULTS_DIR, gb._req = saved
        return room
    lane.owner_dm = "!MovedDm:ag2.space"
    before = len(lane.posts)
    gb._payload_room = other_drainer_delivers
    try:
        lane.drain()
    finally:
        gb._payload_room = real
    item = vendored.read_item(root, item_id) or {}
    check(item.get("status") == "DELIVERED" and item.get("provider_ref") == "$won",
          f"7. the stale park did not erase the delivery ({item.get('status')}, {item.get('reason')}, {item.get('provider_ref')})")
    vendored.requeue_item_with_epoch(root, item_id, operator="test", operator_release=True) \
        if "operator_release" in vendored.requeue_item_with_epoch.__code__.co_varnames \
        else vendored.requeue_item_with_epoch(root, item_id, operator="test")
    lane.write("proactive-7.txt", "seven\n")
    lane.owner_dm = pl.OWNER_DM
    lane.drain()
    check(len(lane.posts) - before == 1,
          f"7. one POST, by the valid claimant; a later release cannot duplicate it ({len(lane.posts) - before})")


def hold_move_fails() -> None:
    lane = pl.Lane(gb, ISO, refused)
    lane.write("proactive-8.txt", "eight\n")
    lane.drain()
    lane.owner_dm = "!MovedDm8:ag2.space"
    backend = lane.backend()
    backend.clock = lambda: gb.time.time() + 120
    real = gb.undelivered_quarantine.place_as
    failed = []

    def fail_once(*a, **k):
        if not failed:
            failed.append(True)
            raise OSError("disk full")
        return real(*a, **k)
    gb.undelivered_quarantine.place_as = fail_once
    try:
        lane.drain()
        n = len(lane.logs)
        lane.drain()
    finally:
        gb.undelivered_quarantine.place_as = real
    later = lane.logs[n:]
    check(lane.where("proactive-8") == "undelivered" and any("outbox_cli.py" in m and "requeue" in m for m in later),
          f"8. after a failed move the next pass parks it and logs the release ({lane.where('proactive-8')}, "
          f"{[m[-100:] for m in later]})")


def delivered_archive_fails() -> None:
    lane = pl.Lane(gb, ISO)
    lane.write("proactive-9d.txt", "nine\n")
    real = Path.rename
    failed = []

    def rename(self, target):
        if ".sending." in self.name and Path(target).parent.name == "archive" and not failed:
            failed.append(True)
            raise OSError("archive unavailable")
        return real(self, target)
    Path.rename = rename
    try:
        lane.drain()
    finally:
        Path.rename = real
    live = sorted(q.name for q in lane.results.glob("proactive-*.txt"))
    hidden = sorted(q.name for q in lane.results.glob(".proactive-9d.delivered-*"))
    lane.drain()
    check(live == [] and len(hidden) == 1 and lane.where("proactive-9d") == "archived" and len(lane.posts) == 1,
          f"9. a delivered file that could not be archived is kept hidden from every bridge's glob, then "
          f"archived, never resent (live {live}, hidden {hidden}, {lane.where('proactive-9d')}, {len(lane.posts)} POSTs)")


def delivered_cannot_be_hidden() -> None:
    lane = pl.Lane(gb, ISO)
    lane.write("proactive-9h.txt", "nine h\n")
    real = Path.rename

    def rename(self, target):
        if ".sending." in self.name and (Path(target).parent.name == "archive"
                                         or Path(target).name.startswith(".proactive-9h")):
            raise OSError("read-only")
        return real(self, target)
    Path.rename = rename
    try:
        lane.drain()
    finally:
        Path.rename = real
    claims = list(lane.results.glob("proactive-9h.sending.*"))
    check(len(claims) == 1 and not (lane.results / "proactive-9h.txt").exists()
          and any("could not be hidden either" in m and claims[0].name in m for m in lane.logs),
          f"9b. archive and hide both fail: the delivered file stays claimed, logged with its path, "
          f"never live ({[q.name for q in lane.results.glob('proactive-9h*')]})")


def newer_body_survives_archive_failure() -> None:
    lane = pl.Lane(gb, ISO)
    lane.write("proactive-10.txt", "body A\n")
    real = Path.rename
    failed = []

    def rename(self, target):
        if ".sending." in self.name and Path(target).parent.name == "archive" and not failed:
            failed.append(True)
            (lane.results / "proactive-10.txt").write_text("body B\n", encoding="utf-8")
            raise OSError("archive unavailable")
        return real(self, target)
    Path.rename = rename
    try:
        lane.drain()
    finally:
        Path.rename = real
    canonical = lane.results / "proactive-10.txt"
    check(canonical.exists() and canonical.read_text() == "body B\n",
          f"11. a newer body written during the archive failure is untouched "
          f"({canonical.read_text() if canonical.exists() else None!r})")
    check(any(q.read_text() == "body A\n" for q in lane.results.glob(".proactive-10.delivered-*")),
          "11. the delivered body A is retained (hidden)")
    for _ in range(3):
        lane.drain()
    bodies = [q.get("body") for q in lane.posts]
    check(bodies == ["body A", "body B"], f"11. B is sent once and A never again ({bodies})")


def dead_claim_then_moved_dm() -> None:
    lane = pl.Lane(gb, ISO, refused)
    lane.write("proactive-12.txt", "twelve\n")
    lane.drain()
    item_id = gb._proactive_item_id("proactive-12", "twelve")
    root = lane.results / ".outbox-ag2space-proactive"
    vendored.acquire_delivery_claim(root, item_id, "crashed-drainer")
    die_as_restart(lane, item_id)
    lane.owner_dm = "!MovedDm12:ag2.space"
    lane.respond = lambda payload: {"ok": True, "event_id": "$12"}
    n = len(lane.posts)
    for _ in range(3):
        lane.drain()
    item = vendored.read_item(root, item_id) or {}
    check(any("released the expired claim of dead drainer pid" in m and item_id in m and "s old" in m
              for m in lane.logs), "12. the dead drainer's released claim is logged with item, pid and age")
    check(len(lane.posts) == n and item.get("status") == "PARKED" and item.get("reason") == "destination-changed"
          and lane.where("proactive-12") == "undelivered",
          f"12. a dead drainer's expired claim with a moved DM: parked destination-changed, no POST "
          f"({item.get('status')}, {item.get('reason')}, {lane.where('proactive-12')}, {len(lane.posts) - n})")
    run = run_logged(lane, "proactive-12", 0)
    lane.backend().clock = lambda: gb.time.time() + 120
    lane.drain()
    check(run is not None and run.returncode == 0 and len(lane.posts) == n + 1
          and lane.posts[-1].get("room_id") == pl.OWNER_DM,
          f"12. its logged release sends it once to its stored room ({run and run.returncode}, {len(lane.posts) - n})")

    lane = pl.Lane(gb, ISO, refused)
    lane.write("proactive-13.txt", "thirteen\n")
    lane.drain()
    item_id = gb._proactive_item_id("proactive-13", "thirteen")
    root = lane.results / ".outbox-ag2space-proactive"
    vendored.acquire_delivery_claim(root, item_id, "live-drainer")
    lane.owner_dm = "!MovedDm13:ag2.space"
    n = len(lane.posts)
    lane.drain()
    item = vendored.read_item(root, item_id) or {}
    check(len(lane.posts) == n and item.get("status") != "PARKED" and lane.where("proactive-13") == "pending"
          and any("another drainer holds it" in m for m in lane.logs),
          f"12. a live owner's claim is still refused, and the handback is logged ({item.get('status')})")


def retained_claim_survives_next_drain() -> None:
    lane = pl.Lane(gb, ISO)
    calls = {"n": 0}

    def respond(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            (lane.results / "proactive-14r.txt").write_text("body B\n", encoding="utf-8")
            raise urllib.error.HTTPError("https://gw.example/relay/v1/room", 429, "slow", {}, None)
        return {"ok": True, "event_id": f"$e{calls['n']}"}
    lane.respond = respond
    lane.write("proactive-14r.txt", "body A\n")
    lane.drain()
    lane.drain()
    retained = [q.read_text() for q in lane.results.glob("proactive-14r.sending.*")]
    sent = [q.get("body") for q in lane.posts]
    check(retained == ["body A\n"] and sent == ["body A", "body B"],
          f"14. a retained claim A survives the next drain, which sends the newer B once "
          f"(retained {retained}, sent {sent})")


def torn_claim_then_moved_dm() -> None:
    import os as _os
    for label, age, want_parked in (("old torn", 3600, True), ("fresh torn", 0, False)):
        lane = pl.Lane(gb, ISO, refused)
        stem = f"proactive-15{'o' if want_parked else 'f'}"
        lane.write(f"{stem}.txt", "fifteen\n")
        lane.drain()
        item_id = gb._proactive_item_id(stem, "fifteen")
        root = lane.results / ".outbox-ag2space-proactive"
        cp = vendored._claim_path(root, item_id)
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text("")
        old = gb.time.time() - age
        _os.utime(cp, (old, old))
        lane.owner_dm = f"!Moved{stem}:ag2.space"
        lane.respond = lambda payload: {"ok": True, "event_id": "$15"}
        n = len(lane.posts)
        for _ in range(3):
            lane.drain()
        item = vendored.read_item(root, item_id) or {}
        if want_parked:
            check(len(lane.posts) == n and item.get("reason") == "destination-changed"
                  and lane.where(stem) == "undelivered" and any("swept a torn claim" in m for m in lane.logs),
                  f"15. {label} claim + moved DM: swept, parked, no POST ({item.get('status')}, {lane.where(stem)})")
            run = run_logged(lane, stem, 0)
            lane.backend().clock = lambda: gb.time.time() + 120
            lane.drain()
            check(run is not None and run.returncode == 0 and len(lane.posts) == n + 1
                  and lane.posts[-1].get("room_id") == pl.OWNER_DM,
                  f"15. {label}: the logged release sends it once to its stored room")
        else:
            check(len(lane.posts) == n and item.get("status") != "PARKED" and lane.where(stem) == "pending",
                  f"15. {label} claim (a writer may be live): refused, nothing sent ({item.get('status')})")


def malformed_and_unknown_claims() -> None:
    import os as _os
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    for label, pid, want_parked in (("dead", dead.pid, True), ("live", _os.getpid(), False)):
        lane = pl.Lane(gb, ISO, refused)
        stem = f"proactive-16{label[0]}"
        lane.write(f"{stem}.txt", "sixteen\n")
        lane.drain()
        item_id = gb._proactive_item_id(stem, "sixteen")
        cp = vendored._claim_path(lane.results / ".outbox-ag2space-proactive", item_id)
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps({"item_id": item_id, "drainer_id": "x", "pid": pid, "claimed_at": "not-a-time"}))
        old = gb.time.time() - 3600
        _os.utime(cp, (old, old))
        lane.owner_dm = f"!Moved{stem}:ag2.space"
        n = len(lane.posts)
        for _ in range(2):
            lane.drain()
        item = vendored.read_item(lane.results / ".outbox-ag2space-proactive", item_id) or {}
        if want_parked:
            line = next((m for m in lane.logs if item_id in m and "claim" in m and "park it" in m), "")
            age = [int(w[1:].rstrip("s")) for w in line.split() if w.startswith("(") and w.endswith("s")]
            check(item.get("reason") == "destination-changed" and len(lane.posts) == n
                  and "malformed claim" in line and str(pid) in line and age and 3500 <= age[0] <= 3700,
                  f"16. a malformed claim naming a dead pid parks, logged with its file's age ({line[-120:]!r})")
        else:
            check(item.get("status") != "PARKED" and len(lane.posts) == n and lane.where(stem) == "pending",
                  f"16. a malformed claim naming a live owner is refused ({item.get('status')})")


def recovered_delivered_claim_not_resent() -> None:
    lane = pl.Lane(gb, ISO)
    lane.write("proactive-17.txt", "seventeen\n")
    real = Path.rename

    def rename(self, target):
        if ".sending." in self.name and (Path(target).parent.name == "archive"
                                         or Path(target).name.startswith(".proactive-17")):
            raise OSError("read-only")
        return real(self, target)
    Path.rename = rename
    try:
        lane.drain()
    finally:
        Path.rename = real
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    for c in lane.results.glob("proactive-17.sending.*"):
        rest = c.name.split(".sending.", 1)[1].split(".", 1)
        c.rename(c.with_name(f"proactive-17.sending.{dead.pid}.{rest[1] if len(rest) > 1 else '0'}"))
    lane.restart()
    recovered = lane.where("proactive-17")
    for _ in range(2):
        lane.drain()
    check(recovered == "pending" and len(lane.posts) == 1 and lane.where("proactive-17") == "archived",
          f"17. a delivered file orphan recovery returns to its live name is archived, not resent "
          f"({recovered} -> {lane.where('proactive-17')}, {len(lane.posts)} POSTs)")


def upgrade_park_refused() -> None:
    upgraded = {"reason": "room_upgraded", "replacement_room": "!Up:ag2.space"}

    def respond(payload):
        raise urllib.error.HTTPError("https://gw.example/relay/v1/room", 400, "upgraded", {},
                                     __import__("io").BytesIO(json.dumps(upgraded).encode()))
    lane = pl.Lane(gb, ISO, respond)
    held = []
    saved = gb._hold_owner_dm_upgrade
    gb._hold_owner_dm_upgrade = lambda *a, **k: held.append(a)
    backend = lane.backend()
    real_park = backend.park_unclaimed
    backend.park_unclaimed = lambda *a, **k: False
    try:
        lane.write("proactive-14.txt", "fourteen\n")
        lane.drain()
    finally:
        gb._hold_owner_dm_upgrade = saved
        backend.park_unclaimed = real_park
    check(held == [] and any("handed back, not held" in m for m in lane.logs),
          f"13. a refused upgrade park is not treated as parked ({held})")


def restore_refuses_empty_digest() -> None:
    results = pl.scratch("restore-empty-")
    (results / "undelivered").mkdir()
    (results / "undelivered" / "proactive-x-1.txt").write_text("x\n")
    try:
        out = undelivered_quarantine.restore(results, "proactive-x", body_sha256="")
        refused_it = False
    except ValueError:
        out, refused_it = None, True
    check(refused_it and (results / "undelivered" / "proactive-x-1.txt").exists()
          and not (results / "proactive-x.txt").exists(),
          f"10. restore() refuses an empty digest itself and moves nothing ({out})")
    try:
        undelivered_quarantine.restore(results, "proactive-x", body_sha256="abc")
        bad = False
    except ValueError:
        bad = True
    check(bad and (results / "undelivered" / "proactive-x-1.txt").exists(),
          "10. restore() refuses a malformed digest ('abc') too")


def main() -> int:
    automatic_requeue_is_not_a_release()
    park_races_a_delivery()
    hold_move_fails()
    delivered_archive_fails()
    delivered_cannot_be_hidden()
    newer_body_survives_archive_failure()
    dead_claim_then_moved_dm()
    upgrade_park_refused()
    retained_claim_survives_next_drain()
    torn_claim_then_moved_dm()
    malformed_and_unknown_claims()
    recovered_delivered_claim_not_resent()
    restore_refuses_empty_digest()
    unreadable_first_generation()
    two_generations()
    past_the_deadline(with_receipt=True)
    past_the_deadline(with_receipt=False)
    crash_after_final_refusal()
    dm_changes_after_refusal()
    return check.exit_code()


if __name__ == "__main__":
    sys.exit(main())
