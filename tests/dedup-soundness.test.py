#!/usr/bin/env python3
"""Contract for src/dedup_soundness.py, and that both consumers DELEGATE to it.

The module exists because two tools asked "is this `[deduped:]` sound?" and
answered differently. `scripts/unanswered-tasks.py` (post-hoc) had the fuller
rule; `skills/proactive-loop/scripts/check-dedup-targets.py` (the PRE-write
guard) had a weaker one — which is the dangerous direction, since the guard
cleared writes the post-hoc check would later condemn.

The delegation arms are the point. A contract test alone passes forever while a
consumer quietly grows a private copy back, and that is exactly how the drift
started.
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import dedup_soundness as ds  # noqa: E402

SRC = REPO / "src"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _ws(results=None, archive=None, tasks=None, task_archive=None) -> Path:
    ws = Path(tempfile.mkdtemp())
    (ws / "results" / "archive").mkdir(parents=True)
    (ws / "tasks" / "archive").mkdir(parents=True)
    for d, files in (("results", results), ("results/archive", archive),
                     ("tasks", tasks), ("tasks/archive", task_archive)):
        for n, b in (files or {}).items():
            (ws / d / n).write_text(b)
    return ws


class ResultPath(unittest.TestCase):
    """Which file counts as the target's DELIVERED result."""

    def test_a_quarantined_result_is_not_a_delivered_one(self):
        """`{id}.too-old.<epoch>` was quarantined — the opposite of delivered.
        The pre-write guard used a bare `{id}*` glob, which matches it."""
        ws = _ws(archive={"task-b.too-old.1788000000.txt": "never sent\n"})
        self.assertIsNone(ds.result_path(ws / "results", "task-b"))

    def test_the_real_archive_shape_IS_found(self):
        """Control: without it, 'reject quarantine' could be 'reject everything'."""
        ws = _ws(archive={"task-b-1788000001.txt": "the reply\n"})
        self.assertIsNotNone(ds.result_path(ws / "results", "task-b"))

    def test_the_undecorated_archive_shape_is_found(self):
        ws = _ws(archive={"task-b.txt": "the reply\n"})
        self.assertIsNotNone(ds.result_path(ws / "results", "task-b"))

    def test_a_live_result_wins_over_the_archive(self):
        ws = _ws(results={"task-b.txt": "live\n"}, archive={"task-b-1.txt": "old\n"})
        self.assertEqual(ds.result_path(ws / "results", "task-b").read_text(), "live\n")

    def test_the_per_channel_pull_namespace_is_found(self):
        ws = _ws(results={"phone-abc.task-b.txt": "the reply\n"})
        self.assertIsNotNone(ds.result_path(ws / "results", "task-b"))


class Problems(unittest.TestCase):
    """One taxonomy, so both tools name the same condition the same way."""

    def _p(self, ws, tid="task-a", with_tasks=True):
        return ds.dedup_problem(ws / "results", tid,
                                (ws / "tasks") if with_tasks else None, src_dir=SRC)

    def test_a_real_reply_is_clean(self):
        ws = _ws(results={"task-a.txt": "[deduped: task-b]\n", "task-b.txt": "the reply\n"})
        self.assertIsNone(self._p(ws))

    def test_a_non_dedup_result_is_clean(self):
        ws = _ws(results={"task-a.txt": "an ordinary reply\n"})
        self.assertIsNone(self._p(ws))

    def test_cross_room_is_named(self):
        """The failure that motivated sharing: the holder delivers correctly, in
        the wrong room, so the asking room hears nothing and nothing errors."""
        ws = _ws(results={"task-a.txt": "[deduped: task-b]\n", "task-b.txt": "the reply\n"},
                 tasks={"task-a.txt": "channel_id: !room:x\nuser_id: @u:x\ntask: q\n",
                        "task-b.txt": "channel_id: !dm:x\nuser_id: @u:x\ntask: q\n"})
        self.assertIn("CROSS-ROOM", self._p(ws))

    def test_cross_sender_is_named(self):
        ws = _ws(results={"task-a.txt": "[deduped: task-b]\n", "task-b.txt": "the reply\n"},
                 tasks={"task-a.txt": "channel_id: !r:x\nuser_id: @alice:x\ntask: q\n",
                        "task-b.txt": "channel_id: !r:x\nuser_id: @bob:x\ntask: q\n"})
        self.assertIn("CROSS-SENDER", self._p(ws))

    def test_same_room_same_sender_stays_clean(self):
        """Green on purpose: the legitimate dedup must survive, or the two arms
        above would pass against a rule that flags every dedup."""
        ws = _ws(results={"task-a.txt": "[deduped: task-b]\n", "task-b.txt": "the reply\n"},
                 tasks={"task-a.txt": "channel_id: !r:x\nuser_id: @u:x\ntask: q\n",
                        "task-b.txt": "channel_id: !r:x\nuser_id: @u:x\ntask: q\n"})
        self.assertIsNone(self._p(ws))

    def test_a_skip_holder_is_named(self):
        ws = _ws(results={"task-a.txt": "[deduped: task-b]\n", "task-b.txt": "[no-send]\n"})
        self.assertIn("HOLDER-SKIPPED", self._p(ws, with_tasks=False))

    def test_a_chained_holder_names_the_chain(self):
        ws = _ws(results={"task-a.txt": "[deduped: task-b]\n",
                          "task-b.txt": "[deduped: task-c]\n", "task-c.txt": "reply\n"})
        why = self._p(ws, with_tasks=False)
        self.assertIn("HOLDER-SKIPPED", why)
        self.assertIn("task-c", why, "naming the chain is what says walking it would not help")

    def test_a_dangling_target_is_distinguished_from_an_orphaned_one(self):
        ws = _ws(results={"task-a.txt": "[deduped: task-gone]\n"})
        self.assertIn("DANGLING", self._p(ws))          # tasks/ present, id unknown
        self.assertIn("ORPHANED", self._p(ws, with_tasks=False))  # cannot tell

    def test_an_empty_target_is_named(self):
        ws = _ws(results={"task-a.txt": "[deduped: ]\n"})
        self.assertEqual(self._p(ws), "deduped into nothing (no target id)")

    def test_without_tasks_the_addressee_checks_are_SKIPPED_not_passed(self):
        """A caller that cannot supply tasks/ gets the weaker verdict it asked
        for — never a silent all-clear dressed as the full check."""
        ws = _ws(results={"task-a.txt": "[deduped: task-b]\n", "task-b.txt": "reply\n"},
                 tasks={"task-a.txt": "channel_id: !room:x\ntask: q\n",
                        "task-b.txt": "channel_id: !dm:x\ntask: q\n"})
        self.assertIn("CROSS-ROOM", self._p(ws, with_tasks=True))
        self.assertIsNone(self._p(ws, with_tasks=False))


