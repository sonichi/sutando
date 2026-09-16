#!/usr/bin/env python3
"""A launcher and the bridge it launched are ONE bridge, not two.

THE DEFECT (observed live 2026-08-04, on this host, standing for weeks).
`pgrep -f 'telegram-bridge\\.py$'` matches the launcher as well as the bridge,
because the launcher's own argv ends with the same script path:

    27538  secret-vault.py env TELEGRAM_BOT_TOKEN -- python3 src/telegram-bridge.py
    27541  python3 src/telegram-bridge.py                        <- ppid 27538

So the duplicate-process check reported `multiple processes (2 PIDs:
27538,27541)` for a healthy single bridge, on every run. Discord did NOT warn
purely because its launch happens not to use a token-injecting launcher — the
difference was the launcher, never the bridge's health.

WHY THIS MATTERS MORE THAN THE COSMETICS: this probe exists to catch a real
duplicate — two pollers racing for one Telegram `getUpdates` cursor, which
splits inbound owner messages between them and loses half. A warning that is
permanently on for a benign reason is the one you learn to scroll past, so the
false positive disables the true positive.

THE CONTROLS ARE THE POINT. "Collapses to one" is free for any function that
returns a single element, so the suite pins both directions:

  * launcher + child            -> 1, and it is the CHILD that survives
  * two INDEPENDENT bridges     -> 2, still warns   <- the true positive
  * unrelated pids              -> unchanged
  * ps failing                  -> unchanged (never silently drop a real poller)
"""
from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
assert SPEC and SPEC.loader
health = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health)


def _ps(mapping: dict, rc: int = 0):
    """Fake `ps -o pid=,ppid=` over {pid: ppid}."""
    text = "".join(f"{pid} {ppid}\n" for pid, ppid in mapping.items())
    return mock.Mock(returncode=rc, stdout=text, stderr="")


class DropLauncherParentsTest(unittest.TestCase):
    def _run(self, pids, mapping, rc=0):
        with mock.patch.object(subprocess, "run", return_value=_ps(mapping, rc)):
            return health._drop_launcher_parents(pids)

    def test_launcher_and_its_child_collapse_to_the_child(self):
        """The real observed shape. The CHILD is the process doing the work, so
        keeping the parent instead would make every downstream pid-based check
        point at the launcher."""
        kept = self._run(["27538", "27541"], {"27538": "1", "27541": "27538"})
        self.assertEqual(kept, ["27541"])

    def test_two_independent_bridges_still_report_two(self):
        """The true positive this probe exists for. Without this case, a
        function that always returned one element would pass every other test
        here and silently disable duplicate detection."""
        kept = self._run(["100", "200"], {"100": "1", "200": "1"})
        self.assertEqual(sorted(kept), ["100", "200"])

    def test_a_single_pid_is_untouched_without_calling_ps(self):
        with mock.patch.object(subprocess, "run") as run:
            self.assertEqual(health._drop_launcher_parents(["42"]), ["42"])
            run.assert_not_called()

    def test_empty_input(self):
        self.assertEqual(health._drop_launcher_parents([]), [])

    def test_parent_outside_the_matched_set_is_not_dropped(self):
        """Both bridges launched by the same shell (ppid 900, not itself a
        match) are two real bridges and must both survive."""
        kept = self._run(["100", "200"], {"100": "900", "200": "900"})
        self.assertEqual(sorted(kept), ["100", "200"])

    def test_ps_failure_returns_the_input_unchanged(self):
        """Over-reporting a duplicate is recoverable; dropping a real second
        poller is not. Fail toward the noisier answer."""
        kept = self._run(["100", "200"], {}, rc=1)
        self.assertEqual(sorted(kept), ["100", "200"])

    def test_ps_raising_returns_the_input_unchanged(self):
        with mock.patch.object(subprocess, "run", side_effect=OSError("boom")):
            self.assertEqual(sorted(health._drop_launcher_parents(["1", "2"])), ["1", "2"])

    def test_a_three_deep_chain_keeps_only_the_leaf(self):
        """shell -> vault -> bridge. Only the leaf is the poller."""
        kept = self._run(["10", "20", "30"], {"10": "1", "20": "10", "30": "20"})
        self.assertEqual(kept, ["30"])

    def test_never_returns_empty_even_if_every_pid_is_a_parent(self):
        """A cycle or a self-parent must not erase the process list entirely —
        an empty result would read as 'bridge not running' and trigger a
        restart of a healthy bridge via fix_down_bridges()."""
        kept = self._run(["10", "20"], {"10": "20", "20": "10"})
        self.assertTrue(kept)

    def _run_telegram_health_check(self, pgrep_pids, ps_mapping):
        """Exercise the production call site, not only the helper contract."""
        original_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if cmd[:3] == ["/bin/ps", "-o", "pid=,ppid="]:
                return _ps(ps_mapping)
            return original_run(cmd, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            channels = Path(tmp) / "channels"
            telegram = channels / "telegram"
            telegram.mkdir(parents=True)
            (telegram / "access.json").write_text("{}\n")

            original_home_path = health.claude_home_path

            def fake_home_path(*parts):
                if parts and parts[0] == "channels":
                    return Path(tmp).joinpath(*parts)
                return original_home_path(*parts)

            with mock.patch.object(health, "claude_home_path", side_effect=fake_home_path), \
                 mock.patch.object(health, "_should_skip_bridge", side_effect=lambda channel, _env: channel != "telegram"), \
                 mock.patch.object(health, "probe_pids", return_value=(pgrep_pids, True)), \
                 mock.patch.object(subprocess, "run", side_effect=fake_run):
                checks = health.run_all_checks()
        return next(check for check in checks if check["name"] == "telegram-bridge")

    def test_run_all_checks_collapses_launcher_child_pair(self):
        check = self._run_telegram_health_check(
            ["27538", "27541"], {"27538": "1", "27541": "27538"}
        )
        self.assertNotIn("multiple processes", check["detail"])

    def test_run_all_checks_preserves_real_duplicate_warning(self):
        check = self._run_telegram_health_check(
            ["100", "200"], {"100": "1", "200": "1"}
        )
        self.assertEqual(check["status"], "warn")
        self.assertEqual(check["detail"], "multiple processes (2 PIDs: 100,200)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
