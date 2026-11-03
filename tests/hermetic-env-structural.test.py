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
              modules) in a test file without a module-top isolation (a tests/_helpers library may
              instead spawn with the helper's env); a mock.patch.dict never counts as isolation
  before-isolation  such code (an import) at module level above that call
  misplaced-isolation  isolate_os_environ() or isolate_then_import() anywhere but a module-level
              statement preceded only by the docstring, imports that resolve (by module spec, not name)
              to the standard library or tests/_helpers, sys.path.insert/append of a literal or a path
              built from __file__ by pathlib/os.path, and other such calls. A project or third-party import
              runs code, so it goes below the isolation. At that point the file itself has started no
              patch, so none of its patches can later put the outer environment back; patches below it
              save an isolated one. Only the isolator's own definition in tests/_helpers may call it elsewhere.
  foreign-isolator  an isolator name not bound by `from _helpers.hermetic_env import isolate_os_environ`
              or `from _helpers.hermetic_gateway import isolate_then_import` (or those modules, dotted or
              aliased), or any of those names rebound or assigned through:
              another import, a def or an assignment of that name certifies nothing

Threat model: this gate and the helper catch ACCIDENTAL parent-environment leaks in test code.
Deliberately adversarial code (monkeypatching the helper module itself, say) is out of scope, and
so is a test that plants shadow stdlib or _helpers modules on a path it inserts: imports are resolved
against this scanner's own sys.path, not the scanned file's.
"""
from __future__ import annotations

import ast
import functools
import importlib.util
import re
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from unittest import mock
from pathlib import Path, PureWindowsPath

from _helpers.hermetic_env import hermetic_env, isolate_os_environ
from _helpers.credential_scope import credential_modules, reference_pattern, target_pattern

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


HELPERS_DIR = (REPO / "tests" / "_helpers").resolve()
STDLIB_DIRS = {Path(sysconfig.get_paths()[k]).resolve() for k in ("stdlib", "platstdlib")}
OS_PATH_FUNCS = {"dirname", "abspath", "join", "realpath", "normpath"}
# The one binding of each isolator name that certifies isolation.
CANONICAL = {"_helpers.hermetic_env": "isolate_os_environ", "_helpers.hermetic_gateway": "isolate_then_import"}


# Python 3.9 has no sys.stdlib_module_names: the stdlib roots this tree imports above an isolation.
# Anything else fails closed there; add it here when a test needs it.
STDLIB_FALLBACK = frozenset({
    "__future__", "argparse", "ast", "atexit", "base64", "builtins", "concurrent", "contextlib", "ctypes",
    "datetime", "errno", "functools", "glob", "hashlib", "hmac", "http", "importlib", "inspect", "io", "json",
    "multiprocessing", "os", "pathlib", "plistlib", "random", "re", "runpy", "secrets", "select", "shlex",
    "shutil", "signal", "socket", "sqlite3", "stat", "struct", "subprocess", "sys", "sysconfig", "tarfile",
    "tempfile", "textwrap", "threading", "time", "types", "unittest", "urllib", "venv", "zipfile"})
STDLIB = getattr(sys, "stdlib_module_names", None) or STDLIB_FALLBACK


def _source_path(name: str) -> Path:
    """A scanned file's name as a resolved path: repo-relative or absolute, either separator."""
    p = Path(PureWindowsPath(name).as_posix()) if "\\" in name else Path(name)
    return (p if p.is_absolute() else REPO / p).resolve()


@functools.lru_cache(maxsize=None)
def _origin(module: str) -> "Path | str | None":
    """Where `module` would be loaded from on this sys.path ("built-in"/"frozen", a file, or None)."""
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return None
    if spec is None:
        return None
    if spec.origin in ("built-in", "frozen"):
        return spec.origin
    loc = spec.origin if spec.origin not in (None, "namespace") else next(iter(spec.submodule_search_locations or ()), None)
    return Path(loc).resolve() if loc else None


def _runs_no_project_code(module: "str | None") -> bool:
    """`module` resolves to the standard library or to a file in tests/_helpers, not by name alone."""
    root = (module or "").split(".")[0]
    if root == "_helpers":
        o = _origin(module)
        return isinstance(o, Path) and (o == HELPERS_DIR or HELPERS_DIR in o.parents)
    if root not in STDLIB:
        return False
    o = _origin(root)
    return o in ("built-in", "frozen") or (isinstance(o, Path) and "site-packages" not in o.parts
                                           and any(d in o.parents for d in STDLIB_DIRS))


class PathNames:
    """The local names a file binds to pathlib.Path, pathlib, os and os.path's path functions."""

    def __init__(self, tree, imp: "Imports"):
        self.cls, self.pathlib, self.funcs, self.os = set(), set(), set(), imp.mods["os"]
        for n in tree.body:
            if isinstance(n, ast.ImportFrom) and n.level == 0:
                for a in n.names:
                    if n.module == "pathlib" and a.name == "Path":
                        self.cls.add(a.asname or a.name)
                    elif n.module == "os.path" and a.name in OS_PATH_FUNCS:
                        self.funcs.add(a.asname or a.name)
            elif isinstance(n, ast.Import):
                self.pathlib |= {a.asname or a.name for a in n.names if a.name == "pathlib"}

    def ok(self, e) -> bool:
        """A literal, or a path computed from __file__ by pathlib and os.path alone."""
        if isinstance(e, ast.Constant):
            return isinstance(e.value, str)
        if isinstance(e, ast.Name):
            return e.id == "__file__"
        if isinstance(e, ast.BinOp):
            return isinstance(e.op, ast.Div) and self.ok(e.left) and self.ok(e.right)
        if isinstance(e, ast.Attribute):
            return e.attr == "parent" and self.ok(e.value)
        if isinstance(e, ast.Subscript):
            index = e.slice.value if isinstance(e.slice, getattr(ast, "Index", ())) else e.slice
            return (isinstance(e.value, ast.Attribute) and e.value.attr == "parents" and self.ok(e.value.value)
                    and isinstance(index, ast.Constant) and isinstance(index.value, int))
        if not isinstance(e, ast.Call) or e.keywords:
            return False
        f, args = e.func, e.args
        # A zero-argument Path() or str() is the cwd or "": neither is built from __file__.
        if isinstance(f, ast.Name):
            known = f.id == "str" or f.id in self.cls or f.id in self.funcs
            return known and bool(args) and all(self.ok(a) for a in args)
        if not isinstance(f, ast.Attribute):
            return False
        if f.attr in ("resolve", "absolute") and not args:
            return self.ok(f.value)
        base = ast.unparse(f.value)
        known = ((f.attr in OS_PATH_FUNCS and base in {f"{o}.path" for o in self.os})
                 or (f.attr == "fspath" and base in self.os) or (f.attr == "Path" and base in self.pathlib))
        return known and bool(args) and all(self.ok(a) for a in args)


def _module_top(stmt, index: int, paths: PathNames) -> bool:
    """A statement that may precede a module-top isolation: the docstring, an import that resolves to
    the standard library or tests/_helpers, or a sys.path.insert/append of a path built from __file__."""
    if isinstance(stmt, ast.Import):
        return all(_runs_no_project_code(a.name) for a in stmt.names)
    if isinstance(stmt, ast.ImportFrom):
        return stmt.level == 0 and _runs_no_project_code(stmt.module)
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, (ast.Call, ast.Constant)):
        return False
    if isinstance(stmt.value, ast.Constant):
        return index == 0 and isinstance(stmt.value.value, str)
    f, args = stmt.value.func, stmt.value.args
    return (isinstance(f, ast.Attribute) and ast.unparse(f.value) == "sys.path" and not stmt.value.keywords
            and ((f.attr == "append" and len(args) == 1 and paths.ok(args[0]))
                 or (f.attr == "insert" and len(args) == 2 and isinstance(args[0], ast.Constant)
                     and paths.ok(args[1]))))


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


