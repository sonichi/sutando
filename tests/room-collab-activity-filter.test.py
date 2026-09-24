#!/usr/bin/env python3
"""The activity hook must not count this client's own presence renewal.

`Awareness.start()` re-sends our state every `outdated_timeout/2` so the server
does not expire it. Each of those emits a local `update`. If the activity hook
counts them, `last_activity` refreshes forever and the daemon's 30-minute idle
drop can never fire — a silent surface reads as busy.

This drives the REAL client subscription, not a stub: it builds an `Awareness`
with renewal running and asserts the production callback stays silent, then
asserts a genuine remote peer still wakes it. A stub cannot see this class of
bug at all, which is why the daemon's own suite missed it.
"""
import asyncio
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "packages" / "room-collab"

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f" — {detail}"))
    if not cond:
        FAILS.append(label)


def load(name):
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def run(client, pycrdt):
    doc = pycrdt.Doc()
    doc["content"] = pycrdt.Text()
    # 1s, so renewal runs every ~0.5s and 2.5s of silence is ~5 renewals. The
    # production default is 30s, where the same bug hides behind a long wait.
    aw = pycrdt.Awareness(doc, outdated_timeout=1000)

    surface = object.__new__(client.RoomDoc)
    surface._kind = client.DEFAULT_KIND
    surface._doc = doc
    surface._text = doc["content"]
    surface._awareness = aw

    hits = []
    # Through the public entry the daemon actually calls.
    stop = client.RoomDoc.on_activity(surface, lambda: hits.append(1))
    task = asyncio.create_task(aw.start())
    try:
        await asyncio.sleep(2.5)
        renewal_hits = len(hits)

        # A real remote peer, applied through the production path.
        peer = pycrdt.Awareness(pycrdt.Doc())
        peer.set_local_state({"name": "someone"})
        aw.apply_awareness_update(peer.encode_awareness_update([peer.client_id]), "peer")
        await asyncio.sleep(0.05)
        peer_hits = len(hits) - renewal_hits

        # A local edit is not activity either: our own writing is not someone
        # else being present, and the daemon already knows what it wrote.
        with doc.transaction(origin=client.LOCAL_ORIGIN):
            doc["content"] += "typed by us"
        await asyncio.sleep(0.05)
        local_edit_hits = len(hits) - renewal_hits - peer_hits
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await aw.stop()
        stop()

    check("our own presence renewal is not activity", renewal_hits == 0,
          f"{renewal_hits} hits in 2.5s of silence — the idle drop cannot fire")
    check("a remote peer IS activity", peer_hits >= 1, f"{peer_hits} hits")
    check("our own edit is not activity", local_edit_hits == 0, f"{local_edit_hits} hits")


async def every_kind(client, pycrdt):
    """The filter must hold for every surface kind, not only markdown."""
    from room_collab_board import BOARD_KIND, ELEMENTS_KEY
    from room_kanban import CARDS_KEY, KANBAN_KIND

    for kind, key in ((BOARD_KIND, ELEMENTS_KEY), (KANBAN_KIND, CARDS_KEY)):
        doc = pycrdt.Doc()
        doc[key] = pycrdt.Map()
        aw = pycrdt.Awareness(doc, outdated_timeout=1000)
        surface = object.__new__(client.RoomDoc)
        surface._kind, surface._doc, surface._text, surface._awareness = kind, doc, None, aw
        hits = []
        stop = client.RoomDoc.on_activity(surface, lambda: hits.append(1))
        task = asyncio.create_task(aw.start())
        try:
            await asyncio.sleep(1.2)
            renewal = len(hits)
            with doc.transaction(origin="someone-else"):
                doc[key]["a"] = {"id": "a"}
            await asyncio.sleep(0.05)
            remote_edit = len(hits) - renewal
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await aw.stop()
            stop()
        check(f"{kind}: renewal is not activity", renewal == 0, f"{renewal} hits")
        check(f"{kind}: a remote edit IS activity", remote_edit >= 1, f"{remote_edit} hits")


def main():
    try:
        import pycrdt
    except ImportError:
        print("  skip  pycrdt is not installed")
        return 0
    client = load("room_collab_client")
    asyncio.run(run(client, pycrdt))
    asyncio.run(every_kind(client, pycrdt))
    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all activity-filter checks ok'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
