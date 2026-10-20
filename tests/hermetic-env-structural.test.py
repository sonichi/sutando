#!/usr/bin/env python3
"""No test that reaches gateway credentials inherits the outer environment.

A test file (under tests/ or packages/*/tests, or a src/*.test.py or skills/*/test_*.py) that names the remote gateway bridge, Hatch,
room_ops' gateway client, the credential modules, or any module tests/_helpers/credential_scope.py
finds reading a credential name (or importing one that does) in its code, not a comment or docstring,
must build every environment with tests/_helpers/hermetic_env.py. Refused anywhere else in such a file:

  copy        os.environ.copy(), dict(os.environ, ...), {**os.environ, ...}, f(**os.environ), a
              dict comprehension over os.environ, os.environ | {...}, x.update(os.environ) or
              copy.copy/deepcopy(os.environ) (a denylist still carries every other selector);
              aliases (`import os as o`, `from os import environ`) resolve through the imports
  spawn-env   a subprocess handed env=None or env=os.environ, or an os.exec*e/spawn*e handed os.environ
  env-param   a subprocess handed an `env` parameter that defaults to None and is never rebuilt
  unisolated  code run (a subprocess or os/asyncio spawn, a module loader, an import of those
              modules) in a test file that never calls isolate_os_environ() or isolate_then_import()
              as a module-level statement (a tests/_helpers library may instead isolate in the
              function, or spawn with the helper's env); a call under `if` does not isolate, and
              a clearing mock.patch.dict never counts: its lifetime is not the spawn's
  before-isolation  such code at module level above that call
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
import unittest
from pathlib import Path

from _helpers.credential_scope import credential_modules, reference_pattern, target_pattern
from _helpers.hermetic_env import hermetic_env, isolate_os_environ

isolate_os_environ()

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "tests" / "_helpers" / "hermetic_env.py"
# The regression's instrument: it must run its target unisolated and snapshot os.environ to see a leak.
AUDITOR = REPO / "tests" / "_helpers" / "sentinel_audit.py"

REFERENCE = re.compile(
    r"remote_gateway_bridge|remote-gateway-bridge|hermetic_gateway|_helpers\.gateway_relay|older_bridge"
    r"|gateway_credential|ag2_gateway_credentials|adapters[./]hatch|agent-room-ops"
    r"|import room_ops|import _gateway|" + reference_pattern().pattern)
TARGET = re.compile(
    r"remote_gateway_bridge|remote-gateway-bridge|gateway_credential|ag2_gateway_credentials"
    r"|adapters\.hatch|adapters/hatch|agent-room-ops|^room_ops$|^_gateway$|(^|\.)room_ops$|(^|\.)_gateway$|"
    + target_pattern().pattern)
ISOLATORS = {"isolate_os_environ", "isolate_then_import"}
BUILDERS = ("hermetic_env(", "isolated_child_env(")
# room_ops' skill modules are imported by bare name once its directory is on sys.path.
ROOM_OPS_MODULES = {p.stem for p in (REPO / "skills" / "agent-room-ops").glob("*.py")}
SPAWNS = {"run", "Popen", "check_output", "check_call", "call"}
LOADERS = {"import_module", "spec_from_file_location", "exec_module", "run_path", "run_module", "__import__"}


MODULES = ("os", "copy", "asyncio", "subprocess")
OS_SPAWNS = ("system", "popen", "exec", "spawn", "posix_spawn")
ASYNC_SPAWNS = {"create_subprocess_exec", "create_subprocess_shell"}


class Imports:
    """The names a file binds to os, copy, asyncio, subprocess and their members, `as` names included."""

    def __init__(self, tree):
        self.mods = {m: {m} for m in MODULES}
        self.members = {"Popen": "subprocess.Popen"}
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    if a.name in MODULES and a.asname:
                        self.mods[a.name].add(a.asname)
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module in MODULES:
                for a in n.names:
                    self.members[a.asname or a.name] = f"{n.module}.{a.name}"

    def qual(self, n) -> "str | None":
        if isinstance(n, ast.Name):
            return self.members.get(n.id) or next((m for m, ns in self.mods.items() if n.id in ns), None)
        if isinstance(n, ast.Attribute) and self.qual(n.value) in MODULES:
            return f"{self.qual(n.value)}.{n.attr}"
        return None

    def environ(self, n) -> bool:
        return self.qual(n) == "os.environ"

    def spawn(self, call: ast.Call) -> bool:
        q = self.qual(call.func) or ""
        mod, _, attr = q.partition(".")
        return ((mod == "subprocess" and attr in SPAWNS) or (mod == "os" and attr.startswith(OS_SPAWNS))
                or (mod == "asyncio" and attr in ASYNC_SPAWNS))


def _name(f) -> str:
    return getattr(f, "attr", None) or getattr(f, "id", "") or ""


def _over_environ(it, imp: Imports) -> bool:
    return imp.environ(it) or (isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute)
                               and it.func.attr in ("items", "keys") and imp.environ(it.func.value))


def _imports_target(node) -> bool:
    if isinstance(node, ast.Import):
        return any(TARGET.search(a.name) or a.name in ROOM_OPS_MODULES for a in node.names)
    if isinstance(node, ast.ImportFrom):
        mod = node.module or ""
        return (bool(TARGET.search(mod)) or (node.level == 0 and mod in ROOM_OPS_MODULES)
                or any(TARGET.search(f"{mod}.{a.name}") or TARGET.search(a.name) for a in node.names))
    if isinstance(node, ast.Call) and _name(node.func) in LOADERS:
        return bool(TARGET.search(ast.unparse(node)))
    return False


def code_text(tree) -> str:
    """The file's code without comments or docstrings, so only names in code put it in scope."""
    for n in ast.walk(tree):
        if (isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body
                and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)
                and isinstance(n.body[0].value.value, str)):
            n.body[0] = ast.Pass()
    return ast.unparse(tree)


