"""Contract tests for src/workspace_layout.py: recoverable breaks heal to the
durable symlink; a data-holding dir is NEVER touched; overrides are no-ops."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "workspace_layout.py"
_spec = importlib.util.spec_from_file_location("workspace_layout", _SRC)
wl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wl)


def _app_install(tmp: Path) -> Path:
    """<app-root>/engine/sutando checkout + durable <app-root>/workspace."""
    repo = tmp / "engine" / "sutando"
    repo.mkdir(parents=True)
    (tmp / "workspace").mkdir()
    (tmp / "workspace" / "tasks").mkdir()
    return repo


class AppLayout(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _app_install(self.tmp)
        self.durable = (self.tmp / "workspace").resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def _assert_healthy_link(self):
        ws = self.repo / "workspace"
        self.assertTrue(ws.is_symlink())
        self.assertEqual(ws.resolve(), self.durable)

    def test_healthy_symlink_is_untouched(self):
        (self.repo / "workspace").symlink_to("../../workspace")
        before = os.readlink(self.repo / "workspace")
        report = wl.ensure_workspace_layout(self.repo)
        self.assertEqual(report["state"], "ok")
        self.assertEqual(report["action"], "none")
        self.assertEqual(os.readlink(self.repo / "workspace"), before)

    def test_missing_entry_is_relinked(self):
        report = wl.ensure_workspace_layout(self.repo)
        self.assertEqual(report["action"], "healed-missing")
        self._assert_healthy_link()

    def test_dangling_symlink_is_relinked(self):
        (self.repo / "workspace").symlink_to("../../nonexistent")
        report = wl.ensure_workspace_layout(self.repo)
        self.assertEqual(report["action"], "healed-dangling")
        self._assert_healthy_link()

    def test_wrong_target_symlink_is_relinked(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.repo / "workspace").symlink_to(elsewhere)
        report = wl.ensure_workspace_layout(self.repo)
        self.assertEqual(report["action"], "healed-wrong-target")
        self._assert_healthy_link()

    def test_materialized_empty_dir_is_replaced(self):
        ws = self.repo / "workspace"
        ws.mkdir()
        (ws / ".gitkeep").touch()
        report = wl.ensure_workspace_layout(self.repo)
        self.assertEqual(report["action"], "healed-materialized-empty")
        self._assert_healthy_link()

    def test_data_arriving_between_classify_and_delete_is_preserved(self):
        # A file landing between classification and the unlink loop must abort
        # the heal — simulated via a stale "empty" classification.
        ws = self.repo / "workspace"
        ws.mkdir()
        (ws / ".gitkeep").touch()
        late = ws / "task-late.txt"
        late.write_text("landed mid-heal")
        orig = wl.inspect_layout
        wl.inspect_layout = lambda root=None: {
            "path": str(ws), "app_target": str(self.durable),
            "state": "materialized-empty", "detail": "stale classification",
        }
        try:
            report = wl.ensure_workspace_layout(self.repo)
        finally:
            wl.inspect_layout = orig
        self.assertEqual(report["action"], "left-broken")
        self.assertEqual(report["state"], "materialized-with-data")
        self.assertFalse(ws.is_symlink())
        self.assertEqual(late.read_text(), "landed mid-heal")

    def test_materialized_dir_with_data_is_never_touched(self):
        ws = self.repo / "workspace"
        (ws / "tasks").mkdir(parents=True)
        stranded = ws / "tasks" / "task-123.txt"
        stranded.write_text("owner ask")
        report = wl.ensure_workspace_layout(self.repo)
        self.assertEqual(report["state"], "materialized-with-data")
        self.assertEqual(report["action"], "left-broken")
        self.assertFalse(ws.is_symlink())
        self.assertEqual(stranded.read_text(), "owner ask")

    def test_cli_exit_codes(self):
        # main() resolves the repo via _repo_root(); pin it to the fixture.
        orig = wl._repo_root
        wl._repo_root = lambda: self.repo
        try:
            ws = self.repo / "workspace"
            (ws / "tasks").mkdir(parents=True)
            (ws / "tasks" / "task-1.txt").write_text("x")
            self.assertEqual(wl.main(["prog", "--check"]), 2)   # broken → 2
            self.assertEqual(wl.main(["prog", "--ensure"]), 2)  # unhealable → still 2
            (ws / "tasks" / "task-1.txt").unlink()
            (ws / "tasks").rmdir()
            self.assertEqual(wl.main(["prog", "--ensure"]), 0)  # healed → 0
            self._assert_healthy_link()
            self.assertEqual(wl.main(["prog", "--check"]), 0)   # healthy → 0
        finally:
            wl._repo_root = orig


class EdgeBranches(unittest.TestCase):
    """Fail-safe direction of the rarely-taken branches."""

    def test_repo_root_is_the_checkout_root(self):
        self.assertEqual(wl._repo_root(), _SRC.parent.parent)

    def test_unresolvable_override_counts_as_override(self):
        # Non-strict resolve() rarely raises, so force the OSError to pin the
        # fail-safe direction: an override we cannot resolve means DO NOT touch.
        with tempfile.TemporaryDirectory() as tmp:
            repo = _app_install(Path(tmp))
            (repo / "sutando.config.local.json").write_text(
                json.dumps({"workspace": {"path": "/somewhere/custom"}})
            )
            real_path = wl.Path

            class _Unresolvable:
                def __init__(self, *a): pass
                def resolve(self, *a, **k):
                    raise OSError("unresolvable custom path")

            wl.Path = _Unresolvable
            try:
                self.assertTrue(wl._has_workspace_override(repo))
            finally:
                wl.Path = real_path
            report = wl.ensure_workspace_layout(repo)
            self.assertEqual(report["action"], "none")  # override → guard off

    def test_symlinked_app_target_is_not_a_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            repo = tmp / "engine" / "sutando"
            repo.mkdir(parents=True)
            (tmp / "real-ws").mkdir()
            (tmp / "workspace").symlink_to(tmp / "real-ws")  # target is itself a link
            self.assertIsNone(wl.app_workspace_target(repo))

    def test_unreadable_dir_counts_as_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "locked"
            d.mkdir()
            os.chmod(d, 0o000)
            try:
                self.assertTrue(wl._dir_holds_data(d))
            finally:
                os.chmod(d, 0o700)

    def test_heal_failure_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _app_install(Path(tmp))  # app layout, ws entry missing
            os.chmod(repo, 0o555)  # read-only checkout: symlink_to must fail
            try:
                report = wl.ensure_workspace_layout(repo)
            finally:
                os.chmod(repo, 0o755)
            self.assertEqual(report["action"], "heal-failed")
            self.assertIn("heal failed", report["detail"])
            self.assertFalse((repo / "workspace").exists())

    def test_regular_file_at_workspace_is_left_broken(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _app_install(Path(tmp))
            (repo / "workspace").write_text("not a dir")
            report = wl.ensure_workspace_layout(repo)
            self.assertEqual(report["state"], "not-a-directory")
            self.assertEqual(report["action"], "left-broken")
            self.assertEqual((repo / "workspace").read_text(), "not a dir")


class PlainCheckout(unittest.TestCase):
    def test_real_dir_without_app_layout_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "checkout"
            (repo / "workspace" / "tasks").mkdir(parents=True)
            report = wl.ensure_workspace_layout(repo)
            self.assertEqual(report["state"], "ok")
            self.assertEqual(report["action"], "none")
            self.assertFalse((repo / "workspace").is_symlink())

    def test_tracked_default_path_does_not_disable_guard(self):
        # The shipped config states the default explicitly; treating that as an
        # override would make the guard a global no-op.
        with tempfile.TemporaryDirectory() as tmp:
            repo = _app_install(Path(tmp))
            (repo / "sutando.config.json").write_text(
                json.dumps({"workspace": {"path": "${REPO_DIR}/workspace"}})
            )
            report = wl.ensure_workspace_layout(repo)  # entry missing → must heal
            self.assertEqual(report["action"], "healed-missing")
            self.assertTrue((repo / "workspace").is_symlink())

    def test_workspace_path_override_disables_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _app_install(Path(tmp))
            (repo / "sutando.config.local.json").write_text(
                json.dumps({"workspace": {"path": "/somewhere/else"}})
            )
            # even with a broken entry, an override means the symlink is inert
            report = wl.ensure_workspace_layout(repo)
            self.assertEqual(report["state"], "ok")
            self.assertEqual(report["action"], "none")

    def test_symlink_already_pointing_at_override_stays_untouched(self):
        # A symlink at the default path resolving to the SAME custom dir must
        # not launder the override into "equal" and get rewired to app default.
        with tempfile.TemporaryDirectory() as tmp:
            repo = _app_install(Path(tmp))
            custom = Path(tmp) / "custom-workspace"
            custom.mkdir()
            (repo / "sutando.config.local.json").write_text(
                json.dumps({"workspace": {"path": str(custom)}})
            )
            (repo / "workspace").symlink_to(custom)
            report = wl.ensure_workspace_layout(repo)
            self.assertEqual(report["action"], "none")
            self.assertEqual((repo / "workspace").resolve(), custom.resolve())


if __name__ == "__main__":
    unittest.main()