HISTORICAL_FIXTURE = "tests/fixtures/old_main/tutorial_cf87f324/getting_started.py"
HISTORICAL_REASON = (
    "cf87f324's getting_started.py, byte for byte: the deployed writer the tutorial's rollout fence is "
    "measured against (tests/tutorial-deployed-fence.test.py pins its SHA-256 and runs it as its own "
    "process). Its findings are evidence of what is deployed, so they can't be fixed in place.")
# Preserves the deployed parent's state as evidence: only these exact nodes, as (line, column, source text),
# only in that one file; the same token anywhere else is reported.
HISTORICAL = {HISTORICAL_FIXTURE: frozenset({
    (103, 15, "importlib.util.spec_from_file_location('_tutorial_room_commons', script)"),
    (105, 8, "spec.loader.exec_module(mod)"),
    (119, 12, "dict(os.environ)"),
    (122, 15, "importlib.util.spec_from_file_location('room_commons', script)"),
    (124, 8, "spec.loader.exec_module(rc)"),
    (136, 4, "from channel_credentials import channel_credentials"),
    (181, 4, "from channel_credentials import channel_value"),
    (375, 19, "subprocess.run(argv, capture_output=True, text=True, timeout=120, env=self.env)")})}


@functools.lru_cache(maxsize=None)
def _in_scope(source: str) -> "bool | None":
    """Whether the code, not its comments or docstrings, names a gateway reference; None if unparseable."""
    if not REFERENCE.search(source):
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    return bool(REFERENCE.search(code_text(tree)))


