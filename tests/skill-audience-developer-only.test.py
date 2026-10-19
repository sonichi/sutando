#!/usr/bin/env python3
"""Developer-only skills (`manifest.json` `audience`) stay out of ordinary installs.

Run: python3 tests/skill-audience-developer-only.test.py

Pins the rule (src/skill_audience.py), the `skills.developer` opt-in, skills/install.sh end to
end in a throwaway repo (both roots, Claude and Codex targets), health-check's skill-symlinks
--fix, and that every installer delegates to the one helper instead of reading `audience` itself.
Hermetic: temp dirs only, a minimal environment, no live workspace or Claude home.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import skill_audience as sa  # noqa: E402
import sutando_config  # noqa: E402


def make_skill(root: Path, name: str, manifest=None, raw: str | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"# {name}\n")
    if raw is not None:
        (d / "manifest.json").write_text(raw)
    elif manifest is not None:
        (d / "manifest.json").write_text(json.dumps(manifest))
    return d


class AudienceRule(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_values(self):
        cases = {
            "no-manifest": (None, None, "user"),
            "no-field": ({"name": "x"}, None, "user"),
            "user": ({"audience": "user"}, None, "user"),
            "dev": ({"audience": "developer"}, None, "developer"),
            "typo": ({"audience": "develoepr"}, None, "developer"),
            "upper": ({"audience": "Developer"}, None, "developer"),
            "empty": ({"audience": ""}, None, "developer"),
            "null": ({"audience": None}, None, "developer"),
            "list": ({"audience": ["user"]}, None, "developer"),
            "broken": (None, "{not json", "developer"),
            "array": (None, "[]", "developer"),
        }
        for name, (manifest, raw, want) in cases.items():
            d = make_skill(self.root, name, manifest, raw)
            self.assertEqual(sa.audience(d).value, want, name)

    def test_installable_honours_the_opt_in(self):
        user = make_skill(self.root, "u", {"audience": "user"})
        dev = make_skill(self.root, "d", {"audience": "developer"})
        typo = make_skill(self.root, "t", {"audience": "dev"})
        self.assertIsNone(sa.installable(user, False).reason)
        self.assertEqual(sa.installable(dev, False).reason, "developer-only")
        self.assertIn("unknown audience", sa.installable(typo, False).reason)
        for d in (user, dev, typo):
            self.assertIsNone(sa.installable(d, True).reason)
        self.assertEqual([n for n, _ in sa.excluded([user, dev, typo], False)], ["d", "t"])
        self.assertEqual(sa.excluded([user, dev, typo], True), [])


class OptInKey(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("SUTANDO_")}
        os.environ["SUTANDO_TEST_MODE"] = "1"
        os.environ["SUTANDO_WORKSPACE"] = str(self.root / "ws")

    def tearDown(self):
        for k in [k for k in os.environ if k.startswith("SUTANDO_")]:
            del os.environ[k]
        os.environ.update(self.saved)
        sutando_config._reset_cache_for_tests()
        self.tmp.cleanup()

    def resolve(self, cfg) -> bool:
        sutando_config._reset_cache_for_tests()
        text = cfg if isinstance(cfg, str) else json.dumps(cfg)
        (self.root / "sutando.config.json").write_text(text)
        return sutando_config.resolve_skills_developer(self.root)

    def test_only_json_true_opts_in(self):
        self.assertFalse(self.resolve({}))
        self.assertFalse(self.resolve({"skills": {}}))
        self.assertTrue(self.resolve({"skills": {"developer": True}}))
        self.assertFalse(self.resolve({"skills": {"developer": False}}))
        self.assertFalse(self.resolve({"skills": {"developer": "true"}}))
        self.assertFalse(self.resolve({"skills": {"developer": 1}}))
        self.assertFalse(self.resolve("{broken"))

    def test_the_workspace_layer_opts_in(self):
        (self.root / "ws").mkdir()
        (self.root / "ws" / "sutando.config.local.json").write_text(
            json.dumps({"skills": {"developer": True}}))
        self.assertTrue(self.resolve({}))

    def test_skills_is_a_known_object_key(self):
        self.assertIn("skills", sutando_config._KNOWN_TOP_LEVEL_KEYS)
        self.assertIn("skills", sutando_config._OBJECT_TOP_LEVEL_KEYS)


class HelperInProcess(unittest.TestCase):
    """main() and developer_opt_in() run in-process, against a temp repo's config."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.repo = t / "repo"
        self.repo.mkdir()
        self.dev = make_skill(t / "skills", "dev-tool", {"audience": "developer"})
        self.typo = make_skill(t / "skills", "typo-tool", {"audience": "devloper"})
        self.plain = make_skill(t / "skills", "plain")
        self.saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("SUTANDO_")}
        os.environ["SUTANDO_TEST_MODE"] = "1"
        os.environ["SUTANDO_WORKSPACE"] = str(t / "ws")

    def tearDown(self):
        for k in [k for k in os.environ if k.startswith("SUTANDO_")]:
            del os.environ[k]
        os.environ.update(self.saved)
        sutando_config._reset_cache_for_tests()
        self.tmp.cleanup()

    def configure(self, developer):
        sutando_config._reset_cache_for_tests()
        (self.repo / "sutando.config.json").write_text(json.dumps({"skills": {"developer": developer}}))

    def run_main(self, argv) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = sa.main(argv, repo_root=self.repo)
        return rc, out.getvalue(), err.getvalue()

    def test_excluded_without_opt_in(self):
        self.configure(False)
        rc, out, _ = self.run_main(["excluded", str(self.dev), str(self.typo), str(self.plain)])
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), [
            "dev-tool\tdeveloper-only",
            "typo-tool\tunknown audience 'devloper'; treated as developer-only"])

    def test_excluded_with_opt_in(self):
        self.configure(True)
        self.assertEqual(self.run_main(["excluded", str(self.dev), str(self.plain)]), (0, "", ""))

    def test_usage(self):
        for argv in ([], ["other"]):
            rc, out, err = self.run_main(argv)
            self.assertEqual((rc, out), (2, ""), argv)
            self.assertIn("usage: skill_audience.py excluded", err)

    def test_developer_opt_in_reads_the_given_repo(self):
        self.configure(True)
        self.assertTrue(sa.developer_opt_in(self.repo))
        self.configure(False)
        self.assertFalse(sa.developer_opt_in(self.repo))

    def test_developer_opt_in_puts_src_on_the_path(self):
        src = str(REPO / "src")
        saved = list(sys.path)
        sys.path[:] = [p for p in sys.path if p != src]
        try:
            self.configure(True)
            self.assertTrue(sa.developer_opt_in(self.repo))
            self.assertIn(src, sys.path)
        finally:
            sys.path[:] = saved


