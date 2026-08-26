#!/usr/bin/env python3
"""Orphan reconciliation must only touch results in its OWN task namespace.

`RESULTS_DIR` is shared by every gateway lane on a host. The orphan sweep globs
`task-*.txt`, which matches both the primary's unscoped ids and a named
instance's `task-<inst>~<id>` encoding — so without an ownership filter each lane
recovers, delivers and archives the other's replies.

Both directions are covered here. Filtering only the named-instance side leaves
the primary claiming every lane's files, which is the same defect mirrored.
"""
import importlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "packages" / "ag2-sparrow", REPO / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _bridge(instance):
    """Import the bridge fresh with GATEWAY_INSTANCE set (or cleared).

    Re-imported rather than monkeypatched: GATEWAY_INSTANCE is resolved at module
    scope, so a patched attribute would not match what the real process computes.
    """
    if instance:
        os.environ["GATEWAY_INSTANCE"] = instance
    else:
        os.environ.pop("GATEWAY_INSTANCE", None)
    for name in [k for k in sys.modules if k.startswith("ag2_sparrow")]:
        del sys.modules[name]
    return importlib.import_module("ag2_sparrow.remote_gateway_bridge")


class OwnershipMatrix(unittest.TestCase):
    """The table the fix has to satisfy, stated as the owner specified it."""

    def test_primary_owns_unscoped_ids(self):
        self.assertTrue(_bridge(None)._owns_local_tid("task-123"))

    def test_primary_does_not_own_a_named_instance_id(self):
        # The row the first draft of this fix missed: `task-*` matches this too.
        self.assertFalse(_bridge(None)._owns_local_tid("task-dev~123"))

    def test_instance_owns_its_own_ids(self):
        self.assertTrue(_bridge("dev")._owns_local_tid("task-dev~123"))

    def test_instance_does_not_own_unscoped_ids(self):
        self.assertFalse(_bridge("dev")._owns_local_tid("task-123"))

    def test_instance_does_not_own_another_instances_ids(self):
        self.assertFalse(_bridge("dev")._owns_local_tid("task-staging~123"))

    def test_an_unrecognised_scope_is_owned_by_nobody(self):
        # Unowned beats misrouted: a lane this build does not know about must not
        # fall through to the primary.
        self.assertFalse(_bridge(None)._owns_local_tid("task-unknown~9"))
        self.assertFalse(_bridge("dev")._owns_local_tid("task-unknown~9"))


