#!/usr/bin/env python3
"""Direct coverage for skills/room-collab/scripts/presence_store.py.

The concurrency case runs the PRODUCTION writer in real subprocesses. A
threaded stand-in would prove nothing here: the contract is an `flock`, which
is held per open file description, so only separate processes exercise it.

Everything runs under `main()`. multiprocessing's default start method on this
platform is `spawn`, which re-imports this module in every child — checks at
import time would run again in each one, and a Pool at import time spawns Pools
from inside Pools.
"""
import importlib.util
import json
import multiprocessing as mp
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODULE = REPO / "skills" / "room-collab" / "scripts" / "presence_store.py"

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f" — {detail}"))
    if not cond:
        FAILS.append(label)


def load():
    spec = importlib.util.spec_from_file_location("presence_store", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _register(args):
    """A whole separate agent registering one surface, through the real writer."""
    path, n = args
    mod = load()
    mod.mutate_desired(Path(path), lambda es: mod.upsert(
        es, {"room": f"!r{n}", "kind": "markdown", "summoned_at": float(n)}))


def main() -> int:
    store = load()
    TMP = Path(tempfile.mkdtemp(prefix="presence-store-"))

    print("── round trip ──")
    p = TMP / "desired.json"
    store.write_entries(p, [{"room": "!a", "kind": "markdown"}])
    check("what is written is what is read",
          store.read_entries(p) == [{"room": "!a", "kind": "markdown"}])
    check("the record carries its schema version", json.loads(p.read_text())["v"] == store.SCHEMA)

    print("── a record that cannot be trusted reads as empty ──")
    check("a missing file", store.read_entries(TMP / "nope.json") == [])
    (TMP / "empty.json").write_text("")
    check("an empty file (the truncate window a `>` redirect opens)",
          store.read_entries(TMP / "empty.json") == [])
    (TMP / "half.json").write_text('{"v": 1, "entries": [{"room": "!a"')
    check("a half-written file", store.read_entries(TMP / "half.json") == [])
    (TMP / "old.json").write_text(json.dumps({"v": 99, "entries": [{"room": "!a", "kind": "x"}]}))
    check("a record of another schema is NOT read as entries",
          store.read_entries(TMP / "old.json") == [])
    (TMP / "junk.json").write_text(
        json.dumps({"v": 1, "entries": ["not a dict", {"room": "!a", "kind": "x"}]}))
    check("non-dict entries are dropped, the rest kept",
          store.read_entries(TMP / "junk.json") == [{"room": "!a", "kind": "x"}])

    print("── publication is atomic ──")
    big = TMP / "big.json"
    store.write_entries(big, [{"room": f"!r{n}", "kind": "markdown", "pad": "x" * 500}
                              for n in range(200)])
    before = big.stat().st_ino
    store.write_entries(big, [{"room": "!only", "kind": "markdown"}])
    check("a rewrite replaces the file rather than truncating it in place",
          big.stat().st_ino != before)
    check("no temp file is left behind",
          [f.name for f in TMP.iterdir() if f.name.startswith(".big.json.")] == [])

    print("── unreadable is not empty ──")
    # ⚠ Found by running the daemon for real: an unreadable record read as
    # empty, and the agent was evicted with reason `left` over a TCC blip.
    import stat as _stat
    locked = TMP / "locked.json"
    store.write_entries(locked, [{"room": "!a", "kind": "markdown"}])
    locked.chmod(0)
    try:
        try:
            store.read_entries(locked)
            unreadable_raised = False
        except store.RecordUnreadable:
            unreadable_raised = True
        except OSError:
            unreadable_raised = True
    finally:
        locked.chmod(_stat.S_IRUSR | _stat.S_IWUSR)
    check("a record that exists but cannot be read RAISES", unreadable_raised)
    check("...while a record that is simply absent is empty",
          store.read_entries(TMP / "never-written.json") == [])

    print("── a failed publish leaves nothing behind ──")
    # The temp file is the whole point of publishing atomically; leaking one on
    # failure turns a write error into a directory that slowly fills.
    class Unserialisable:
        pass

    before = set(TMP.iterdir())
    try:
        store.write_entries(TMP / "boom.json", [{"bad": Unserialisable()}])
        check("a value json cannot encode raises", False, "no exception")
    except (TypeError, ValueError):
        check("a value json cannot encode raises", True)
    leaked = [p.name for p in set(TMP.iterdir()) - before]
    check("...and no temp file survives the failure", leaked == [], str(leaked))

    print("── the lock: concurrent agents must not lose a registration ──")
    race = TMP / "race.json"
    N = 16
    with mp.Pool(8) as pool:
        pool.map(_register, [(str(race), n) for n in range(N)])
    rooms = sorted(e["room"] for e in store.read_entries(race))
    check(f"all {N} concurrent registrations survive",
          rooms == sorted((f"!r{n}" for n in range(N))), f"got {len(rooms)} of {N}")

    print("── upsert / without ──")
    es = [{"room": "!a", "kind": "markdown", "summoned_at": 1}]
    es2 = store.upsert(es, {"room": "!a", "kind": "markdown", "summoned_at": 9})
    check("a second summon into the same surface refreshes, not duplicates",
          es2 == [{"room": "!a", "kind": "markdown", "summoned_at": 9}])
    es3 = store.upsert(es2, {"room": "!a", "kind": "board", "summoned_at": 5})
    check("another surface of the same room is a separate entry", len(es3) == 2)
    check("without() removes only the named surface", store.without(es3, "!a", "board") == es2)

    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all presence-store checks ok'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
