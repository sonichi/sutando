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
  "PreCompact|sutando-conversations/|cp \\"\\$TRANSCRIPT_PATH\\" \\"\\$HOME/Desktop/sutando-conversations/\\$(date +%Y).jsonl\\""
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
            "PreCompact": [{"hooks": [{"command": 'cp "$TRANSCRIPT_PATH" "$HOME/Desktop/sutando-conversations/x.jsonl"'},
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
        self.assertIn("NOT running the installer's command", out["detail"])

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
            # production shape: the installer shell-quotes via $(shq ...)
            'HOOKS=(\n  "Stop|src/check-pending-tasks.sh|bash $(shq "$REPO_DIR/src/check-pending-tasks.sh")"\n)\n'
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
                self.assertIn("NOT running the installer's command", out["detail"])

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

    def test_a_command_that_only_MENTIONS_the_script_is_not_an_invocation(self):
        # The nastiest false-clean: the expected path is present, so both a substring
        # test AND a scan-every-token test say "registered" — while something else
        # entirely runs. A stale or replaced hook keeps the probe green just by
        # carrying its old target as inert data.
        decoys = {
            "echo": "echo {p}",
            "printf-with-format": 'printf "%s" {p}',
            "runs a DIFFERENT script, path as arg": "bash /tmp/other.sh {p}",
            "path in a comment-ish trailing arg": "bash /tmp/other.sh --note {p}",
        }
        for label, tmpl in decoys.items():
            with self.subTest(decoy=label):
                repo = self._one_hook_repo(
                    f"decoy-{abs(hash(label))}",
                    lambda r, _t=tmpl: _t.format(p=f"{r}/src/check-pending-tasks.sh"),
                )
                out = self.hc.check_claude_hook_registration(repo_dir=repo)
                self.assertEqual(out["status"], "warn", f"{label}: {out['detail']}")
                self.assertIn("NOT running the installer's command", out["detail"])

    def test_the_installers_OWN_command_shape_is_what_counts_as_registered(self):
        # Over-trigger control for the above: the command the installer actually
        # writes must still read as registered, or the probe just cries wolf.
        repo = self._one_hook_repo("genuine-cmd", lambda r: f"bash {r}/src/check-pending-tasks.sh")
        self.assertEqual(self.hc.check_claude_hook_registration(repo_dir=repo)["status"], "ok")

    def test_malformed_shapes_at_EVERY_nesting_level_warn_instead_of_raising(self):
        # Round one validated the top two containers only. These are the levels below
        # it, and each raised TypeError straight out of the probe — which, because it
        # runs inside run_all_checks(), aborted every later check. Cover the DEPTH
        # axis, not the two shapes a reviewer happened to name.
        shapes = {
            "event value is an int": {"Stop": 7},
            "event value is a string": {"Stop": "nope"},
            "group is not a dict": {"Stop": [5]},
            "group.hooks is an int": {"Stop": [{"hooks": 7}]},
            "group.hooks entry not a dict": {"Stop": [{"hooks": [5]}]},
            "command is an int": {"Stop": [{"hooks": [{"command": 7}]}]},
            "command is a list": {"Stop": [{"hooks": [{"command": ["bash"]}]}]},
        }
        for label, hooks in shapes.items():
            with self.subTest(shape=label):
                self._settings(hooks)
                try:
                    out = self.hc.check_claude_hook_registration(repo_dir=self.repo)
                except Exception as e:
                    self.fail(f"{label} must warn, not propagate {e!r}")
                self.assertEqual(out["status"], "warn", label)

    def test_not_a_sutando_checkout_is_ok_not_a_warning(self):
        (self.repo / "src" / "install-claude-hooks.sh").unlink()
        out = self.hc.check_claude_hook_registration(repo_dir=self.repo)
        self.assertEqual(out["status"], "ok")

    def test_probe_is_wired_into_run_all_checks(self):
        # Reachability: a probe defined but never called is invisible.
        names = [c.get("name") for c in self.hc.run_all_checks() if isinstance(c, dict)]
        self.assertIn("claude-hooks", names)



class TestAgainstTheRealInstaller(unittest.TestCase):
    """Everything above uses a SIMPLIFIED fixture, and that is how three rounds of
    review kept finding false-cleans this suite was green through.

    The real `src/install-claude-hooks.sh` writes its command as
    `bash $(shq "$REPO_DIR/src/check-pending-tasks.sh")`. The probe reads HOOKS as
    literal source, so before the unwrap the path was welded into a `$(shq` token and
    NO production hook resolved positionally — every one took the permissive fallback,
    which meant `echo <path>` counted as registered on the only path that ships.

    So this class builds its fixture from the repository's OWN installer. If the
    installer's command shape changes into something the parser can't reduce, these
    fail — which a hand-written approximation of it could never do.
    """

    def setUp(self):
        self.hc = _load()
        self.installer_src = (REPO / "src" / "install-claude-hooks.sh").read_text()
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _repo(self, stop_command, archive_command=None):
        r = Path(self._tmp.name) / f"repo{abs(hash((stop_command, archive_command))) % 99999}"
        (r / "src").mkdir(parents=True)
        (r / ".claude").mkdir(parents=True)
        (r / "src" / "install-claude-hooks.sh").write_text(self.installer_src)
        handoff = f'bash {r}/src/session-handoff.sh "$TRANSCRIPT_PATH"'
        (r / ".claude" / "settings.json").write_text(json.dumps({"hooks": {
            "PreCompact": [{"hooks": [
                {"command": archive_command or
                 'cp "$TRANSCRIPT_PATH" "$HOME/Desktop/sutando-conversations/x.jsonl"'},
                {"command": handoff}]}],
            "SessionEnd": [{"hooks": [{"command": handoff}]}],
            "Stop": [{"hooks": [{"command": stop_command.format(
                p=f"{r}/src/check-pending-tasks.sh")}]}],
        }}))
        return r

    def test_the_installers_real_shq_command_reads_as_registered(self):
        # Over-trigger control, and the one that matters most: failing closed is only
        # correct if the genuine production shape still passes. If this breaks, the
        # probe warns on every healthy host.
        out = self.hc.check_claude_hook_registration(repo_dir=self._repo("bash {p}"))
        self.assertEqual(out["status"], "ok", out["detail"])
        self.assertIn("4", out["detail"])

    def test_decoys_are_rejected_on_the_REAL_installer_shape(self):
        for label, cmd in {
            "echo": "echo {p}",
            "printf": 'printf "%s" {p}',
            "different script, path as arg": "bash /tmp/other.sh {p}",
        }.items():
            with self.subTest(decoy=label):
                out = self.hc.check_claude_hook_registration(repo_dir=self._repo(cmd))
                self.assertEqual(out["status"], "warn", f"{label}: {out['detail']}")
                self.assertIn("NOT running the installer's command", out["detail"])

    def test_the_ARCHIVE_hook_is_validated_too_not_just_src_paths(self):
        # I exempted every marker that is not a `src/` path from command-shape
        # validation, reasoning that a non-repo path has no identity to compare.
        # That confused path identity with command shape: the archive hook still
        # has an installer-owned command, and `echo` is not `cp`. Under the old
        # substring-only test BOTH of these read as a healthy archiver — and the
        # second is the one that settles it, because a destructive command
        # certified as a working archive hook inverts the probe's whole purpose.
        for label, cmd in {
            "echo": "echo sutando-conversations/",
            "rm -rf": "rm -rf sutando-conversations/",
            "a different copier": "rsync x sutando-conversations/",
        }.items():
            with self.subTest(decoy=label):
                out = self.hc.check_claude_hook_registration(
                    repo_dir=self._repo("bash {p}", archive_command=cmd))
                self.assertEqual(out["status"], "warn", f"{label}: {out['detail']}")
                self.assertIn("sutando-conversations/", out["detail"])

    def test_a_cp_that_carries_the_marker_but_archives_the_WRONG_THING(self):
        # Program-only validation left these two: both are `cp`, both carry the
        # marker, neither archives the session transcript to the owned destination.
        #
        # I had justified stopping at the program with a "compatibility boundary"
        # argument — the installer preserves operator-customized archive hooks, so
        # any cp must be acceptable. That was WRONG, and checkable: Phase 0 does
        # skip sweeping a custom archiver, but Phase 1 detects presence by EXACT
        # command string (`index($cmd)`), so a custom cp never satisfies it and the
        # installer ADDS its own alongside. They coexist — meaning the installer's
        # own command IS present on any host where it ran, and the probe can say so.
        cases = {
            "wrong source": 'cp /tmp/not-the-transcript "$HOME/Desktop/sutando-conversations/x.jsonl"',
            "wrong destination": 'cp "$TRANSCRIPT_PATH" /tmp/sutando-conversations/not-desktop.jsonl',
            # Three-operand cp: the owned prefix IS present, just not in the
            # destination position. cp reads this as two sources plus a target and
            # fails at runtime unless the last path is a directory — so nothing is
            # archived. Accepting the prefix in "any token" certified it.
            "extra operand, prefix not in dest position":
                'cp "$TRANSCRIPT_PATH" /tmp/not-the-archive "$HOME/Desktop/sutando-conversations/x.jsonl"',
            # Same shape, fewer operands than the installer writes.
            "too few operands": 'cp "$HOME/Desktop/sutando-conversations/x.jsonl"',
        }
        for label, cmd in cases.items():
            with self.subTest(case=label):
                out = self.hc.check_claude_hook_registration(
                    repo_dir=self._repo("bash {p}", archive_command=cmd))
                self.assertEqual(out["status"], "warn", f"{label}: {out['detail']}")
                self.assertIn("sutando-conversations/", out["detail"])

    def test_the_installer_template_parses_into_CLEAN_tokens(self):
        # The genuine case regressed to `warn` twice while I was fixing the above,
        # both times because the template failed to tokenize and fell back to a
        # whitespace split that keeps stray quotes. Pin the parse itself so the
        # next person sees the cause, not just a mysterious false warning.
        import re
        src = (REPO / "src" / "install-claude-hooks.sh").read_text()
        body = re.search(r"^HOOKS=\((.*?)^\)", src, re.M | re.S).group(1)
        line = [l.strip() for l in body.split("\n")
                if l.strip().startswith('"') and "sutando-conversations" in l][0]
        _ev, _marker, cmd = line.strip('"').split("|", 2)
        toks = self.hc._shell_tokens(self.hc._unwrap_installer_command(cmd))
        self.assertEqual(len(toks), 3, f"archive template did not tokenize cleanly: {toks}")
        self.assertEqual(toks[0], "cp")
        for t in toks:
            self.assertNotIn('"', t, f"stray quote survived tokenization: {t!r}")

    def test_the_genuine_archive_cp_still_registers(self):
        # Over-trigger control. The real command interpolates $HOME and $(date …),
        # so this must not become a shape-pinning test that warns on healthy hosts.
        out = self.hc.check_claude_hook_registration(repo_dir=self._repo("bash {p}"))
        self.assertEqual(out["status"], "ok", out["detail"])

    def test_an_unreducible_template_fails_CLOSED(self):
        # The fallback used to accept the path anywhere in the first two tokens. A
        # fallback the real data always took was not a fallback — it was the behaviour.
        # If a future wrapper defeats the unwrap, warn; never silently accept.
        self.assertFalse(self.hc._hook_command_targets(
            "echo /repo/src/x.sh", Path("/repo/src/x.sh"), "somecmd $(unknown_wrapper x)"))

if __name__ == "__main__":
    unittest.main(verbosity=2)
