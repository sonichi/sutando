#!/usr/bin/env python3
"""Regression test: `check_codex_presence` must key on PATH resolution, not on
an engine-tree location, must distinguish a wiped binary from one that was
never installed, and must only warn WHERE CODEX WOULD BE USED.

That last rule is the one this file previously got backwards. Every case here
asserted `warn` whenever the binary was absent, with no host-configuration
input at all — so an owner-only install, which never takes the sandboxed
delegation path, got a permanent warn it could not clear, and these tests
encoded that rather than preventing it.

Cases:
  a) codex on PATH                          -> ok, detail names the resolved path
  b) absent, consumer, ~/.codex present     -> warn, "wiped binary", remedy given
  c) absent, consumer, ~/.codex absent      -> warn, "never installed"
  d) resolved OUTSIDE the engine tree       -> still ok (the second known
                                               topology; a tree-keyed probe
                                               would be wrong on both hosts)
  e) absent, NO consumer                    -> ok — absent and unused is not a
                                               fault (the false positive above)
  f) the probe is registered in run_checks  -> a probe nobody calls reports
                                               nothing

`_codex_delegation_consumer` gets its own cases. Both of its signals are
required and neither is sufficient: config is predictive but under-detects,
and received traffic is exact but lags. The under-detection is not
hypothetical — it was measured on a live host whose only access.json holds
`tofuOwner` and no `tierMap`, and which had nonetheless already received
guest-tier tasks (`test_received_non_owner_task_without_any_tiermap`).

Run: python3 tests/health-check-codex-presence.test.py
Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent

def CONSUMER():
    """Any reason string works; the probe only asks whether there IS one."""
    return "core runtime is codex"


def NO_CONSUMER():
    return None


def _load_module():
    spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hc = _load_module()


class CodexPresence(unittest.TestCase):
    def test_present_is_ok_and_names_the_path(self):
        r = hc.check_codex_presence(which=lambda _: "/Users/x/.local/bin/codex")
        self.assertEqual(r["status"], "ok")
        self.assertIn("/Users/x/.local/bin/codex", r["detail"])

    def test_outside_the_engine_tree_is_still_ok(self):
        # Both known hosts resolve codex outside the engine tree; a probe keyed
        # to the tree would warn on a perfectly working install.
        r = hc.check_codex_presence(which=lambda _: "/opt/homebrew/bin/codex")
        self.assertEqual(r["status"], "ok")

    def test_absent_with_config_reads_as_wiped_and_gives_the_remedy(self):
        with patch.object(Path, "is_dir", return_value=True):
            r = hc.check_codex_presence(which=lambda _: None, consumer=CONSUMER)
        self.assertEqual(r["status"], "warn")
        self.assertIn("wiped binary", r["detail"])
        self.assertIn("--prefix ~/.local", r["detail"])
        self.assertIn("non-owner", r["detail"])

    def test_absent_without_config_reads_as_never_installed(self):
        with patch.object(Path, "is_dir", return_value=False):
            r = hc.check_codex_presence(which=lambda _: None, consumer=CONSUMER)
        self.assertEqual(r["status"], "warn")
        self.assertIn("never installed", r["detail"])
        self.assertNotIn("wiped binary", r["detail"])

    def test_warn_names_why_this_host_needs_codex(self):
        """A warn the owner cannot act on is the fault this probe used to be."""
        with patch.object(Path, "is_dir", return_value=False):
            r = hc.check_codex_presence(which=lambda _: None,
                                        consumer=lambda: "cardinal/access.json maps sender(s) to tier team")
        self.assertIn("cardinal/access.json maps sender(s) to tier team", r["detail"])

    # ── the false positive this change exists to prevent ────────────────────
    def test_owner_only_host_without_codex_is_ok_not_warn(self):
        for config_present in (True, False):
            with self.subTest(codex_config_dir=config_present):
                with patch.object(Path, "is_dir", return_value=config_present):
                    r = hc.check_codex_presence(which=lambda _: None, consumer=NO_CONSUMER)
                self.assertEqual(r["status"], "ok",
                                 "an owner-only install never takes the sandboxed path, so a "
                                 "missing optional binary is not a health fault")
                self.assertIn("not a fault", r["detail"])

    def test_probe_is_registered(self):
        src = (REPO / "src" / "health-check.py").read_text()
        self.assertIn("checks.append(check_codex_presence())", src,
                      "probe exists but nothing calls it")


class CodexDelegationConsumer(unittest.TestCase):
    def _tmpdir(self) -> Path:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def _dirs(self) -> tuple[Path, Path]:
        root = self._tmpdir()
        tasks, channels = root / "tasks", root / "channels"
        tasks.mkdir()
        channels.mkdir()
        return tasks, channels

    def _access(self, channels: Path, name: str, data: dict) -> None:
        (channels / name).mkdir(parents=True, exist_ok=True)
        (channels / name / "access.json").write_text(json.dumps(data))

    def _task(self, tasks: Path, tid: str, tier: str) -> None:
        (tasks / f"task-{tid}.txt").write_text(
            f"id: task-{tid}\ntask: do a thing\nsource: discord\naccess_tier: {tier}\n")

    def test_bare_owner_only_host_has_no_consumer(self):
        tasks, channels = self._dirs()
        self._access(channels, "ag2space", {"tofuOwner": "@john:example"})
        self._task(tasks, "1", "owner")
        with patch.object(hc, "_codex_runtime_selected", return_value=False):
            self.assertIsNone(hc._codex_delegation_consumer(tasks_dir=tasks,
                                                            channels_dir=channels))

    def test_codex_runtime_alone_is_a_consumer(self):
        tasks, channels = self._dirs()
        with patch.object(hc, "_codex_runtime_selected", return_value=True):
            self.assertEqual(hc._codex_delegation_consumer(tasks_dir=tasks,
                                                           channels_dir=channels),
                             "core runtime is codex")

    def test_configured_non_owner_tier_is_a_consumer(self):
        tasks, channels = self._dirs()
        self._access(channels, "discord", {"tierMap": {"u1": "team"}})
        with patch.object(hc, "_codex_runtime_selected", return_value=False):
            why = hc._codex_delegation_consumer(tasks_dir=tasks, channels_dir=channels)
        self.assertIsNotNone(why)
        self.assertIn("team", why)

    def test_owner_only_tiermap_is_not_a_consumer(self):
        """Positive control: a tierMap that names ONLY owner must not trip it."""
        tasks, channels = self._dirs()
        self._access(channels, "discord", {"tierMap": {"u1": "owner"}})
        with patch.object(hc, "_codex_runtime_selected", return_value=False):
            self.assertIsNone(hc._codex_delegation_consumer(tasks_dir=tasks,
                                                            channels_dir=channels))

    def test_received_non_owner_task_without_any_tiermap(self):
        """The measured live case config alone misses entirely."""
        tasks, channels = self._dirs()
        self._access(channels, "ag2space", {"tofuOwner": "@john:example"})
        self._task(tasks, "2", "guest")
        with patch.object(hc, "_codex_runtime_selected", return_value=False):
            why = hc._codex_delegation_consumer(tasks_dir=tasks, channels_dir=channels)
        self.assertIsNotNone(why)
        self.assertIn("guest", why)

    def test_unreadable_access_record_is_not_a_consumer(self):
        tasks, channels = self._dirs()
        (channels / "broken").mkdir()
        (channels / "broken" / "access.json").write_text("{not json")
        with patch.object(hc, "_codex_runtime_selected", return_value=False):
            self.assertIsNone(hc._codex_delegation_consumer(tasks_dir=tasks,
                                                            channels_dir=channels))

    def test_missing_dirs_do_not_raise(self):
        root = self._tmpdir()
        with patch.object(hc, "_codex_runtime_selected", return_value=False):
            self.assertIsNone(hc._codex_delegation_consumer(tasks_dir=root / "nope",
                                                            channels_dir=root / "nope2"))

    def test_scan_is_bounded(self):
        """An owner-only host with a large archive must not scan without limit."""
        tasks, channels = self._dirs()
        for i in range(20):
            self._task(tasks, str(i), "owner")
        with patch.object(hc, "_codex_runtime_selected", return_value=False):
            self.assertIsNone(hc._codex_delegation_consumer(
                tasks_dir=tasks, channels_dir=channels, scan_cap=5))

    def test_unreadable_task_is_skipped_not_counted_as_evidence(self):
        """A task the probe cannot read is not evidence either way — the
        non-owner sibling beside it must still be found."""
        tasks, channels = self._dirs()
        (tasks / "task-unreadable.txt").mkdir()
        self._task(tasks, "3", "team")
        with patch.object(hc, "_codex_runtime_selected", return_value=False):
            why = hc._codex_delegation_consumer(tasks_dir=tasks, channels_dir=channels)
        self.assertIsNotNone(why)
        self.assertIn("team", why)

    def test_unreadable_task_alone_yields_no_consumer(self):
        tasks, channels = self._dirs()
        (tasks / "task-unreadable.txt").mkdir()
        with patch.object(hc, "_codex_runtime_selected", return_value=False):
            self.assertIsNone(hc._codex_delegation_consumer(tasks_dir=tasks,
                                                            channels_dir=channels))

    def test_unimportable_protocol_yields_no_consumer(self):
        """The probe must not break the health run when its delegate is absent —
        and must not invent a tier vocabulary of its own to carry on with."""
        tasks, channels = self._dirs()
        self._access(channels, "discord", {"tierMap": {"u1": "team"}})
        self._task(tasks, "4", "guest")
        with patch.object(hc, "_codex_runtime_selected", return_value=False), \
                patch.dict(sys.modules, {"local_task_protocol": None}):
            self.assertIsNone(hc._codex_delegation_consumer(tasks_dir=tasks,
                                                            channels_dir=channels))


if __name__ == "__main__":
    unittest.main(verbosity=2)
