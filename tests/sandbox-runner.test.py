#!/usr/bin/env python3
"""The non-owner sandbox runner has ONE owner: src/policy/sandbox_runner.py.

Pins: the first safe, installed runner in order agy -> codex -> claude is chosen
(agy is never chosen: it has no flag-only read-only, no-network mode); each
runner's argv carries its read-only/no-network flags; the guardrail text and the
Discord bridge both take the runner from the module, so neither can drift.

Run: python3 tests/sandbox-runner.test.py
"""
import asyncio
import importlib.util
import itertools
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from policy import sandbox_runner as sr  # noqa: E402
from policy import guardrail  # noqa: E402

FORBIDDEN_CLAUDE_TOOLS = ("Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch")


def fake_which(present):
    """shutil.which stand-in: `present` names resolve under /fake/bin, nothing else."""
    def which(name, path=None):
        return f"/fake/bin/{name}" if name in present and path is None else None
    return which


class OrderSelection(unittest.TestCase):
    def test_every_presence_combination(self):
        for combo in itertools.product((False, True), repeat=3):
            present = {n for n, on in zip(("agy", "codex", "claude"), combo) if on}
            with self.subTest(present=sorted(present)):
                got = sr.select_runner(sr.DEFAULT_ORDER, fake_which(present))
                if "codex" in present:
                    want = "codex"
                elif "claude" in present:
                    want = "claude"
                else:
                    want = None
                self.assertEqual(got[0].name if got else None, want)
                if got:
                    self.assertEqual(got[1], f"/fake/bin/{want}")

    def test_agy_is_listed_first_but_never_selected(self):
        self.assertEqual(sr.DEFAULT_ORDER[0], "agy")
        self.assertFalse(sr.RUNNERS["agy"].safe)
        self.assertIn("no flag-only read-only, no-network mode", sr.RUNNERS["agy"].unsafe_reason)
        self.assertIsNone(sr.select_runner(("agy",), fake_which({"agy"})))
        with self.assertRaises(ValueError):
            sr.invocation(sr.RUNNERS["agy"], "/fake/bin/agy", "p")

    def test_configured_order_is_honored(self):
        both = fake_which({"codex", "claude"})
        self.assertEqual(sr.select_runner(("claude", "codex"), both)[0].name, "claude")
        env = {sr.CONFIG_KEY: "claude,codex"}
        self.assertEqual(sr.select_runner(None, both, env)[0].name, "claude")

    def test_none_installed_renders_codex_so_the_core_reports_unavailable(self):
        self.assertIsNone(sr.select_runner(sr.DEFAULT_ORDER, fake_which(set())))
        self.assertEqual(sr.selected_or_codex(sr.DEFAULT_ORDER, fake_which(set())).name, "codex")

    def test_user_bin_dir_is_searched_when_path_misses(self):
        """A launchd bridge's PATH lacks ~/.local/bin, where the claude installer puts it."""
        def which(name, path=None):
            return f"{path}/{name}" if path and name == "claude" else None
        got = sr.select_runner(("codex", "claude"), which)
        self.assertEqual(got[0].name, "claude")
        self.assertEqual(got[1], f"{sr.USER_BIN_DIRS[0]}/claude")