def _copy_rule(n, imp: Imports) -> bool:
    """A node that copies os.environ into a new mapping."""
    if isinstance(n, ast.Call):
        f, q = n.func, imp.qual(n.func)
        return ((isinstance(f, ast.Attribute) and f.attr == "copy" and imp.environ(f.value))
                or (isinstance(f, ast.Name) and f.id == "dict" and bool(n.args) and imp.environ(n.args[0]))
                or any(k.arg is None and imp.environ(k.value) for k in n.keywords)
                or (isinstance(f, ast.Attribute) and f.attr == "update" and any(imp.environ(a) for a in n.args))
                or (q in ("copy.copy", "copy.deepcopy") and any(imp.environ(a) for a in n.args)))
    if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr):
        return imp.environ(n.left) or imp.environ(n.right)
    if isinstance(n, ast.AugAssign) and isinstance(n.op, ast.BitOr):
        return imp.environ(n.value)
    if isinstance(n, ast.Dict):
        return any(k is None and imp.environ(v) for k, v in zip(n.keys, n.values))
    if isinstance(n, ast.DictComp):
        return any(_over_environ(g.iter, imp) for g in n.generators)
    return False


def violations(source: str, name: str = "<sample>") -> "list[str]":
    """Each inheritance site in one file's source, as 'name:line: rule'."""
    if not REFERENCE.search(source):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [f"{name}:0: unparseable"]
    if not REFERENCE.search(code_text(ast.parse(source))):
        return []
    imp = Imports(tree)
    parents = {}
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            parents[c] = p

    def enclosing_def(n):
        while n in parents:
            n = parents[n]
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return n
        return None

    def statement(n) -> bool:
        """`n` is a whole statement's value directly in a module or function body: it runs."""
        s = parents.get(n)
        holder = parents.get(s)
        return (isinstance(s, (ast.Expr, ast.Assign, ast.AnnAssign)) and s.value is n
                and isinstance(holder, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef)) and s in holder.body)

    def isolates(n) -> bool:
        """An isolator call run as a statement: in effect for the rest of the process."""
        return isinstance(n, ast.Call) and _name(n.func) in ISOLATORS and statement(n)

    def runs_code(n) -> bool:
        return (isinstance(n, ast.Call) and (imp.spawn(n) or _name(n.func) in LOADERS)) or _imports_target(n)

    library = "/_helpers/" in name

    def covered(n) -> bool:
        """In a helper library: isolated earlier in its function, or a spawn with a helper-built env."""
        if not library:
            return False
        fn = enclosing_def(n)
        if fn is not None and any(isolates(c) and c.lineno < n.lineno for c in ast.walk(fn)):
            return True
        env = {k.arg: k.value for k in n.keywords}.get("env") if isinstance(n, ast.Call) and imp.spawn(n) else None
        if env is None:
            return False
        if any(b in ast.unparse(env) for b in BUILDERS):
            return True
        return isinstance(env, ast.Name) and fn is not None and any(
            isinstance(a, ast.Assign) and any(isinstance(t, ast.Name) and t.id == env.id for t in a.targets)
            and any(b in ast.unparse(a.value) for b in BUILDERS) for a in ast.walk(fn))

    nodes = list(ast.walk(tree))
    module_iso = sorted(n.lineno for n in nodes if isolates(n) and enclosing_def(n) is None)
    runs = sorted((n for n in nodes if runs_code(n) and not covered(n)), key=lambda n: n.lineno)
    out = []
    if runs and not module_iso:
        out.append((runs[0].lineno, "unisolated"))
    elif runs:
        out += [(n.lineno, "before-isolation") for n in runs
                if enclosing_def(n) is None and n.lineno < module_iso[0]]
    for n in nodes:
        rule = None
        if _copy_rule(n, imp):
            rule = "copy"
        elif isinstance(n, ast.Call) and imp.spawn(n):
            rule = _spawn_env_rule(n, enclosing_def(n), imp)
        if rule:
            out.append((n.lineno, rule))
    return [f"{name}:{line}: {rule}" for line, rule in sorted(set(out))]