class InstallShEndToEnd(unittest.TestCase):
    """skills/install.sh, copied with its helpers into a throwaway repo with fake skills."""

    COPIED = ("skills/install.sh", "scripts/sutando-config.sh", "scripts/python-binary.sh",
              "src/sutando_config.py", "src/skill_audience.py")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.repo, self.ws, self.home = t / "repo", t / "ws", t / "home"
        for rel in self.COPIED:
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, self.repo / rel)
        self.write_repo_config({})
        skills = self.repo / "skills"
        make_skill(skills, "plain")
        make_skill(skills, "for-users", {"audience": "user"})
        make_skill(skills, "dev-tool", {"audience": "developer"})
        make_skill(skills, "typo-tool", {"audience": "devloper"})
        wss = self.ws / "skills"
        make_skill(wss, "my-skill")
        make_skill(wss, "my-dev-skill", {"audience": "developer"})
        self.home.mkdir()
        self.ccd = t / "ccd"
        self.codex = t / "codex"

    def tearDown(self):
        self.tmp.cleanup()

    def write_repo_config(self, cfg: dict):
        (self.repo / "sutando.config.json").write_text(json.dumps(cfg))

    def run_install(self) -> str:
        env = {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "HOME": str(self.home),
               "CLAUDE_CONFIG_DIR": str(self.ccd), "CODEX_HOME": str(self.codex),
               "SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(self.ws),
               "SUTANDO_PY": sys.executable}
        out = subprocess.run(["bash", str(self.repo / "skills" / "install.sh")], env=env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return out.stdout

    def linked(self, target: Path) -> set:
        return {p.name for p in target.iterdir() if p.is_symlink()} if target.exists() else set()

    def test_default_install_leaves_developer_skills_out(self):
        out = self.run_install()
        self.assertEqual(self.linked(self.ccd / "skills"), {"plain", "for-users", "my-skill"})
        self.assertIn("dev-tool (developer-only; not installed", out)
        self.assertIn("typo-tool (unknown audience 'devloper'", out)
        self.assertIn("my-dev-skill (workspace skill, developer-only; not installed", out)

    def test_codex_target_is_gated_too(self):
        self.write_repo_config({"core": {"runtime": "codex"}})
        self.run_install()
        self.assertEqual(self.linked(self.codex / "skills"), {"plain", "for-users", "my-skill"})

    def test_opt_in_installs_them(self):
        (self.ws / "sutando.config.local.json").write_text(json.dumps({"skills": {"developer": True}}))
        self.run_install()
        self.assertEqual(self.linked(self.ccd / "skills"),
                         {"plain", "for-users", "dev-tool", "typo-tool", "my-skill", "my-dev-skill"})

    def test_a_string_opt_in_is_not_an_opt_in(self):
        self.write_repo_config({"skills": {"developer": "true"}})
        self.run_install()
        self.assertNotIn("dev-tool", self.linked(self.ccd / "skills"))

    def test_an_already_installed_developer_skill_is_left_with_a_note(self):
        (self.ccd / "skills").mkdir(parents=True)
        (self.ccd / "skills" / "dev-tool").symlink_to(self.repo / "skills" / "dev-tool")
        out = self.run_install()
        self.assertTrue((self.ccd / "skills" / "dev-tool").is_symlink())
        self.assertIn("dev-tool (developer-only; already installed, left in place", out)


class HealthCheckFix(unittest.TestCase):
    """skill-symlinks must not report a developer skill as unlinked, so --fix never links it."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
        cls.hc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.hc)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.src, self.dst = t / "skills", t / "dst"
        make_skill(self.src, "plain")
        make_skill(self.src, "dev-tool", {"audience": "developer"})
        self.dst.mkdir()
        self.saved = (self.hc.REPO_DIR, self.hc.claude_home_path, sa.developer_opt_in)
        self.hc.REPO_DIR = t
        self.hc.claude_home_path = lambda *p: self.dst.joinpath(*p[1:]) if p and p[0] == "skills" else t

    def tearDown(self):
        self.hc.REPO_DIR, self.hc.claude_home_path, sa.developer_opt_in = self.saved
        self.tmp.cleanup()

    def check_and_fix(self, developer: bool) -> set:
        sa.developer_opt_in = lambda *_a, **_k: developer
        result = self.hc.check_skill_symlinks()
        self.hc.fix_skill_symlinks(result)
        return {p.name for p in self.dst.iterdir()}

    def test_without_opt_in(self):
        self.assertEqual(self.check_and_fix(False), {"plain"})

    def test_with_opt_in(self):
        self.assertEqual(self.check_and_fix(True), {"plain", "dev-tool"})


class LintRefusesUnsafeManifests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("lint_skill", REPO / "scripts" / "lint-skill.py")
        cls.lint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.lint)

    def errors(self, extra: dict) -> list:
        with tempfile.TemporaryDirectory() as t:
            m = {"name": "x", "version": "0.1.0", "owner": "o", "stability": "experimental", **extra}
            d = make_skill(Path(t), "x", m)
            (d / "tools.ts").write_text("export const tools = [];\n")
            return self.lint._lint_manifest(d)[0]

    def test_audience_values(self):
        self.assertEqual(self.errors({"audience": "user"}), [])
        self.assertEqual(self.errors({"audience": "developer"}), [])
        self.assertTrue(any("not in" in e for e in self.errors({"audience": "devloper"})))

    def test_developer_skill_may_not_declare_runtime_activated_fields(self):
        tools = {"tools": "./tools.ts", "enabled": True, "access_tier": "owner"}
        self.assertEqual(self.errors(tools), [])
        self.assertTrue(any("'tools'" in e for e in self.errors({**tools, "audience": "developer"})))
        self.assertTrue(any("'documented_for_core'" in e
                            for e in self.errors({"audience": "developer", "documented_for_core": True})))


class EveryInstallerDelegates(unittest.TestCase):
    def test_installers_call_the_helper(self):
        for rel in ("skills/install.sh", "skills/install.ps1"):
            text = (REPO / rel).read_text()
            self.assertIn("src/skill_audience.py", text, rel)
            self.assertIn("excluded", text, rel)
        hc = (REPO / "src" / "health-check.py").read_text()
        self.assertIn("from skill_audience import developer_opt_in, installable", hc)

    def test_no_other_code_reads_the_audience_field(self):
        # "audience" means other things elsewhere; a manifest reader that names it is the drift.
        allowed = {"src/skill_audience.py", "scripts/lint-skill.py"}
        grep = ["git", "-C", str(REPO), "grep", "-l"]
        scope = ["--", "src", "scripts", "skills", ":!*.md", ":!*.json"]
        names = lambda pat: set(subprocess.run(grep + ["-E", pat] + scope, capture_output=True,
                                               text=True).stdout.split())
        found = (names(r"""["']audience["']""") & names("manifest")) - allowed
        self.assertEqual(found, set(), "read the audience through src/skill_audience.py")

if __name__ == "__main__":
    unittest.main(verbosity=1)
