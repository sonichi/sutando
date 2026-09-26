#!/usr/bin/env python3
"""Exercise the published label contract and Sutando's adapter in the coverage lane."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parent.parent
PACKAGE_TEST = REPO / "packages/ag2-sparrow/tests/test_worker_profile_labels.py"
WRAPPER = REPO / "src/remote-gateway-bridge.py"
SCRIPTS = REPO / "skills/worker-pool/scripts"
WID = "274cb60d473744dba54040a9de119877"
MXID = "@label-owner:ag2.space"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "src"))

import pool_roster as roster  # noqa: E402


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkerLabelSync(unittest.TestCase):
    def setUp(self):
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_standalone_profile_contract_runs_under_coverage(self):
        suite = load(PACKAGE_TEST, "worker_profile_contract")
        cases = (
            suite.test_only_owner_overrides_are_applied_by_id,
            suite.test_profile_is_published_before_owner_labels_are_read,
            suite.test_empty_override_map_clears_but_missing_map_never_does,
            suite.test_wrong_identity_or_unpublished_card_cannot_rename,
            suite.test_identity_change_during_profile_read_cannot_apply_old_owner_labels,
            suite.test_invalid_label_and_unsupported_endpoint_leave_pool_untouched,
        )
        for case in cases:
            with self.subTest(case=case.__name__):
                case()

    def test_optional_read_and_apply_failures_retry_without_blocking(self):
        suite = load(PACKAGE_TEST, "worker_profile_failures")
        with tempfile.TemporaryDirectory() as directory:
            bridge = suite._module(Path(directory))
            record = suite._ready(bridge)
            calls = []
            bridge._req = lambda *args, **_kwargs: calls.append(args)
            self.assertFalse(bridge._maybe_pull_worker_labels(record))
            self.assertEqual(calls, [], "no adapter means no profile read")

            def fail_apply(_labels, _version, _mxid):
                raise OSError("roster is upgrading")

            bridge._WORKER_LABEL_APPLIER = fail_apply
            bridge._req = lambda *_args, **_kwargs: suite._profile({WID: "Ryan"})
            self.assertFalse(bridge._maybe_pull_worker_labels(record))
            self.assertGreater(bridge._profile_labels_retry_at, time.time() + 250)

            bridge._profile_labels_retry_at = 0.0
            bridge._profile_labels_checked_at = 0.0
            bridge._req = mock.Mock(side_effect=TimeoutError("gateway offline"))
            self.assertFalse(bridge._maybe_pull_worker_labels(record))
            self.assertEqual(bridge._req.call_count, 1)
            self.assertGreater(bridge._profile_labels_retry_at, time.time() + 250)

    def test_wrapper_applies_labels_to_roster_and_reports_missing_roster(self):
        with tempfile.TemporaryDirectory() as directory:
            ws = Path(directory)
            os.environ["SUTANDO_TEST_WORKSPACE"] = str(ws)
            os.environ["REMOTE_TASK_URL"] = "https://gw.invalid/relay"
            os.environ["REMOTE_TASK_TOKEN"] = "test-token"
            with mock.patch("workspace_default.resolve_workspace", return_value=ws):
                bridge = load(WRAPPER, "worker_label_wrapper")
            bridge.WS = ws
            with self.assertRaisesRegex(RuntimeError, "worker label apply failed"):
                bridge._sutando_apply_worker_label_overrides({WID: "Ryan"}, 7, MXID)

            roster.register_worker(ws, WID, "base-name", runtime="codex")
            result = bridge._sutando_apply_worker_label_overrides({WID: "Ryan"}, 7, MXID)
            self.assertTrue(result["changed"])
            row = roster.load_roster(ws)["workers"][WID]
            self.assertEqual((row["label"], row["display_label"]), ("base-name", "Ryan"))
            self.assertEqual(roster.resolve_label(roster.load_roster(ws), "Ryan"), WID)


if __name__ == "__main__":
    unittest.main()