def _spawn_env_rule(n: ast.Call, fn, imp: Imports) -> "str | None":
    if (imp.qual(n.func) or "").startswith("os.") and any(imp.environ(a) for a in n.args):
        return "spawn-env"
    env = {k.arg: k.value for k in n.keywords}.get("env")
    if env is None:
        return None
    if (isinstance(env, ast.Constant) and env.value is None) or imp.environ(env):
        return "spawn-env"
    if not isinstance(env, ast.Name) or fn is None or isinstance(fn, ast.Lambda):
        return None
    a = fn.args
    positional = a.posonlyargs + a.args
    defaults = dict(zip([p.arg for p in positional][::-1], a.defaults[::-1]))
    defaults.update({p.arg: d for p, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None})
    d = defaults.get(env.id)
    rebuilt = any(isinstance(t, ast.Name) and t.id == env.id
                  for s in ast.walk(fn) if isinstance(s, (ast.Assign, ast.AugAssign, ast.AnnAssign))
                  for t in (s.targets if isinstance(s, ast.Assign) else [s.target]))
    if isinstance(d, ast.Constant) and d.value is None and not rebuilt:
        return "env-param"
    return None


def scanned_files() -> "list[Path]":
    roots = [REPO / "tests"] + sorted((REPO / "packages").glob("*/tests"))
    files = {p for r in roots for p in r.rglob("*.py")}
    files |= set((REPO / "src").rglob("*.test.py")) | set((REPO / "skills").rglob("*.test.py"))
    files |= set((REPO / "skills").rglob("test_*.py"))
    return sorted(p for p in files if p not in (HELPER, AUDITOR) and "node_modules" not in p.parts)


