#!/usr/bin/env python3
"""`review-pr.sh <N> --non-owner` reviews in a dir holding only the PR's code.

Offline: `gh`, `claude` and `codex` are stand-ins on PATH. Pins the confined argv
(read-only file tools, --restricted, no MCP), that the run's cwd is a fresh dir
outside the repo holding only the diff and the PR's regular files, that codex is
never invoked, that the Team rulebook's command takes that path, and that the owner path
still runs codex.

Run: python3 tests/review-pr-non-owner.test.py
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "claude-codex" / "scripts" / "review-pr.sh"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests" / "_helpers"))
sys.path.insert(0, str(REPO / "skills" / "claude-codex" / "scripts"))

from policy import sandbox_runner as sr, seatbelt  # noqa: E402
import discord_env, review_pr_fixture, review_pr_sandbox as rps  # noqa: E401,E402

os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="review-pr-ccd-")
_CFG = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_CFG.mkdir(parents=True, exist_ok=True)
(_CFG / "access.json").write_text('{"allowFrom": []}')
discord_env.seed_discord_token(Path(os.environ["CLAUDE_CONFIG_DIR"]))


def _load_bridge():
    discord_env.install_discord_stub()
    spec = importlib.util.spec_from_file_location("dbridge_review_pr", REPO / "src" / "discord-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dbridge_review_pr"] = mod
    spec.loader.exec_module(mod)
    return mod


# Unique per run: a leftover from an earlier run in the shared TMPDIR must not fail this one.
ESCAPE = f"escape-{os.getpid()}-{os.urandom(4).hex()}.txt"
SPEC = {
    "url": "https://github.com/example/demo/pull/7", "sha": "b" * 40,
    "diff": "diff --git a/src/a.py b/src/a.py\n+x = 1\n",
    "files": {
        "src/a.py": "x = 1\n",
        "docs/b.md": "# b\n",
        "link": {"type": "symlink", "target": "/etc/passwd"},
        "vendor/sub": {"type": "submodule"},
        f"../{ESCAPE}": "nope\n",
        "/abs.txt": "nope\n",
        "removed.py": None,
    },
}

# Records argv + cwd + the cwd's file list, then prints a verdict.
FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
files = sorted(os.path.relpath(os.path.join(d, f), ".") for d, _, fs in os.walk(".") for f in fs)
json.dump({{"argv": sys.argv[1:], "cwd": os.getcwd(), "files": files}}, open({log!r}, "w"))
print("- STUB REVIEW: no blocking issues")
"""
FAKE_CODEX = """#!/bin/sh
echo codex-called >> {log!r}
exit 1
"""


def gh(args):
    return review_pr_fixture.answer(SPEC, args)


class Populate(unittest.TestCase):
    def setUp(self):
        self.root = rps.review_dir()
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_writes_the_diff_and_only_regular_in_tree_files(self):
        written = rps.populate(self.root, "7", "", SPEC["diff"], gh)
        self.assertEqual(sorted(written), ["PR.diff", "docs/b.md", "src/a.py"])
        self.assertEqual((self.root / "PR.diff").read_text(), SPEC["diff"])
        self.assertEqual((self.root / "src" / "a.py").read_text(), "x = 1\n")
        on_disk = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")
                         if p.is_file() or p.is_symlink())
        self.assertEqual(on_disk, ["PR.diff", "docs/b.md", "src/a.py"])
        self.assertFalse(any(p.is_symlink() for p in self.root.rglob("*")))
        self.assertFalse((self.root.parent / ESCAPE).exists())

    def test_unreadable_metadata_leaves_the_diff_only(self):
        self.assertEqual(rps.populate(self.root, "7", "", "d", lambda a: None), ["PR.diff"])

    def test_safe_relpath(self):
        for bad in ("", "/x", "../x", "a/../b", "a//b", "./a", "a\\b", "PR.diff", "a\x00b"):
            self.assertIsNone(rps.safe_relpath(bad), bad)
        self.assertEqual(str(rps.safe_relpath("a/b.py")), "a/b.py")

    def test_review_dir_is_outside_the_repo_and_private(self):
        self.assertNotIn(REPO, self.root.parents)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)

    def test_review_dir_refuses_a_tmpdir_inside_the_workspace(self):
        ws = Path(tempfile.mkdtemp(prefix="review-pr-ws-")).resolve()
        self.addCleanup(shutil.rmtree, ws, True)
        orig = tempfile.tempdir
        tempfile.tempdir = str(ws / "tmp")
        (ws / "tmp").mkdir()
        try:
            with self.assertRaisesRegex(RuntimeError, "inside the workspace"):
                rps.review_dir(workspace=ws)
        finally:
            tempfile.tempdir = orig
        self.assertEqual(list((ws / "tmp").iterdir()), [])


