#!/usr/bin/env python3
"""Controls for scripts/skill-read-receipt.py — every exit path, both directions.

A checker never observed FAILING is not a validated checker, so each assertion drives
the script into the state it is meant to catch and asserts the non-zero, not only the
clean path.

These call `main(argv)` IN-PROCESS rather than spawning the script. A subprocess test
proves the CLI works and gives the coverage gate nothing — measured on this PR's first
CI run: 106 lines changed, 106 missing, 0.0%. Driving main() exercises the same arg
parsing and the same exit codes while staying visible to coverage.
"""
import contextlib
import importlib.util
import json
import subprocess
import sys
import threading
import io
import os
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "skill_read_receipt", REPO / "scripts" / "skill-read-receipt.py")
srr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(srr)


@contextlib.contextmanager
def session(value):
    """Set CLAUDE_CODE_SESSION_ID for one block; '' means unset-equivalent."""
    prev = os.environ.get("CLAUDE_CODE_SESSION_ID")
    os.environ["CLAUDE_CODE_SESSION_ID"] = value
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        else:
            os.environ["CLAUDE_CODE_SESSION_ID"] = prev


def run(argv, sess="sess-A"):
    """(rc, combined output) from main() in-process."""
    out, err = io.StringIO(), io.StringIO()
    with session(sess), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = srr.main(argv)
    return rc, out.getvalue() + err.getvalue()