SAMPLE_FLAGGED = '''
import os, subprocess
from ag2_sparrow import remote_gateway_bridge
env = os.environ.copy()
env2 = dict(os.environ, REMOTE_TASK_TOKEN="t")
env3 = {**os.environ, "X": "1"}
env4 = {k: v for k, v in os.environ.items() if not k.startswith("AG2_")}
env5 = hermetic_env(**os.environ)
subprocess.run(["python3", "remote_gateway_bridge.py"], env=None)
subprocess.run(["python3", "remote_gateway_bridge.py"], env=os.environ)
subprocess.run(["python3", "remote_gateway_bridge.py"])
def spawn(args, env=None):
    return subprocess.run(args, env=env)
'''

SAMPLE_CLEAN = '''
import os, subprocess
from _helpers.hermetic_env import hermetic_env, isolate_os_environ
isolate_os_environ(REMOTE_TASK_TOKEN="t")
from ag2_sparrow import remote_gateway_bridge
subprocess.run(["python3", "remote_gateway_bridge.py"], env=hermetic_env(X="1"))
subprocess.run(["git", "status"])
allow = {k: os.environ[k] for k in ("PATH",) if k in os.environ}
def spawn(args, env=None):
    env = env or hermetic_env()
    return subprocess.run(args, env=env)
'''


IN_SCOPE = 'BRIDGE = "src/remote-gateway-bridge.py"\n'
ISOLATED = "from _helpers.hermetic_env import isolate_os_environ\nisolate_os_environ()\n" + IN_SCOPE
GATEWAY_IMPORT = "from ag2_sparrow import remote_gateway_bridge\n"
HEAD = 'BRIDGE = "remote_gateway_bridge"\nimport os, sys\nfrom unittest import mock\nfrom subprocess import run\n'
CLEAR = "mock.patch.dict(os.environ, {}, clear=True)"
PRINT = "run([sys.executable, '-c', 'import os; print(os.environ.get(\"GATEWAY_TOKEN\", \"\"))'])\n"
TESTCLASS = f"@{CLEAR}\nclass T:\n    def test_x(self):\n        pass\n"
# Each runs a child that prints the parent's GATEWAY_TOKEN although a clearing patch appears in the file.
LEAKS = {
    "dead-branch": HEAD + f"if False:\n    with {CLEAR}:\n        pass\n" + PRINT,
    "exited": HEAD + f"with {CLEAR}:\n    pass\n" + PRINT,
    "started-then-stopall": HEAD + f"{CLEAR}.start()\nmock.patch.stopall()\n" + PRINT,
    "class-decorator": HEAD + TESTCLASS + PRINT,
    "class-decorator-helper": HEAD + "def helper():\n    " + PRINT + TESTCLASS + "helper()\n",
    "import-used-after-with": HEAD + f"with {CLEAR}:\n    from ag2_sparrow import remote_gateway_bridge as b\n"
                              'print(b._env_compat("REMOTE_TASK_TOKEN", "GATEWAY_TOKEN"))\n',
    "decorated-generator": HEAD + f"@{CLEAR}\ndef gen():\n    " + PRINT + "    yield\nnext(gen())\n",
}
ISOLATED_CONTROL = (f"import sys\nsys.path.insert(0, {str(REPO / 'tests')!r})\n"
                    "from _helpers.hermetic_env import isolate_os_environ\nisolate_os_environ()\n" + HEAD + PRINT)
SYNTHETIC = "qa-synthetic-token"


