#!/usr/bin/env python3
"""Behavioural tests for the pending-questions triage queue.

The queue shows ONE question at a time, which changes what a bug costs. In the old
flat list a mis-ranked question was still on screen; here whatever sorts first is
the only thing the owner is asked, so an ordering or filtering defect makes real
questions invisible rather than merely inconvenient.

Two properties get the most coverage because they are the ones that lose data:

  * a failed re-check must never look like "nothing is blocked" or "already
    resolved" — an undecided reference stays blocking and the row stays visible;
  * dismissal is permanent and is the ONLY thing allowed to remove a row.

Run: python3 tests/pending-questions-triage.test.py
Exit: 0 = all pass, 1 = failure
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import pending_questions_triage as triage  # noqa: E402
from util_paths import _host_label  # noqa: E402 — needs the sys.path above


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


api = _load("agent_api_triage", REPO / "src" / "agent-api.py")


def _row(qid, text="", detail="", age=None):
    return {"id": qid, "text": text, "detail": detail, "age_days": age}


class ReferenceExtraction(unittest.TestCase):
    def test_distinct_refs_in_first_seen_order(self):
        self.assertEqual(
            [(None, 3346), (None, 3344)],
            triage.extract_refs("PR #3346/#3344", "again #3346"),
        )

    def test_a_colour_literal_is_not_an_issue_reference(self):
        # '#3fa9c1' starts with a digit; a naive \d+ reads it as issue 3.
        self.assertEqual([], triage.extract_refs("brand colour #3fa9c1"))

    def test_a_hash_inside_a_word_is_not_a_reference(self):
        self.assertEqual([], triage.extract_refs("anchor foo#9 in the doc"))

    def test_a_source_file_anchor_is_not_a_repository_reference(self):
        self.assertEqual([], triage.extract_refs("see src/agent-api.py#3"))

    def test_a_qualified_reference_keeps_its_repository(self):
        self.assertEqual(
            [("sonichi/sutando", 3998)], triage.extract_refs("sonichi/sutando#3998")
        )

    def test_a_pull_request_url_is_a_qualified_reference(self):
        self.assertEqual(
            [("sonichi/sutando", 12)],
            triage.extract_refs("https://github.com/sonichi/sutando/pull/12"),
        )

    def test_bare_numbers_inherit_the_one_repository_the_question_names(self):
        self.assertEqual(
            [("sonichi/sutando", 3998), ("sonichi/sutando", 3747)],
            triage.extract_refs("sonichi/sutando#3998 and also #3747"),
        )

    def test_bare_numbers_stay_unbound_when_no_repository_is_named(self):
        # The live file discusses several repositories in one prose corpus, so
        # `#292` there is NOT this checkout's #292 — it is undecidable.
        self.assertEqual([(None, 292)], triage.extract_refs("sutando-life CI, see #292"))

    def test_bare_numbers_stay_unbound_when_two_repositories_are_named(self):
        refs = triage.extract_refs("a/b#1 and c/d#2 and bare #3")
        self.assertIn((None, 3), refs)


class Probeable(unittest.TestCase):
    def test_only_repository_qualified_references_are_looked_up(self):
        refs = [("a/b", 1), (None, 2), ("a/b", 1)]
        self.assertEqual([("a/b", 1)], triage.probeable(refs))

    def test_an_unbound_reference_is_never_probed_so_can_never_be_called_stale(self):
        self.assertEqual([], triage.probeable([(None, 292), (None, 818)]))


class RecheckVerdict(unittest.TestCase):
    def test_an_undecided_reference_still_counts_as_blocking(self):
        # The probe returned nothing (offline, rate-limited, gh absent). Treating
        # that as resolved would demote a genuinely blocking question.
        rows = triage.apply_recheck([_row("Q1", "waiting on a/b#10 and a/b#11")], {})
        self.assertEqual(2, rows[0]["blocks"])
        self.assertIsNone(rows[0]["recheck"])

    def test_every_reference_resolved_marks_the_row_stale_and_keeps_it(self):
        rows = triage.apply_recheck(
            [_row("Q1", "blocked on a/b#10", "and a/b#11")],
            {("a/b", 10): "MERGED", ("a/b", 11): "CLOSED"},
        )
        self.assertEqual(1, len(rows), "a stale question must still be shown")
        self.assertEqual(triage.RECHECK_STALE, rows[0]["recheck"]["status"])
        self.assertEqual(0, rows[0]["blocks"])
        self.assertIn("a/b#10 merged", rows[0]["recheck"]["note"])

    def test_a_partly_resolved_row_is_reported_as_still_blocking(self):
        rows = triage.apply_recheck(
            [_row("Q1", "a/b#10 a/b#11")],
            {("a/b", 10): "MERGED", ("a/b", 11): "OPEN"},
        )
        self.assertEqual(triage.RECHECK_BLOCKING, rows[0]["recheck"]["status"])
        self.assertEqual(1, rows[0]["blocks"])

    def test_a_row_with_no_references_gets_no_verdict(self):
        rows = triage.apply_recheck(
            [_row("Q1", "what should I be called?")], {("a/b", 10): "MERGED"}
        )
        self.assertEqual(0, rows[0]["blocks"])
        self.assertIsNone(rows[0]["recheck"])


class Ranking(unittest.TestCase):
    def test_what_it_blocks_can_outrank_a_longer_wait(self):
        older = _row("older", "no refs", age=10)
        blocking = _row("blocking", "holds up a/b#10", age=5)
        triage.apply_recheck([older, blocking], {})
        self.assertEqual(
            ["blocking", "older"], [r["id"] for r in triage.rank([older, blocking])]
        )

    def test_a_longer_wait_still_wins_when_neither_blocks(self):
        a = _row("a", age=3)
        b = _row("b", age=30)
        triage.apply_recheck([a, b], {})
        self.assertEqual(["b", "a"], [r["id"] for r in triage.rank([a, b])])

    def test_an_undated_question_sorts_last_rather_than_defaulting_to_zero(self):
        # Placed FIRST and tied against a true age-0 row: the only arrangement
        # where "defaulted to 0" and "genuinely 0" differ.
        undated = _row("undated", age=None)
        fresh = _row("fresh", age=0)
        triage.apply_recheck([undated, fresh], {})
        self.assertEqual(
            ["fresh", "undated"], [r["id"] for r in triage.rank([undated, fresh])]
        )

    def test_ranking_never_adds_or_drops_a_row(self):
        rows = [_row(str(n), "a/b#%d" % n, age=n) for n in range(6)]
        triage.apply_recheck(rows, {})
        self.assertEqual({r["id"] for r in rows}, {r["id"] for r in triage.rank(rows)})


class Dismissal(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pq-dismiss-"))
        self.store = self.dir / "state" / "dismissed-questions.json"

    def test_a_dismissed_id_is_remembered(self):
        triage.dismiss(self.store, "Qabc")
        self.assertEqual({"Qabc"}, triage.load_dismissed(self.store))

    def test_a_missing_or_corrupt_store_dismisses_nothing(self):
        self.assertEqual(set(), triage.load_dismissed(self.store))
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self.store.write_text("{ not json")
        self.assertEqual(set(), triage.load_dismissed(self.store))

    def test_a_store_of_the_wrong_shape_dismisses_nothing(self):
        # Anything but a list of ids is refused outright; coercing it risks
        # reading some unrelated structure as "these questions are gone".
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self.store.write_text(json.dumps({"dismissed": {"Q1": True}}))
        self.assertEqual(set(), triage.load_dismissed(self.store))

    def test_a_failed_write_leaves_no_temp_file_behind(self):
        triage.dismiss(self.store, "Qkeep")
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                triage.save_dismissed(self.store, {"Qnew"})
        self.assertEqual([], list(self.store.parent.glob("*.tmp")))
        self.assertEqual({"Qkeep"}, triage.load_dismissed(self.store))

    def test_only_the_dismissed_row_is_removed(self):
        rows = [_row("Q1"), _row("Q2"), _row("Q3")]
        kept = triage.without_dismissed(rows, {"Q2"})
        self.assertEqual(["Q1", "Q3"], [r["id"] for r in kept])

    def test_a_concurrent_reader_never_sees_a_truncated_store(self):
        # The production writer, not a stand-in: a plain `open(...,'w')` truncates
        # before it writes, so a reader in that window loses every dismissal.
        triage.dismiss(self.store, "seed")
        observations = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                observations.append(triage.load_dismissed(self.store))

        t = threading.Thread(target=reader)
        t.start()
        try:
            for n in range(200):
                triage.dismiss(self.store, "Q%d" % n)
        finally:
            stop.set()
            t.join()
        self.assertTrue(observations, "reader thread never sampled the store")
        self.assertTrue(
            all("seed" in seen for seen in observations),
            "a reader observed a store missing an already-committed dismissal",
        )
        self.assertEqual([], list(self.dir.glob("state/*.tmp")), "temp file left behind")


PQ_FIXTURE = """# Pending Questions

