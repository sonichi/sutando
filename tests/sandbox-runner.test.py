#!/usr/bin/env python3
"""The non-owner sandbox runner has ONE owner: src/policy/sandbox_runner.py.

Pins: agy, fenced, is the default; claude is chosen only when the owner opts in;
codex is never chosen; with none usable nothing runs (no unfenced fallback). agy's
Seatbelt-fenced runs are pinned in tests/seatbelt-fence.test.py. claude runs with no
tools in a fresh empty temp dir. The in-band command the guardrail text and the
Discord bridge render runs the module itself, so it follows the same rule.

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
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from policy import guardrail, sandbox_runner as sr, seatbelt  # noqa: E402
sys.path.insert(0, str(REPO / "tests" / "_helpers"))
import discord_env  # noqa: E402

os.environ.pop(sr.CLAUDE_OPT_IN_KEY, None)
OPTED_IN = {sr.CLAUDE_OPT_IN_KEY: "1"}


def fake_which(present):
    """shutil.which stand-in: `present` names resolve under /fake/bin, nothing else."""
    def which(name, path=None):
        return f"/fake/bin/{name}" if name in present and path is None else None
    return which


class OrderSelection(unittest.TestCase):
    def test_every_presence_combination(self):
        """agy when it can run fenced; else claude only if opted in; else nothing."""
        for combo in itertools.product((False, True), repeat=5):
            *installed, fence, opt_in = combo
            present = {n for n, on in zip(("agy", "codex", "claude"), installed) if on}
            env = dict(OPTED_IN if opt_in else {}, GEMINI_API_KEY="k")
            with self.subTest(present=sorted(present), fence=fence, opt_in=opt_in), \
                    mock.patch.object(seatbelt, "available", return_value=fence):
                got = sr.select_runner(None, fake_which(present), env, fenced=True)
                if "agy" in present and fence:
                    want = "agy"
                elif "claude" in present and opt_in:
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
        with mock.patch.object(seatbelt, "available", return_value=False), \
                sr.empty_dir() as d, self.assertRaises(ValueError):
            sr.invocation(sr.RUNNERS["agy"], "/fake/bin/agy", "p", cwd=d)

    def test_codex_is_never_selected(self):
        """No codex sandbox policy denies reads outside its cwd, so it cannot hold a guest."""
        self.assertFalse(sr.RUNNERS["codex"].safe)
        self.assertIn("denies reads outside", sr.RUNNERS["codex"].unsafe_reason)
        self.assertIsNone(sr.select_runner(("codex",), fake_which({"codex"})))
        self.assertIsNone(sr.select_runner(None, fake_which({"codex"}), {sr.CONFIG_KEY: "codex"}))
        for fence in (False, True):  # with or without the macOS fence, codex never runs
            with self.subTest(fence=fence), \
                    mock.patch.object(seatbelt, "available", return_value=fence):
                self.assertIsNone(sr.select_runner(("codex",), fake_which({"codex"}), fenced=True))
                for build in (sr.invocation, sr.confined_invocation):
                    with sr.empty_dir() as d, self.assertRaises(ValueError):
                        build(sr.RUNNERS["codex"], "/fake/bin/codex", "p", cwd=d)
        self.assertNotIn("codex", sr.DEFAULT_ORDER)

    def test_configured_order_is_honored(self):
        both = fake_which({"codex", "claude"})
        self.assertEqual(sr.select_runner(("codex", "claude"), both, OPTED_IN)[0].name, "claude")
        env = dict(OPTED_IN, **{sr.CONFIG_KEY: "codex,claude"})
        self.assertEqual(sr.select_runner(None, both, env)[0].name, "claude")

    def test_none_usable_labels_the_default_and_selects_nothing(self):
        self.assertIsNone(sr.select_runner(sr.DEFAULT_ORDER, fake_which(set())))
        got = sr.selected_or_fallback(sr.DEFAULT_ORDER, fake_which(set()))
        self.assertEqual(got.name, sr.FALLBACK)
        self.assertEqual(sr.FALLBACK, "agy")

    def test_user_bin_dir_is_searched_when_path_misses(self):
        """A launchd bridge's PATH lacks ~/.local/bin, where the claude installer puts it."""
        def which(name, path=None):
            return f"{path}/{name}" if path and name == "claude" else None
        got = sr.select_runner(("codex", "claude"), which, OPTED_IN)
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

    def test_the_repo_declares_the_keys_in_a_manifest(self):
        self.assertEqual(sr.declared_order(), "agy,claude")
        self.assertEqual(sr.declared_config(sr.CLAUDE_OPT_IN_KEY), "0")
        self.assertFalse(sr.claude_opted_in({}))

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
    def test_a_runner_with_no_safe_flags_and_no_fence_has_no_invocation(self):
        with mock.patch.object(seatbelt, "available", return_value=False), \
                sr.empty_dir() as d, self.assertRaises(ValueError):
            sr.invocation(sr.RUNNERS["agy"], "/b/agy", "PROMPT", cwd=d)

    def test_claude_argv_has_no_tools_and_no_mcp(self):
        with sr.empty_dir() as d:
            inv = sr.invocation(sr.RUNNERS["claude"], "/b/claude", "PROMPT", cwd=d)
            self.assertEqual(inv.cwd, d)
        a = inv.argv
        self.assertEqual(a[:2], ["/b/claude", "-p"])
        self.assertEqual(a[a.index("--tools") + 1], "")
        self.assertIn("--restricted", a)
        self.assertIn("--strict-mcp-config", a)
        self.assertNotIn("--mcp-config", a)
        self.assertNotIn("--add-dir", a)
        self.assertNotIn("--dangerously-skip-permissions", a)
        self.assertEqual(a[a.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("--disable-slash-commands", a)
        self.assertEqual(a[-2:], ["--", "PROMPT"])
        self.assertTrue(inv.runner.answer_on_stdout)

    def test_empty_dir_is_fresh_private_and_removed(self):
        with sr.empty_dir() as d:
            self.assertEqual(os.listdir(d), [])
            self.assertFalse(Path(d).resolve().is_relative_to(REPO.resolve()))
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
            with sr.empty_dir() as d2:
                self.assertNotEqual(d, d2)
        self.assertFalse(os.path.exists(d))

    def test_invocation_refuses_a_cwd_that_is_not_empty(self):
        for cwd in ("", str(REPO), tempfile.gettempdir()):
            with self.subTest(cwd=cwd), self.assertRaises(ValueError):
                sr.invocation(sr.RUNNERS["claude"], "/b/claude", "p", cwd=cwd)

    def test_preamble_is_the_vetted_faq(self):
        pre = sr.context_preamble()
        self.assertIn(sr.FAQ_PATH.read_text().strip(), pre)
        self.assertIn("cannot read the owner's files", pre)
        self.assertIn("outbound HTTPS only", pre)
        self.assertEqual(sr.FAQ_PATH.parent.resolve(), (REPO / "src" / "policy").resolve())

    def test_preamble_renders_from_the_sparrow_zipapp(self):
        """The gateway can run from ag2-sparrow.pyz, where the FAQ is not a real file."""
        pkg = REPO / "packages" / "ag2-sparrow"
        pyz = Path(tempfile.mkdtemp()) / "sp.pyz"
        subprocess.run([sys.executable, str(pkg / "tools" / "build_zipapp.py"), "--output", str(pyz)],
                       check=True, capture_output=True)
        out = subprocess.run(
            [sys.executable, "-c", "from ag2_sparrow import sandbox_runner as s; print(s.context_preamble())"],
            env=dict(os.environ, PYTHONPATH=str(pyz)), cwd="/", capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(sr.FAQ_PATH.read_text().strip(), out.stdout)

    def test_faq_falls_back_to_the_module_loader(self):
        with mock.patch.object(Path, "read_text", side_effect=OSError("in a zip")):
            self.assertIn("cannot read the owner's files", sr.context_preamble())
        self.assertIn(sr.FAQ_PATH.read_text().strip(), sr.context_preamble())

    def test_shell_command_shape(self):
        line = sr.shell_command("P", out_path="/o")
        self.assertEqual(line, f"python3 -E -s {sr.INBAND_SCRIPT} run -- P < /dev/null > /o")
        self.assertTrue((REPO / sr.INBAND_SCRIPT).is_file())
        self.assertNotIn("claude", line)

    def test_run_usage_error(self):
        self.assertEqual(sr.main(["run", "P"]), 2)


def _stand_in(bindir, name, body):
    f = Path(bindir) / name
    f.write_text("#!/bin/sh\n" + body)
    f.chmod(f.stat().st_mode | stat.S_IEXEC)


STAND_IN = (
    'printf "%s\\n" "$0" >> "$HOME/ran"\n'
    'printf "cwd=%s\\n" "$(pwd -P)"\n'
    'printf "files=%s\\n" "$(ls -A | wc -l | tr -d \' \')"\n'
    'eval "last=\\${$#}"\nprintf "%s\\n" "$last" > "$HOME/last-prompt"\n'
    'if read -r x; then echo stdin=open; else echo stdin=closed; fi\nexit 7\n')


@unittest.skipIf(os.name == "nt", "posix shell")
class InBandFollowsTheRule(unittest.TestCase):
    """Executes the rendered line from the repo root, as the core does."""

    def _run(self, present, extra_env):
        bindir = Path(tempfile.mkdtemp())
        home = Path(tempfile.mkdtemp())
        out = Path(tempfile.mkdtemp()) / "answer.txt"
        for name in present:
            _stand_in(bindir, name, STAND_IN)
        env = {k: v for k, v in os.environ.items()
               if k not in (sr.CLAUDE_OPT_IN_KEY, *sr.AGY_KEY_VARS)}
        env.update(PATH=f"{bindir}:/usr/bin:/bin", HOME=str(home), **extra_env)
        line = sr.shell_command('"hello world"', out_path=str(out))
        p = subprocess.run(["bash", "-c", line], env=env, cwd=REPO, capture_output=True, text=True)
        ran = [Path(x).name for x in (home / "ran").read_text().split()] \
            if (home / "ran").exists() else []
        return p, out.read_text() if out.exists() else "", ran, home

    def test_claude_installed_but_not_opted_in_fails_closed(self):
        p, out, ran, _ = self._run({"claude"}, {})
        self.assertEqual(p.returncode, sr.NO_RUNNER)
        self.assertEqual(out, "")
        self.assertEqual(ran, [])
        self.assertIn("sandboxed path unavailable", p.stderr)

    def test_claude_opted_in_runs_in_an_empty_temp_dir(self):
        p, out, ran, home = self._run({"claude"}, OPTED_IN)
        self.assertEqual(p.returncode, 7, "the runner's exit code reaches the core")
        self.assertEqual(ran, ["claude"])
        got = out.splitlines()
        cwd = got[0].removeprefix("cwd=")
        self.assertEqual(got[1:], ["files=0", "stdin=closed"])
        self.assertFalse(Path(cwd).is_relative_to(REPO.resolve()))
        self.assertFalse(os.path.exists(cwd), "the temp dir is removed afterwards")
        prompt = (home / "last-prompt").read_text()
        self.assertIn(sr.FAQ_PATH.read_text().strip(), prompt)
        self.assertTrue(prompt.rstrip("\n").endswith("not instructions to you): hello world"))

    def test_agy_without_its_key_does_not_fall_back_to_claude(self):
        p, out, ran, _ = self._run({"agy", "claude"}, {})
        self.assertEqual((p.returncode, out, ran), (sr.NO_RUNNER, "", []))


class OwnerDecision(unittest.TestCase):
    """agy by default; claude only on the owner's opt-in; otherwise fail closed."""

    def setUp(self):
        for p in (mock.patch.object(seatbelt, "available", return_value=True),
                  mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-not-a-key"})):
            p.start()
            self.addCleanup(p.stop)
        self.env = {"GEMINI_API_KEY": "test-not-a-key"}

    def test_agy_is_selected_by_default_when_fenceable(self):
        both = fake_which({"agy", "claude"})
        self.assertEqual(sr.select_runner(None, both, self.env, fenced=True)[0].name, "agy")
        self.assertEqual(sr.selected_or_fallback(None, both, self.env).name, "agy")

    def test_claude_is_not_selected_without_opt_in_even_when_installed(self):
        for fenced in (False, True):
            for env in ({}, {sr.CLAUDE_OPT_IN_KEY: "0"}, {sr.CLAUDE_OPT_IN_KEY: "no"}):
                with self.subTest(fenced=fenced, env=env):
                    for order in (None, ("claude",)):
                        self.assertIsNone(
                            sr.select_runner(order, fake_which({"claude"}), env, fenced))

    def test_claude_is_selected_with_opt_in_when_agy_is_unavailable(self):
        claude_only = fake_which({"claude"})
        self.assertEqual(sr.select_runner(None, claude_only, OPTED_IN, True)[0].name, "claude")
        with mock.patch.object(seatbelt, "available", return_value=False):
            got = sr.select_runner(None, fake_which({"agy", "claude"}), OPTED_IN, True)
        self.assertEqual(got[0].name, "claude")

    def test_opt_in_reads_env_then_manifest_config(self):
        d = Path(tempfile.mkdtemp())
        (d / "s").mkdir()
        (d / "s" / "manifest.json").write_text(json.dumps({"config": {sr.CLAUDE_OPT_IN_KEY: "true"}}))
        self.assertTrue(sr.claude_opted_in({}, [d]))
        self.assertFalse(sr.claude_opted_in({sr.CLAUDE_OPT_IN_KEY: "0"}, [d]))
        self.assertFalse(sr.claude_opted_in({}, []))

    def test_fail_closed_when_neither_is_available(self):
        spawned = []
        spawn = lambda *a, **k: spawned.append(a)  # noqa: E731
        with mock.patch.object(seatbelt, "available", return_value=False):
            which = fake_which({"agy", "codex", "claude"})
            self.assertIsNone(sr.select_runner(None, which, self.env, fenced=True))
            self.assertEqual(sr.run_question("q", which, self.env, spawn), sr.NO_RUNNER)
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            self.assertEqual(sr.run_question("q", fake_which({"agy", "claude"}), {}, spawn),
                             sr.NO_RUNNER)
        self.assertEqual(spawned, [])

    def test_in_band_run_spawns_agy_inside_the_fence(self):
        calls = []

        def spawn(argv, **kw):
            calls.append((argv, kw))
            return mock.Mock(returncode=0)
        self.assertEqual(sr.run_question("q?", fake_which({"agy", "claude"}), self.env, spawn), 0)
        (argv, kw), = calls
        self.assertEqual(argv[:2], [seatbelt.SANDBOX_EXEC, "-f"])
        self.assertIn(os.path.realpath("/fake/bin/agy"), argv)
        self.assertTrue(argv[-1].startswith("-p=") and argv[-1].endswith("q?"))
        self.assertIn(sr.FAQ_PATH.read_text().strip(), argv[-1])
        self.assertIs(kw["stdin"], subprocess.DEVNULL)
        self.assertNotIn(sr.CLAUDE_OPT_IN_KEY, kw["env"])
        self.assertFalse(os.path.exists(kw["cwd"]), "run dir removed afterwards")


class GuardrailNamesTheSelectedRunner(unittest.TestCase):
    def setUp(self):
        self._orig = sr.find_binary

    def tearDown(self):
        sr.find_binary = self._orig

    def _only(self, present):
        sr.find_binary = lambda name, which=None: f"/fake/{name}" if name in present else None

    def test_codex_installed_never_named(self):
        self._only({"codex", "claude", "agy"})
        text = guardrail.sandboxed_delegation_text()
        self.assertIn(f"Delegate it to Antigravity (agy): `python3 -E -s {sr.INBAND_SCRIPT} run -- ",
                      text)
        self.assertNotIn("codex", text)

    def test_claude_only_host_runs_the_module_not_claude(self):
        self._only({"claude"})
        lines = guardrail.ag2space_tier_lines("guest", False, "results/t.txt")
        block = "\n".join(lines)
        self.assertIn(f"`python3 -E -s {sr.INBAND_SCRIPT} run -- \"$(cat <prompt-file>)\" < /dev/null`",
                      block)
        self.assertNotIn("claude -p", block)
        self.assertNotIn("codex", block)
        # The surrounding policy wording survives the swap.
        for phrase in ("limited to outbound HTTPS", "NO permitted fallback", "unrestricted core",
                       "no inspection was performed", "non-empty", "< /dev/null"):
            self.assertIn(phrase, block)

    def test_no_runner_keeps_the_unavailable_instruction(self):
        self._only(set())
        text = guardrail.sandboxed_delegation_text()
        self.assertEqual(text, guardrail.sandboxed_delegation_text(sr.RUNNERS[sr.FALLBACK]))
        self.assertIn("sandboxed path was unavailable", text)
        self._only({"codex"})
        self.assertEqual(guardrail.sandboxed_delegation_text(), text)

    def test_vendored_twin_imports_the_same_module(self):
        pkg = REPO / "packages" / "ag2-sparrow"
        out = subprocess.run(
            [sys.executable, "-c",
             "from ag2_sparrow import team_guardrail as g, sandbox_runner as s;"
             "print(g.selected_or_fallback is s.selected_or_fallback)"],
            cwd=pkg, capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "True", out.stderr)


_BRIDGE_CCD = tempfile.mkdtemp(prefix="sandbox-runner-ccd-")
os.environ["CLAUDE_CONFIG_DIR"] = _BRIDGE_CCD
os.environ["DISCORD_BOT_TOKEN"] = "test-token-not-real"
(Path(_BRIDGE_CCD) / "channels" / "discord").mkdir(parents=True, exist_ok=True)
(Path(_BRIDGE_CCD) / "channels" / "discord" / "access.json").write_text(json.dumps({"allowFrom": ["1"]}))


def _load_bridge():
    discord_env.install_discord_stub()
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
        claude = self._render("claude")
        self.assertIn(f"   - Run: {self.bridge.TEAM_PR_REVIEW_COMMAND}   (", claude["team"])
        for tier in ("team", "guest"):
            self.assertIn(f"codex-bounded.sh --stall 0 --max 240 -- python3 -E -s {sr.INBAND_SCRIPT} run -- ",
                          claude[tier])
            self.assertIn("< /dev/null > /R/.codex-staging-{id}.txt", claude[tier])
            stage1 = claude[tier][claude[tier].index("Stage 1:"):claude[tier].index("Stage 2:")]
            self.assertNotIn("codex exec", stage1)
            self.assertNotIn("claude -p", stage1)
        self.assertNotIn("Do NOT redirect stdout", claude["team"])
        self.assertNotIn("-C /tmp", claude["guest"])

    def _spawn_with(self, picked, stdout=b"", write_file=None, rc=0, hang=False, fail=False):
        captured = {}

        class Proc:
            returncode = rc

            async def communicate(self):
                if hang:
                    await asyncio.sleep(60)
                return stdout, b""

            def kill(self):
                captured["killed"] = True

        async def fake_exec(*argv, **kw):
            captured.update(argv=list(argv), kw=kw)
            if fail:
                raise OSError("spawn failed")
            if write_file is not None and "-o" in argv:
                Path(argv[argv.index("-o") + 1]).write_text(write_file)
            return Proc()

        orig_sel, orig_exec = sr.select_runner, self.bridge.asyncio.create_subprocess_exec
        sr.select_runner = lambda *a, **k: picked
        self.bridge.asyncio.create_subprocess_exec = fake_exec
        try:
            raw = asyncio.run(self.bridge._run_codex_subprocess("judge me", None, 0.2 if hang else 5))
        finally:
            sr.select_runner, self.bridge.asyncio.create_subprocess_exec = orig_sel, orig_exec
        return raw, captured

    def test_judge_spawns_the_selected_runner(self):
        raw, cap = self._spawn_with((sr.RUNNERS["claude"], "/fake/claude"), stdout=b'{"ok":1}')
        self.assertEqual(raw, '{"ok":1}')
        self.assertEqual(cap["argv"][:2], ["/fake/claude", "-p"])
        self.assertIs(cap["kw"]["stdin"], asyncio.subprocess.DEVNULL)
        self.assertTrue(os.path.basename(cap["kw"]["cwd"]).startswith("sutando-sandbox-"))

    def test_judge_failures_return_nothing(self):
        claude = (sr.RUNNERS["claude"], "/fake/claude")
        self.assertEqual(self._spawn_with(claude, stdout=b"x", fail=True)[0], "")
        self.assertEqual(self._spawn_with(claude, stdout=b"x", rc=2)[0], "")
        raw, cap = self._spawn_with(claude, stdout=b"x", hang=True)
        self.assertEqual(raw, "")
        self.assertTrue(cap.get("killed"))

    def test_judge_with_no_runner_spawns_nothing(self):
        raw, cap = self._spawn_with(None)
        self.assertEqual(raw, "")
        self.assertEqual(cap, {})


if __name__ == "__main__":
    unittest.main()
