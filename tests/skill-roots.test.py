#!/usr/bin/env python3
"""src/skill_roots.py — the one scan of the installed-skill roots: both roots derive from the
repo's workspace helper, the field is the caller's, a declaration must stay inside its skill,
and two declarers (one per root included) are a refusal, not a pick.

Run: python3 tests/skill-roots.test.py
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import skill_roots  # noqa: E402

FIELD = "example_script"


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skill-roots-"))
        self.ws = self.tmp / "workspace"
        self.engine = self.tmp / "engine-skills"
        self.engine.mkdir()
        (self.ws / "skills").mkdir(parents=True)

    def skill(self, root, name, manifest, script="scripts/run.py"):
        d = root / name
        (d / "scripts").mkdir(parents=True)
        if script:
            (d / script).write_text("x = 1\n")
        (d / "manifest.json").write_text(manifest if isinstance(manifest, str) else json.dumps(manifest))
        return d


class Roots(_Tmp):
    def test_the_two_roots_come_from_the_workspace_helper(self):
        self.assertEqual(skill_roots.skill_roots(self.ws), [skill_roots.REPO_SKILLS, self.ws / "skills"])
        with mock.patch("workspace_default.resolve_workspace", return_value=self.ws) as rw:
            self.assertEqual(skill_roots.skill_roots(), [skill_roots.REPO_SKILLS, self.ws / "skills"])
        rw.assert_called_once_with(migrate=False)
        self.assertEqual(skill_roots.REPO_SKILLS, REPO / "skills")
        src = (REPO / "src" / "skill_roots.py").read_text()
        self.assertNotRegex(src, r"SUTANDO_WORKSPACE|expanduser|\.sutando|environ", "no hand-rolled fallback")

    def test_the_same_directory_is_scanned_once(self):
        with mock.patch.object(skill_roots, "REPO_SKILLS", self.ws / "skills"):
            self.assertEqual(skill_roots.skill_roots(self.ws), [self.ws / "skills"])
        link = self.tmp / "link"
        os.symlink(self.ws, link)
        with mock.patch.object(skill_roots, "REPO_SKILLS", link / "skills"):
            self.assertEqual(skill_roots.skill_roots(self.ws), [link / "skills"])


class Declarations(_Tmp):
    def test_only_an_enabled_contained_declaration_counts(self):
        self.skill(self.engine, "off", {"enabled": False, FIELD: "scripts/run.py"})
        self.skill(self.engine, "escapes", {FIELD: "../off/scripts/run.py"})
        self.skill(self.engine, "missing", {FIELD: "scripts/none.py"}, script=None)
        self.skill(self.engine, "other-field", {"other": "scripts/run.py"})
        self.skill(self.engine, "broken", "{not json")
        self.skill(self.engine, "not-a-dict", "[1, 2]")
        self.assertEqual(skill_roots.declared_scripts(FIELD, self.engine), [])
        ok = self.skill(self.engine, "ok", {FIELD: "scripts/run.py"})
        self.assertEqual(skill_roots.declared_scripts(FIELD, self.engine), [("ok", (ok / "scripts" / "run.py").resolve())])
        self.assertEqual(skill_roots.declared_scripts(FIELD, [self.engine]), skill_roots.declared_scripts(FIELD, self.engine))
        self.assertEqual(skill_roots.declared_script(FIELD, self.engine), (ok / "scripts" / "run.py").resolve())
        self.assertIsNone(skill_roots.declared_script(FIELD, self.ws / "skills"))

    def test_a_declarer_in_each_root_is_a_conflict_and_the_workspace_one_alone_is_picked(self):
        mine = self.skill(self.ws / "skills", "mine", {FIELD: "scripts/run.py"})
        with mock.patch.object(skill_roots, "REPO_SKILLS", self.engine):
            self.assertEqual(skill_roots.declared(FIELD, self.ws),
                             skill_roots.Declaration((mine / "scripts" / "run.py").resolve(), None))
            self.skill(self.engine, "shipped", {FIELD: "scripts/run.py"})
            with self.assertRaisesRegex(skill_roots.DeclarationConflict, f"{FIELD}: mine, shipped; refusing to pick one"):
                skill_roots.declared_script(FIELD, skill_roots.skill_roots(self.ws))
            decl = skill_roots.declared(FIELD, self.ws)
            self.assertIsNone(decl.script)
            self.assertIn("mine, shipped", decl.reason)
            self.assertEqual(skill_roots.declared(FIELD, self.ws, override=self.tmp / "x.py"),
                             skill_roots.Declaration(self.tmp / "x.py", None), "an override is taken as given")
            self.assertEqual(skill_roots.declared(FIELD, roots=self.engine).script, (self.engine / "shipped" / "scripts" / "run.py").resolve())

    def test_the_same_skill_name_in_both_roots_is_shadowed_not_a_conflict(self):
        """install.sh's rule: the shipped copy wins a name collision; the owner's copy of the same
        name is skipped, and a plain symlink of the shipped skill into the workspace root changes
        nothing. A different name in the other root is still a conflict."""
        shipped = self.skill(self.engine, "pq", {FIELD: "scripts/run.py"})
        os.symlink(shipped, self.ws / "skills" / "pq")
        with mock.patch.object(skill_roots, "REPO_SKILLS", self.engine):
            self.assertEqual(skill_roots.declared(FIELD, self.ws),
                             skill_roots.Declaration((shipped / "scripts" / "run.py").resolve(), None))
        (self.ws / "skills" / "pq").unlink()
        own = self.skill(self.ws / "skills", "pq", {FIELD: "scripts/other.py"}, script="scripts/other.py")
        with mock.patch.object(skill_roots, "REPO_SKILLS", self.engine):
            d = skill_roots.declared(FIELD, self.ws)
        self.assertEqual(d.script, (shipped / "scripts" / "run.py").resolve())
        self.assertNotEqual(d.script, (own / "scripts" / "other.py").resolve())
        self.assertEqual([n for n, _ in skill_roots.declared_scripts(FIELD, [self.ws / "skills", self.engine])],
                         ["pq"], "root order is the precedence, whichever root comes first")

    def test_two_declarers_in_one_root_are_a_conflict_not_an_alphabetical_pick(self):
        self.skill(self.engine, "aaa", {FIELD: "scripts/run.py"})
        self.skill(self.engine, "zzz", {FIELD: "scripts/run.py"})
        with self.assertRaisesRegex(skill_roots.DeclarationConflict, "aaa, zzz"):
            skill_roots.declared_script(FIELD, self.engine)
        self.assertEqual(skill_roots.declared(FIELD, roots=[self.engine]).script, None)


if __name__ == "__main__":
    unittest.main()
