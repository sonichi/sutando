#!/usr/bin/env python3
"""
Tests for the per-host user-defined health checks in src/health-check.py.

A host-specific probe used to require editing `run_all_checks` — shared repo code
— so every host either carried every other host's probes or went without its own.
`<workspace>/hosts/<host>/health-checks-extra.json` is the opt-in extension point,
mirroring the tool-suites extras convention
(skills/proactive-loop/scripts/tool-suites-check.py).

Covers:
  a) no file at all                     -> zero rows (the CI / fresh-install case)
  b) an exit-0 command                  -> ok, with its output as the detail
  c) a non-zero command                 -> warn, exit code + output in the detail
  d) a command that hangs               -> warn, killed at the declared timeout
  e) a command that cannot be spawned   -> warn, never an exception
  f) malformed JSON / wrong shape       -> no-op, never an exception
  g) entries missing name or command    -> dropped, siblings still run
  h) an extra row is distinguishable    -> the `extra:` name prefix
  i) an extra row never becomes an issue-> is_issue() stays False on a failing one
  j) the path is per-host + workspace    -> hosts/<label>/health-checks-extra.json
  k) run_all_checks appends them        -> the wiring exists, not just the helper
  l) a shell that exits 0 leaving a bg child -> the child is killed, not orphaned
  m) output far larger than the detail cap   -> retained bytes stay bounded
  n) every outcome carries alerting: False   -> an opt-in probe cannot page anyone

Run: python3 tests/health-check-user-defined-checks.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import ast
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


class TestUserDefinedChecks(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.host = "test-host"

    def tearDown(self):
        self._tmp.cleanup()

    def _declare(self, payload) -> Path:
        path = self.ws / "hosts" / self.host / hc.USER_CHECKS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        return path

    def _run(self) -> list:
        return hc.check_user_defined(workspace_dir=self.ws, host=self.host)

    # --- a) absent ----------------------------------------------------------

    def test_no_declaration_file_yields_no_rows(self):
        """The fresh-install / CI case: nothing declared, nothing added."""
        self.assertEqual(self._run(), [])

    # --- j) where the file lives -------------------------------------------

    def test_path_is_per_host_under_the_given_workspace(self):
        path = hc.user_checks_path(workspace_dir=self.ws, host=self.host)
        self.assertEqual(path, self.ws / "hosts" / self.host / "health-checks-extra.json")

    def test_path_defaults_to_the_repo_host_label_resolver(self):
        """The label must come from `_host_label()`, not a raw hostname slug."""
        with mock.patch.object(hc, "_host_label", return_value="resolved-label") as m:
            path = hc.user_checks_path(workspace_dir=self.ws)
        m.assert_called_once_with()
        self.assertEqual(path.parent.name, "resolved-label")

    # --- b/c/h/i) the pass and fail verdicts --------------------------------

    def test_exit_zero_is_ok_and_carries_the_command_output(self):
        self._declare({"checks": [{"name": "backup", "command": "echo backup fresh"}]})
        rows = self._run()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["status"], "ok", rows[0])
        self.assertIn("backup fresh", rows[0]["detail"])

    def test_nonzero_exit_is_warn_with_the_code_and_the_output(self):
        self._declare({"checks": [
            {"name": "backup", "command": "echo snapshot 9 days old >&2; exit 3"}]})
        row = self._run()[0]
        self.assertEqual(row["status"], "warn", row)
        self.assertIn("exit 3", row["detail"])
        self.assertIn("snapshot 9 days old", row["detail"])

    def test_an_extra_row_is_name_prefixed_so_it_reads_as_non_builtin(self):
        self._declare({"checks": [{"name": "backup", "command": "true"}]})
        self.assertEqual(self._run()[0]["name"], "extra:backup")

    def test_a_failing_extra_check_is_never_an_issue(self):
        """`warn` on purpose: a host's own probe must not move the exit code or
        wake --emit-task / --notify-*, which key off `is_issue`."""
        self._declare({"checks": [{"name": "backup", "command": "exit 1"}]})
        row = self._run()[0]
        self.assertEqual(row["status"], "warn")
        self.assertFalse(hc.is_issue(row))

    def test_a_silent_command_still_gets_a_readable_detail(self):
        self._declare({"checks": [{"name": "quiet", "command": "true"}]})
        self.assertEqual(self._run()[0]["detail"], "(no output)")

    def test_detail_is_capped_so_a_chatty_command_cannot_flood_the_report(self):
        self._declare({"checks": [
            {"name": "chatty", "command": "python3 -c \"print('x' * 5000)\""}]})
        self.assertLessEqual(len(self._run()[0]["detail"]), hc.USER_CHECK_DETAIL_CAP)

    # --- d) a hang must not hang the health check ---------------------------

    def test_a_hanging_command_is_killed_at_its_timeout_and_warns(self):
        self._declare({"checks": [
            {"name": "hangs", "command": "sleep 30", "timeout": 1}]})
        started = time.monotonic()
        row = self._run()[0]
        elapsed = time.monotonic() - started
        self.assertEqual(row["status"], "warn", row)
        self.assertIn("TIMEOUT", row["detail"])
        self.assertLess(elapsed, 15, f"took {elapsed:.1f}s — the timeout did not bound it")

    def test_a_backgrounded_grandchild_cannot_hold_the_run_open(self):
        """The reason output goes to a file and the whole process GROUP is killed:
        a pipe held by a surviving grandchild outlives the timeout."""
        self._declare({"checks": [
            {"name": "daemonizes", "command": "sleep 30 & sleep 30", "timeout": 1}]})
        started = time.monotonic()
        row = self._run()[0]
        elapsed = time.monotonic() - started
        self.assertEqual(row["status"], "warn", row)
        self.assertLess(elapsed, 15, f"took {elapsed:.1f}s — a grandchild blocked the drain")

    def test_a_bad_timeout_value_falls_back_to_the_default(self):
        for bad in (0, -5, True, "30", None):
            with self.subTest(timeout=bad):
                self._declare({"checks": [
                    {"name": "n", "command": "true", "timeout": bad}]})
                decls = hc.load_user_checks(
                    hc.user_checks_path(workspace_dir=self.ws, host=self.host))
                self.assertEqual(decls[0]["timeout"], hc.USER_CHECK_TIMEOUT_S)

    def test_the_group_kill_falls_back_to_the_process_when_there_is_no_group(self):
        """A process that already exited has no group left to signal; the fallback
        must still reap the direct child rather than let the OSError escape."""
        proc = mock.Mock(pid=os.getpid())
        with mock.patch.object(hc.os, "getpgid", side_effect=OSError("ESRCH")):
            hc._kill_user_command_tree(proc)
        proc.kill.assert_called_once_with()
        proc.wait.assert_called_once()

    def test_a_process_that_ignores_the_kill_does_not_block_the_run(self):
        proc = mock.Mock(pid=os.getpid())
        proc.wait.side_effect = hc.subprocess.TimeoutExpired(cmd="x", timeout=5)
        with mock.patch.object(hc.os, "getpgid", return_value=1), \
                mock.patch.object(hc.os, "killpg"):
            hc._kill_user_command_tree(proc)

    def test_an_unresolvable_path_yields_no_rows_instead_of_raising(self):
        """The declaration path is resolved OUTSIDE load_user_checks, so its own
        failure needs its own guard — otherwise it takes the health check down."""
        self._declare({"checks": [{"name": "n", "command": "true"}]})
        with mock.patch.object(hc, "_host_label", side_effect=RuntimeError("no scutil")):
            self.assertEqual(hc.check_user_defined(workspace_dir=self.ws), [])

    # --- e) a command that cannot even be spawned ---------------------------

    def test_a_spawn_failure_is_a_warn_not_an_exception(self):
        self._declare({"checks": [{"name": "boom", "command": "true"}]})
        with mock.patch.object(hc.subprocess, "Popen", side_effect=OSError("no fork")):
            row = self._run()[0]
        self.assertEqual(row["status"], "warn", row)
        self.assertIn("no fork", row["detail"])

    def test_an_unexpected_runner_error_is_a_warn_not_an_exception(self):
        self._declare({"checks": [{"name": "boom", "command": "true"}]})
        with mock.patch.object(hc, "run_user_command", side_effect=RuntimeError("kaboom")):
            row = self._run()[0]
        self.assertEqual(row["status"], "warn", row)
        self.assertIn("kaboom", row["detail"])

    def test_a_missing_command_binary_warns_rather_than_raising(self):
        self._declare({"checks": [
            {"name": "absent", "command": "sutando-no-such-binary-xyz"}]})
        row = self._run()[0]
        self.assertEqual(row["status"], "warn", row)

    # --- f/g) malformed declarations ----------------------------------------

    def _load(self) -> list:
        """The LOADER, not the caller. `check_user_defined` wraps it in its own
        try/except, so asserting only through that path leaves this guard untested
        — a mutation removing it passed the whole suite until these were added."""
        return hc.load_user_checks(hc.user_checks_path(workspace_dir=self.ws, host=self.host))

    def test_the_loader_itself_swallows_malformed_json(self):
        self._declare("{ this is not json")
        self.assertEqual(self._load(), [])

    def test_the_loader_itself_swallows_a_non_object_document(self):
        self._declare(["echo hi"])
        self.assertEqual(self._load(), [])

    def test_the_loader_itself_swallows_a_non_list_checks_key(self):
        self._declare({"checks": "echo hi"})
        self.assertEqual(self._load(), [])

    def test_the_loader_itself_swallows_a_missing_file(self):
        self.assertEqual(self._load(), [])

    def test_the_loader_itself_swallows_an_unreadable_file(self):
        self._declare({"checks": [{"name": "n", "command": "true"}]})
        with mock.patch.object(Path, "read_text", side_effect=OSError("EACCES")):
            self.assertEqual(self._load(), [])

    def test_malformed_json_is_a_silent_no_op(self):
        self._declare("{ this is not json")
        self.assertEqual(self._run(), [])

    def test_a_non_object_document_is_a_silent_no_op(self):
        self._declare(["echo hi"])
        self.assertEqual(self._run(), [])

    def test_a_non_list_checks_key_is_a_silent_no_op(self):
        self._declare({"checks": "echo hi"})
        self.assertEqual(self._run(), [])

    def test_an_unreadable_declaration_is_a_silent_no_op(self):
        self._declare({"checks": [{"name": "n", "command": "true"}]})
        with mock.patch.object(Path, "read_text", side_effect=OSError("EACCES")):
            self.assertEqual(self._run(), [])

    def test_a_directory_where_the_file_belongs_is_a_silent_no_op(self):
        (self.ws / "hosts" / self.host / hc.USER_CHECKS_FILE).mkdir(parents=True)
        self.assertEqual(self._run(), [])

    def test_incomplete_entries_are_dropped_and_valid_siblings_still_run(self):
        """A typo in one entry must not silently cancel the rest of the file.
        Above, in order: no name, no command, blank name, blank command, and an
        entry that is not an object at all."""
        self._declare({"checks": [
            {"command": "true"},
            {"name": "no-command"},
            {"name": "  ", "command": "true"},
            {"name": "blank-cmd", "command": "   "},
            "echo not-an-object",
            {"name": "good", "command": "echo ran"},
        ]})
        rows = self._run()
        self.assertEqual([r["name"] for r in rows], ["extra:good"], rows)
        self.assertEqual(rows[0]["status"], "ok")

    def test_declared_order_is_preserved(self):
        self._declare({"checks": [
            {"name": "one", "command": "true"},
            {"name": "two", "command": "true"},
            {"name": "three", "command": "true"},
        ]})
        self.assertEqual([r["name"] for r in self._run()],
                         ["extra:one", "extra:two", "extra:three"])

    # --- k) the wiring, not just the helper ---------------------------------

    def test_run_all_checks_calls_check_user_defined(self):
        """A helper nothing calls is a feature nobody gets.

        Asserted over `run_all_checks`'s own AST rather than by executing it: the
        real function shells out to tmux, lsof, pgrep and the network, and a stub
        set wide enough to neutralise 60 probes breaks on the next probe added.
        """
        tree = ast.parse((REPO / "src" / "health-check.py").read_text())
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "run_all_checks"), None)
        self.assertIsNotNone(fn, "run_all_checks not found")
        called = {n.func.id for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertIn("check_user_defined", called,
                      "run_all_checks never calls check_user_defined — the extras "
                      "are unreachable however well the helper behaves")

    def test_the_call_site_extends_the_checks_list(self):
        """`checks.extend(...)`, not a bare call: a list of rows appended one at a
        time or dropped on the floor reaches no reader."""
        tree = ast.parse((REPO / "src" / "health-check.py").read_text())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "run_all_checks")
        extended = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "extend"
            and any(isinstance(a, ast.Call) and isinstance(a.func, ast.Name)
                    and a.func.id == "check_user_defined" for a in n.args)
        ]
        self.assertEqual(len(extended), 1, "expected exactly one checks.extend(check_user_defined(...))")



class TestUserCheckContainment(unittest.TestCase):
    """The three runtime bounds: descendant lifetime, retained output, alerting."""

    def test_a_background_child_does_not_outlive_a_normal_exit(self):
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / "survived"
            # The shell exits 0 immediately; the child would write AFTER that exit.
            cmd = f"(sleep 0.6; touch {marker}) & exit 0"
            code, _ = hc.run_user_command(cmd, 10.0, Path(d))
            self.assertEqual(code, 0, "the shell itself must still report its own exit")
            time.sleep(1.2)
            self.assertFalse(marker.exists(),
                             "a background grandchild outlived the command that spawned it")

    def test_the_background_child_really_would_have_written_without_the_bound(self):
        # Positive control: the same shape, run so nothing reaps the group, DOES write.
        # Without this, the assertion above passes if `touch` simply never worked.
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / "survived"
            os.system(f"(sleep 0.2; touch {marker}) & exit 0")
            time.sleep(0.9)
            self.assertTrue(marker.exists(), "control failed: the child never wrote at all")

    def test_output_far_over_the_cap_is_not_retained_whole(self):
        with tempfile.TemporaryDirectory() as d:
            mib = 2 * 1024 * 1024
            code, output = hc.run_user_command(
                f"python3 -c \"print('x' * {mib})\"", 30.0, Path(d))
            self.assertEqual(code, 0)
            self.assertLessEqual(len(output), hc.USER_CHECK_OUTPUT_READ_CAP,
                                 "the whole sink was read into memory")
            self.assertGreater(len(output), hc.USER_CHECK_DETAIL_CAP,
                               "the read must still cover what gets rendered")

    def test_every_outcome_suppresses_alerting(self):
        outcomes = {
            "ok": "exit 0",
            "nonzero": "exit 3",
            "timeout": "sleep 30",
        }
        for label, command in outcomes.items():
            with self.subTest(outcome=label):
                decl = {"name": label, "command": command, "timeout": 0.4}
                row = hc.run_user_check(decl, cwd=Path("."))
                self.assertIs(row.get("alerting"), False,
                              f"{label} outcome can wake a notifier surface")
                self.assertFalse(hc.is_issue(row))

    def test_a_command_that_cannot_run_also_suppresses_alerting(self):
        with mock.patch.object(hc, "run_user_command", side_effect=RuntimeError("boom")):
            row = hc.run_user_check({"name": "x", "command": "true", "timeout": 1.0})
        self.assertIs(row.get("alerting"), False)
        self.assertEqual(row["status"], "warn")


if __name__ == "__main__":
    unittest.main(verbosity=2)
