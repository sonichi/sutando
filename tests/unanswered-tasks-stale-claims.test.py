#!/usr/bin/env python3
"""Stale service claims are reported without assigning their work to the core."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "worker-pool" / "scripts"))
sys.path.insert(0, str(REPO / "src"))

import pool_claim_policy as policy  # noqa: E402
import pool_router  # noqa: E402

import task_delegation  # noqa: E402
import worker_delivery  # noqa: E402

from delivery.task_dispatch import worker_holds  # noqa: E402

SPEC = importlib.util.spec_from_file_location("unanswered_stale", REPO / "scripts" / "unanswered-tasks.py")
uat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(uat)
NOW = 1_794_384_000.0
TASK = "task-stale-probe"


class StaleClaims(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        for name in ("tasks", "results", "state"):
            (self.ws / name).mkdir()
        self.task = self.ws / "tasks" / f"{TASK}.txt"
        self.task.write_text(f"id: {TASK}\nsource: ag2space\ntask: synthetic work\n")
        os.utime(self.task, (NOW - 900, NOW - 900))
        self.roster = self.ws / "state" / "roster.json"
        self.roster.write_text(json.dumps({"version": 1, "workers": {}}))

    def claim(self, recipient="side-claims", age=601, *, marked=True, suffix=".accepted"):
        folder = self.ws / "deliveries" / recipient
        folder.mkdir(parents=True, exist_ok=True)
        if marked:
            (folder / ".non-exclusive").write_text("")
        sentinel = folder / f"{TASK}{suffix}"
        sentinel.write_text("")
        os.utime(sentinel, (NOW - age, NOW - age))
        return sentinel

    def main(self, *, json_mode=False, min_age=120):
        argv = ["unanswered-tasks.py", "--workspace", str(self.ws), "--min-age-sec", str(min_age)]
        if json_mode:
            argv.append("--json")
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", argv), patch.object(uat.time, "time", return_value=NOW), \
                patch.object(uat, "_inbox_view", return_value={"standby_only": False}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = uat.main()
        return rc, json.loads(out.getvalue()) if json_mode else out.getvalue(), err.getvalue()

    def assert_verdict(self, expected):
        text_rc, text, _ = self.main()
        json_rc, snap, _ = self.main(json_mode=True)
        self.assertEqual((text_rc, json_rc), (expected, expected))
        if expected in (0, 1):
            self.assertEqual(snap["unanswered_count"], int(expected == 1))
        return text, snap

    def test_stale_only_claim_is_reported_but_still_owned(self):
        sentinel = self.claim()
        before = self.tree_state()
        text, snap = self.assert_verdict(1)
        self.assertIn(f"UNANSWERED {TASK}", text)
        self.assertIn("non-exclusive claimant side-claims for 10.0m", text)
        self.assertIn("inspect the claimant; task remains held", text)
        task = snap["tasks"][0]
        self.assertEqual(task["holder"], "side-claims")
        self.assertTrue(task["claimed"])
        self.assertFalse(task["answered"])
        self.assertIsNone(task["unclaimed_age_s"])
        self.assertIsNone(snap["oldest_unclaimed_task"])
        self.assertEqual(task["delivery_holders"], [
            {"recipient": "side-claims", "non_exclusive": True, "claim_age_s": 601.0}])
        self.assertTrue(sentinel.is_file())
        self.assertEqual(worker_delivery.holder_of(self.ws, TASK), "side-claims")
        self.assertTrue(worker_holds(self.ws / "deliveries", self.task.name))
        self.assertEqual(self.tree_state(), before)

    def test_claim_wording_and_text_suffix_stay_consistent(self):
        self.claim()
        notice = "inspect this service before recovering its assigned task"
        with patch.object(uat, "CLAIM_INSPECTION_NOTICE", notice, create=True):
            text, snap = self.assert_verdict(1)
        self.assertIn(notice, text)
        self.assertNotIn("the room heard nothing", text)
        self.assertIn(notice, snap["tasks"][0]["unanswered"])

    def test_repeated_probes_do_not_grow_import_path(self):
        self.claim()
        before = sys.path.count(str(uat._POOL_SCRIPTS))
        for _ in range(3):
            self.assert_verdict(1)
        self.assertEqual(sys.path.count(str(uat._POOL_SCRIPTS)), max(1, before))

    def test_reply_published_during_claim_scan_suppresses_both_modes(self):
        self.claim()
        original = worker_delivery.holder_evidence
        result = self.ws / "results" / f"{TASK}.txt"

        def publish_during_scan(*args, **kwargs):
            evidence = original(*args, **kwargs)
            result.write_text("Reply completed during the evidence scan\n")
            return evidence

        for json_mode in (False, True):
            with self.subTest(json_mode=json_mode):
                with patch.object(worker_delivery, "holder_evidence", side_effect=publish_during_scan):
                    rc, output, _ = self.main(json_mode=json_mode)
                self.assertTrue(result.is_file())
                self.assertEqual(rc, 0)
                if json_mode:
                    self.assertTrue(output["tasks"][0]["answered"])
                    self.assertIsNone(output["tasks"][0]["unanswered"])
                    self.assertEqual(output["unanswered_count"], 0)
                else:
                    self.assertNotIn("UNANSWERED", output)
                result.unlink()

    def tree_state(self):
        return {str(p.relative_to(self.ws)): (p.lstat().st_mode, p.lstat().st_mtime_ns,
                                             p.read_bytes() if p.is_file() else None)
                for p in self.ws.rglob("*")}

    def test_boundary_fresh_and_future_claims_stay_quiet(self):
        for age in (60, 600, -30):
            with self.subTest(age=age):
                self.claim(age=age)
                self.assert_verdict(0)

    def test_task_minimum_age_still_applies(self):
        self.claim()
        os.utime(self.task, (NOW - 30, NOW - 30))
        self.assert_verdict(0)
        self.assertEqual(self.main(min_age=0)[0], 1)

    def test_renamed_task_uses_canonical_id_in_both_modes(self):
        self.claim()
        for stage in ("claimed-core-2", "assigned-worker"):
            with self.subTest(stage=stage):
                renamed = self.task.with_name(f"{TASK}.{stage}.txt")
                self.task.rename(renamed)
                try:
                    text, snap = self.assert_verdict(1)
                    self.assertIn(f"UNANSWERED {TASK} ", text)
                    self.assertEqual(snap["tasks"][0]["task_id"], TASK)
                    self.assertTrue(snap["tasks"][0]["claimed"])
                finally:
                    renamed.rename(self.task)

    def test_invalid_task_identity_is_skipped_without_a_path_lookup(self):
        self.task.unlink()
        invalid = self.ws / "tasks" / "task-invalid!.txt"
        invalid.write_text("id: ../../outside\ntask: malformed record\n")
        os.utime(invalid, (NOW - 900, NOW - 900))
        _, snap = self.assert_verdict(0)
        self.assertEqual(snap["tasks"], [])

    def test_every_sentinel_stage_can_hold_a_stale_claim(self):
        for suffix in worker_delivery.SENTINEL_SUFFIXES:
            with self.subTest(suffix=suffix):
                sentinel = self.claim(suffix=suffix)
                self.assert_verdict(1)
                sentinel.unlink()

    def test_youngest_sentinel_prevents_false_stale_claim(self):
        self.claim()
        self.claim(age=60, suffix=".txt")
        _, snap = self.assert_verdict(0)
        self.assertEqual(snap["tasks"][0]["delivery_holders"][0]["claim_age_s"], 60.0)

    def test_all_service_claimants_must_be_stale(self):
        self.claim("a-service", 900)
        fresh = self.claim("z-service", 60)
        self.assert_verdict(0)
        os.utime(fresh, (NOW - 601, NOW - 601))
        text, snap = self.assert_verdict(1)
        self.assertIn("a-service, z-service", text)
        self.assertEqual(len(snap["tasks"][0]["delivery_holders"]), 2)

    def test_real_worker_after_alphabetically_first_stale_claim_suppresses(self):
        self.claim("a-service", 900)
        self.claim("z-worker", 3600, marked=False)
        self.assert_verdict(0)

    def test_live_background_delegation_suppresses_but_expired_does_not(self):
        self.claim()
        task_delegation.mark(self.ws, TASK, ttl_s=900, now=NOW - 300)
        self.assert_verdict(0)
        task_delegation.mark(self.ws, TASK, ttl_s=1, now=NOW - 300)
        self.assert_verdict(1)

    def test_answered_and_archived_results_suppress(self):
        self.claim()
        for name in (f"{TASK}.txt", f"archive/2026-11/{TASK}-1794383000.txt", f"{TASK}.txt.sending"):
            with self.subTest(name=name):
                result = self.ws / "results" / name
                result.parent.mkdir(parents=True, exist_ok=True)
                result.write_text("Synthetic completed reply\n")
                self.assert_verdict(0)
                result.unlink()

    def test_unmarked_service_is_never_expired(self):
        self.claim(marked=False, age=86400)
        self.roster.unlink()
        self.assert_verdict(0)

    def test_malformed_or_missing_roster_is_undecidable(self):
        self.claim()
        for value in (None, "not json", '{"workers": []}', '{"workers": {"worker": null}}'):
            with self.subTest(value=value):
                if value is None:
                    self.roster.unlink()
                else:
                    self.roster.write_text(value)
                _, snap = self.assert_verdict(2)
                self.assertIn("error", snap)

    def test_marker_on_real_worker_or_core_is_undecidable(self):
        for recipient in ("worker", "core"):
            with self.subTest(recipient=recipient):
                self.roster.write_text(json.dumps({"workers": {"worker": {"state": "live"}}}))
                sentinel = self.claim(recipient)
                self.assert_verdict(2)
                sentinel.unlink()

    def test_nonregular_marker_is_undecidable_without_blocking(self):
        sentinel = self.claim()
        marker = sentinel.parent / ".non-exclusive"
        marker.unlink()
        for kind in ("directory", "symlink", "fifo"):
            with self.subTest(kind=kind):
                if kind == "directory":
                    marker.mkdir()
                elif kind == "symlink":
                    marker.symlink_to(self.roster)
                else:
                    os.mkfifo(marker)
                self.assert_verdict(2)
                marker.rmdir() if kind == "directory" else marker.unlink()

    def test_nonregular_sentinel_is_undecidable_without_blocking(self):
        sentinel = self.claim()
        sentinel.unlink()
        for kind in ("directory", "symlink", "fifo"):
            with self.subTest(kind=kind):
                if kind == "directory":
                    sentinel.mkdir()
                elif kind == "symlink":
                    sentinel.symlink_to(self.roster)
                else:
                    os.mkfifo(sentinel)
                self.assert_verdict(2)
                sentinel.rmdir() if kind == "directory" else sentinel.unlink()

    def test_unrelated_marker_is_never_classified(self):
        self.claim("worker", marked=False)
        unrelated = self.ws / "deliveries" / "other-service"
        unrelated.mkdir()
        (unrelated / ".non-exclusive").mkdir()
        self.roster.write_text("not json")
        self.assert_verdict(0)

    def test_recipient_removed_during_probe_does_not_create_a_stale_claim(self):
        sentinel = self.claim()
        original_open = os.open

        def remove_before_open(path, flags, *args, **kwargs):
            if Path(path) == sentinel.parent:
                sentinel.unlink()
                (sentinel.parent / ".non-exclusive").unlink()
                sentinel.parent.rmdir()
            return original_open(path, flags, *args, **kwargs)

        with patch.object(worker_delivery.os, "open", side_effect=remove_before_open):
            self.assertEqual(worker_delivery.holder_evidence(self.ws, TASK, now=NOW), [])

    def test_unreadable_marker_sentinel_and_recipient_are_undecidable(self):
        if os.geteuid() == 0:
            self.skipTest("root cannot be denied a read")
        sentinel = self.claim()
        for path in (sentinel.parent / ".non-exclusive", sentinel, sentinel.parent):
            with self.subTest(path=path.name):
                mode = path.stat().st_mode
                path.chmod(0)
                try:
                    self.assert_verdict(2)
                finally:
                    path.chmod(mode)

    def test_missing_optional_skill_keeps_unheld_tasks_reportable(self):
        with patch.object(uat, "_POOL_SCRIPTS", self.ws / "absent-skill"):
            self.assert_verdict(1)

    def test_router_and_diagnostic_share_marker_policy(self):
        self.claim()
        self.assertIs(worker_delivery.pool_claim_policy, pool_router.pcp)
        with patch.object(policy, "is_non_exclusive", wraps=policy.is_non_exclusive) as classify:
            evidence = worker_delivery.holder_evidence(self.ws, TASK, now=NOW)
            self.assertTrue(evidence[0]["non_exclusive"])
            self.assertEqual(classify.call_count, 1)
            self.assertEqual(pool_router.holders(self.ws, TASK), [])
            self.assertEqual(classify.call_count, 2)


if __name__ == "__main__":
    unittest.main()
