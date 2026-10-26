#!/usr/bin/env python3
"""Public-write privacy gate: a `gh` write to a non-private repo whose text names
a private term, a Matrix ID or room ID, or a local home path is refused before
it runs (hooks/public-write-privacy-guard.py + src/public_write_privacy.py).

Each command shape is driven both clean (allowed) and dirty (denied); the end-to-
end cases pipe a real PreToolUse payload into the hook as Claude Code does.

Run:  python3 tests/public-write-privacy-guard.test.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "public-write-privacy-guard.py"
SEED = REPO / "scripts" / "seed-public-denylist.py"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "hooks"))
import public_write_privacy as policy  # noqa: E402

spec = importlib.util.spec_from_file_location("guard", HOOK)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

TERM = "zq-private-backend"
AGENT = "Zephyrine"
LOCAL = "/" + "Users" + "/jdoe/work/x.md"
MXID = "@zq-agent:matrix.zq.chat"
ROOM_ID = "!AbCdEfGh12:matrix.zq.chat"
DIRTY = (TERM, AGENT, LOCAL, MXID, ROOM_ID)

PUBLIC = lambda repo: "PUBLIC"  # noqa: E731
PRIVATE = lambda repo: "PRIVATE"  # noqa: E731


def shapes(text, tmp):
    """Every publishing command shape, carrying `text` as its published prose."""
    body = Path(tmp) / "body.md"
    body.write_text(text)
    q = text.replace("'", "")
    return {
        "pr create --body": f"gh pr create --repo o/r --title t --body '{q}'",
        "pr create --title": f"gh pr create -R o/r --title '{q}' --body ok",
        "pr create --body-file": f"gh pr create -R o/r --title t --body-file {body}",
        "pr edit -F": f"gh pr edit 5 -R o/r -F {body}",
        "pr comment -b": f"gh pr comment 5 -R o/r -b '{q}'",
        "pr comment --body=": f"gh pr comment 5 -R o/r --body='{q}'",
        "pr comment url": f"gh pr comment https://github.com/o/r/pull/5 --body '{q}'",
        "pr review": f"gh pr review 5 -R o/r --comment --body '{q}'",
        "issue create": f"gh issue create -R o/r --title t --body '{q}'",
        "issue comment": f"gh -R o/r issue comment 7 --body-file {body}",
        "issue edit": f"gh issue edit 7 -R o/r --body \"$(cat {body})\"",
        "release create": f"gh release create v1 -R o/r --notes '{q}'",
        "api POST comments": f"gh api repos/o/r/issues/5/comments -f body='{q}'",
        "api PATCH pull": f"gh api -X PATCH repos/o/r/pulls/5 -f body='{q}'",
        "api --method POST review": f"gh api --method POST /repos/o/r/pulls/5/reviews -F body=@{body}",
        "api --input": f"gh api -X POST repos/o/r/issues --input {body}",
        "heredoc stdin": f"gh pr comment 5 -R o/r --body-file - <<'EOF'\n{text}\nEOF",
        "here-string": f"gh pr comment 5 -R o/r -F - <<< '{q}'",
        "written then posted": f"cat > {Path(tmp) / 'new.md'} <<'EOF'\n{text}\nEOF\n"
                               f"gh pr comment 5 -R o/r --body-file {Path(tmp) / 'new.md'}",
        "bash -c": f"bash -c \"gh pr comment 5 -R o/r -b '{q}'\"",
        "env prefix": f"GH_PAGER= gh pr comment 5 -R o/r -b '{q}'",
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ws = Path(self.tmp) / "ws"
        pdir = policy.privacy_dir(self.ws)
        pdir.mkdir(parents=True)
        (pdir / policy.DENYLIST_NAME).write_text(f"# test\nrepo: {TERM}\nagent: {AGENT}\n")

    def decide(self, command, lookup=PUBLIC):
        return guard.decide(command, self.tmp, self.ws, lookup=lookup)


class EachShape(Base):
    def test_every_shape_blocks_every_kind_of_private_detail(self):
        for needle in DIRTY:
            for name, cmd in shapes(f"see {needle} for details", self.tmp).items():
                with self.subTest(shape=name, needle=needle):
                    reason = self.decide(cmd)
                    self.assertIsNotNone(reason, cmd)
                    self.assertIn(needle.split("/work")[0], reason)

    def test_refusal_names_term_and_category(self):
        reason = self.decide(f"gh pr comment 5 -R o/r -b 'uses {TERM}'")
        self.assertIn(f"`{TERM}` [repo]", reason)
        reason = self.decide(f"gh pr comment 5 -R o/r -b 'ping {MXID}'")
        self.assertIn("[Matrix ID]", reason)

    def test_clean_text_passes_every_shape(self):
        clean = "A private backend repo and agent A, described by function; see src/x.py."
        for name, cmd in shapes(clean, self.tmp).items():
            with self.subTest(shape=name):
                self.assertIsNone(self.decide(cmd), cmd)


class Boundaries(Base):
    def test_private_repo_is_skipped(self):
        for name, cmd in shapes(f"uses {TERM} at {LOCAL}", self.tmp).items():
            if "-R o/r" not in cmd and "repos/o/r" not in cmd and "/o/r/" not in cmd:
                continue
            with self.subTest(shape=name):
                self.assertIsNone(self.decide(cmd, lookup=PRIVATE), cmd)

    def test_unknown_visibility_is_scanned(self):
        self.assertIsNotNone(self.decide(f"gh pr comment 5 -b '{TERM}'", lookup=lambda r: None))

    def test_reads_are_not_writes(self):
        for cmd in (f"gh pr view 5 -R o/r --json body | grep {TERM}",
                    f"gh api repos/o/r/pulls/5 --jq .body  # {TERM}",
                    f"echo {TERM} {LOCAL}", "gh pr review 5 -R o/r --approve"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.decide(cmd))

    def test_case_rules(self):
        self.assertIsNone(self.decide("gh pr comment 5 -R o/r -b 'zephyrine is lowercase prose'"))
        self.assertIsNotNone(self.decide(f"gh pr comment 5 -R o/r -b '{TERM.upper()}'"))
        self.assertIsNone(self.decide(f"gh pr comment 5 -R o/r -b '{TERM}x2'"))

    def test_placeholders_are_not_findings(self):
        text = "e.g. @bob:example.org in !room123:example.com under /" + "Users/runner/work"
        self.assertIsNone(self.decide(f"gh pr comment 5 -R o/r -b '{text}'"))

    def test_allowlist_and_own_signature(self):
        (policy.privacy_dir(self.ws) / policy.ALLOWLIST_NAME).write_text(f"{TERM}\n")
        self.assertIsNone(self.decide(f"gh pr comment 5 -R o/r -b 'uses {TERM}'"))
        os.environ["SUTANDO_AGENT_MXID"] = MXID
        try:
            self.assertIsNone(self.decide(f"gh pr comment 5 -R o/r -b '— me ({MXID})'"))
        finally:
            del os.environ["SUTANDO_AGENT_MXID"]


class FailsClosed(Base):
    def assertRefused(self, cmd, why):
        reason = self.decide(cmd)
        self.assertIsNotNone(reason, cmd)
        self.assertIn(why, reason)

    def test_unreadable_body_file(self):
        self.assertRefused("gh pr create -R o/r -t t --body-file /nonexistent/body.md", "cannot read")
        self.assertRefused("gh api repos/o/r/issues/5/comments -F body=@/nonexistent/b", "cannot read")

    def test_stdin_it_cannot_see(self):
        self.assertRefused("printf x | gh pr comment 5 -R o/r --body-file -", "stdin")

    def test_runtime_expansion(self):
        self.assertRefused('gh pr comment 5 -R o/r --body "$BODY"', "expands at run time")
        self.assertRefused("gh pr comment 5 -R o/r --body \"`cat f`\"", "expands at run time")

    def test_file_written_by_same_command_without_heredoc(self):
        f = Path(self.tmp) / "gen.md"
        f.write_text("stale clean text")
        self.assertRefused(f"python3 gen.py > {f} && gh pr comment 5 -R o/r -F {f}", "same command")

    def test_unparseable_line(self):
        self.assertRefused("gh pr comment 5 -R o/r -b 'unterminated", "does not parse")

    def test_missing_denylist(self):
        (policy.privacy_dir(self.ws) / policy.DENYLIST_NAME).unlink()
        self.assertRefused("gh pr comment 5 -R o/r -b 'anything'", "seed-public-denylist")


class VisibilityCache(unittest.TestCase):
    def test_cached_and_expiring(self):
        cache = Path(tempfile.mkdtemp()) / "vis.json"
        calls = []
        look = lambda r: calls.append(r) or "PRIVATE"  # noqa: E731
        now = time.time()
        self.assertEqual(policy.repo_visibility("o/r", cache, look, now), "PRIVATE")
        self.assertEqual(policy.repo_visibility("O/R", cache, look, now + 60), "PRIVATE")
        self.assertEqual(len(calls), 1)
        policy.repo_visibility("o/r", cache, look, now + policy.TTL_SECONDS["PRIVATE"] + 1)
        self.assertEqual(len(calls), 2)

    def test_unresolvable_repo(self):
        self.assertIsNone(policy.repo_visibility("{owner}/{repo}", None, PUBLIC))
        self.assertIsNone(policy.repo_visibility(None, None, PUBLIC))


class Hook(Base):
    def run_hook(self, command):
        cache = policy.privacy_dir(self.ws) / policy.VISIBILITY_CACHE_NAME
        cache.write_text(json.dumps({"o/r": {"visibility": "PUBLIC", "at": time.time()}}))
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": self.tmp,
                   "hook_event_name": "PreToolUse"}
        r = subprocess.run([sys.executable, str(HOOK), "--workspace", str(self.ws)],
                           input=json.dumps(payload), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["hookSpecificOutput"] if r.stdout.strip() else None

    def test_end_to_end_deny_and_allow(self):
        out = self.run_hook(f"gh pr create -R o/r -t t --body 'names {TERM}'")
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn(TERM, out["permissionDecisionReason"])
        self.assertIsNone(self.run_hook("gh pr create -R o/r -t t --body 'clean'"))
        self.assertIsNone(self.run_hook("ls -la"))


class DenylistStaysOutOfTheRepo(unittest.TestCase):
    def test_no_tracked_denylist(self):
        tracked = subprocess.run(["git", "-C", str(REPO), "ls-files"], capture_output=True,
                                 text=True).stdout.splitlines()
        names = {policy.DENYLIST_NAME, policy.ALLOWLIST_NAME, policy.VISIBILITY_CACHE_NAME}
        self.assertEqual([t for t in tracked if Path(t).name in names], [])

    def test_default_location_is_ignored_by_git(self):
        target = policy.privacy_dir(REPO / "workspace") / policy.DENYLIST_NAME
        r = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", str(target)])
        self.assertEqual(r.returncode, 0, f"{target} would be committable")

    def test_seed_writes_only_the_workspace_and_prints_no_terms(self):
        ws = Path(tempfile.mkdtemp())
        roster = ws / "hosts" / "h1" / "data" / "collaboration-intelligence" / "reviewer-stands.json"
        roster.parent.mkdir(parents=True)
        roster.write_text(json.dumps({"k": {"human": "Quillon Vext", "stand_name": AGENT,
                                            "github": "qvext", "discord_id": 123456789012345678}}))
        (ws / "state").mkdir()
        (ws / "state" / "ag2space-rooms.json").write_text(json.dumps(
            {"rooms": [{"name": "Zq Triage Room", "room_id": ROOM_ID}]}))
        before = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"],
                                capture_output=True, text=True).stdout
        r = subprocess.run([sys.executable, str(SEED), "--workspace", str(ws), "--no-github"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        written = (policy.privacy_dir(ws) / policy.DENYLIST_NAME).read_text()
        for term in ("Quillon Vext", AGENT, "Zq Triage Room", ROOM_ID, "123456789012345678"):
            self.assertIn(term, written)
            self.assertNotIn(term, r.stdout + r.stderr)
        self.assertNotIn("qvext", written)  # a GitHub login is already public
        after = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"],
                               capture_output=True, text=True).stdout
        self.assertEqual(before, after)

    def test_reseed_keeps_manual_lines(self):
        ws = Path(tempfile.mkdtemp())
        target = policy.privacy_dir(ws) / policy.DENYLIST_NAME
        for _ in range(2):
            subprocess.run([sys.executable, str(SEED), "--workspace", str(ws), "--no-github"],
                           capture_output=True, text=True, timeout=120, check=True)
            if "agent: Kept" not in target.read_text():
                target.write_text(target.read_text() + "agent: Kept\n")
        self.assertEqual(target.read_text().count("agent: Kept"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