def violations(source: str, name: str = "<sample>", permitted: "frozenset | None" = None) -> "list[str]":
    """Each inheritance site in one file's source, as 'name:line: rule'."""
    allowed = HISTORICAL.get(name, frozenset()) if permitted is None else permitted
    at = {}
    for line, col, text in allowed:
        at.setdefault((line, col), set()).add(text)

    def historical(n) -> bool:
        # Position first, so ast.unparse runs only on the few nodes at a permitted (line, column).
        texts = at.get((getattr(n, "lineno", None), getattr(n, "col_offset", None))) if at else None
        return bool(texts) and ast.unparse(n) in texts

    scope = _in_scope(source)
    if scope is None:
        return [f"{name}:0: unparseable"]
    if not scope:
        return []
    tree = ast.parse(source)
    imp = Imports(tree)
    source = _source_path(name)
    library = HELPERS_DIR in source.parents
    paths = PathNames(tree, imp)
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

    canon, modules, canonical_sites = set(), {}, set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.level == 0:
            for a in n.names:
                if CANONICAL.get(n.module or "") == a.name:
                    canon.add(a.asname or a.name)
                    canonical_sites.add(a)
                elif n.module == "_helpers" and f"_helpers.{a.name}" in CANONICAL:
                    modules[a.asname or a.name] = f"_helpers.{a.name}"
                    canonical_sites.add(a)
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name in CANONICAL:
                    modules[a.asname or a.name] = a.name
                if a.name in CANONICAL and a.asname:
                    canonical_sites.add(a)
                elif not a.asname and a.name.split(".")[0] == "_helpers":
                    canonical_sites.add(a)
    # A watched name bound anywhere else (import-as, assignment, def, loop, with, except) is not the isolator.
    watched = ISOLATORS | canon | {k.split(".")[0] for k in modules}
    foreign, rebound = [], set()
    alias_import = {a: n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    for n in ast.walk(tree):
        if isinstance(n, ast.alias):
            local = (n.asname or n.name).split(".")[0]
            site = n
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.ExceptHandler)):
            local, site = n.name, n
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            local, site = n.id, n
        elif isinstance(n, (ast.Attribute, ast.Subscript)) and isinstance(n.ctx, ast.Store):
            root = n
            while isinstance(root, (ast.Attribute, ast.Subscript)):
                root = root.value
            if not isinstance(root, ast.Name):
                continue
            local, site = root.id, n
        elif isinstance(n, ast.arg):
            local, site = n.arg, n
        else:
            continue
        if local not in watched or site in canonical_sites:
            continue
        if (isinstance(n, ast.FunctionDef) and source == HELPERS_DIR / "hermetic_gateway.py"
                and n.name == "isolate_then_import"):
            continue
        foreign.append(alias_import.get(n, n))
        rebound.add(local)

    def isolator_call(c) -> bool:
        """A call of a canonically bound isolator."""
        if not isinstance(c, ast.Call):
            return False
        f = c.func
        if isinstance(f, ast.Name):
            return f.id in canon and f.id not in rebound
        base = ast.unparse(f.value) if isinstance(f, ast.Attribute) else ""
        return (bool(base) and base.split(".")[0] not in rebound
                and CANONICAL.get(modules.get(base, "")) == f.attr)

    def isolator_stmt(st) -> bool:
        v = getattr(st, "value", None)
        return isinstance(st, (ast.Expr, ast.Assign, ast.AnnAssign)) and isolator_call(v)

    top, helpers_seen = set(), False
    for i, st in enumerate(tree.body):
        if isolator_stmt(st):
            top.add(st.value)
            continue
        if not _module_top(st, i, paths):
            break
        # The first _helpers import must be the isolator's own module: another could run before the snapshot.
        mods = [a.name for a in st.names] if isinstance(st, ast.Import) else (
            [st.module or ""] if isinstance(st, ast.ImportFrom) else [])
        for m in mods:
            if m.split(".")[0] == "_helpers" and not helpers_seen:
                helpers_seen = True
                if m not in CANONICAL and not (m == "_helpers" and any(
                        f"_helpers.{a.name}" in CANONICAL for a in st.names)):
                    break
        else:
            continue
        break

    def isolates(n) -> bool:
        """An isolation at the module top, before any statement that could start a patcher."""
        return n in top

    def implementation(n) -> bool:
        fn = enclosing_def(n)
        return (library and isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name in ISOLATORS
                and fn not in foreign)

    def runs_code(n) -> bool:
        return (isinstance(n, ast.Call) and (imp.spawn(n) or _name(n.func) in LOADERS)) or _imports_target(n)

    def covered(n) -> bool:
        """In a helper library: the isolator's own definition after it isolates, or a spawn with a
        helper-built env."""
        if not library:
            return False
        fn = enclosing_def(n)
        if fn is not None and any(isolator_call(c) and implementation(c)
                                  and c.lineno < n.lineno for c in ast.walk(fn)):
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
    runs = sorted((n for n in nodes if runs_code(n) and not covered(n) and not historical(n)),
                  key=lambda n: n.lineno)
    out = []
    if runs and not module_iso:
        out.append((runs[0].lineno, "unisolated"))
    elif runs:
        out += [(n.lineno, "before-isolation") for n in runs
                if enclosing_def(n) is None and n.lineno < module_iso[0]]
    out += [(n.lineno, "misplaced-isolation") for n in nodes
            if isolator_call(n) and not isolates(n) and not implementation(n)]
    out += [(n.lineno, "foreign-isolator") for n in foreign]
    out += [(n.lineno, "foreign-isolator") for n in nodes if isinstance(n, ast.Call)
            and _name(n.func) in ISOLATORS and not isolator_call(n)]
    for n in nodes:
        rule = None
        if historical(n):
            continue
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
ISOLATE = (f"import sys\nsys.path.insert(0, {str(REPO / 'tests')!r})\n"
           "from _helpers.hermetic_env import isolate_os_environ\n")