class ReceiptContract(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.skill = self.d / "SKILL.md"
        self.skill.write_text("alpha\nbeta\n")
        self.state = self.d / "state"

    def _base(self):
        return ["--skill", str(self.skill), "--state-dir", str(self.state)]

    def test_no_receipt_demands_a_full_read(self):
        rc, out = run(self._base() + ["--check"])
        self.assertEqual(rc, 1, out)
        self.assertIn("FULL READ REQUIRED", out)

    def test_record_then_check_passes(self):
        self.assertEqual(run(self._base() + ["--record"])[0], 0)
        rc, out = run(self._base() + ["--check"])
        self.assertEqual(rc, 0, out)
        self.assertIn("receipt valid", out)

    def test_changed_bytes_invalidate_the_receipt(self):
        run(self._base() + ["--record"])
        self.skill.write_text("alpha\nbeta CHANGED\n")
        rc, out = run(self._base() + ["--check"])
        self.assertEqual(rc, 1, out)
        self.assertIn("content changed", out)

    def test_another_sessions_receipt_is_never_reused(self):
        run(self._base() + ["--record"], sess="sess-A")
        rc, out = run(self._base() + ["--check"], sess="sess-B")
        self.assertEqual(rc, 1, out)

    def test_unscoped_record_is_refused(self):
        rc, out = run(self._base() + ["--record"], sess="")
        self.assertEqual(rc, 2, out)
        self.assertIn("REFUSED", out)

    def test_unscoped_check_cannot_answer(self):
        rc, out = run(self._base() + ["--check"], sess="")
        self.assertEqual(rc, 2, out)

    def test_missing_file_cannot_answer(self):
        rc, out = run(["--skill", str(self.d / "nope.md"),
                       "--state-dir", str(self.state), "--check"])
        self.assertEqual(rc, 2, out)

    def test_absent_marker_cannot_answer_on_both_paths(self):
        """--check and --record share one precondition helper so they cannot disagree."""
        for mode in ("--check", "--record"):
            rc, out = run(self._base() + ["--marker", "NOT PRESENT", mode])
            self.assertEqual(rc, 2, f"{mode}: {out}")

    def test_marker_defaults_to_none_so_the_tool_is_generic(self):
        """The pr-triage original defaulted to its own HARNESS-GREEN clause; a generic
        tool keeping that default would refuse every skill that does not carry it."""
        self.assertEqual(run(self._base() + ["--record"])[0], 0)

    def test_a_corrupt_receipt_store_does_not_authorise_a_skip(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / srr.RECEIPTS).write_text("{ not json")
        rc, _ = run(self._base() + ["--check"])
        self.assertEqual(rc, 1)

    # --- audit -------------------------------------------------------------
    def test_audit_clean_when_nothing_changed(self):
        run(self._base() + ["--record"])
        rc, out = run(["--state-dir", str(self.state), "--audit"])
        self.assertEqual(rc, 0, out)
        self.assertIn("0 drifted", out)

    def test_audit_FAILS_when_a_read_file_changed_underneath(self):
        run(self._base() + ["--record"])
        self.skill.write_text("alpha\nbeta CHANGED\n")
        rc, out = run(["--state-dir", str(self.state), "--audit"])
        self.assertEqual(rc, 1, out)
        self.assertIn("DRIFTED", out)

    def test_audit_FAILS_when_a_read_file_was_deleted(self):
        run(self._base() + ["--record"])
        self.skill.unlink()
        rc, out = run(["--state-dir", str(self.state), "--audit"])
        self.assertEqual(rc, 1, out)
        self.assertIn("GONE", out)

    def test_audit_ignores_other_sessions(self):
        run(self._base() + ["--record"], sess="sess-A")
        self.skill.write_text("changed\n")
        rc, out = run(["--state-dir", str(self.state), "--audit"], sess="sess-B")
        self.assertEqual(rc, 0, out)
        self.assertIn("no receipts for this session", out)

    def test_audit_without_a_session_cannot_answer(self):
        rc, out = run(["--state-dir", str(self.state), "--audit"], sess="")
        self.assertEqual(rc, 2, out)

    def test_no_receipts_is_clean_not_a_failure(self):
        rc, out = run(["--state-dir", str(self.d / "empty"), "--audit"])
        self.assertEqual(rc, 0, out)

    def test_check_without_skill_is_an_arg_error(self):
        with self.assertRaises(SystemExit):
            run(["--state-dir", str(self.state), "--check"])

    # --- review findings from qingyun-wu on 46f48bd5 -----------------------
    def test_concurrent_records_do_not_erase_each_other(self):
        """P2: load->mutate->write was unserialised, so two record() calls could each
        load the same snapshot and each write the WHOLE store back. Both returned 0
        while only one receipt survived, and --audit then reported clean for a skill
        nobody was tracking. Real processes, because the race is between writers."""
        a, b = self.d / "A.md", self.d / "B.md"
        a.write_text("aaa\n"); b.write_text("bbb\n")
        env = dict(os.environ, CLAUDE_CODE_SESSION_ID="sess-race")
        procs = [subprocess.Popen(
            [sys.executable, str(REPO / "scripts" / "skill-read-receipt.py"),
             "--skill", str(f), "--state-dir", str(self.state), "--record"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            for f in (a, b)]
        rcs = [p.wait() for p in procs]
        self.assertEqual(rcs, [0, 0], "both records reported success")
        store = json.loads((self.state / srr.RECEIPTS).read_text())
        names = sorted(Path(k.split(":", 1)[1]).name for k in store)
        self.assertEqual(names, ["A.md", "B.md"],
                         f"a reported-successful receipt was erased: {names}")

    def test_audit_REFUSES_a_corrupt_store_instead_of_reporting_clean(self):
        """P2: _load collapsed corruption, a wrong top-level type and every I/O error
        into {}, which --audit rendered as 'nothing read in full yet', rc=0. A broken
        probe reporting clean is the exact failure this file exists to prevent."""
        self.state.mkdir(parents=True, exist_ok=True)
        for corrupt in ("{ truncated", "[]", "null"):
            (self.state / srr.RECEIPTS).write_text(corrupt)
            rc, out = run(["--state-dir", str(self.state), "--audit"])
            self.assertEqual(rc, 2, f"{corrupt!r} -> rc={rc}: {out}")
            self.assertIn("CANNOT ANSWER", out)

    def test_record_refuses_to_overwrite_a_store_it_cannot_read(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / srr.RECEIPTS).write_text("{ truncated")
        rc, out = run(self._base() + ["--record"])
        self.assertEqual(rc, 2, out)
        self.assertIn("REFUSED", out)

    def test_a_missing_store_is_empty_not_corrupt(self):
        """The distinction has to cut both ways, or every fresh state dir refuses."""
        rc, out = run(["--state-dir", str(self.d / "never-used"), "--audit"])
        self.assertEqual(rc, 0, out)


if __name__ == "__main__":
    unittest.main(verbosity=1)