## 2026-08-01 — Old and unblocked
Nothing references a PR here.

## 2026-08-20 — Blocked on sonichi/sutando#4242
This one is waiting on a pull request.

## 2026-08-25 — Blocked on sonichi/sutando#4243
So is this one.

# Resolved

## 2026-07-01 — Archived
Must never be offered as open.
"""


class ReferenceProbe(unittest.TestCase):
    """The gh lookup itself — the one part of the re-check that touches the network."""

    def setUp(self):
        api._pq_ref_cache.clear()

    def _probe(self, returncode=0, stdout='{"state": "MERGED"}'):
        return mock.patch.object(
            api.subprocess, "run",
            return_value=subprocess.CompletedProcess([], returncode, stdout, ""),
        )

    def test_a_successful_lookup_reports_the_state(self):
        with self._probe() as run:
            self.assertEqual({("a/b", 7): "MERGED"}, api._probe_ref_states([("a/b", 7)]))
        argv = run.call_args[0][0]
        self.assertEqual(["--repo", "a/b"], argv[argv.index("--repo"):argv.index("--repo") + 2],
                         "the lookup must name the repository, not the ambient checkout")

    def test_a_nonzero_exit_decides_nothing(self):
        with self._probe(returncode=1, stdout=""):
            self.assertEqual({}, api._probe_ref_states([("a/b", 7)]))

    def test_unparseable_output_decides_nothing(self):
        with self._probe(stdout="not json"):
            self.assertEqual({}, api._probe_ref_states([("a/b", 7)]))

    def test_a_second_look_inside_the_window_does_not_rerun_gh(self):
        with self._probe() as run:
            api._probe_ref_states([("a/b", 7)])
            api._probe_ref_states([("a/b", 7)])
        self.assertEqual(1, run.call_count, "the cache did not spare a repeat lookup")

    def test_a_cached_undecided_result_is_not_retried_either(self):
        with self._probe(returncode=1, stdout="") as run:
            api._probe_ref_states([("a/b", 7)])
            self.assertEqual({}, api._probe_ref_states([("a/b", 7)]))
        self.assertEqual(1, run.call_count)

    def test_the_fan_out_is_capped_however_many_references_a_question_makes(self):
        refs = [("a/b", n) for n in range(api.PQ_REF_PROBE_LIMIT + 5)]
        with self._probe() as run:
            api._probe_ref_states(refs)
        self.assertEqual(api.PQ_REF_PROBE_LIMIT, run.call_count)


class AdapterRows(unittest.TestCase):
    """The API adapter over a workspace that is provably not the operator's."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pq-triage-ws-"))
        host = _host_label()
        # Per-host file FIRST so personal_path's first probe hits: a fresh tmp
        # otherwise falls through to the operator's vault-synced memory tree.
        self.pq = self.tmp / "hosts" / host / "pending-questions.md"
        self.pq.parent.mkdir(parents=True, exist_ok=True)
        self.pq.write_text(PQ_FIXTURE)
        self._saved_ws = api.WORKSPACE_DIR
        api.WORKSPACE_DIR = self.tmp
        resolved = Path(api.personal_path("pending-questions.md", self.tmp))
        assert resolved == self.pq, f"workspace escaped tmp: {resolved} != {self.pq}"

    def tearDown(self):
        api.WORKSPACE_DIR = self._saved_ws

    def test_no_questions_file_yields_no_rows_rather_than_an_error(self):
        self.pq.unlink()
        self.assertEqual([], api._pending_question_rows())

    def test_rows_are_ranked_and_exclude_the_resolved_section(self):
        rows = api._pending_question_rows()
        self.assertEqual(3, len(rows))
        self.assertNotIn("Archived", " ".join(r["text"] for r in rows))

    def test_a_dismissed_question_stops_being_offered(self):
        rows = api._pending_question_rows()
        target = rows[0]["id"]
        status, body = api.dismiss_question(target)
        self.assertEqual((200, True), (status, body["ok"]))
        remaining = api._pending_question_rows()
        self.assertEqual(len(rows) - 1, len(remaining))
        self.assertNotIn(target, [r["id"] for r in remaining])

    def test_dismissing_does_not_write_to_the_questions_file(self):
        before = self.pq.read_text()
        api.dismiss_question(api._pending_question_rows()[0]["id"])
        self.assertEqual(before, self.pq.read_text())

    def test_an_empty_id_is_rejected_rather_than_stored(self):
        status, _ = api.dismiss_question("")
        self.assertEqual(400, status)
        self.assertEqual(set(), triage.load_dismissed(api._dismissed_questions_path()))

    def test_a_live_recheck_labels_a_merged_blocker_without_dropping_the_row(self):
        probe = {("sonichi/sutando", 4242): "MERGED"}
        with mock.patch.object(api, "_probe_ref_states", return_value=probe):
            rows = api._pending_question_rows(recheck=True)
        self.assertEqual(3, len(rows), "re-check must never remove a question")
        stale = next(r for r in rows if "sonichi/sutando#4242" in r["refs"])
        self.assertEqual(triage.RECHECK_STALE, stale["recheck"]["status"])
        still = next(r for r in rows if "sonichi/sutando#4243" in r["refs"])
        self.assertIsNone(still["recheck"], "an undecided reference is not a verdict")

    def test_a_failing_probe_leaves_every_question_visible_and_unlabelled(self):
        with mock.patch.object(api.subprocess, "run", side_effect=OSError("gh missing")):
            rows = api._pending_question_rows(recheck=True)
        self.assertEqual(3, len(rows))
        self.assertTrue(all(r["recheck"] is None for r in rows))
        self.assertEqual(
            1, next(r for r in rows if "sonichi/sutando#4242" in r["refs"])["blocks"]
        )

    def test_the_queue_payload_carries_the_rechecked_rows(self):
        api._pq_ref_cache.clear()
        with mock.patch.object(api, "_probe_ref_states", return_value={}):
            payload = api._questions_queue_payload()
        self.assertEqual(3, len(payload["questions"]))
        self.assertIn("age_days", payload["questions"][0])


if __name__ == "__main__":
    unittest.main(verbosity=1)