# A patch over os.environ started before isolation: stopall() puts the outer environment back.
PATCHED_FIRST = {
    clear: HEAD + ISOLATE + f"mock.patch.dict(os.environ, {{'X': '1'}}, clear={clear}).start()\n"
    "isolate_os_environ()\nmock.patch.stopall()\n" + PRINT
    for clear in (True, False)
}
PROBE_HEAD = ("import os, sys, contextlib\nfrom unittest import mock\nfrom subprocess import run\n"
              f"sys.path.insert(0, {str(REPO / 'tests')!r})\nfrom _helpers.hermetic_env import isolate_os_environ\n")
# Each isolates while some patch of os.environ is live; once that patch ends the outer environment is back.
UNDER_A_PATCH = {
    "keyword": "with mock.patch.dict(in_dict=os.environ, values={}):\n    isolate_os_environ()\n",
    "alias": "from unittest.mock import patch as P\nwith P.dict(os.environ, {}):\n    isolate_os_environ()\n",
    "patch.object": 'mock.patch.object(os, "environ", dict(os.environ)).start()\nisolate_os_environ()\n',
    "patch-by-name": 'mock.patch("os.environ", dict(os.environ)).start()\nisolate_os_environ()\n',
    "exitstack": "with contextlib.ExitStack() as s:\n    s.enter_context(mock.patch.dict(os.environ, {}))\n"
                 "    isolate_os_environ()\n",
    "nested": "with mock.patch.dict(os.environ, {}):\n    with mock.patch.dict(os.environ, {}):\n"
              "        isolate_os_environ()\n",
    "decorator": "@mock.patch.dict(os.environ, {})\ndef f():\n    isolate_os_environ()\nf()\n",
    "entered-by-hand": "P = mock.patch.dict(os.environ, {})\nP.__enter__()\nisolate_os_environ()\n"
                       "P.__exit__(None, None, None)\n",
    "reassigned": "os.environ = dict(os.environ)\nisolate_os_environ()\n",
}
# os.environ no longer the process environment: the helper refuses to isolate a stand-in.
REFUSED_AT_RUNTIME = {"patch.object", "patch-by-name", "reassigned"}
MODULE_TOP = "isolate_os_environ()\nmock.patch.dict(os.environ, {'X': '1'}, clear=True).start()\n"


