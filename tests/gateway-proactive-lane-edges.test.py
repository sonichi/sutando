#!/usr/bin/env python3
"""The proactive lane's edges: the outbox writers it adds, the about-task grammar, and the
drain's fail paths (an auth refusal, a hand-back that fails, an unreadable task, a stale claim).

Run: python3 tests/gateway-proactive-lane-edges.test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from _helpers.hermetic_gateway import isolate_then_import

gb, _IMPORT_READS = isolate_then_import(env={"REMOTE_PROACTIVE_ROOM": ""})
import dataclasses
import json
import tempfile
import urllib.error

from _helpers import proactive_lane as pl
import importlib.util

# By file path: a flat `import outbox` resolves to the package's vendored copy, not src's.
_SPEC = importlib.util.spec_from_file_location("src_outbox_under_test", Path(__file__).resolve().parents[1] / "src" / "outbox.py")
src_outbox = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = src_outbox
_SPEC.loader.exec_module(src_outbox)
import proactive_dispatch_gate
import proactive_routing
import undelivered_quarantine
from ag2_sparrow.delivery_core import (DeliveryCore, DesignAClaimBackend, DrainStatus,
                                       ProviderCapabilities, ProviderPermanentRefused, RetryPolicy)
from ag2_sparrow.delivery_core.provider_ag2space import AG2SpaceRoomMessageProvider

ISO = pl.scratch("gw-proactive-edges-")
check = pl.Checks()


def outbox_writers() -> None:
    root = Path(tempfile.mkdtemp(dir=ISO))
    check(src_outbox.record_provider_ref(root, "absent", "$e") is False, "provider ref: a missing item is refused")
    backend = DesignAClaimBackend(root)
    backend.publish("i1", b'{"room_id": "!r:s", "body": "b"}')
    src_outbox.clear_dispatching(root, "i1")
    check("dispatching" not in src_outbox.read_item(root, "i1"), "clear with no mark writes nothing")
    with src_outbox._item_lock(root, "i1"):
        src_outbox.record_dispatching(root, "i1", "inc-1")
    with src_outbox._item_lock(root, "i1"):
        src_outbox.clear_dispatching(root, "i1")
    check("dispatching" not in src_outbox.read_item(root, "i1"), "a mark is cleared")
    check(src_outbox.record_provider_ref(root, "i1", "$e", destination="!r:s") is True,
          "provider ref recorded with its room")
    src_outbox.record_delivered(root, "i1", provider_ref="$e")
    d = src_outbox.read_item(root, "i1")
    check(d.get("status") == "DELIVERED" and d.get("provider_ref") == "$e" and d.get("destination") == "!r:s",
          f"delivered keeps the ref and room ({d.get('provider_ref')}, {d.get('destination')})")
    check(src_outbox.record_provider_ref(root, "i1", "$x") is False, "provider ref: a terminal item is refused")
    backend.publish("i2", b'{"room_id": "!old:s", "body": "b"}')
    src_outbox.park_item(root, "i2", "room-upgraded")
    src_outbox.requeue_item_with_epoch(root, "i2", payload='{"room_id": "!new:s", "body": "b"}')
    check(json.loads(src_outbox.read_item(root, "i2")["payload"])["room_id"] == "!new:s",
          "a requeue may replace the payload of a send that never left")


def guarded_park_and_release() -> None:
    root = Path(tempfile.mkdtemp(dir=ISO))
    check(src_outbox.park_unclaimed(root, "absent", "x") is False, "park: a missing item is refused")
    backend = DesignAClaimBackend(root)
    backend.publish("p1", b'{"room_id": "!a:s"}')
    src_outbox.acquire_delivery_claim(root, "p1", "someone")
    check(src_outbox.park_unclaimed(root, "p1", "x") is False, "park: a claimed item is refused")
    src_outbox.release_delivery_claim(root, "p1", "someone")
    check(src_outbox.park_unclaimed(root, "p1", "x") is True, "park: a live unclaimed item parks")
    check(src_outbox.park_unclaimed(root, "p1", "y") is False, "park: an already parked item is refused")
    src_outbox.requeue_item_with_epoch(root, "p1", operator="t", operator_release=True)
    d = src_outbox.read_item(root, "p1")
    check(src_outbox.released_by_operator(d), "an operator requeue releases exactly the stored payload")
    check(not src_outbox.released_by_operator({**d, "payload": '{"room_id": "!b:s"}'}),
          "a release does not cover another payload")


def held_while_claimed() -> None:
    def refuse(payload):
        raise urllib.error.HTTPError("https://gw.example/relay/v1/room", 429, "slow", {}, None)
    lane = pl.Lane(gb, ISO, refuse)
    lane.write("proactive-claimed.txt", "nudge\n")
    lane.drain()
    from ag2_sparrow import outbox as vendored
    item_id = gb._proactive_item_id("proactive-claimed", "nudge")
    vendored.acquire_delivery_claim(lane.results / ".outbox-ag2space-proactive", item_id, "other-drainer")
    lane.owner_dm = "!Elsewhere:ag2.space"
    n = len(lane.posts)
    lane.drain()
    check(len(lane.posts) == n and lane.where("proactive-claimed") == "pending",
          "a moved destination while another drainer holds the item: nothing sent, file handed back")


def handback_and_reaper() -> None:
    d = Path(tempfile.mkdtemp(dir=ISO))
    claim, live = d / "proactive-hb.sending.1", d / "proactive-hb.txt"
    claim.write_text("old\n")
    live.write_text("newer\n")
    logs, saved = [], gb._log
    gb._log = logs.append
    try:
        gb._hand_back(claim, live)
    finally:
        gb._log = saved
    check(live.read_text() == "newer\n" and claim.exists() and any("held by a newer file" in m for m in logs),
          "a handback never replaces a newer live file; the claim is kept and said")
    saved_dirs = gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR
    gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR = d, d / "archive"
    (d / ".proactive-r.delivered-notanumber").write_text("x")
    (d / ".proactive-r.delivered-5").write_text("kept\n")
    real = gb.undelivered_quarantine.place_as
    gb.undelivered_quarantine.place_as = lambda *a, **k: (_ for _ in ()).throw(OSError("archive down"))
    try:
        gb._reap_hidden_delivered()
        still = (d / ".proactive-r.delivered-5").exists()
    finally:
        gb.undelivered_quarantine.place_as = real
    gb._reap_hidden_delivered()
    gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR = saved_dirs
    check(still and not (d / ".proactive-r.delivered-5").exists()
          and (d / "archive" / "proactive-r-5.txt").read_text() == "kept\n"
          and (d / ".proactive-r.delivered-notanumber").exists(),
          "a hidden delivered file waits out an archive failure, then is archived; a foreign name is left")


def grammar() -> None:
    name = proactive_routing.about_task_filename("task-17", 5, "ag2space")
    check(name == "proactive-about-task-17--5.to-ag2space.txt" and proactive_routing.about_task(name) == "task-17",
          f"about-task name round-trips ({name})")
    try:
        proactive_routing.about_task_filename("not a task", 5)
        check(False, "a bad task id is refused")
    except ValueError:
        check(True, "a bad task id is refused")
    check(proactive_routing.about_task("proactive-1.txt") is None, "an ordinary name is about no task")
    check(proactive_routing.about_task_ts(name) == 5 and proactive_routing.about_task_ts("proactive-1.txt") is None,
          "an about-task name gives back its timestamp; an ordinary one none")
    check(undelivered_quarantine.canonical_result(ISO, "proactive-1").name == "proactive-1.txt"
          and undelivered_quarantine.canonical_result(ISO, "17").name == "task-17.txt",
          "the quarantine names a proactive stem as itself and a bare id as a task")


def restore_by_generation() -> None:
    import hashlib
    import outbox_cli
    results = Path(tempfile.mkdtemp(dir=ISO))
    q = results / "undelivered"
    q.mkdir()
    (q / "proactive-g-100.txt").write_text("A\n", encoding="utf-8")
    (q / "proactive-g-200.txt").write_text("B\n", encoding="utf-8")
    want = hashlib.sha256(b"A\n").hexdigest()
    outcome, path = undelivered_quarantine.restore(results, "proactive-g", body_sha256=want)
    check(outcome is undelivered_quarantine.RestoreOutcome.RESTORED and path.read_text() == "A\n",
          "restore by digest installs that generation, not the newest")
    check(undelivered_quarantine.restore(results, "proactive-g", body_sha256="0" * 64)[0]
          is undelivered_quarantine.RestoreOutcome.NOTHING_QUARANTINED, "an unmatched digest restores nothing")
    check(undelivered_quarantine.sha256_of(results / "absent") is None, "an unreadable copy has no digest")
    check(src_outbox.dispatch_unsettled({"provider_ref": "$e"}) and src_outbox.dispatch_unsettled({"dispatching": {}})
          and not src_outbox.dispatch_unsettled({}), "unsettled means a receipt or a mark")
    root = results / ".outbox-x"
    DesignAClaimBackend(root).publish("proactive-g~1", b"{}")
    src_outbox.park_item(root, "proactive-g~1", "outcome-unknown")
    (results / "proactive-g.txt").unlink()
    rc = outbox_cli.main(["--root", str(root), "requeue", "proactive-g~1", "--body-id", "proactive-g",
                          "--body-sha256", hashlib.sha256(b"B\n").hexdigest()])
    check(rc == 0 and (results / "proactive-g.txt").read_text() == "B\n",
          f"the CLI restores the generation its digest names ({rc})")


def gate_unreadable_task() -> None:
    ws = Path(tempfile.mkdtemp(dir=ISO))
    (ws / "tasks" / "task-unreadable.txt").mkdir(parents=True)
    (ws / "tasks" / "task-1.txt").write_text("id: task-1\ntask: hi\n", encoding="utf-8")
    check(proactive_dispatch_gate.dispatch_veto("task-1", ws) is None, "an unreadable task file is skipped")


def core_and_provider() -> None:
    root = Path(tempfile.mkdtemp(dir=ISO))
    backend = DesignAClaimBackend(root)
    backend.publish("s1", b"{}")
    token = backend.claim("s1", "w")
    check(backend.mark_dispatching(dataclasses.replace(token, incarnation="stale")) is False,
          "a stale token cannot mark a send")

    class NoMark(DesignAClaimBackend):
        def mark_dispatching(self, token):
            return False

    class Provider:
        capabilities = ProviderCapabilities()
        calls = 0

        def deliver(self, *a):
            Provider.calls += 1

        def reconcile(self, attempt):
            return None

    nm = NoMark(Path(tempfile.mkdtemp(dir=ISO)))
    nm.publish("s2", b"{}")
    r = DeliveryCore(nm, Provider(), policy=RetryPolicy(park_interrupted_send=True)).deliver_one("s2", b"{}")
    check(r.status is DrainStatus.NOT_CLAIMED and Provider.calls == 0, "a send that cannot be marked is not made")
    sent = []
    prov = AG2SpaceRoomMessageProvider(lambda *a: sent.append(a) or {"event_id": "$e"}, lambda *a: None)
    try:
        prov.deliver("t", b'{"room_id": "!r:s", "body": "b", "thread_root": "x"}', "k")
        check(False, "a malformed thread root is refused")
    except ProviderPermanentRefused:
        check(sent == [], "a malformed thread root is refused before any send")
    prov.deliver("t", b'{"room_id": "!r:s", "body": "b", "thread_root": "$T"}', "k")
    check(sent == [("!r:s", "b", "k", "$T")], f"the thread root reaches the sender ({sent})")


def bridge_edges() -> None:
    saved = gb.RESULTS_DIR
    gb.RESULTS_DIR = Path(tempfile.mkdtemp(dir=ISO))
    try:
        gb._delivery_core("proactive")
        try:
            gb._record_proactive_receipt("never-published", "$e")
            check(False, "a receipt for no item raises")
        except OSError:
            check(True, "a receipt for no item raises")
    finally:
        gb.RESULTS_DIR = saved
    check(gb._proactive_dispatch_veto("i", b"not json") is None, "an unreadable payload is not withdrawn")
    lane = pl.Lane(gb, ISO)
    saved_tasks, gb.TASKS_DIR = gb.TASKS_DIR, lane.tasks
    try:
        (lane.tasks / "task-1761300000007.txt").mkdir()
        check(gb._about_task_headers("task-1761300000007") == {}, "an unreadable task file has no headers")
        (lane.tasks / "task-1761300000008.txt").write_text("thread_root: $T\ntask: hi\n", encoding="utf-8")
        check(gb._about_task_headers("task-1761300000008") == {"thread_root": "$T"},
              "a task-last file is read by the strict parser only")
    finally:
        gb.TASKS_DIR = saved_tasks

    def refuse(code):
        def respond(payload):
            raise urllib.error.HTTPError("https://gw.example/relay/v1/room", code, "err", {}, None)
        return respond

    lane = pl.Lane(gb, ISO, refuse(401))
    lane.write("proactive-auth.txt", "nudge\n")
    try:
        lane.drain()
        check(False, "an auth refusal reaches the poll loop")
    except urllib.error.HTTPError as e:
        check(e.code == 401 and lane.where("proactive-auth") == "pending",
              f"an auth refusal reaches the poll loop and the file waits ({lane.where('proactive-auth')})")

    lane = pl.Lane(gb, ISO, refuse(429))
    lane.write("proactive-stuck.txt", "nudge\n")
    real = Path.rename
    real_noreplace = gb.undelivered_quarantine.rename_noreplace

    def noreplace(src, dst, *a, **k):
        if Path(dst).name == "proactive-stuck.txt":
            raise OSError("hand-back refused")
        return real_noreplace(src, dst, *a, **k)
    gb.undelivered_quarantine.rename_noreplace = noreplace
    try:
        lane.drain()
    finally:
        gb.undelivered_quarantine.rename_noreplace = real_noreplace
    check(any("could not be handed back" in m for m in lane.logs), "a failed hand-back is logged loudly")
    check(gb._payload_room("not json") is None, "an unreadable stored payload names no room")

    lane = pl.Lane(gb, ISO, refuse(429))
    lane.write("proactive-torn.txt", "nudge\n")
    lane.drain()
    [torn] = lane.items()
    src_outbox._item_path(lane.results / ".outbox-ag2space-proactive", torn["item_id"]).write_text('{"payl')
    lane.drain()
    check(len(lane.posts) == 1 and lane.where("proactive-torn") == "undelivered",
          f"an item record corrupt before the pass is parked, not sent again ({len(lane.posts)} POSTs, "
          f"{lane.where('proactive-torn')})")

    lane = pl.Lane(gb, ISO, refuse(429))
    lane.write("proactive-moved.txt", "nudge\n")
    lane.drain()
    lane.owner_dm = "!Moved:ag2.space"
    Path.rename = lambda self, target: (_ for _ in ()).throw(OSError("no")) \
        if ".sending." in self.name and Path(target).name == "proactive-moved.txt" else real(self, target)
    try:
        lane.drain()
    finally:
        Path.rename = real
    check(lane.posts and len(lane.posts) == 1, "a held file whose hand-back fails is still not sent")

    lane = pl.Lane(gb, ISO, lambda payload: {"ok": True})
    lane.write("proactive-unread.txt", "nudge\n")
    real_bytes = Path.read_bytes
    Path.read_bytes = lambda self: (_ for _ in ()).throw(OSError("no")) if ".sending." in self.name \
        else real_bytes(self)
    try:
        lane.drain()
    finally:
        Path.read_bytes = real_bytes
    check(lane.where("proactive-unread") == "undelivered" and any("NO recovery command" in m for m in lane.logs),
          "a parked body that cannot be read still parks, with no runnable command")


def main() -> int:
    outbox_writers()
    grammar()
    gate_unreadable_task()
    guarded_park_and_release()
    handback_and_reaper()
    held_while_claimed()
    restore_by_generation()
    core_and_provider()
    bridge_edges()
    return check.exit_code()


if __name__ == "__main__":
    sys.exit(main())
