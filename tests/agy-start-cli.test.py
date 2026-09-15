#!/usr/bin/env python3
"""Tests for the agy (Antigravity CLI) launcher scaffold — Slice 1 of
sonichi#4272 (src/agent/agy/).

Covers what can be tested without a real agy session + real Google auth:
  - onboarding_seed.py: idempotent JSON merge/seed logic, in isolation.
  - start-cli.sh --check: argument parsing + agy-present/absent and
    auth-ok/auth-fail branches, via a stubbed `agy` on PATH.
  - start-cli.sh launch + idempotency guard: a stubbed `agy` + stubbed `tmux`
    on PATH prove the script starts exactly one session and a second
    invocation attaches/reports rather than double-launching, without a real
    tmux server or a real agy binary.

Does NOT attempt a real agy CLI + real Google auth — that can't run in CI;
see docs/... sonichi#4272 for how that was verified by hand instead.
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(os.environ.get(
    "SUTANDO_TEST_REPO", Path(__file__).resolve().parents[1]
)).resolve()

SEED_MODULE_PATH = REPO / "src/agent/agy/onboarding_seed.py"
LAUNCHER = REPO / "src/agent/agy/cli/start-cli.sh"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("agy_onboarding_seed", SEED_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OnboardingSeedTests(unittest.TestCase):
    """onboarding_seed.py in isolation — no agy binary involved."""

    def setUp(self):
        self.seed = _load_seed_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _path(self, *parts):
        return str(Path(self.tmp.name, *parts))

    def test_missing_file_is_created_with_all_three_fields_true(self):
        path = self._path("cache", "onboarding.json")
        changed = self.seed.seed(path)
        self.assertTrue(changed)
        data = json.loads(Path(path).read_text())
        for field in self.seed.FIELDS:
            self.assertIs(data[field], True, field)

    def test_idempotent_rerun_reports_no_change(self):
        path = self._path("onboarding.json")
        self.assertTrue(self.seed.seed(path))
        mtime_before = Path(path).stat().st_mtime_ns
        self.assertFalse(self.seed.seed(path))
        self.assertEqual(Path(path).stat().st_mtime_ns, mtime_before,
                          "a no-op seed must not rewrite the file")

    def test_partial_false_fields_are_flipped_true(self):
        # The real shape observed on disk before seeding (sonichi#4272).
        path = self._path("onboarding.json")
        Path(path).write_text(json.dumps({
            "consumerOnboardingComplete": False,
            "enterpriseOnboardingComplete": False,
            "onboardingComplete": False,
        }))
        self.assertTrue(self.seed.seed(path))
        data = json.loads(Path(path).read_text())
        for field in self.seed.FIELDS:
            self.assertIs(data[field], True, field)

    def test_corrupt_json_is_tolerated_and_overwritten(self):
        path = self._path("onboarding.json")
        Path(path).write_text("not json{{{")
        self.assertTrue(self.seed.seed(path))
        data = json.loads(Path(path).read_text())
        for field in self.seed.FIELDS:
            self.assertIs(data[field], True, field)

    def test_unrelated_keys_are_preserved_not_clobbered(self):
        # Merge, never replace wholesale — a future agy version may add keys
        # this script has no reason to know about.
        path = self._path("onboarding.json")
        Path(path).write_text(json.dumps({"someFutureField": "keep-me"}))
        self.assertTrue(self.seed.seed(path))
        data = json.loads(Path(path).read_text())
        self.assertEqual(data["someFutureField"], "keep-me")
        for field in self.seed.FIELDS:
            self.assertIs(data[field], True, field)

    def test_write_is_atomic_no_tmp_file_left_behind(self):
        path = self._path("onboarding.json")
        self.seed.seed(path)
        self.assertFalse(Path(path + ".tmp").exists())

    def test_default_path_matches_the_documented_agy_cache_location(self):
        # Field names + location verified by reading the real file on disk
        # (sonichi#4272) — pin them so a refactor can't silently drift.
        self.assertTrue(self.seed.default_path().endswith(
            ".gemini/antigravity-cli/cache/onboarding.json"))
        self.assertEqual(set(self.seed.FIELDS), {
            "consumerOnboardingComplete",
            "enterpriseOnboardingComplete",
            "onboardingComplete",
        })

    def test_seed_handles_a_bare_filename_with_no_directory_component(self):
        # dirname("") is falsy — os.makedirs must be skipped, not called
        # with an empty string (which would raise).
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, cwd)
        changed = self.seed.seed("onboarding.json")
        self.assertTrue(changed)
        data = json.loads(Path("onboarding.json").read_text())
        for field in self.seed.FIELDS:
            self.assertIs(data[field], True, field)

    def test_json_top_level_non_dict_is_treated_as_empty(self):
        # Valid JSON that parses to a list/scalar rather than an object —
        # distinct from the corrupt-JSON path, which raises instead.
        path = self._path("onboarding.json")
        Path(path).write_text(json.dumps([1, 2, 3]))
        changed = self.seed.seed(path)
        self.assertTrue(changed)
        data = json.loads(Path(path).read_text())
        self.assertIsInstance(data, dict)
        for field in self.seed.FIELDS:
            self.assertIs(data[field], True, field)

    def test_main_prints_seeded_then_already_seeded_and_returns_zero(self):
        path = self._path("onboarding.json")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.seed.main(["onboarding_seed.py", path])
        self.assertEqual(rc, 0)
        self.assertIn("seeded: " + path, out.getvalue())

        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2):
            rc2 = self.seed.main(["onboarding_seed.py", path])
        self.assertEqual(rc2, 0)
        self.assertIn("already seeded: " + path, out2.getvalue())


class StartCliHermeticTests(unittest.TestCase):
    """start-cli.sh against a stubbed PATH — no real agy, no real tmux server."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.onboarding_path = self.root / "onboarding.json"
        self.tmux_log = self.root / "tmux.log"
        self.tmux_state = self.root / "tmux-session.started"

    def _write_exe(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def _write_fake_agy(self, auth_ok=True, version="9.9.9-fake"):
        self._write_exe("agy", f'''#!/bin/bash
if [ "${{1:-}}" = --version ]; then echo "{version}"; exit 0; fi
if [ "${{1:-}}" = models ]; then
  if [ "{1 if auth_ok else 0}" = 1 ]; then echo "fake-model"; exit 0; fi
  echo "auth error" >&2; exit 1
fi
# persistent-session stand-in for the launch path: just idle.
exec sleep 300
''')

    def _write_fake_tmux(self):
        # Minimal stateful stub: has-session reflects whether new-session ran.
        self._write_exe("tmux", f'''#!/bin/bash
printf '%s\\n' "$*" >> "{self.tmux_log}"
[ "${{1:-}}" = -S ] && shift 2
case "${{1:-}}" in
  has-session)
    [ -f "{self.tmux_state}" ] && exit 0
    exit 1
    ;;
  new-session)
    touch "{self.tmux_state}"
    exit 0
    ;;
  attach)
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
''')

    def _env(self, extra=None):
        env = dict(os.environ)
        env.pop("SUTANDO_CORE_SESSION", None)
        env.update({
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "SUTANDO_AGY_TMUX_SOCKET": str(self.root / "fake.sock"),
            "SUTANDO_AGY_TMUX_SESSION": "sutando-agy-test",
            "SUTANDO_AGY_ONBOARDING_PATH": str(self.onboarding_path),
            "HOME": str(self.root),
        })
        if extra:
            env.update(extra)
        return env

    def run_launcher(self, *args, env_extra=None):
        return subprocess.run(
            ["/bin/bash", str(LAUNCHER), *args],
            env=self._env(env_extra),
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=15,
        )

    # ---- --check ----

    def test_check_reports_ok_when_agy_present_and_authenticated(self):
        self._write_fake_agy(auth_ok=True)
        result = self.run_launcher("--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("agy:", result.stdout)
        self.assertIn("auth: OK", result.stdout)

    def test_check_reports_failure_when_not_authenticated(self):
        self._write_fake_agy(auth_ok=False)
        result = self.run_launcher("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NOT authenticated", result.stdout)

    def test_check_reports_missing_binary(self):
        # No fake agy written — PATH has no `agy` at all.
        result = self.run_launcher("--check")
        self.assertEqual(result.returncode, 127)
        self.assertIn("not found on PATH", result.stdout)

    def test_check_makes_no_tmux_calls(self):
        self._write_fake_agy(auth_ok=True)
        self._write_fake_tmux()
        self.run_launcher("--check")
        self.assertFalse(self.tmux_log.exists(),
                          "--check must not touch tmux at all")

    def test_unknown_argument_is_rejected(self):
        self._write_fake_agy(auth_ok=True)
        result = self.run_launcher("--bogus")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown argument", result.stderr)

    # ---- launch + idempotency ----

    def test_missing_agy_on_launch_path_fails_loud(self):
        self._write_fake_tmux()
        result = self.run_launcher()
        self.assertEqual(result.returncode, 127)
        self.assertIn("agy CLI not found", result.stderr)

    def test_launch_seeds_onboarding_and_starts_one_session(self):
        self._write_fake_agy(auth_ok=True)
        self._write_fake_tmux()
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.tmux_state.exists(), "no tmux session was started")
        data = json.loads(self.onboarding_path.read_text())
        self.assertTrue(all(data[f] is True for f in (
            "consumerOnboardingComplete",
            "enterpriseOnboardingComplete",
            "onboardingComplete",
        )))
        log = self.tmux_log.read_text()
        self.assertIn("new-session", log)
        self.assertIn("agy --dangerously-skip-permissions", log)

    def test_second_invocation_attaches_instead_of_relaunching(self):
        self._write_fake_agy(auth_ok=True)
        self._write_fake_tmux()
        first = self.run_launcher()
        self.assertEqual(first.returncode, 0)
        new_session_calls_after_first = self.tmux_log.read_text().count("new-session")
        second = self.run_launcher()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("already running", second.stdout)
        new_session_calls_after_second = self.tmux_log.read_text().count("new-session")
        self.assertEqual(
            new_session_calls_after_first, new_session_calls_after_second,
            "a second invocation must not start a duplicate session",
        )


if __name__ == "__main__":
    unittest.main()