# Project modules a test might import before isolating: each defeats an isolation that follows it.
IMPORTED = {
    "evil_entry": "import os\nfrom unittest import mock\nP = mock.patch.dict(os.environ, {})\nP.__enter__()\n",
    "evil_stack": ("import contextlib, os\nfrom unittest import mock\nSTACK = contextlib.ExitStack()\n"
                   "STACK.enter_context(mock.patch.dict(os.environ, {}))\n"),
    "evil_helper": "def isolate_os_environ(*args, **kwargs):\n    return None\n",
    "evil_environ": ("import os\ne = os.environ\n"
                     "os.environ = os._Environ(dict(e._data), e.encodekey, e.decodekey, e.encodevalue, e.decodevalue)\n"),
}
CANON = "from _helpers.hermetic_env import isolate_os_environ\n"
IMPORTED_CONTROLS = {
    "imported-entry": "import evil_entry\n" + CANON + "isolate_os_environ()\nevil_entry.P.__exit__(None, None, None)\n",
    "imported-exitstack": "import evil_stack\n" + CANON + "isolate_os_environ()\nevil_stack.STACK.close()\n",
    "wrong-source": "from evil_helper import isolate_os_environ\nisolate_os_environ()\n",
    "distinct-environ": CANON + "import evil_environ\nisolate_os_environ()\n",
}


def imported_probe(body: str, modules: Path) -> str:
    return (f"import os, sys\nfrom subprocess import run\nsys.path.insert(0, {str(REPO / 'tests')!r})\n"
            f"sys.path.insert(0, {str(modules)!r})\n" + body + PRINT + 'BRIDGE = "remote_gateway_bridge"\n')