class DualBridgeSharedResultsDir(unittest.TestCase):
    """Two lanes, one RESULTS_DIR: the selections must partition, not overlap."""

    def _select(self, mod, results_dir):
        return sorted(p.name for p in results_dir.glob("task-*.txt")
                      if mod._owns_local_tid(p.stem))

    def test_each_lane_selects_only_its_own_files(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for n in ("task-111.txt", "task-222.txt",
                      "task-dev~111.txt", "task-staging~111.txt"):
                (d / n).write_text("body\n")

            prod = self._select(_bridge(None), d)
            dev = self._select(_bridge("dev"), d)

            self.assertEqual(prod, ["task-111.txt", "task-222.txt"])
            self.assertEqual(dev, ["task-dev~111.txt"])
            self.assertEqual(set(prod) & set(dev), set(),
                             "a file selected by both lanes is a double delivery")
            # staging is absent on this host: its file is claimed by neither,
            # which is the safe outcome — a stranded file, not a misrouted one.
            self.assertNotIn("task-staging~111.txt", prod + dev)

    def test_the_unfiltered_glob_would_have_overlapped(self):
        """Positive control: without the filter these selections are identical,
        so the assertions above are testing the filter and not the fixture."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for n in ("task-111.txt", "task-dev~111.txt"):
                (d / n).write_text("body\n")
            unfiltered = sorted(p.name for p in d.glob("task-*.txt"))
            self.assertEqual(unfiltered, ["task-111.txt", "task-dev~111.txt"])
            self.assertIn("task-dev~111.txt", unfiltered,
                          "the bare glob must match the named-instance file — "
                          "that overlap is the bug being fixed")


class SweepHonoursOwnership(unittest.TestCase):
    """Wiring: the shipped `_reconcile_orphan_results` must apply the predicate.

    The classes above exercise `_owns_local_tid` directly, so they stay green if
    the sweep stops calling it. This one drives the real function and asserts on
    what it POSTs.
    """

    def _run_sweep(self, instance, names):
        mod = _bridge(instance)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        d = Path(td.name)
        old = time.time() - (mod.ORPHAN_GRACE_S + 60)
        for n in names:
            f = d / n
            f.write_text("recovered body\n")
            os.utime(f, (old, old))

        posted = []
        mod.RESULTS_DIR = d
        mod.ARCHIVE_RESULTS_DIR = d / "archive"
        # TASKS_DIR must be fixture-bound too: _archive_result creates
        # TASKS_DIR/archive, so an ambient value escapes the sandbox.
        mod.TASKS_DIR = d / "tasks"
        mod.TASKS_DIR.mkdir()
        mod._last_orphan_sweep = 0.0
        mod._delivered_copy_exists = lambda tid: False
        mod.find_task_file = lambda tasks_dir, tid: d / f"{tid}.task"
        mod._delivery_tid = lambda tid: tid
        mod._req = lambda method, path, payload=None, **kw: (
            posted.append(payload.get("id")) or {})
        mod._reconcile_orphan_results(set())
        return posted

    # Egress posts `_broker_tid(...)`, which unwraps the namespace: the primary's
    # ids pass through, `task-dev~111` goes up as `111`. Asserting both pins that.
    def test_primary_sweep_skips_named_instance_results(self):
        posted = self._run_sweep(None, ["task-111.txt", "task-dev~111.txt"])
        self.assertEqual(posted, ["task-111"],
                         "the primary must deliver only its own result; "
                         "'task-dev~111' here means it took the dev lane's file")

    def test_instance_sweep_skips_primary_results(self):
        posted = self._run_sweep("dev", ["task-111.txt", "task-dev~111.txt"])
        self.assertEqual(posted, ["111"],
                         "the dev lane must deliver only its own result (as the "
                         "bare broker id); 'task-111' here is the original defect")


class DedupRequeueStaysInItsLane(unittest.TestCase):
    """The re-ask id `_dedup_plan` mints must be OWNED by the minting lane.

    Unscoped (`task-<uuid>`), a named instance's requeue is orphaned to the
    primary: after an in-flight-ledger loss — the exact condition the orphan
    sweep exists to recover — the dev lane skips its own recovered answer and
    the primary consumes it without the dev alias. This drives the production
    `_dedup_plan` (via `_post_ready_results`) and `_reconcile_orphan_results`
    end to end across both lanes sharing one RESULTS_DIR.
    """

    ORIG_BROKER = "task-orig1234567890"       # the id the broker waits on
    HOLDER = "task-22d83e59601f3a1fef"

    def _bind(self, mod, d: Path, posted: list):
        """Fixture-bind every path/state the flows touch (no ambient escapes)."""
        results, tasks, state = d / "results", d / "tasks", d / "state"
        (results / "archive").mkdir(parents=True, exist_ok=True)
        tasks.mkdir(exist_ok=True)
        state.mkdir(exist_ok=True)
        mod.RESULTS_DIR = results
        mod.ARCHIVE_RESULTS_DIR = results / "archive"
        mod.TASKS_DIR = tasks
        mod.DEDUP_ALIAS_FILE = state / f"remote-dedup-alias-{id(mod)}.json"
        rooms = {}
        mod._load_task_rooms = lambda *a, **k: dict(rooms)
        mod._save_task_rooms = lambda r, *a, **k: rooms.update(r)
        mod._forget_task_room = lambda *a, **k: None
        mod._save_inflight = lambda *a, **k: None
        mod._delivered_copy_exists = lambda tid: False
        mod._req = lambda method, path, payload=None, **kw: (
            posted.append(payload.get("id")) or {})

    def test_named_lane_requeue_is_recovered_by_its_own_sweep(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        shared = Path(td.name)                  # one host: shared results dir

        dev_posts, primary_posts = [], []
        dev = _bridge("dev")
        self._bind(dev, shared, dev_posts)
        orig_local = dev._local_tid(self.ORIG_BROKER)

        # Pass 1 on the dev lane: unsubstantiated dedup -> re-ask minted.
        (dev.TASKS_DIR / f"{orig_local}.txt").write_text(
            f"id: {orig_local}\nsource: gateway\naccess_tier: owner\ntask: q\n")
        (dev.RESULTS_DIR / f"{orig_local}.txt").write_text(
            f"[deduped: {self.HOLDER}]")
        (dev.ARCHIVE_RESULTS_DIR / f"{self.HOLDER}-1785976425.txt").write_text("")
        dev._post_ready_results({orig_local})
        requeued = [p for p in dev.TASKS_DIR.glob("task-*")
                    if p.stem != orig_local and p.parent == dev.TASKS_DIR]
        self.assertEqual(len(requeued), 1, "no re-ask was written")
        new_id = requeued[0].stem

        # The blocker itself: the mint must live in the dev namespace.
        self.assertTrue(dev._owns_local_tid(new_id),
                        f"re-ask id {new_id!r} escaped the dev lane")

        # The core answers the re-ask; the in-flight ledger is then LOST.
        answer = shared / "results" / f"{new_id}.txt"
        answer.write_text("the recovered answer\n")
        old = time.time() - (dev.ORPHAN_GRACE_S + 60)
        os.utime(answer, (old, old))

        # Primary lane, same shared results dir: must leave it untouched.
        primary = _bridge(None)
        pdir = Path(tempfile.mkdtemp(dir=td.name))
        self._bind(primary, pdir, primary_posts)
        primary.RESULTS_DIR = shared / "results"          # the shared surface
        primary.ARCHIVE_RESULTS_DIR = shared / "results" / "archive"
        self.assertFalse(primary._owns_local_tid(new_id),
                         "primary claims the dev lane's re-ask id")
        primary._last_orphan_sweep = 0.0
        primary._reconcile_orphan_results(set())
        self.assertEqual(primary_posts, [],
                         "primary sweep delivered the dev lane's answer")
        self.assertTrue(answer.exists(),
                        "primary sweep consumed the dev lane's file")

        # Dev's own sweep recovers it — under the ORIGINAL broker id.
        dev._last_orphan_sweep = 0.0
        dev._reconcile_orphan_results(set())
        self.assertEqual(dev_posts, [self.ORIG_BROKER],
                         "recovered answer must POST under the id the broker "
                         "is waiting on (alias -> _broker_tid unwrap)")
        self.assertFalse(answer.exists(), "delivered result was not archived")


if __name__ == "__main__":
    unittest.main(verbosity=2)