class OrderConfig(unittest.TestCase):
    def _skills(self, config):
        d = Path(tempfile.mkdtemp())
        (d / "some-skill").mkdir()
        (d / "some-skill" / "manifest.json").write_text(json.dumps({"name": "x", "config": config}))
        return [d]

    def test_env_beats_manifest_beats_default(self):
        skills = self._skills({sr.CONFIG_KEY: "claude"})
        self.assertEqual(sr.resolve_order({}, skills), ("claude",))
        self.assertEqual(sr.resolve_order({sr.CONFIG_KEY: "codex"}, skills), ("codex",))
        self.assertEqual(sr.resolve_order({}, []), sr.DEFAULT_ORDER)

    def test_unknown_names_are_dropped_and_empty_falls_back(self):
        self.assertEqual(sr.resolve_order({sr.CONFIG_KEY: " Claude , bogus "}, []), ("claude",))
        self.assertEqual(sr.resolve_order({sr.CONFIG_KEY: "bogus"}, []), sr.DEFAULT_ORDER)

    def test_disabled_manifest_is_ignored(self):
        d = Path(tempfile.mkdtemp())
        (d / "s").mkdir()
        (d / "s" / "manifest.json").write_text(json.dumps(
            {"enabled": False, "config": {sr.CONFIG_KEY: "claude"}}))
        self.assertIsNone(sr.declared_order([d]))

    def test_the_repo_declares_the_key_in_a_manifest(self):
        self.assertEqual(sr.declared_order(), "agy,codex,claude")

    def test_an_unreadable_manifest_is_skipped(self):
        d = Path(tempfile.mkdtemp())
        (d / "a").mkdir()
        (d / "a" / "manifest.json").write_text("{not json")
        (d / "b").mkdir()
        (d / "b" / "manifest.json").write_text(json.dumps({"config": {sr.CONFIG_KEY: "claude"}}))
        self.assertEqual(sr.declared_order([d]), "claude")

    def test_outside_a_checkout_there_is_no_skills_dir(self):
        with mock.patch.object(Path, "is_file", return_value=False):
            self.assertEqual(sr._skills_dirs(), [])


