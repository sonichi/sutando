#!/usr/bin/env python3
"""Contract for src/witness_owed.py and its wiring into self-upgrade."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("witness_owed", ROOT / "src" / "witness_owed.py")
wo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wo)


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "ws"
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
             "--allow-empty", "-m", "base")
        self.base = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
             "--allow-empty", "-m", "owed pr")
        self.owed = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
             "--allow-empty", "-m", "later")
        self.later = _git(self.repo, "rev-parse", "HEAD")

    def tearDown(self):
        self.tmp.cleanup()


class Records(Fixture):
    def test_open_requires_every_field(self):
        for bad in ({"head": "notasha"}, {"host": ""}, {"reason": " "}, {"opened_by": ""}):
            kw = dict(head=self.owed, host="h", reason="r", opened_by="me")
            kw.update(bad)
            with self.assertRaises(ValueError, msg=bad):
                wo.open_record(self.ws, "o/r", 1, **kw)
        with self.assertRaises(ValueError):
            wo.open_record(self.ws, "o r", 1, head=self.owed, host="h", reason="r", opened_by="me")

    def test_open_list_close_round_trip(self):
        p = wo.open_record(self.ws, "o/r", 12, self.owed, "hostA", "no supervised lane", "001")
        self.assertEqual(p.name, "o-r#12.json")
        recs = wo.list_open(self.ws)
        self.assertEqual([(r["repo"], r["pr"], r["head"]) for r in recs], [("o/r", 12, self.owed)])
        with self.assertRaises(ValueError):
            wo.close_record(self.ws, "o/r", 12, "")
        closed = wo.close_record(self.ws, "o/r", 12, "https://example/pr/12#c1")
        self.assertEqual(wo.list_open(self.ws), [])
        data = json.loads(closed.read_text())
        self.assertEqual(data["witness"], "https://example/pr/12#c1")
        self.assertIn("closed_at", data)

    def test_a_malformed_record_blocks_rather_than_vanishes(self):
        d = wo.records_dir(self.ws)
        d.mkdir(parents=True)
        (d / "o-r#3.json").write_text("{not json")
        (d / "o-r#4.json").write_text(json.dumps({"repo": "o/r"}))
        hits = wo.blocking(self.ws, self.repo, self.later)
        self.assertEqual(sorted(h["reason"][:10] for h in hits), ["unreadable", "unreadable"])


class Gate(Fixture):
    def test_blocks_only_a_target_that_newly_contains_the_owed_head(self):
        wo.open_record(self.ws, "o/r", 1, self.owed, "hostA", "reason", "001")
        # target contains the owed head, current does not -> blocked
        self.assertEqual(len(wo.blocking(self.ws, self.repo, self.later, self.base)), 1)
        # target is before the owed head -> nothing to activate
        self.assertEqual(wo.blocking(self.ws, self.repo, self.base), [])
        # already running the owed head -> the upgrade adds nothing owed
        self.assertEqual(wo.blocking(self.ws, self.repo, self.later, self.owed), [])
        # closing it releases the gate
        wo.close_record(self.ws, "o/r", 1, "posted")
        self.assertEqual(wo.blocking(self.ws, self.repo, self.later, self.base), [])

    def test_unknown_head_fails_closed(self):
        wo.open_record(self.ws, "o/r", 1, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", "h", "r", "me")
        self.assertEqual(len(wo.blocking(self.ws, self.repo, self.later)), 1)

    def test_canary_releases_only_the_owing_host(self):
        wo.open_record(self.ws, "o/r", 1, self.owed, "hostA", "reason", "001")
        wo.mark_canary(self.ws, "o/r", 1, "hostA")
        self.assertEqual(wo.blocking(self.ws, self.repo, self.later, host="hostA"), [])
        self.assertEqual(len(wo.blocking(self.ws, self.repo, self.later, host="hostB")), 1)
        self.assertEqual(len(wo.blocking(self.ws, self.repo, self.later)), 1)


class Cli(Fixture):
    def _run(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "src" / "witness_owed.py"),
                               "--workspace", str(self.ws), *args],
                              capture_output=True, text=True)

    def test_check_exit_codes_and_messages(self):
        self.assertEqual(self._run("check", "--ref", self.later, "--repo-root", str(self.repo)).returncode, 0)
        r = self._run("open", "o/r#7", "--head", self.owed, "--host", "hostA",
                      "--reason", "no supervised lane on any host", "--by", "001")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self._run("check", "--ref", self.later, "--repo-root", str(self.repo))
        self.assertEqual(r.returncode, 3)
        self.assertIn("witness owed: o/r#7", r.stderr)
        self.assertEqual(self._run("check", "--ref", self.later, "--repo-root", str(self.repo),
                                   "--host", "hostA").returncode, 3, "canary not yet declared")
        self.assertEqual(self._run("canary", "o/r#7", "--host", "hostA").returncode, 0)
        self.assertEqual(self._run("check", "--ref", self.later, "--repo-root", str(self.repo),
                                   "--host", "hostA").returncode, 0)
        self.assertEqual(self._run("close", "o/r#7", "--witness", "thread").returncode, 0)
        self.assertEqual(self._run("check", "--ref", self.later, "--repo-root", str(self.repo)).returncode, 0)
        self.assertNotEqual(self._run("open", "bad key", "--head", self.owed, "--host", "h",
                                      "--reason", "r", "--by", "me").returncode, 0)


class UpgradeWiring(unittest.TestCase):
    def test_self_upgrade_checks_the_record_before_it_pulls(self):
        src = (ROOT / "skills/self-upgrade/scripts/upgrade.sh").read_text()
        check = src.index("src/witness_owed.py")
        pull = src.index("git pull --ff-only")
        self.assertLess(check, pull, "the gate must run before the head changes")
        self.assertIn('check --ref "$REMOTE/$BRANCH"', src)
        self.assertIn("--current HEAD", src)
        self.assertIn("--canary", src)


if __name__ == "__main__":
    unittest.main()
