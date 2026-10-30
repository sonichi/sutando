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
                        SEGMENT.search(n.value) or n.value in names):
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


# One owner, connect_common: no launcher-side adapter module runs these steps itself (the tmux launcher
# reads the consumer and occupancy for its own generation classification, which is not the holder sequence).
HOLDER_STEPS = ("task_io.acquire_lock(", "task_io.release_lock(", "task_io.attach_consumer(", "task_io.holder_record_bytes(",
                "cm._run_until_end(")
WORDS = ("task_io.holder_consumer(", "task_io.occupancy(")
SHIM = ("def launchd_exit", "def run_launchd", "signal.signal(sig, forward)")
OWNED_STEPS = {"adapters/claude_code/hold.py": HOLDER_STEPS + WORDS + SHIM,
               "adapters/claude_code/cli.py": HOLDER_STEPS + WORDS + SHIM,
               "adapters/claude_code/launcher.py": HOLDER_STEPS + SHIM,
               "adapters/codex/connect_cli.py": HOLDER_STEPS + WORDS + SHIM}
LAUNCHER_SIDE = tuple(OWNED_STEPS)
DELEGATES = {"adapters/claude_code/hold.py": ("cm.hold_and_supervise(", "cm.supervise_child("),
             "adapters/claude_code/cli.py": ("cm.holder_words(", "cm.occupancy_line(", "cm.run_child_argv("),
             "adapters/codex/connect_cli.py": ("cm.hold_and_supervise(", "cm.supervise_child(", "cm.holder_words(",
                                               "cm.occupancy_line(", "cm.run_launchd(", "cm.run_child_argv(")}


def direct_imports(text, steps):
    """Names a step's `task_io.` or `cm.` function is imported as directly, which the substring scan misses."""
    owned = {step.split(".", 1)[1].rstrip("(") for step in steps if step.startswith(("task_io.", "cm."))}
    return sorted(a.name for n in ast.walk(ast.parse(text)) if isinstance(n, ast.ImportFrom)
                  and (n.module or "").split(".")[-1] in ("task_io", "connect_common")
                  for a in n.names if a.name in owned)


# One owner, connect_bridge: the agent bridge's launch env, its unit or declaration, and connect's
# redemption into the seat. Neither -local adapter builds any of them itself.
BRIDGE_ADAPTERS = ("claude_code", "codex")
BRIDGE_CALLS = {"seat_bridge_environ", "write_bridge_declaration", "remove_bridge_declaration", "render_supervisor",
                "start_supervisor", "stop_supervisor", "write_seat_credentials", "write_seat", "redeem_code",
                "read_code", "relay_problem", "code_problem"}
BRIDGE_ENV_KEYS = {"AGENT_CONNECT_TASK_DIR", "AGENT_CONNECT_RESULT_DIR", "AGENT_CONNECT_STATE_DIR", "AG2_DEVICE_ENV",
                   "REMOTE_TASK_SEAT", "REMOTE_TASK_TOKEN_FILE", "AGENT_MXID"}
BRIDGE_DELEGATES = {"adapters/claude_code/cli.py": ("cb.wire_bridge(", "cb.wire_bridge_bound(", "cb.unwire_bridge(", "cb.redeem_into_seat(",
                                                    "cb.host_problem(", "cb.agent_root("),
                    "adapters/claude_code/shape.py": ("bridge_shape(LOCAL)",),
                    "adapters/codex/connect_cli.py": ("cb.wire_bridge_bound(", "cb.unwire_bridge(", "cb.redeem_into_seat(",
                                                      "cb.host_problem(", "cb.agent_root(", "cb.bridge_shape(LOCAL)")}


def bridge_builders(tree):
    """Calls of a bridge-building step and literal launch-env keys, as (kind, name)."""
    found = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            name = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
            if name in BRIDGE_CALLS:
                found.append(("call", name))
        elif isinstance(n, ast.Constant) and n.value in BRIDGE_ENV_KEYS:
            found.append(("env key", n.value))
    return found


class SharedAgentBridge(unittest.TestCase):
    def test_neither_local_adapter_builds_its_own_bridge_launch_env_or_redemption(self):
        for adapter in BRIDGE_ADAPTERS:
            for path in sorted((PKG_ROOT / PKG / "adapters" / adapter).rglob("*.py")):
                with self.subTest(module=str(path.relative_to(PKG_ROOT))):
                    self.assertEqual(bridge_builders(ast.parse(path.read_text(encoding="utf-8"))), [])

    def test_both_delegate_to_connect_bridge(self):
        for rel, calls in BRIDGE_DELEGATES.items():
            text = (PKG_ROOT / PKG / rel).read_text(encoding="utf-8")
            for call in calls:
                with self.subTest(module=rel, call=call):
                    self.assertIn(call, text)

    def test_the_scan_catches_a_copy(self):
        copy = 'env = {"AGENT_CONNECT_TASK_DIR": t}\nseat = _sources.seat_bridge_environ(p, read)\ncm.start_supervisor(B)\n'
        self.assertEqual(sorted(bridge_builders(ast.parse(copy))), [("call", "seat_bridge_environ"),
                                                                    ("call", "start_supervisor"),
                                                                    ("env key", "AGENT_CONNECT_TASK_DIR")])


class BoundaryTest(unittest.TestCase):
    def test_the_package_has_no_crossings(self):
        self.assertEqual(violations(PKG_ROOT), [])

    def test_the_launcher_side_adapters_delegate_the_holder_sequence_the_occupancy_words_and_the_shim(self):
        for rel in LAUNCHER_SIDE:
            text = (PKG_ROOT / PKG / rel).read_text(encoding="utf-8")
            with self.subTest(module=rel):
                self.assertEqual([step for step in OWNED_STEPS[rel] if step in text], [], "a copy of connect_common's policy")
                self.assertEqual(direct_imports(text, OWNED_STEPS[rel]), [], "a step imported under another name")
                for call in DELEGATES.get(rel, ()):
                    self.assertIn(call, text, "the delegation")

    def test_an_aliased_import_of_an_owned_step_is_caught(self):
        steps = OWNED_STEPS["adapters/codex/connect_cli.py"]
        self.assertEqual(direct_imports("from ...task_io import acquire_lock as take\n", steps), ["acquire_lock"])
        self.assertEqual(direct_imports("from ..connect_common import _run_until_end\n", steps), ["_run_until_end"])
        self.assertEqual(direct_imports("from ... import task_io\nfrom ...task_io import parse\n", steps), [])

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

    def test_prose_mentioning_adapters_is_not_a_path(self):
        root = self.tree({"ag2_sparrow/shared.py": '"""Adapters share this; so adapters need not import."""\n'})
        self.assertEqual(violations(root), [])


if __name__ == "__main__":
    unittest.main()
