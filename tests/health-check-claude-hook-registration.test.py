#!/usr/bin/env python3
"""Regression: nothing verified that the Claude Code hooks we install stay installed.

2026-08-03, measured on a live host: ZERO of the four hooks `install-claude-hooks.sh`
owns were present in its own target file, including BOTH PreCompact entries. So
session-state.md was never regenerated on compaction and the transcript archiver had
never run — for days, silently, because no probe looks at hook registration. A peer
host showed the same shape.

Run: python3 tests/health-check-claude-hook-registration.test.py
"""
from __future__ import annotations
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("hc_hooks_test", REPO / "src" / "health-check.py")
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    return hc


INSTALLER = '''#!/usr/bin/env bash
SETTINGS="$REPO_DIR/.claude/settings.json"
HOOKS=(
  "PreCompact|sutando-conversations/|cp x y"
  "PreCompact|src/session-handoff.sh|bash $REPO_DIR/src/session-handoff.sh"
  "SessionEnd|src/session-handoff.sh|bash $REPO_DIR/src/session-handoff.sh"
  "Stop|src/check-pending-tasks.sh|bash $REPO_DIR/src/check-pending-tasks.sh"
)
'''


class TestHookRegistration(unittest.TestCase):
    def setUp(self):
        self.hc = _load()
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "src").mkdir(parents=True)
        (self.repo / ".claude").mkdir(parents=True)
        (self.repo / "src" / "install-claude-hooks.sh").write_text(INSTALLER)

    def tearDown(self):
        self._tmp.cleanup()

    def _settings(self, hooks: dict):
        (self.repo / ".claude" / "settings.json").write_text(json.dumps({"hooks": hooks}))

    def _entry(self, cmd):
        return [{"hooks": [{"type": "command", "command": cmd}]}]

    def _all_registered(self, repo_path=None):
        r = str(repo_path or self.repo)
        return {
            "PreCompact": [{"hooks": [{"command": "cp $TRANSCRIPT_PATH ~/Desktop/sutando-conversations/x.jsonl"},
                                      {"command": f"bash {r}/src/session-handoff.sh"}]}],
            "SessionEnd": self._entry(f"bash {r}/src/session-handoff.sh"),
            "Stop": self._entry(f"bash {r}/src/check-pending-tasks.sh"),
        }

    def test_all_registered_is_ok(self):
        self._settings(self._all_registered())
        out = self.hc.check_claude_hook_registration(repo_dir=self.repo)
        self.assertEqual(out["status"], "ok", out["detail"])
        self.assertIn("4", out["detail"])

    def test_the_REAL_case_no_hooks_at_all_warns_and_names_them(self):
        # The live finding: the installer's own target had none of the four.
        self._settings({})
        out = self.hc.check_claude_hook_registration(repo_dir=self.repo)
        self.assertEqual(out["status"], "warn")
        self.assertIn("4 NOT registered", out["detail"])
        self.assertIn("PreCompact", out["detail"], "name what is missing, not just a count")

    def test_partial_registration_names_only_the_missing(self):
        h = self._all_registered()
        del h["PreCompact"]
        self._settings(h)
        out = self.hc.check_claude_hook_registration(repo_dir=self.repo)
        self.assertEqual(out["status"], "warn")
        self.assertIn("2 NOT registered", out["detail"])
        self.assertNotIn("Stop:", out["detail"], "a registered hook must not be reported missing")

    def test_registered_but_pointing_at_ANOTHER_checkout_warns(self):
        # The failure that looks healthiest: present, so an existence check passes,
        # but aimed at a stale copy — this host ran a 5-day-old script for days.
        self._settings(self._all_registered(repo_path="/somewhere/else/sutando"))
        out = self.hc.check_claude_hook_registration(repo_dir=self.repo)
        self.assertEqual(out["status"], "warn")
        self.assertIn("another checkout", out["detail"])

    def test_unparseable_HOOKS_array_warns_rather_than_reporting_clean(self):
        # An empty owned-list would otherwise mean "0 missing of 0" -> "ok", i.e. a
        # probe that cannot fail. Fail toward noise instead.
        (self.repo / "src" / "install-claude-hooks.sh").write_text("#!/usr/bin/env bash\necho hi\n")
        self._settings({})
        out = self.hc.check_claude_hook_registration(repo_dir=self.repo)
        self.assertEqual(out["status"], "warn")
        self.assertIn("could not parse", out["detail"])

    def test_missing_settings_file_warns(self):
        out = self.hc.check_claude_hook_registration(repo_dir=self.repo)
        self.assertEqual(out["status"], "warn")
        self.assertIn("never run", out["detail"])

    def test_malformed_settings_warns_never_raises(self):
        (self.repo / ".claude" / "settings.json").write_text("{not json")
        try:
            out = self.hc.check_claude_hook_registration(repo_dir=self.repo)
        except Exception as e:
            self.fail(f"must not propagate: {e!r}")
        self.assertEqual(out["status"], "warn")

    def test_VALID_json_of_the_wrong_shape_warns_and_never_aborts_the_run(self):
        # Unparseable JSON was covered; PARSEABLE-but-wrong-shape was not, and that is a
        # different axis. `[]` parses fine and then .get() raises AttributeError, which
        # takes down every probe after this one in run_all_checks(). Cover the shape axis,
        # not just the two spellings a reviewer happened to name.
        for payload in ("[]", '"a string"', "3", "null",
                        '{"hooks": []}', '{"hooks": "nope"}', '{"hooks": 7}'):
            with self.subTest(payload=payload):
                (self.repo / ".claude" / "settings.json").write_text(payload)
                try:
                    out = self.hc.check_claude_hook_registration(repo_dir=self.repo)
                except Exception as e:
                    self.fail(f"{payload!r} must warn, not propagate {e!r}")
                self.assertEqual(out["status"], "warn", payload)

    def _one_hook_repo(self, root_name, command):
        """A repo with a single owned Stop hook registered to `command`."""
        repo = Path(self._tmp.name) / root_name
        (repo / "src").mkdir(parents=True)
        (repo / ".claude").mkdir(parents=True)
        (repo / "src" / "install-claude-hooks.sh").write_text(
            '#!/usr/bin/env bash\nSETTINGS="$REPO_DIR/.claude/settings.json"\n'
            'HOOKS=(\n  "Stop|src/check-pending-tasks.sh|bash $REPO_DIR/src/check-pending-tasks.sh"\n)\n'
        )
        (repo / ".claude" / "settings.json").write_text(json.dumps({"hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": command(repo)}]}]}}))
        return repo

    def test_a_path_that_merely_SHARES_A_PREFIX_is_a_different_checkout(self):
        # `str(repo) in command` says these are the same checkout. They are not — and this
        # is precisely the present-but-pointing-elsewhere case the probe exists to catch,
        # so a substring test certifying it clean defeats the probe's whole purpose.
        # Both directions: the stale copy longer than the repo, and shorter.
        cases = {
            "sibling-suffix": ("a/sutando", lambda r: f"bash {r}-old/src/check-pending-tasks.sh"),
            "sibling-prefix": ("b/sutando-new", lambda r: f"bash {str(r)[:-4]}/src/check-pending-tasks.sh"),
            "same-basename-elsewhere": ("c/sutando", lambda r: "bash /opt/sutando/src/check-pending-tasks.sh"),
        }
        for label, (root, cmd) in cases.items():
            with self.subTest(case=label):
                repo = self._one_hook_repo(root, cmd)
                out = self.hc.check_claude_hook_registration(repo_dir=repo)
                self.assertEqual(out["status"], "warn", f"{label}: {out['detail']}")
                self.assertIn("another checkout", out["detail"])

    def test_a_GENUINE_checkout_is_not_reported_foreign(self):
        # Over-trigger control for the fix above: a warning that fires on healthy hosts is
        # its own defect. Exact path, and the same path reached through a symlink (macOS
        # /var -> /private/var makes this the common case, not an exotic one).
        repo = self._one_hook_repo("real", lambda r: f"bash {r}/src/check-pending-tasks.sh")
        self.assertEqual(self.hc.check_claude_hook_registration(repo_dir=repo)["status"], "ok")

        link = Path(self._tmp.name) / "linked"
        link.symlink_to(repo)
        out = self.hc.check_claude_hook_registration(repo_dir=link)
        self.assertEqual(out["status"], "ok", f"symlinked checkout must not read as foreign: {out['detail']}")

    def test_a_quoted_path_with_spaces_still_resolves(self):
        repo = self._one_hook_repo("has space", lambda r: f'bash "{r}/src/check-pending-tasks.sh"')
        self.assertEqual(self.hc.check_claude_hook_registration(repo_dir=repo)["status"], "ok")

    # --- the fail-soft branches themselves. Arguing a probe "fails toward noise"
    # and then leaving its error paths unexercised is how the argument stops being
    # true; each of these was uncovered until CI said so.

    def test_unbalanced_quoting_in_a_hook_command_does_not_raise(self):
        # shlex.split raises ValueError on an unterminated quote. A settings file is
        # hand-editable, so this is reachable, and it must degrade rather than take
        # out the run like the wrong-shape JSON did.
        cmd = 'bash "{r}/src/check-pending-tasks.sh'
        # Assert the fixture actually enters the branch. Without this the test could
        # pass while shlex parsed the string fine, i.e. never exercising the handler
        # it claims to cover.
        with self.assertRaises(ValueError):
            __import__("shlex").split(cmd.format(r="/x"))
        repo = self._one_hook_repo("unbalanced", lambda r: cmd.format(r=r))
        try:
            out = self.hc.check_claude_hook_registration(repo_dir=repo)
        except Exception as e:
            self.fail(f"must degrade, not propagate: {e!r}")
        self.assertEqual(out["status"], "warn")

    def test_an_unreadable_installer_warns_rather_than_raising(self):
        # Patched rather than chmod 000: CI runs as root, where 000 is still readable,
        # so a permissions fixture would pass locally and silently never exercise this.
        import unittest.mock as mock
        self._settings(self._all_registered())
        with mock.patch.object(Path, "read_text", side_effect=OSError("boom")):
            out = self.hc.check_claude_hook_registration(repo_dir=self.repo)
        self.assertEqual(out["status"], "warn")
        self.assertIn("cannot read installer", out["detail"])

    def test_settings_with_no_hooks_key_at_all_is_treated_as_none_registered(self):
        # Distinct from {"hooks": {}}: the key is ABSENT, so conf.get returns None.
        # A file that has never had hooks written to it takes this path, which makes
        # it the likely shape on a fresh host — the one this probe exists to catch.
        (self.repo / ".claude" / "settings.json").write_text(json.dumps({"model": "opus"}))
        out = self.hc.check_claude_hook_registration(repo_dir=self.repo)
        self.assertEqual(out["status"], "warn")
        self.assertIn("4 NOT registered", out["detail"])

    def test_not_a_sutando_checkout_is_ok_not_a_warning(self):
        (self.repo / "src" / "install-claude-hooks.sh").unlink()
        out = self.hc.check_claude_hook_registration(repo_dir=self.repo)
        self.assertEqual(out["status"], "ok")

    def test_probe_is_wired_into_run_all_checks(self):
        # Reachability: a probe defined but never called is invisible.
        names = [c.get("name") for c in self.hc.run_all_checks() if isinstance(c, dict)]
        self.assertIn("claude-hooks", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