class SelfTest(unittest.TestCase):
    def test_every_rule_fires_on_the_flagged_sample(self):
        got = violations(SAMPLE_FLAGGED)
        rules = [g.rsplit(": ", 1)[1] for g in got]
        self.assertEqual(rules.count("copy"), 5, got)
        self.assertEqual(rules.count("spawn-env"), 2, got)
        self.assertEqual(rules.count("env-param"), 1, got)
        self.assertEqual(rules.count("unisolated"), 1, got)

    def test_the_clean_sample_passes(self):
        self.assertEqual(violations(SAMPLE_CLEAN), [])

    def test_isolation_after_the_import_does_not_count(self):
        late = SAMPLE_CLEAN.replace('isolate_os_environ(REMOTE_TASK_TOKEN="t")\n', "") + "isolate_os_environ()\n"
        self.assertIn("before-isolation", " ".join(violations(late)))

    def test_a_bridge_loaded_by_path_or_run_by_code_under_test_needs_isolation(self):
        by_path = ('import importlib.util\nBRIDGE = "src/remote-gateway-bridge.py"\n'
                   'spec = importlib.util.spec_from_file_location("b", BRIDGE)\n')
        self.assertIn("unisolated", " ".join(violations(by_path)))
        hc = 'NOTE = "drives the remote_gateway_bridge restart"\nimport subprocess\nsubprocess.run(["bash", "x.sh"], env=E)\n'
        self.assertIn("unisolated", " ".join(violations(hc)))
        by_name = 'import sys\nsys.path.insert(0, "skills/agent-room-ops")\nimport read as rd\n'
        self.assertIn("unisolated", " ".join(violations(by_name)))

    def test_only_a_helper_library_may_rely_on_a_helper_built_env_or_local_isolation(self):
        ok = ('import subprocess\n# remote_gateway_bridge child\n'
              'def start():\n    env = hermetic_env(X="1")\n    subprocess.Popen(["python3"], env=env)\n'
              'def imp():\n    isolate_os_environ()\n    from ag2_sparrow import remote_gateway_bridge\n')
        self.assertEqual(violations(ok, "tests/_helpers/sample.py"), [])
        self.assertIn("unisolated", " ".join(violations(ok, "tests/sample.test.py")))

    def test_scope_is_derived_from_the_modules_that_read_credential_names(self):
        found = {str(p.relative_to(REPO)) for p in credential_modules()}
        for m in ("skills/bee-actions/scripts/bee_actions.py", "src/health-check.py",
                  "skills/agent-room-ops/_gateway.py"):
            self.assertIn(m, found)
        bee = ('import importlib.util\nfrom pathlib import Path\n'
               'S = Path("skills") / "bee-actions" / "scripts" / "bee_actions.py"\n'
               'spec = importlib.util.spec_from_file_location("bee", S)\n')
        self.assertIn("unisolated", " ".join(violations(bee)))
        self.assertIn("unisolated", " ".join(violations('import sys\nsys.path.insert(0, "src")\nimport channel_token\n')))

    def test_aliased_merged_updated_and_copied_environments_are_copies(self):
        for body in ("import os as _os\nenv = _os.environ.copy()\n",
                     "from os import environ\nenv = environ.copy()\n",
                     "from os import environ\nenv = dict(environ)\n",
                     "from os import environ as E\nenv = {**E}\n",
                     'import os\nenv = os.environ | {"X": "1"}\n',
                     'import os\nenv = {"X": "1"} | os.environ\n',
                     "import os\nenv = {}\nenv.update(os.environ)\n",
                     "import copy, os\nenv = copy.copy(os.environ)\n",
                     "import copy as c, os\nenv = c.deepcopy(os.environ)\n",
                     "from copy import deepcopy\nimport os\nenv = deepcopy(os.environ)\n"):
            with self.subTest(body=body):
                self.assertIn("copy", " ".join(violations(ISOLATED + body)))

    def test_os_and_asyncio_spawns_need_isolation(self):
        for body in ('import os\nos.system("python3 x.py")\n', 'import os as o\no.popen("x")\n',
                     'import os\nos.execv("/bin/sh", ["sh"])\n', 'from os import spawnlp\nspawnlp(0, "sh", "sh")\n',
                     'import os\nos.posix_spawn("/bin/sh", ["sh"], {})\n',
                     'import asyncio\nasyncio.create_subprocess_exec("sh")\n',
                     'import asyncio as aio\naio.create_subprocess_shell("sh")\n',
                     'from asyncio import create_subprocess_exec\ncreate_subprocess_exec("sh")\n',
                     'from subprocess import run\nrun(["sh"])\n', 'import subprocess as sp\nsp.check_call(["sh"])\n'):
            with self.subTest(body=body):
                self.assertIn("unisolated", " ".join(violations(IN_SCOPE + body)))

    def test_spawns_handed_the_outer_environment(self):
        for body in ('import os\nos.execve("/bin/sh", ["sh"], os.environ)\n',
                     'import asyncio, os\nasyncio.create_subprocess_exec("sh", env=os.environ)\n',
                     'import os\nfrom subprocess import run\nrun(["python3", "remote_gateway_bridge.py"], env=os.environ)\n'):
            with self.subTest(body=body):
                self.assertIn("spawn-env", " ".join(violations(ISOLATED + body)))

    def test_isolation_that_never_runs_does_not_count(self):
        for iso in ("if False:\n    isolate_os_environ()\n", "if TYPE_CHECKING:\n    isolate_os_environ()\n",
                    "try:\n    pass\nexcept Exception:\n    isolate_os_environ()\n"):
            with self.subTest(iso=iso):
                self.assertIn("unisolated", " ".join(violations(iso + GATEWAY_IMPORT)))
        for iso in ("isolate_os_environ()\n", "ENV = isolate_os_environ()\n"):
            with self.subTest(iso=iso):
                self.assertEqual(violations(iso + GATEWAY_IMPORT), [])

    def test_a_clearing_patch_never_counts_as_isolation(self):
        for iso in (f"{CLEAR}\n", f"P = {CLEAR}\n", f"{CLEAR}.start()\n", f"with {CLEAR}:\n    pass\n",
                    f"with {CLEAR}:\n    " + GATEWAY_IMPORT, f"@{CLEAR}\ndef t():\n    " + GATEWAY_IMPORT):
            with self.subTest(iso=iso):
                sample = "import os\nfrom unittest import mock\n" + iso + GATEWAY_IMPORT
                self.assertIn("unisolated", " ".join(violations(sample)))
        for name, sample in LEAKS.items():
            with self.subTest(name=name):
                self.assertIn("unisolated", " ".join(violations(sample)))

    def test_each_refused_shape_really_leaks_a_parent_token_and_the_isolator_does_not(self):
        env = hermetic_env(GATEWAY_TOKEN=SYNTHETIC, PYTHONPATH=str(REPO / "packages" / "ag2-sparrow"))
        for name, sample in {**LEAKS, "isolated": ISOLATED_CONTROL}.items():
            with self.subTest(name=name):
                got = subprocess.run([sys.executable, "-c", sample], capture_output=True, text=True,
                                     env=env, timeout=60)
                self.assertEqual(got.returncode, 0, got.stderr)
                leaked = SYNTHETIC in got.stdout
                self.assertEqual(leaked, name != "isolated", got.stdout)
                self.assertEqual(violations(sample) == [], not leaked, violations(sample))

    def test_a_mention_only_in_a_comment_or_docstring_is_out_of_scope(self):
        body = "import os, subprocess\nenv = os.environ.copy()\nsubprocess.run(['x'])\n"
        for head in ('"""Exercises the remote_gateway_bridge and Hatch (adapters/hatch)."""\n',
                     "# drives the remote_gateway_bridge; see gateway_credential\n",
                     'def f():\n    """Imports room_ops: import room_ops."""\n'):
            with self.subTest(head=head):
                self.assertEqual(violations(head + body), [])
        self.assertIn("copy", " ".join(violations('B = "remote_gateway_bridge"\n' + body)))

    def test_a_file_naming_no_gateway_module_is_out_of_scope(self):
        self.assertEqual(violations("import os\nenv = os.environ.copy()\n"), [])


class Tree(unittest.TestCase):
    def test_no_test_reaching_gateway_credentials_inherits_the_outer_environment(self):
        files = scanned_files()
        self.assertGreater(len(files), 100, "the scan found almost no files: the roots moved")
        bad = [v for p in files for v in violations(p.read_text(errors="replace"), str(p.relative_to(REPO)))]
        self.assertEqual(bad, [], "build these environments with tests/_helpers/hermetic_env.py:\n" + "\n".join(bad))


if __name__ == "__main__":
    unittest.main(verbosity=1)