def probe(body: str) -> str:
    return PROBE_HEAD + body + "mock.patch.stopall()\n" + PRINT + 'BRIDGE = "remote_gateway_bridge"\n'


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
        self.assertIn("misplaced-isolation", " ".join(violations(late)))
        self.assertIn("misplaced-isolation", " ".join(violations(GATEWAY_IMPORT + CANON + "isolate_os_environ()\n")))

    def test_a_bridge_loaded_by_path_or_run_by_code_under_test_needs_isolation(self):
        by_path = ('import importlib.util\nBRIDGE = "src/remote-gateway-bridge.py"\n'
                   'spec = importlib.util.spec_from_file_location("b", BRIDGE)\n')
        self.assertIn("unisolated", " ".join(violations(by_path)))
        hc = 'NOTE = "drives the remote_gateway_bridge restart"\nimport subprocess\nsubprocess.run(["bash", "x.sh"], env=E)\n'
        self.assertIn("unisolated", " ".join(violations(hc)))
        by_name = 'import sys\nsys.path.insert(0, "skills/agent-room-ops")\nimport read as rd\n'
        self.assertIn("unisolated", " ".join(violations(by_name)))

    def test_only_a_helper_library_may_rely_on_a_helper_built_env_or_local_isolation(self):
        ok = ('import subprocess\n# remote_gateway_bridge child\n' + CANON +
              'def start():\n    env = hermetic_env(X="1")\n    subprocess.Popen(["python3"], env=env)\n'
              'def isolate_then_import():\n    isolate_os_environ()\n    from ag2_sparrow import remote_gateway_bridge\n')
        self.assertEqual(violations(ok, "tests/_helpers/hermetic_gateway.py"), [])
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
                self.assertEqual(violations(CANON + iso + GATEWAY_IMPORT), [])

    def test_a_clearing_patch_never_counts_as_isolation(self):
        for iso in (f"{CLEAR}\n", f"P = {CLEAR}\n", f"{CLEAR}.start()\n", f"with {CLEAR}:\n    pass\n",
                    f"with {CLEAR}:\n    " + GATEWAY_IMPORT, f"@{CLEAR}\ndef t():\n    " + GATEWAY_IMPORT):
            with self.subTest(iso=iso):
                sample = "import os\nfrom unittest import mock\n" + iso + GATEWAY_IMPORT
                self.assertIn("unisolated", " ".join(violations(sample)))
        for name, sample in LEAKS.items():
            with self.subTest(name=name):
                self.assertIn("unisolated", " ".join(violations(sample)))

    def test_isolation_counts_only_at_the_module_top(self):
        for name, body in UNDER_A_PATCH.items():
            with self.subTest(name=name):
                self.assertIn("misplaced-isolation", " ".join(violations(probe(body))))
        for clear, sample in PATCHED_FIRST.items():
            with self.subTest(clear=clear):
                self.assertIn("misplaced-isolation", " ".join(violations(sample)))
        for body in ("def f():\n    isolate_os_environ()\n", "try:\n    isolate_os_environ()\nexcept OSError:\n    pass\n",
                     "for _ in ():\n    isolate_os_environ()\n", "class C:\n    isolate_os_environ()\n",
                     "REPO = 1\nisolate_os_environ()\n", 'sys.path.insert(0, open("p").read())\nisolate_os_environ()\n'):
            with self.subTest(body=body):
                self.assertIn("misplaced-isolation", " ".join(violations("import sys\n" + CANON + body + GATEWAY_IMPORT)))
        self.assertEqual(violations(probe(MODULE_TOP)), [])
        top = ('"""Doc."""\nfrom __future__ import annotations\nimport sys\nfrom pathlib import Path\n'
               "sys.path.insert(0, str(Path(__file__).resolve().parents[2]))\n" + CANON +
               "from _helpers.hermetic_gateway import isolate_then_import\nisolate_os_environ()\n"
               "isolate_then_import()\n" + GATEWAY_IMPORT)
        self.assertEqual(violations(top), [])

    def test_a_patch_live_at_isolation_leaks_unless_refused_and_one_after_a_module_top_isolation_does_not(self):
        env = hermetic_env(GATEWAY_TOKEN=SYNTHETIC)
        for name, body in {**UNDER_A_PATCH, "module-top": MODULE_TOP}.items():
            with self.subTest(name=name):
                got = subprocess.run([sys.executable, "-c", probe(body)], capture_output=True, text=True,
                                     env=env, timeout=60)
                refused = "os.environ replaced" in got.stderr
                self.assertEqual(refused, name in REFUSED_AT_RUNTIME, got.stderr)
                self.assertEqual(got.returncode != 0, refused, got.stderr)
                self.assertEqual(SYNTHETIC in got.stdout, name != "module-top" and not refused, got.stdout)

    def test_an_import_or_a_same_named_helper_before_isolation_is_refused_and_would_leak(self):
        with tempfile.TemporaryDirectory() as d:
            for mod, src in IMPORTED.items():
                (Path(d) / f"{mod}.py").write_text(src)
            env = hermetic_env(GATEWAY_TOKEN=SYNTHETIC)
            for name, body in IMPORTED_CONTROLS.items():
                with self.subTest(name=name):
                    sample = imported_probe(body, Path(d))
                    self.assertTrue({"misplaced-isolation", "foreign-isolator"}
                                    & {v.rsplit(": ", 1)[1] for v in violations(sample)}, violations(sample))
                    got = subprocess.run([sys.executable, "-c", sample], capture_output=True, text=True,
                                         env=env, timeout=60)
                    refused = "os.environ replaced" in got.stderr
                    self.assertEqual(refused, name == "distinct-environ", got.stderr)
                    self.assertTrue(refused or SYNTHETIC in got.stdout, (got.stdout, got.stderr))

    def test_a_rebound_isolator_name_certifies_nothing_and_leaks(self):
        for body in ("from _helpers.hermetic_env import isolate_os_environ as iso\nfrom os import getpid as iso\niso()\n",
                     CANON + "isolate_os_environ()\ntry:\n    pass\nexcept OSError as isolate_os_environ:\n    pass\n",
                     "import _helpers.hermetic_env as he\nimport json as he\nhe.isolate_os_environ()\n",
                     CANON + "def f(isolate_os_environ):\n    pass\nisolate_os_environ()\n"):
            with self.subTest(body=body):
                self.assertIn("foreign-isolator", " ".join(violations(body + GATEWAY_IMPORT)))
        leak = imported_probe("from _helpers.hermetic_env import isolate_os_environ as iso\n"
                              "from os import getpid as iso\niso()\n", REPO / "tests")
        got = subprocess.run([sys.executable, "-c", leak], capture_output=True, text=True,
                             env=hermetic_env(GATEWAY_TOKEN=SYNTHETIC), timeout=60)
        self.assertIn(SYNTHETIC, got.stdout, got.stderr)

    def test_the_first_helpers_import_is_the_isolators_and_the_snapshot_predates_every_helper(self):
        early = "from _helpers.credential_scope import CREDENTIAL_NAME\n" + CANON + "isolate_os_environ()\n"
        self.assertIn("misplaced-isolation", " ".join(violations(early + GATEWAY_IMPORT)))
        swapped = ("import sys\nsys.path.insert(0, %r)\nimport os\nimport _helpers\ne = os.environ\n"
                   "os.environ = os._Environ(dict(e._data), e.encodekey, e.decodekey, e.encodevalue, e.decodevalue)\n"
                   "from _helpers.hermetic_env import isolate_os_environ\nisolate_os_environ()\n" % str(REPO / "tests"))
        got = subprocess.run([sys.executable, "-c", swapped], capture_output=True, text=True,
                             env=hermetic_env(), timeout=60)
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("os.environ replaced", got.stderr)

    def test_helper_files_are_recognised_by_resolved_path_not_spelling(self):
        real = HELPERS_DIR / "hermetic_gateway.py"
        source = real.read_text()
        for name in ("tests\\_helpers\\hermetic_gateway.py", str(real), "tests/./_helpers/hermetic_gateway.py"):
            with self.subTest(name=name):
                self.assertEqual(violations(source, name), [])
        elsewhere = violations(source, "packages/ag2-sparrow/tests/_helpers/hermetic_gateway.py")
        self.assertIn("foreign-isolator", " ".join(elsewhere))

    def test_the_dotted_canonical_form_counts_and_rebinding_it_does_not(self):
        dotted = "import _helpers.hermetic_env\n_helpers.hermetic_env.isolate_os_environ()\n"
        self.assertEqual(violations(dotted + GATEWAY_IMPORT), [])
        for tail in ("_helpers = None\n", "_helpers.hermetic_env.isolate_os_environ = print\n",
                     "import json as _helpers\n"):
            with self.subTest(tail=tail):
                self.assertIn("foreign-isolator", " ".join(violations(dotted + tail + GATEWAY_IMPORT)))

    def test_prelude_imports_count_by_where_they_resolve_not_by_name(self):
        isolate = CANON + "isolate_os_environ()\n" + GATEWAY_IMPORT
        self.assertIn("misplaced-isolation", " ".join(violations(
            CANON + "import _helpers.not_a_helper\nisolate_os_environ()\n" + GATEWAY_IMPORT)))
        self.assertEqual(violations("import plistlib\n" + isolate), [])
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "plistlib.py").write_text("")
            saved = sys.modules.pop("plistlib", None)
            sys.path.insert(0, d)
            _origin.cache_clear()
            importlib.invalidate_caches()
            try:
                self.assertIn("misplaced-isolation", " ".join(violations("import plistlib\n" + isolate)))
            finally:
                sys.path.remove(d)
                _origin.cache_clear()
                if saved is not None:
                    sys.modules["plistlib"] = saved

    def test_a_sys_path_entry_is_a_literal_or_built_from_file(self):
        head = "import os, sys, pathlib\nfrom pathlib import Path\n"
        isolate = CANON + "isolate_os_environ()\n" + GATEWAY_IMPORT
        for line in ('sys.path.insert(0, str(Path(os.environ["HOME"])))\n', "sys.path.insert(0, str(x.resolve()))\n",
                     "sys.path.append(str(Path.home()))\n", "sys.path.insert(0, os.path.join(os.getcwd()))\n",
                     "sys.path.insert(0, str(Path()))\n", "sys.path.insert(0, str())\n",
                     "sys.path.insert(0, str(pathlib.Path()))\n",
                     "from pathlib import Path as P\nsys.path.insert(0, str(P()))\n"):
            with self.subTest(line=line):
                self.assertIn("misplaced-isolation", " ".join(violations(head + line + isolate)))
        for line in ("sys.path.insert(0, str(Path(__file__).resolve().parents[2]))\n",
                     'sys.path.append(os.path.join(os.path.dirname(__file__), ".."))\n',
                     'sys.path.insert(0, "tests")\n',
                     'sys.path.insert(0, str(pathlib.Path(__file__).parent / "tests"))\n',
                     "from pathlib import Path as P\nsys.path.insert(0, str(P(__file__).parent))\n",
                     "import pathlib as pl\nsys.path.insert(0, str(pl.Path(__file__).parent))\n"):
            with self.subTest(line=line):
                self.assertEqual(violations(head + line + isolate), [])

    def test_isolating_under_a_started_environ_patch_fails_and_never_leaks(self):
        env = hermetic_env(GATEWAY_TOKEN=SYNTHETIC)
        for clear, sample in PATCHED_FIRST.items():
            with self.subTest(clear=clear):
                got = subprocess.run([sys.executable, "-c", sample], capture_output=True, text=True,
                                     env=env, timeout=60)
                self.assertNotIn(SYNTHETIC, got.stdout)
                self.assertNotEqual(got.returncode, 0)
                self.assertIn("isolate before patching os.environ", got.stderr)
        # A suite loaded by an isolated one re-isolates under the loader's patch; that patch saved an isolated env.
        renested = (HEAD + ISOLATE + "isolate_os_environ()\nP = mock.patch.dict(os.environ)\nP.start()\n"
                    "isolate_os_environ()\nP.stop()\n" + PRINT)
        got = subprocess.run([sys.executable, "-c", renested], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual((got.returncode, got.stdout.strip()), (0, ""), got.stderr)

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

    def test_an_unparseable_file_naming_a_gateway_module_is_reported_each_time(self):
        broken = "import remote_gateway_bridge\ndef (:\n"
        self.assertEqual(violations(broken, "x.py"), ["x.py:0: unparseable"])
        self.assertEqual(violations(broken, "x.py"), ["x.py:0: unparseable"])

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
    def test_the_historical_fixture_is_scanned_and_only_its_exact_nodes_are_permitted(self):
        fixture = REPO / HISTORICAL_FIXTURE
        self.assertIn(fixture, scanned_files())
        self.assertTrue(HISTORICAL_REASON)
        text = fixture.read_text()
        self.assertEqual(violations(text, HISTORICAL_FIXTURE), [])
        # The same tokens anywhere else are findings, as is any one of them left unpermitted.
        elsewhere = "tests/fixtures/old_main/planted.py"
        self.assertEqual(violations(text, elsewhere), [f"{elsewhere}:103: unisolated", f"{elsewhere}:119: copy"])
        for token in HISTORICAL[HISTORICAL_FIXTURE]:
            self.assertTrue(violations(text, HISTORICAL_FIXTURE, HISTORICAL[HISTORICAL_FIXTURE] - {token}), token)
        # A second violation on a permitted line, another node at a permitted position, and the
        # permitted text at another column or line are all still reported.
        lines = text.split("\n")
        for line, extra, rule in ((119, "; leak = os.environ.copy()", "copy"),
                                  (119, "; leak = dict(os.environ)", "copy"),
                                  (103, "; subprocess.run(['true'])", "unisolated")):
            planted = lines[:line - 1] + [lines[line - 1] + extra] + lines[line:]
            self.assertEqual(violations("\n".join(planted), HISTORICAL_FIXTURE),
                             [f"{HISTORICAL_FIXTURE}:{line}: {rule}"], extra)
        swapped = lines[:118] + [lines[118].replace("dict(os.environ)", "os.environ.copy()")] + lines[119:]
        self.assertEqual(violations("\n".join(swapped), HISTORICAL_FIXTURE), [f"{HISTORICAL_FIXTURE}:119: copy"])
        moved = text + "\n\ndef planted():\n    return dict(os.environ)\n"
        self.assertEqual(violations(moved, HISTORICAL_FIXTURE), [f"{HISTORICAL_FIXTURE}:{len(lines) + 3}: copy"])

    def test_no_test_reaching_gateway_credentials_inherits_the_outer_environment(self):
        files = scanned_files()
        self.assertGreater(len(files), 100, "the scan found almost no files: the roots moved")
        bad = [v for p in files for v in violations(p.read_text(errors="replace"), str(p.relative_to(REPO)))]
        self.assertEqual(bad, [], "build these environments with tests/_helpers/hermetic_env.py:\n" + "\n".join(bad))

    def test_the_python_3_9_fallback_covers_the_tree_and_fails_closed_on_anything_else(self):
        with mock.patch.dict(globals(), {"STDLIB": STDLIB_FALLBACK}):
            files = scanned_files()
            bad = [v for p in files for v in violations(p.read_text(errors="replace"), str(p.relative_to(REPO)))]
            self.assertEqual(bad, [], "\n".join(bad))
            unknown = "import tkinter\n" + CANON + "isolate_os_environ()\n" + GATEWAY_IMPORT
            self.assertIn("misplaced-isolation", " ".join(violations(unknown)))
        self.assertLessEqual(STDLIB_FALLBACK, set(getattr(sys, "stdlib_module_names", STDLIB_FALLBACK)))


if __name__ == "__main__":
    unittest.main(verbosity=1)
