"""Package boundaries: no adapter depends on another adapter, and no shared (non-adapter) module
depends on any adapter, by import or by path string. Every module is scanned, subpackages included.

Run:
    python3 packages/ag2-sparrow/tests/test_adapter_boundaries.py
"""
import ast
import re
import tempfile
import unittest
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[1]
PKG = "ag2_sparrow"
CODE = (".py", ".sh", ".ts", ".json")
SEGMENT = re.compile(r"(?:^|[/.\\])adapters(?:[/.\\]|$)")
CROSS = re.compile(r"adapters[/.]([A-Za-z0-9_-]+)|\.\./([A-Za-z0-9_-]+)/")
# Sandbox runner names, not adapter paths: these shared modules name the codex CLI.
RUNNER_NAMES = {("ag2_sparrow.sandbox_runner", "codex"), ("ag2_sparrow.team_guardrail", "codex")}


def modules(root):
    """(dotted module name, path) for every .py under root/ag2_sparrow."""
    for path in sorted((root / PKG).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield ".".join(path.relative_to(root).with_suffix("").parts), path


def resolve(module, node):
    """Absolute dotted name an ImportFrom refers to, from inside `module`."""
    if node.level == 0:
        return node.module or ""
    base = module.split(".")[:-node.level]
    return ".".join(base + ([node.module] if node.module else []))


def imports(module, tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            target = resolve(module, node)
            yield target
            for a in node.names:
                yield "%s.%s" % (target, a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                yield a.name


def adapter_of(dotted):
    parts = dotted.split(".")
    if parts[:2] == [PKG, "adapters"] and len(parts) > 2 and parts[2] != "__init__":
        return parts[2]
    return None


def adapter_dirs(root):
    dirs = [p for p in (root / PKG / "adapters").iterdir() if p.is_dir() and p.name != "__pycache__"]
    extra = root / "adapters"
    if extra.is_dir():
        dirs += [p for p in extra.iterdir() if p.is_dir()]
    return sorted(dirs)


def violations(root):
    """Every boundary crossing under root, as (kind, where, what)."""
    names = {p.name for p in adapter_dirs(root)}
    found = []
    for module, path in modules(root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        own = adapter_of(module)
        for name in imports(module, tree):
            other = adapter_of(name)
            if other and other != own:
                found.append(("import", module, name))
        if own is None:
            for n in ast.walk(tree):
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and (
                        SEGMENT.search(n.value) or n.value in names) \
                        and (module, n.value) not in RUNNER_NAMES:
                    found.append(("shared path", module, n.value[:80]))
    for adapter in adapter_dirs(root):
        for path in sorted(p for p in adapter.rglob("*") if p.suffix in CODE and "__pycache__" not in p.parts):
            where = str(path.relative_to(root))
            for m in CROSS.finditer(path.read_text(encoding="utf-8", errors="replace")):
                other = m.group(1) or m.group(2)
                if other in names and other != adapter.name:
                    found.append(("adapter path", where, m.group(0)))
            if path.suffix == ".py":
                for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    if isinstance(n, ast.Constant) and n.value in names and n.value != adapter.name:
                        found.append(("adapter path", where, n.value))
    return sorted(set(found))


class BoundaryTest(unittest.TestCase):
    def test_the_package_has_no_crossings(self):
        self.assertEqual(violations(PKG_ROOT), [])

    def test_the_scan_sees_every_module_and_resolves_relative_imports(self):
        seen = dict(modules(PKG_ROOT))
        self.assertIn("ag2_sparrow.adapters.hatch.task_fields", seen)
        self.assertIn("ag2_sparrow.delivery_core.__init__", seen, "subpackages are scanned")
        tree = ast.parse(seen["ag2_sparrow.adapters.claude_routine.routine_adapter"].read_text())
        names = set(imports("ag2_sparrow.adapters.claude_routine.routine_adapter", tree))
        self.assertIn("ag2_sparrow.relay_loop", names)
        self.assertIn("ag2_sparrow.task_io", names)
        self.assertIn("claude-code", {p.name for p in adapter_dirs(PKG_ROOT)})


class PositiveControlTest(unittest.TestCase):
    """Each forbidden shape, planted in a throwaway tree, is reported."""

    def tree(self, files):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        root = Path(d.name)
        base = {"ag2_sparrow/__init__.py": "", "ag2_sparrow/adapters/__init__.py": "",
                "ag2_sparrow/adapters/a/__init__.py": "", "ag2_sparrow/adapters/b/__init__.py": "",
                "ag2_sparrow/adapters/b/z.py": "", "ag2_sparrow/sub/__init__.py": ""}
        for rel, text in {**base, **files}.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        return root

    def test_clean_tree_is_clean(self):
        root = self.tree({"ag2_sparrow/shared.py": "import os\n",
                          "ag2_sparrow/adapters/a/m.py": "from ...shared import os\n"})
        self.assertEqual(violations(root), [])

    def test_adapter_to_adapter_import_is_caught(self):
        root = self.tree({"ag2_sparrow/adapters/a/m.py": "from ..b import z\n"})
        self.assertIn(("import", "ag2_sparrow.adapters.a.m", "ag2_sparrow.adapters.b"), violations(root))

    def test_shared_to_adapter_import_is_caught_in_a_subpackage_and_absolute(self):
        root = self.tree({"ag2_sparrow/sub/deep.py": "def f():\n    from ..adapters.b import z\n",
                          "ag2_sparrow/plain.py": "import ag2_sparrow.adapters.b.z\n"})
        found = violations(root)
        self.assertIn(("import", "ag2_sparrow.sub.deep", "ag2_sparrow.adapters.b"), found)
        self.assertIn(("import", "ag2_sparrow.plain", "ag2_sparrow.adapters.b.z"), found)

    def test_shared_path_string_is_caught(self):
        root = self.tree({"ag2_sparrow/sub/paths.py":
                          "import os\nP = os.path.join(os.path.dirname(__file__), 'adapters', 'b', 'x.sh')\n"})
        kinds = {(k, m) for k, m, _ in violations(root)}
        self.assertIn(("shared path", "ag2_sparrow.sub.paths"), kinds)

    def test_adapter_path_to_another_adapter_is_caught(self):
        root = self.tree({"ag2_sparrow/adapters/a/m.py": "S = '../b/x.sh'\nT = 'b'\n",
                          "ag2_sparrow/adapters/a/hook.sh": "python -m ag2_sparrow.adapters.b.z\n"})
        found = violations(root)
        self.assertIn(("adapter path", "ag2_sparrow/adapters/a/m.py", "../b/"), found)
        self.assertIn(("adapter path", "ag2_sparrow/adapters/a/m.py", "b"), found)
        self.assertIn(("adapter path", "ag2_sparrow/adapters/a/hook.sh", "adapters.b"), found)

    def test_runner_names_pass_only_in_their_two_modules(self):
        codex = {"ag2_sparrow/adapters/codex/__init__.py": ""}
        allowed = {"ag2_sparrow/sandbox_runner.py": "R = 'codex'\n",
                   "ag2_sparrow/team_guardrail.py": "L = {'codex': 'Codex'}\n"}
        self.assertEqual(violations(self.tree({**codex, **allowed})), [])
        for rel, text in (("ag2_sparrow/other.py", "R = 'codex'\n"),
                          ("ag2_sparrow/sub/sandbox_runner.py", "R = 'codex'\n"),
                          ("ag2_sparrow/sandbox_runner.py", "R = 'b'\n"),
                          ("ag2_sparrow/team_guardrail.py", "P = 'adapters/codex/x.sh'\n")):
            with self.subTest(rel=rel, text=text):
                found = violations(self.tree({**codex, rel: text}))
                self.assertIn("shared path", {k for k, _, _ in found})

    def test_prose_mentioning_adapters_is_not_a_path(self):
        root = self.tree({"ag2_sparrow/shared.py": '"""Adapters share this; so adapters need not import."""\n'})
        self.assertEqual(violations(root), [])


if __name__ == "__main__":
    unittest.main()