class RefusesRatherThanGuesses(unittest.TestCase):
    def test_an_unimportable_grammar_owner_raises(self):
        saved, had = ds._MARKERS, sys.modules.get("result_markers")
        ds._MARKERS = None
        sys.modules["result_markers"] = None
        try:
            with self.assertRaises(ImportError):
                ds.markers(SRC)
        finally:
            ds._MARKERS = saved
            if had is not None:
                sys.modules["result_markers"] = had
            else:
                sys.modules.pop("result_markers", None)
        self.assertIsNotNone(ds.markers(SRC), "CONTROL: global state restored")


class ConsumersDelegate(unittest.TestCase):
    """Neither tool may re-grow a private copy of the judgement."""

    def test_check_dedup_targets_calls_the_owner(self):
        cdt = _load(REPO / "skills" / "proactive-loop" / "scripts" / "check-dedup-targets.py", "cdt_d")
        ws = _ws(results={"a.txt": "[deduped: task-b]\n", "task-b.txt": "[no-send]\n"})
        sentinel = "SENTINEL-FROM-THE-OWNER"
        with mock.patch.object(ds, "dedup_problem", return_value=sentinel) as spy:
            bad = cdt.check(ws, [ws / "results" / "a.txt"])
        self.assertTrue(spy.called, "the guard did not consult src/dedup_soundness")
        self.assertEqual([b[2] for b in bad], [sentinel],
                         "the guard reported a verdict the owner did not produce")

    def test_unanswered_tasks_calls_the_owner(self):
        uat = _load(REPO / "scripts" / "unanswered-tasks.py", "uat_d")
        ws = _ws(results={"task-a.txt": "[deduped: task-b]\n", "task-b.txt": "[no-send]\n"},
                 tasks={"task-a.txt": "id: task-a\ntask: q\n"})
        sentinel = "SENTINEL-FROM-THE-OWNER"
        with mock.patch.object(ds, "dedup_problem", return_value=sentinel) as spy:
            rows = uat.unanswered(ws, min_age_sec=-1)
        self.assertTrue(spy.called, "unanswered-tasks did not consult src/dedup_soundness")
        self.assertEqual([r[2] for r in rows], [sentinel])

    def test_neither_consumer_carries_its_own_dedup_regex(self):
        """The grammar has one owner. A private `[deduped:` regex is how the
        pre-write guard drifted from the bridge in the first place."""
        for rel in ("skills/proactive-loop/scripts/check-dedup-targets.py",
                    "scripts/unanswered-tasks.py"):
            text = (REPO / rel).read_text()
            self.assertNotIn(r"\[deduped:", text,
                             f"{rel} re-implements the dedup marker grammar")


if __name__ == "__main__":
    unittest.main(verbosity=2)
