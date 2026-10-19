#!/usr/bin/env python3
"""A review ask must include a non-author CODEOWNER — only where CODEOWNERS exists.

The main ruleset requires a code-owner review, so an ask that names only
non-owners leaves the PR unmergeable however many reviewers approve. Every gh
call is stubbed here: nothing reaches GitHub or a room.

Run: python3 tests/notify-reviewers-codeowners.test.py   (stdlib only)
"""
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = (Path(__file__).resolve().parent.parent / "skills"
          / "collaboration-intelligence" / "scripts" / "notify_reviewers.py")
PR = "https://github.com/o/r/pull/7"


def _load():
    spec = importlib.util.spec_from_file_location("_nr_codeowners", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class FakeGitHub:
    """Answers the gh calls the gate makes; anything else is a room_ops send."""

    def __init__(self, files, author="dave", codeowners=None, fail=None, members=None):
        self.files, self.author = files, author
        self.codeowners = codeowners or {}
        self.fail = fail or {}
        self.members = members or {}
        self.calls, self.mentions = [], []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[0] != "gh":
            if "members" in argv and argv[-1] in self.members:
                return _R(out=json.dumps({"ok": True, "members": [
                    {"user_id": u} for u in self.members[argv[-1]]]}))
            if "mention" in argv:
                self.mentions.append(argv)
            return _R(out=json.dumps({"ok": True, "event_id": "$e"}))
        path = next(a for a in argv[1:] if a.startswith("repos/"))
        for key, res in self.fail.items():
            if key in path:
                return res
        if path == "repos/o/r/pulls/7":
            return _R(out=f"main\t{self.author}\t{len(self.files)}\n")
        if path == "repos/o/r/pulls/7/files":
            return _R(out="".join(f + "\n" for f in self.files))
        if "/contents/" in path:
            name = path.split("/contents/", 1)[1].split("?", 1)[0]
            if name in self.codeowners:
                return _R(out=self.codeowners[name])
            return _R(rc=1, err="gh: Not Found (HTTP 404)")
        if path.endswith("/permission"):
            return _R(out="write\n")
        return _R(rc=1, err="unexpected call")


class Gate(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.ledger = d / "review-asks.jsonl"
        roster = {k: {"stand": f"@{k}-stand:x", "room": "!r:x", "gh": f"{k}-gh"}
                  for k in ("alice", "bob", "erin")}
        self.roster_file = d / "roster.json"
        self.roster_file.write_text(json.dumps(roster))

    def _run(self, gh, extra=(), reviewers="alice,bob"):
        err = io.StringIO()
        argv = ["notify_reviewers.py", "--reviewers", reviewers,
                "--message", f"please review {PR}", "--send", *extra]
        with patch.object(self.mod, "ledger_path", lambda: self.ledger), \
             patch.object(self.mod, "roster_path", lambda: self.roster_file), \
             patch.dict("os.environ", {"SUTANDO_SCI_ROSTER": str(self.roster_file)}), \
             patch.object(self.mod.subprocess, "run", gh), \
             patch.object(self.mod.sys, "argv", argv), \
             contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = self.mod.main()
        return rc, err.getvalue()

    def test_no_CODEOWNERS_behaves_as_before(self):
        gh = FakeGitHub(["src/a.py"])
        rc, err = self._run(gh)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(gh.mentions), 2)
        looked = [a[2].split("/contents/")[1].split("?")[0] for a in gh.calls
                  if a[0] == "gh" and "/contents/" in a[2]]
        self.assertEqual(looked, [".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"])

    def test_no_owner_among_the_asked_is_refused(self):
        gh = FakeGitHub(["src/a.py"], codeowners={".github/CODEOWNERS": "* @carol\n"})
        rc, err = self._run(gh)
        self.assertEqual(rc, 8)
        self.assertEqual(gh.mentions, [], "a refused ask must notify nobody")
        self.assertIn("src/a.py", err)
        self.assertIn("@carol", err)

    def test_an_owner_among_the_asked_is_allowed(self):
        gh = FakeGitHub(["src/a.py"], codeowners={".github/CODEOWNERS": "* @carol @bob-gh\n"})
        rc, err = self._run(gh)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(gh.mentions), 2)

    def test_the_author_as_only_owner_does_not_count(self):
        gh = FakeGitHub(["src/a.py"], author="bob-gh",
                        codeowners={"CODEOWNERS": "* @bob-gh\n"})
        rc, err = self._run(gh)
        self.assertEqual(rc, 8)
        self.assertIn("author bob-gh", err)

    def test_the_last_matching_rule_wins(self):
        later = FakeGitHub(["packages/x.py"],
                           codeowners={".github/CODEOWNERS": "* @alice-gh\n/packages/ @carol\n"})
        self.assertEqual(self._run(later)[0], 8, "the later /packages/ rule must override *")
        earlier = FakeGitHub(["packages/x.py"],
                             codeowners={".github/CODEOWNERS": "/packages/ @carol\n* @alice-gh\n"})
        self.assertEqual(self._run(earlier)[0], 0)

    def test_every_owned_file_needs_an_asked_owner(self):
        gh = FakeGitHub(["src/a.py", "packages/x.py"],
                        codeowners={".github/CODEOWNERS": "* @alice-gh\n/packages/ @carol\n"})
        rc, err = self._run(gh)
        self.assertEqual(rc, 8)
        self.assertIn("packages/x.py", err)
        self.assertNotIn("src/a.py", err)

    def test_the_override_sends_and_records_its_reason(self):
        gh = FakeGitHub(["src/a.py"], codeowners={".github/CODEOWNERS": "* @carol\n"})
        rc, err = self._run(gh, ("--no-codeowner", "carol is on leave"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(gh.mentions), 2)
        self.assertIn("carol is on leave", err)
        rows = [json.loads(x) for x in self.ledger.read_text().splitlines()]
        self.assertEqual({r.get("no_codeowner") for r in rows}, {"carol is on leave"})

    def test_an_API_error_on_the_PR_is_refused(self):
        gh = FakeGitHub(["src/a.py"],
                        fail={"pulls/7": _R(rc=1, err="gh: Server Error (HTTP 502)")})
        rc, err = self._run(gh)
        self.assertEqual(rc, 8)
        self.assertEqual(gh.mentions, [])
        self.assertIn("HTTP 502", err)

    def test_a_non_404_CODEOWNERS_error_is_not_read_as_absent(self):
        gh = FakeGitHub(["src/a.py"],
                        fail={"contents/": _R(rc=1, err="gh: Forbidden (HTTP 403)")})
        rc, err = self._run(gh)
        self.assertEqual(rc, 8)
        self.assertIn("HTTP 403", err)

    def test_a_short_file_list_is_refused(self):
        # The files endpoint stops at 3000; fewer than changed_files hides a path.
        class Short(FakeGitHub):
            def __call__(self, argv, **kw):
                res = FakeGitHub.__call__(self, argv, **kw)
                if argv[0] == "gh" and "repos/o/r/pulls/7" in argv:
                    res = _R(out="main\tdave\t3000\n")
                return res
        short = Short(["src/a.py"], codeowners={".github/CODEOWNERS": "* @alice-gh\n"})
        rc, err = self._run(short)
        self.assertEqual(rc, 8)
        self.assertIn("1 of 3000", err)

    def test_gh_failing_to_run_is_refused(self):
        def boom(argv, **kw):
            raise OSError("no gh on PATH")
        rc, err = self._run(boom)
        self.assertEqual(rc, 8)
        self.assertIn("no gh on PATH", err)

    def test_an_unparseable_PR_reply_is_refused(self):
        gh = FakeGitHub(["src/a.py"], fail={"pulls/7": _R(out="not tsv\n")})
        rc, err = self._run(gh)
        self.assertEqual(rc, 8)
        self.assertIn("unreadable PR facts", err)

    def test_an_unowned_file_needs_no_owner(self):
        gh = FakeGitHub(["src/a.py", "vendor/x.c"],
                        codeowners={".github/CODEOWNERS": "# owners\n\n* @alice-gh\n/vendor/\n"})
        rc, err = self._run(gh)
        self.assertEqual(rc, 0, err)

    def test_an_owner_unreachable_in_the_room_is_not_counted(self):
        # alice is the only owner and is not in --room; bob and erin are. The ask
        # must be refused whole, not delivered to the two non-owners.
        gh = FakeGitHub(["src/a.py"], codeowners={".github/CODEOWNERS": "* @alice-gh\n"},
                        members={"!here:x": ["@bob-stand:x", "@erin-stand:x"],
                                 "!r:x": ["@alice-stand:x", "@bob-stand:x", "@erin-stand:x"]})
        rc, err = self._run(gh, ("--room", "!here:x"), reviewers="alice,bob,erin")
        self.assertIn("alice: NOT REACHABLE in !here:x", err)
        self.assertEqual(gh.mentions, [], "the non-owners must not be asked alone")
        self.assertEqual(rc, 8, err)
        self.assertIn("no asked reviewer (bob-gh, erin-gh)", err)

    def test_an_absent_owner_is_not_counted(self):
        gh = FakeGitHub(["src/a.py"], codeowners={".github/CODEOWNERS": "* @alice-gh\n"},
                        members={"!r:x": ["@bob-stand:x", "@erin-stand:x"]})
        rc, err = self._run(gh, reviewers="alice,bob,erin")
        self.assertIn("alice: ABSENT from !r:x", err)
        self.assertEqual(gh.mentions, [])
        self.assertEqual(rc, 8, err)

    def test_an_unreachable_reviewer_counts_against_the_two_reviewer_rule(self):
        gh = FakeGitHub(["src/a.py"], members={"!r:x": ["@bob-stand:x"]})
        rc, err = self._run(gh)
        self.assertIn("alice: ABSENT from !r:x", err)
        self.assertEqual(gh.mentions, [])
        self.assertEqual(rc, 5, err)

    def test_a_notice_is_not_gated(self):
        gh = FakeGitHub(["src/a.py"], codeowners={".github/CODEOWNERS": "* @carol\n"})
        rc, err = self._run(gh, ("--kind", "notice"))
        self.assertEqual(rc, 0, err)


class Matching(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def owners(self, text, path):
        return self.mod.owners_of(path, self.mod.parse_codeowners(text))

    def test_star_stays_in_one_directory_double_star_crosses(self):
        self.assertEqual(self.owners("docs/* @a\n", "docs/x.md"), ["@a"])
        self.assertEqual(self.owners("docs/* @a\n", "docs/sub/x.md"), [])
        self.assertEqual(self.owners("docs/** @a\n", "docs/sub/x.md"), ["@a"])
        self.assertEqual(self.owners("**/logs @a\n", "deep/er/logs/x"), ["@a"])

    def test_a_partial_wildcard_owns_a_matching_directory(self):
        self.assertEqual(self.owners("/skills/room-* @a\n", "skills/room-commons/x.py"), ["@a"])
        self.assertEqual(self.owners("/skills/room-* @a\n", "skills/room-x.md"), ["@a"])
        self.assertEqual(self.owners("/skills/room-* @a\n", "skills/other/x.py"), [])
        self.assertEqual(self.owners("/src/ @b\n/src/voice-* @a\n", "src/voice-x/y.ts"), ["@a"])
        self.assertEqual(self.owners("src/a?/ @a\n", "src/ab/c/d.py"), ["@a"])

    def test_docs_star_reaches_neither_nested_files_nor_a_deeper_docs(self):
        self.assertEqual(self.owners("docs/* @a\n", "docs/a/b.md"), [])
        self.assertEqual(self.owners("docs/* @a\n", "x/docs/a.md"), [], "a middle slash anchors")
        self.assertEqual(self.owners("/docs/* @a\n", "docs/a.md"), ["@a"])

    def test_question_mark_and_escaped_hash(self):
        self.assertEqual(self.owners("a?.md @a\n", "ab.md"), ["@a"])
        self.assertEqual(self.owners("a?.md @a\n", "a/.md"), [])
        self.assertEqual(self.owners("\\#notes @a\n", "#notes"), ["@a"])

    def test_a_leading_slash_anchors_to_the_root(self):
        self.assertEqual(self.owners("/build/ @a\n", "build/x"), ["@a"])
        self.assertEqual(self.owners("/build/ @a\n", "src/build/x"), [])
        self.assertEqual(self.owners("build/ @a\n", "src/build/x"), ["@a"])

    def test_an_extension_glob_matches_at_any_depth(self):
        self.assertEqual(self.owners("*.js @a\n", "a/b/c.js"), ["@a"])
        self.assertEqual(self.owners("*.js @a\n", "a/b/c.ts"), [])

    def test_a_plain_path_owns_the_file_or_the_directory(self):
        self.assertEqual(self.owners("/src/x.ts @a\n", "src/x.ts"), ["@a"])
        self.assertEqual(self.owners("/src/pkg @a\n", "src/pkg/y.ts"), ["@a"])

    def test_inline_comments_and_ownerless_rules(self):
        text = "* @a # fallback\n/vendor/\n"
        self.assertEqual(self.owners(text, "x"), ["@a"])
        self.assertEqual(self.owners(text, "vendor/lib.c"), [], "a bare pattern unowns")

    def test_a_team_owner_never_counts_as_an_asked_reviewer(self):
        rules = self.mod.parse_codeowners("* @org/team\n")
        bad = self.mod.codeowner_violations(["a"], "dave", ["team", "org"], rules)
        self.assertEqual(bad, [("a", ["@org/team"])])

    def test_login_matching_is_case_insensitive(self):
        rules = self.mod.parse_codeowners("* @Qingyun-Wu\n")
        self.assertEqual(self.mod.codeowner_violations(["a"], "x", ["qingyun-wu"], rules), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
