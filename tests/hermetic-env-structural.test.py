#!/usr/bin/env python3
"""No test that reaches gateway credentials inherits the outer environment.

A test file (under tests/ or packages/*/tests, or a src/*.test.py or skills/*/test_*.py) that names the remote gateway bridge, Hatch,
room_ops' gateway client, the credential modules, or any module whose code reads a GATEWAY_*, RELAY_*,
REMOTE_TASK_* or AG2_REMOTE_* name (tests/_helpers/credential_scope.py finds those by scanning the tree)
must build every environment with tests/_helpers/hermetic_env.py. Refused anywhere else in such a file:

  copy        os.environ.copy(), dict(os.environ, ...), {**os.environ, ...}, f(**os.environ), or a
              dict comprehension over os.environ (a denylist still carries every other selector)
  spawn-env   a subprocess handed env=None or env=os.environ
  env-param   a subprocess handed an `env` parameter that defaults to None and is never rebuilt
  unisolated  code run (a subprocess, a module loader, an import of those modules) in a test
              file that never calls isolate_os_environ() or isolate_then_import() at module level
              (a tests/_helpers library may instead isolate in the function, or spawn with the
              helper's env)
  before-isolation  such code at module level above that call
"""
from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

from _helpers.credential_scope import credential_modules, reference_pattern, target_pattern

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


def _is_environ(n) -> bool:
    return (isinstance(n, ast.Attribute) and n.attr == "environ"
            and isinstance(n.value, ast.Name) and n.value.id == "os")


def _name(f) -> str:
    return getattr(f, "attr", None) or getattr(f, "id", "") or ""


def _over_environ(it) -> bool:
    return _is_environ(it) or (isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute)
                               and it.func.attr in ("items", "keys") and _is_environ(it.func.value))


def _is_spawn(call: ast.Call) -> bool:
    f = call.func
    if isinstance(f, ast.Attribute) and f.attr in SPAWNS:
        return isinstance(f.value, ast.Name) and f.value.id == "subprocess"
    return isinstance(f, ast.Name) and f.id == "Popen"


def _clearing_patch(call: ast.Call) -> bool:
    return (_name(call.func) == "dict" and call.args and _is_environ(call.args[0])
            and any(k.arg == "clear" and isinstance(k.value, ast.Constant) and k.value.value is True
                    for k in call.keywords))


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


def violations(source: str, name: str = "<sample>") -> "list[str]":
    """Each inheritance site in one file's source, as 'name:line: rule'."""
    if not REFERENCE.search(source):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [f"{name}:0: unparseable"]
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

    def isolates(n) -> bool:
        return isinstance(n, ast.Call) and (_name(n.func) in ISOLATORS or _clearing_patch(n))

    def runs_code(n) -> bool:
        return (isinstance(n, ast.Call) and (_is_spawn(n) or _name(n.func) in LOADERS)) or _imports_target(n)

    library = "/_helpers/" in name

    def covered(n) -> bool:
        """In a helper library: isolated earlier in its function, or a spawn with a helper-built env."""
        if not library:
            return False
        fn = enclosing_def(n)
        if fn is not None and any(isolates(c) and c.lineno < n.lineno for c in ast.walk(fn)):
            return True
        env = {k.arg: k.value for k in n.keywords}.get("env") if isinstance(n, ast.Call) and _is_spawn(n) else None
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
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and f.attr == "copy" and _is_environ(f.value):
                rule = "copy"
            elif isinstance(f, ast.Name) and f.id == "dict" and n.args and _is_environ(n.args[0]):
                rule = "copy"
            elif any(k.arg is None and _is_environ(k.value) for k in n.keywords):
                rule = "copy"
            elif _is_spawn(n):
                rule = _spawn_env_rule(n, enclosing_def(n))
        elif isinstance(n, ast.Dict) and any(k is None and _is_environ(v) for k, v in zip(n.keys, n.values)):
            rule = "copy"
        elif isinstance(n, ast.DictComp) and any(_over_environ(g.iter) for g in n.generators):
            rule = "copy"
        if rule:
            out.append((n.lineno, rule))
    return [f"{name}:{line}: {rule}" for line, rule in sorted(set(out))]


def _spawn_env_rule(n: ast.Call, fn) -> "str | None":
    env = {k.arg: k.value for k in n.keywords}.get("env")
    if env is None:
        return None
    if (isinstance(env, ast.Constant) and env.value is None) or _is_environ(env):
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
        hc = '# drives the remote_gateway_bridge restart\nimport subprocess\nsubprocess.run(["bash", "x.sh"], env=E)\n'
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
