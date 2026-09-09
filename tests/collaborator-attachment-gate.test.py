#!/usr/bin/env python3
"""Collaborator attachments: per-channel owner opt-in, verdict-only exemption.

The guard exempts ONLY the attach marker, ONLY when the adapter passes
allow_attach=True; redirects and the default-deny stay untouched, and a
withheld attach-only result is quarantined so an owner word can release it.
Path authorization is deliberately absent here — it stays with the transport
allowlist (one owner: src/policy/egress/attachment.py).
"""
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest

# Isolate the channel-config root BEFORE anything bridge-adjacent resolves,
# so no test can read the developer's real per-user allowlist.
_ISOLATED_CONFIG = tempfile.mkdtemp(prefix="cfg-iso-")
os.environ["CLAUDE_CONFIG_DIR"] = _ISOLATED_CONFIG
pathlib.Path(_ISOLATED_CONFIG, "channels", "discord").mkdir(parents=True, exist_ok=True)
pathlib.Path(_ISOLATED_CONFIG, "channels", "discord", "access.json").write_text("{}")

sys.path.insert(0, "src")
spec = importlib.util.spec_from_file_location("guard", "src/policy/egress/result.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
REPO = pathlib.Path(".")


class PolicyBehavior(unittest.TestCase):
    def setUp(self):
        self.sd = pathlib.Path(tempfile.mkdtemp(prefix="ca-"))

    def _guard(self, body, task_id, **kw):
        return guard.guard_result_for_tier(
            body, "team", REPO, suppress_journal=(self.sd, task_id), **kw)

    def test_default_deny_unchanged(self):
        body = "[file: /tmp/x.png]\nmedia"
        out, why = self._guard(body, "t1")
        self.assertNotEqual(out, body)
        self.assertIsNotNone(why)

    def test_allow_attach_passes_byte_identical(self):
        body = "[file: /tmp/x.png]\nmedia"
        out, why = self._guard(body, "t2", allow_attach=True)
        self.assertEqual(out, body)
        self.assertIsNone(why)

    def test_redirect_still_withheld_under_allow_attach(self):
        body = "[channel: 123]\nleak"
        out, _ = self._guard(body, "t3", allow_attach=True)
        self.assertNotEqual(out, body)

    def test_attach_only_withhold_is_quarantined_for_release(self):
        body = "[file: /tmp/x.png]\nmedia"
        self._guard(body, "t4")
        rec_path = guard.quarantined_attachment_path(self.sd, "t4")
        self.assertTrue(rec_path.is_file())
        rec = json.loads(rec_path.read_text())
        self.assertEqual(rec["withheld_body"], body)
        self.assertEqual(rec["status"], "withheld_attachment_pending")

    def test_redirect_bearing_body_is_never_quarantined(self):
        # a redirect is not releasable, so no record may invite releasing it
        self._guard("[channel: 99]\n[file: /tmp/x.png]\nboth", "t5")
        self.assertFalse(guard.quarantined_attachment_path(self.sd, "t5").is_file())

    def test_allowed_delivery_leaves_no_quarantine(self):
        self._guard("[file: /tmp/x.png]\nmedia", "t6", allow_attach=True)
        self.assertFalse(guard.quarantined_attachment_path(self.sd, "t6").is_file())


class RestartRecoveryForgery(unittest.TestCase):
    """A Team sender's BODY text may not forge collaborator status across a
    bridge restart (qingyun's #3500 P1). The re-read parses pre-task headers
    only — the same boundary resolve_access_tier holds for tiers."""

    def _recover(self, content):
        head = content.split("\ntask:", 1)[0]
        vals = [line.partition(":")[2].strip()
                for line in head.split("\n") if line.startswith("collaborator:")]
        return vals == ["true"]

    def test_header_collaborator_recovers_true(self):
        self.assertTrue(self._recover("id: t1\ncollaborator: true\ntask: [D @s] hi\n"))

    def test_body_borne_collaborator_line_does_not_escalate(self):
        self.assertFalse(self._recover(
            "id: t1\naccess_tier: team\ntask: [D @evil] note\ncollaborator: true\n"))

    def test_conflicting_headers_fail_closed(self):
        self.assertFalse(self._recover(
            "id: t1\ncollaborator: true\ncollaborator: false\ntask: [D @s] hi\n"))

    def test_absent_header_is_false(self):
        self.assertFalse(self._recover("id: t1\naccess_tier: team\ntask: [D @s] hi\n"))


class WriterFailurePreservesWithhold(unittest.TestCase):
    """qingyun's second #3500 P1: a raising artifact writer must not turn an
    already-decided withhold into an exception in the delivery loop."""

    def test_raising_writer_still_withholds(self):
        import pathlib
        import tempfile
        sd = pathlib.Path(tempfile.mkdtemp(prefix="wf-"))
        orig = guard._write_artifact
        guard._write_artifact = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        try:
            body = "[file: /tmp/x.png]\nmedia"
            out, why = guard.guard_result_for_tier(
                body, "team", REPO, suppress_journal=(sd, "t-df"))
            self.assertNotEqual(out, body)
            self.assertIsNotNone(why)
        finally:
            guard._write_artifact = orig


class BridgeWiring(unittest.TestCase):
    def test_channel_flag_resolver_default_deny(self):
        import importlib.machinery
        # source-level pin: the bridge computes allow_attach from collaborator
        # status AND the channel flag, and passes it to the guard
        src = open("src/discord-bridge.py").read()
        self.assertIn("channel_allows_collaborator_attachments(", src)
        self.assertIn("allow_attach=_allow_attach", src)
        # Compiled under the real filename at the real line offset, so coverage
        # attributes these lines to the file; importing runs mkdir + subprocesses.
        import re
        _m = re.search(r"def channel_allows_collaborator_attachments.*?(?=\ndef )", src, re.S)
        _pad = "\n" * src[:_m.start()].count("\n")
        ns = {}
        exec(compile(_pad + _m.group(0), "src/discord-bridge.py", "exec"), ns)
        fn = ns["channel_allows_collaborator_attachments"]
        self.assertFalse(fn({}, "123"))
        self.assertFalse(fn({"groups": {"123": {"collaboratorAttachments": False}}}, "123"))
        self.assertFalse(fn({"groups": {"123": True}}, "123"))          # bool cfg, no dict
        self.assertTrue(fn({"groups": {"123": {"collaboratorAttachments": True}}}, "123"))
        self.assertFalse(fn({"groups": {"123": {"collaboratorAttachments": "yes"}}}, "123"))  # is True only


class QuarantineRecord(unittest.TestCase):
    def test_the_withheld_body_is_recorded_for_later_release(self):
        d = tempfile.mkdtemp(prefix="qrec-")
        ok = guard.journal_quarantined_attachment("body text", d, "task-1", now=1700000000)
        self.assertTrue(ok)
        files = list(pathlib.Path(d).rglob("*.json"))
        self.assertEqual(len(files), 1)
        rec = json.loads(files[0].read_text())
        self.assertEqual(rec["status"], "withheld_attachment_pending")
        self.assertEqual(rec["withheld_body"], "body text")
        self.assertEqual(rec["task_id"], "task-1")

    def test_an_unwritable_state_dir_costs_the_record_not_the_withhold(self):
        # Best-effort by contract: a failed record must return False, never raise
        # into the delivery loop that already withheld the attachment.
        self.assertFalse(
            guard.journal_quarantined_attachment("b", "/dev/null/nope", "task-2", now=1700000000))



class TaskScopedAttachRoots(unittest.TestCase):
    """Signal Room 5G ⑤a-cap: a Team result may attach ONLY files under the
    task's own output root. Narrower than allow_attach, which trusts the
    channel; this trusts a directory, realpath'd on both sides."""

    def setUp(self):
        self.sd = pathlib.Path(tempfile.mkdtemp(prefix="ca-"))
        self.results = pathlib.Path(tempfile.mkdtemp(prefix="results-"))
        self.root = self.results / "task-signal-1"
        self.root.mkdir()
        self.inside = self.root / "chart.png"
        self.inside.write_bytes(b"png")
        self.outside = self.results / "task-signal-2"
        self.outside.mkdir()
        (self.outside / "other.png").write_bytes(b"png")

    def _guard(self, body, task_id="ts1", **kw):
        return guard.guard_result_for_tier(
            body, "team", REPO, suppress_journal=(self.sd, task_id),
            attach_roots=(str(self.root),), **kw)

    def test_in_root_absolute_marker_passes_byte_identical(self):
        body = f"here is the chart\n[file: {self.inside}]"
        out, why = self._guard(body)
        self.assertEqual(out, body)
        self.assertIsNone(why)
        self.assertFalse((self.sd / guard.SUPPRESSED_RESULT_DIR).exists(),
                         "a delivered in-root attachment leaves no quarantine")

    def test_sibling_task_dir_is_out_of_root(self):
        out, why = self._guard(f"x [file: {self.outside / 'other.png'}]", "ts2")
        self.assertEqual(out, guard.TEAM_LEAK_RESULT_MARKER)
        self.assertTrue(guard.quarantined_attachment_path(self.sd, "ts2").is_file(),
                        "an out-of-root attach-only withhold is still quarantined for owner release")

    def test_prefix_sharing_dir_is_out_of_root(self):
        # /results/task-signal-1 must not admit /results/task-signal-10/…
        sneaky = self.results / "task-signal-10"
        sneaky.mkdir()
        (sneaky / "a.png").write_bytes(b"png")
        out, _ = self._guard(f"x [file: {sneaky / 'a.png'}]", "ts3")
        self.assertEqual(out, guard.TEAM_LEAK_RESULT_MARKER)

    def test_symlink_escaping_the_root_is_out_of_root(self):
        link = self.root / "escape.png"
        os.symlink(self.outside / "other.png", link)
        out, _ = self._guard(f"x [file: {link}]", "ts4")
        self.assertEqual(out, guard.TEAM_LEAK_RESULT_MARKER)

    def test_relative_marker_is_not_confined(self):
        out, _ = self._guard("x [file: task-signal-1/chart.png]", "ts5")
        self.assertEqual(out, guard.TEAM_LEAK_RESULT_MARKER)

    def test_tilde_marker_is_not_confined_even_when_home_is_the_root(self):
        home = pathlib.Path(os.path.expanduser("~")).resolve()
        out, _ = guard.guard_result_for_tier(
            "x [file: ~/inside.png]", "team", REPO, suppress_journal=(self.sd, "ts5b"),
            attach_roots=(str(home),))
        self.assertEqual(out, guard.TEAM_LEAK_RESULT_MARKER)

    def test_relative_or_tilde_marker_is_refused_even_when_cwd_is_the_root(self):
        """Pins isabs. The pair above withhold for the REALPATH reason, not this one:
        `os.path.realpath` does not expand `~`, so both resolve under CWD, and with cwd
        outside the root they land outside it whether or not isabs runs. Moving cwd AND
        HOME inside the root makes realpath land INSIDE, so only isabs still withholds.
        Credit: john-the-dev, who found it by mutation on #3911 — deleting isabs left
        all 57 tests green."""
        cwd, home = os.getcwd(), os.environ.get("HOME")
        os.chdir(self.root)
        os.environ["HOME"] = str(self.root)
        try:
            self.assertFalse(guard.attach_markers_confined(
                "x\n[file: chart.png]", (str(self.root),)))
            self.assertFalse(guard.attach_markers_confined(
                "x\n[file: ~/chart.png]", (str(self.root),)))
        finally:
            os.chdir(cwd)
            if home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = home

    def test_marker_naming_the_root_itself_is_confined_but_not_sendable(self):
        # The guard confines by LOCATION; the upload path independently requires a
        # regular file, so a marker naming the directory is refused there.
        out, why = self._guard(f"x [file: {self.root}]", "ts5c")
        self.assertEqual((out, why), (f"x [file: {self.root}]", None))
        sys.path.insert(0, "packages/ag2-sparrow")
        from ag2_sparrow.send_allowlist import is_path_sendable
        self.assertFalse(is_path_sendable(str(self.root)))

    def test_one_stray_marker_withholds_the_whole_result(self):
        body = f"x [file: {self.inside}]\n[file: {self.outside / 'other.png'}]"
        out, _ = self._guard(body, "ts6")
        self.assertEqual(out, guard.TEAM_LEAK_RESULT_MARKER)

    def test_redirect_is_never_admitted_by_the_allowance(self):
        out, _ = self._guard(f"[channel: #elsewhere]\n[file: {self.inside}]", "ts7")
        self.assertEqual(out, guard.TEAM_LEAK_RESULT_MARKER)

    def test_empty_roots_change_nothing(self):
        out, _ = guard.guard_result_for_tier(
            f"x [file: {self.inside}]", "team", REPO, suppress_journal=(self.sd, "ts8"),
            attach_roots=())
        self.assertEqual(out, guard.TEAM_LEAK_RESULT_MARKER)

    def test_allow_attach_still_admits_any_path(self):
        body = f"x [file: {self.outside / 'other.png'}]"
        out, _ = self._guard(body, "ts9", allow_attach=True)
        self.assertEqual(out, body)

    def test_owner_tier_is_untouched(self):
        body = "x [file: /anywhere/at/all.png]"
        out, why = guard.guard_result_for_tier(body, "owner", REPO, attach_roots=(str(self.root),))
        self.assertEqual((out, why), (body, None))

    def test_confinement_predicate_directly(self):
        conf = guard.attach_markers_confined
        self.assertTrue(conf(f"[file: {self.inside}]", (str(self.root),)))
        self.assertFalse(conf("no markers here", (str(self.root),)))
        self.assertFalse(conf(f"[file: {self.inside}]", ()))
        self.assertFalse(conf(f"[file: {self.inside}]", ("",)))
        self.assertTrue(conf(f"[file: {self.inside}]", (str(self.root) + "/",)),
                        "a trailing slash on the root is tolerated")


if __name__ == "__main__":
    unittest.main(verbosity=1)