class ConfinedInvocation(unittest.TestCase):
    def setUp(self):
        self.root = rps.review_dir()
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "a.py").write_text("x\n")

    def test_argv_has_read_only_tools_and_no_mcp_or_settings(self):
        inv = sr.confined_invocation(sr.RUNNERS["claude"], "/b/claude", "p", cwd=str(self.root))
        a = inv.argv
        self.assertEqual(inv.cwd, str(self.root))
        self.assertEqual(a[a.index("--tools") + 1], "Read,Grep,Glob")
        for flag in ("--restricted", "--strict-mcp-config", "--disable-slash-commands",
                     "--no-session-persistence"):
            self.assertIn(flag, a)
        self.assertEqual(a[a.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("--mcp-config", a)
        self.assertNotIn("--add-dir", a)
        self.assertEqual(a[-2:], ["--", "p"])

    def test_refuses_unfenced_unsafe_runners_and_bad_dirs(self):
        for name in ("codex", "agy"):
            with mock.patch.object(seatbelt, "available", return_value=False), \
                    self.assertRaises(ValueError):
                sr.confined_invocation(sr.RUNNERS[name], f"/b/{name}", "p", cwd=str(self.root))
        with self.assertRaises(ValueError):
            sr.confined_invocation(sr.RUNNERS["claude"], "/b/claude", "p",
                                   cwd=str(self.root / "missing"))
        (self.root / "d").mkdir()
        os.symlink("/etc/hosts", self.root / "d" / "l")
        with self.assertRaises(ValueError):
            sr.confined_invocation(sr.RUNNERS["claude"], "/b/claude", "p", cwd=str(self.root))

    def test_build_is_none_with_no_safe_runner(self):
        self.assertIsNone(rps.build(self.root, "d", which=lambda n, path=None: None))
        only_codex = lambda n, path=None: "/b/codex" if n == "codex" and path is None else None  # noqa: E731
        self.assertIsNone(rps.build(self.root, "d", which=only_codex))


class InProcess(unittest.TestCase):
    """run()/main()/_gh in this process, with a stand-in claude picked as the runner."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="review-pr-inproc-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.tmp, self.out = tmp, tmp / "out.txt"
        self.claude = tmp / "claude"

    def _claude(self, body):
        self.claude.write_text("#!/bin/sh\n" + body)
        self.claude.chmod(0o755)
        p = mock.patch.object(rps.sr, "select_runner",
                              return_value=(rps.sr.RUNNERS["claude"], str(self.claude)))
        p.start()
        self.addCleanup(p.stop)

    def test_run_writes_the_verdict_and_removes_the_dir(self):
        self._claude('pwd > "$0.cwd"; echo "- no blocking issues"\n')
        rc = rps.run("7", "", SPEC["diff"], self.out, 30, gh)
        self.assertEqual(rc, 0)
        self.assertEqual(self.out.read_text(), "- no blocking issues\n")
        self.assertFalse(Path(Path(f"{self.claude}.cwd").read_text().strip()).exists())

    def test_run_passes_a_failure_through(self):
        self._claude("echo partial; echo boom >&2; exit 5\n")
        self.assertEqual(rps.run("7", "", SPEC["diff"], self.out, 30, gh), 5)

    def test_run_past_max_is_124(self):
        self._claude("sleep 5\n")
        self.assertEqual(rps.run("7", "", SPEC["diff"], self.out, 1, gh), 124)

    def test_run_with_no_runner_is_3(self):
        with mock.patch.object(rps.sr, "select_runner", return_value=None):
            self.assertEqual(rps.run("7", "", SPEC["diff"], self.out, 30, gh), rps.NO_RUNNER)

    def test_main_reads_the_diff_file(self):
        diff = self.tmp / "d.diff"
        diff.write_text(SPEC["diff"])
        with mock.patch.object(rps, "run", return_value=0) as run:
            self.assertEqual(rps.main(["--pr", "7", "--diff-file", str(diff),
                                       "--out", str(self.out)]), 0)
        run.assert_called_once_with("7", "", SPEC["diff"], self.out, 240)

    def test_populate_stops_at_the_byte_cap(self):
        root = rps.review_dir()
        self.addCleanup(shutil.rmtree, root, True)
        with mock.patch.object(rps, "MAX_TOTAL_BYTES", 6):
            written = rps.populate(root, "7", "", SPEC["diff"], gh)
        self.assertEqual(len(written), 2)

    def test_gh_returns_stdout_only_on_success(self):
        bindir = self.tmp / "bin"
        review_pr_fixture.install(bindir, SPEC)
        with mock.patch.dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}"):
            self.assertEqual(rps._gh(["pr", "diff", "7"]), SPEC["diff"])
            self.assertIsNone(rps._gh(["nope"]))
        with mock.patch.object(rps.subprocess, "run", side_effect=OSError("no gh")):
            self.assertIsNone(rps._gh(["pr", "diff", "7"]))


class Script(unittest.TestCase):
    def run_script(self, *args, command=None, opt_in=True):
        tmp = Path(tempfile.mkdtemp(prefix="review-pr-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        bindir = tmp / "bin"
        review_pr_fixture.install(bindir, SPEC)
        self.claude_log, self.codex_log = tmp / "claude.json", tmp / "codex.log"
        (bindir / "claude").write_text(FAKE_CLAUDE.format(log=str(self.claude_log)))
        (bindir / "codex").write_text(FAKE_CODEX.format(log=str(self.codex_log)))
        for n in ("claude", "codex"):
            (bindir / n).chmod(0o755)
        env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}")
        # Default order, but no fenced runner ready: the stub codex has no native
        # binary, and agy (possibly installed in ~/.local/bin) has no API key.
        for k in ("SUTANDO_SANDBOX_RUNNERS", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                  sr.CLAUDE_OPT_IN_KEY):
            env.pop(k, None)
        if opt_in:
            env[sr.CLAUDE_OPT_IN_KEY] = "1"
        argv = ["bash", "-c", command] if command else ["bash", str(SCRIPT), "7", *args]
        return subprocess.run(argv, cwd=REPO, env=env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=60)

    def test_non_owner_runs_claude_confined_and_never_codex_unfenced(self):
        p = self.run_script("--non-owner")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("STUB REVIEW", p.stdout)
        self.assertFalse(self.codex_log.exists(), "codex was invoked")
        rec = json.loads(self.claude_log.read_text())
        cwd = Path(rec["cwd"]).resolve()
        self.assertNotEqual(cwd, REPO)
        self.assertNotIn(REPO, cwd.parents)
        self.assertEqual(rec["files"], ["PR.diff", "docs/b.md", "src/a.py"])
        self.assertEqual(rec["argv"][rec["argv"].index("--tools") + 1], "Read,Grep,Glob")
        self.assertIn("--restricted", rec["argv"])
        self.assertFalse(cwd.exists(), "the review dir was not removed")

    def test_non_owner_without_opt_in_runs_nothing(self):
        """No fenced agy and claude not opted in: exit 3, no runner spawned."""
        p = self.run_script("--non-owner", opt_in=False)
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertIn("sandboxed path unavailable", p.stderr)
        self.assertFalse(self.claude_log.exists(), "claude ran without the owner's opt-in")
        self.assertFalse(self.codex_log.exists(), "codex was invoked")

    def test_owner_path_still_runs_codex(self):
        self.run_script()
        self.assertTrue(self.codex_log.exists(), "owner path no longer runs codex")
        self.assertFalse(self.claude_log.exists())

    def test_team_rulebook_command_never_runs_codex(self):
        bridge = _load_bridge()
        p = self.run_script(command=bridge.TEAM_PR_REVIEW_COMMAND.replace("<N>", "7"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("STUB REVIEW", p.stdout)
        self.assertFalse(self.codex_log.exists(), "the Team rulebook's command ran codex")
        rec = json.loads(self.claude_log.read_text())
        self.assertEqual(rec["files"], ["PR.diff", "docs/b.md", "src/a.py"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