class Invocations(unittest.TestCase):
    def test_a_runner_with_no_safe_flags_has_no_invocation(self):
        with self.assertRaises(ValueError):
            sr.invocation(sr.RUNNERS["agy"], "/b/agy", "PROMPT")

    def test_codex_argv(self):
        inv = sr.invocation(sr.RUNNERS["codex"], "/b/codex", "PROMPT", cwd="/tmp", out_path="/o.txt")
        self.assertEqual(inv.argv, ["/b/codex", "exec", "--sandbox", "read-only",
                                    "--skip-git-repo-check", "-C", "/tmp", "-o", "/o.txt",
                                    "--", "PROMPT"])
        self.assertIsNone(inv.cwd)

    def test_claude_argv_is_read_only_with_no_mcp(self):
        inv = sr.invocation(sr.RUNNERS["claude"], "/b/claude", "PROMPT", cwd="/tmp",
                            out_path="/o.txt", model="ignored")
        a = inv.argv
        self.assertEqual(a[:2], ["/b/claude", "-p"])
        self.assertIn("--restricted", a)
        self.assertIn("--strict-mcp-config", a)
        self.assertNotIn("--mcp-config", a)
        self.assertEqual(a[a.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(a[a.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("--disable-slash-commands", a)
        self.assertEqual(a[-2:], ["--", "PROMPT"])
        for tool in FORBIDDEN_CLAUDE_TOOLS:
            self.assertNotIn(tool, a[a.index("--tools") + 1].split(","))
        self.assertNotIn("--dangerously-skip-permissions", a)
        self.assertNotIn("-o", a)
        self.assertNotIn("--model", a)
        self.assertEqual(inv.cwd, "/tmp")
        self.assertTrue(inv.runner.answer_on_stdout)

    def test_shell_command_closes_stdin_and_lands_the_answer(self):
        codex = sr.shell_command(sr.RUNNERS["codex"], "P", out_path="/o")
        self.assertEqual(codex, "codex exec --sandbox read-only --skip-git-repo-check -o /o -- P < /dev/null")
        claude = sr.shell_command(sr.RUNNERS["claude"], "P", out_path="/o")
        self.assertTrue(claude.startswith("claude -p --restricted --strict-mcp-config --tools Read,Grep,Glob"))
        self.assertTrue(claude.endswith("-- P < /dev/null > /o"))

    @unittest.skipIf(os.name == "nt", "posix shell")
    def test_claude_shell_command_runs_in_cwd_with_closed_stdin(self):
        """Executes the rendered line against a stand-in `claude` that reports what it got."""
        bindir = Path(tempfile.mkdtemp())
        workdir = Path(tempfile.mkdtemp()).resolve()
        out = Path(tempfile.mkdtemp()) / "answer.txt"
        fake = bindir / "claude"
        fake.write_text('#!/bin/sh\nprintf "cwd=%s\\n" "$(pwd -P)"\nprintf "last=%s\\n" "$(eval echo \\${$#})"\n'
                        'if read -r x; then echo stdin=open; else echo stdin=closed; fi\n')
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        line = sr.shell_command(sr.RUNNERS["claude"], '"hello world"', cwd=str(workdir), out_path=str(out))
        env = dict(os.environ, PATH=f"{bindir}:{os.environ.get('PATH', '')}")
        subprocess.run(["bash", "-c", line], env=env, check=True)
        self.assertEqual(out.read_text().splitlines(),
                         [f"cwd={workdir}", "last=hello world", "stdin=closed"])


class GuardrailNamesTheSelectedRunner(unittest.TestCase):
    def setUp(self):
        self._orig = sr.find_binary

    def tearDown(self):
        sr.find_binary = self._orig

    def _only(self, present):
        sr.find_binary = lambda name, which=None: f"/fake/{name}" if name in present else None

    def test_codex_text_is_unchanged(self):
        self._only({"codex", "claude", "agy"})
        self.assertEqual(guardrail.sandboxed_delegation_text(), guardrail.SANDBOXED_DELEGATION_CODEX)
        self.assertIn("`codex exec --sandbox read-only --skip-git-repo-check -- \"$(cat <prompt-file>)\" "
                      "< /dev/null`", guardrail.SANDBOXED_DELEGATION_CODEX)

    def test_claude_only_host_names_claude(self):
        self._only({"claude"})
        lines = guardrail.ag2space_tier_lines("guest", False, "results/t.txt")
        block = "\n".join(lines)
        self.assertIn("Delegate it to Claude Code: `claude -p --restricted", block)
        self.assertNotIn("codex", block)
        # The surrounding policy wording survives the swap.
        for phrase in ("NO NETWORK", "NO permitted fallback", "unrestricted core",
                       "no inspection was performed", "non-empty", "< /dev/null"):
            self.assertIn(phrase, block)

    def test_no_runner_keeps_the_unavailable_instruction(self):
        self._only(set())
        text = guardrail.sandboxed_delegation_text()
        self.assertEqual(text, guardrail.SANDBOXED_DELEGATION_CODEX)
        self.assertIn("sandboxed path was unavailable", text)

    def test_vendored_twin_imports_the_same_module(self):
        pkg = REPO / "packages" / "ag2-sparrow"
        out = subprocess.run(
            [sys.executable, "-c",
             "from ag2_sparrow import team_guardrail as g, sandbox_runner as s;"
             "print(g.selected_or_codex is s.selected_or_codex)"],
            cwd=pkg, capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "True", out.stderr)


_BRIDGE_CCD = tempfile.mkdtemp(prefix="sandbox-runner-ccd-")
os.environ["CLAUDE_CONFIG_DIR"] = _BRIDGE_CCD
os.environ["DISCORD_BOT_TOKEN"] = "test-token-not-real"
(Path(_BRIDGE_CCD) / "channels" / "discord").mkdir(parents=True, exist_ok=True)
(Path(_BRIDGE_CCD) / "channels" / "discord" / "access.json").write_text(json.dumps({"allowFrom": ["1"]}))


def _load_bridge():
    try:  # pragma: no cover - present in dev, absent in clean CI
        import discord  # noqa: F401
    except Exception:
        stub = types.ModuleType("discord")
        stub.Intents = type("Intents", (), {"default": staticmethod(
            lambda: type("I", (), {"message_content": False})())})
        stub.Client = type("Client", (), {"__init__": lambda self, **kw: None,
                                          "event": staticmethod(lambda fn: fn)})
        stub.File = type("File", (), {"__init__": lambda self, *a, **kw: None})
        stub.Message = type("Message", (), {})
        stub.DMChannel = type("DMChannel", (), {})
        sys.modules["discord"] = stub
    spec = importlib.util.spec_from_file_location("dbridge_sandbox_runner", REPO / "src" / "discord-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dbridge_sandbox_runner"] = mod
    spec.loader.exec_module(mod)
    return mod


class DiscordBridgeDelegates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = _load_bridge()
        cls.src = (REPO / "src" / "discord-bridge.py").read_text()

    def _render(self, runner):
        m = re.search(r"\n    tier_instructions = \{\n.*?\n    \}\n", self.src, re.S)
        block = "\n".join(line[4:] for line in m.group(0).strip("\n").split("\n"))
        ns = dict(vars(self.bridge))
        ns.update(codex_prompt_text="hi", _sandbox=sr.RUNNERS[runner], RESULTS_DIR=Path("/R"))
        exec(block, ns)
        return ns["tier_instructions"]

    def test_rulebook_stage1_comes_from_the_module(self):
        codex = self._render("codex")
        self.assertIn("codex-bounded.sh --stall 45 --max 240 -- codex exec --sandbox read-only "
                      "--skip-git-repo-check -o /R/.codex-staging-{id}.txt -- ", codex["team"])
        self.assertIn("--skip-git-repo-check -C /tmp -o /R/.codex-staging-{id}.txt", codex["guest"])
        claude = self._render("claude")
        for tier in ("team", "guest"):
            self.assertIn("codex-bounded.sh --stall 0 --max 240 -- ", claude[tier])
            self.assertIn("claude -p --restricted --strict-mcp-config --tools Read,Grep,Glob", claude[tier])
            self.assertIn("< /dev/null > /R/.codex-staging-{id}.txt", claude[tier])
            stage1 = claude[tier][claude[tier].index("Stage 1:"):claude[tier].index("Stage 2:")]
            self.assertNotIn("codex exec", stage1)  # the PR-review path stays codex-only
        self.assertIn("/bin/sh -c 'cd \"$0\" && exec \"$@\"' /tmp claude -p", claude["guest"])
        self.assertNotIn("Do NOT redirect stdout", claude["team"])

    def _spawn_with(self, picked, stdout=b"", write_file=None):
        captured = {}

        class Proc:
            returncode = 0

            async def communicate(self):
                return stdout, b""

        async def fake_exec(*argv, **kw):
            captured.update(argv=list(argv), kw=kw)
            if write_file is not None and "-o" in argv:
                Path(argv[argv.index("-o") + 1]).write_text(write_file)
            return Proc()

        orig_sel, orig_exec = sr.select_runner, self.bridge.asyncio.create_subprocess_exec
        sr.select_runner = lambda *a, **k: picked
        self.bridge.asyncio.create_subprocess_exec = fake_exec
        try:
            raw = asyncio.run(self.bridge._run_codex_subprocess("judge me", None, 5))
        finally:
            sr.select_runner, self.bridge.asyncio.create_subprocess_exec = orig_sel, orig_exec
        return raw, captured

    def test_judge_spawns_the_selected_runner(self):
        raw, cap = self._spawn_with((sr.RUNNERS["claude"], "/fake/claude"), stdout=b'{"ok":1}')
        self.assertEqual(raw, '{"ok":1}')
        self.assertEqual(cap["argv"][:2], ["/fake/claude", "-p"])
        self.assertIs(cap["kw"]["stdin"], asyncio.subprocess.DEVNULL)
        raw, cap = self._spawn_with((sr.RUNNERS["codex"], "/fake/codex"), write_file='{"v":2}')
        self.assertEqual(raw, '{"v":2}')
        self.assertEqual(cap["argv"][:5], ["/fake/codex", "exec", "--sandbox", "read-only",
                                           "--skip-git-repo-check"])

    def test_judge_with_no_runner_spawns_nothing(self):
        raw, cap = self._spawn_with(None)
        self.assertEqual(raw, "")
        self.assertEqual(cap, {})


if __name__ == "__main__":
    unittest.main()
