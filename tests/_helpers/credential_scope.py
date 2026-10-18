"""Production modules that read a gateway credential name, found by scanning the tree.

A module under src/, skills/ or packages/ is in scope when its code (not a comment or docstring)
holds a GATEWAY_*, RELAY_*, REMOTE_TASK_* or AG2_REMOTE_* name as a string constant. A test that
names such a module, by file, import or dotted path, reaches credential discovery.
"""
from __future__ import annotations

import ast
import functools
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOTS = ("src", "skills", "packages")
CREDENTIAL_NAME = re.compile(r"(?:GATEWAY|RELAY|REMOTE_TASK|AG2_REMOTE)_[A-Z0-9_]+")
# The values a sentinel can stand in for; path selectors (_FILE, _DIR) name files instead.
SELECTOR_SUFFIXES = ("_TOKEN", "_URL", "_INSTANCE", "_ID", "_MXID")


def _is_test(p: Path) -> bool:
    return (p.name.endswith(".test.py") or p.name.startswith("test_") or "tests" in p.parts
            or "test" in p.parts or "node_modules" in p.parts)


def code_names(source: str) -> "frozenset[str]":
    """The credential names a module's code holds as string constants, docstrings excluded."""
    if not CREDENTIAL_NAME.search(source):
        return frozenset()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return frozenset()
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    return frozenset(n.value for n in ast.walk(tree)
                     if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs
                     and CREDENTIAL_NAME.fullmatch(n.value))


@functools.lru_cache(maxsize=None)
def credential_modules() -> "dict[Path, frozenset[str]]":
    """Each production module that reads a credential name, with the names it reads."""
    found = {}
    for root in ROOTS:
        for p in sorted((REPO / root).rglob("*.py")):
            if not _is_test(p.relative_to(REPO)):
                names = code_names(p.read_text(errors="replace"))
                if names:
                    found[p] = names
    return found


def selector_names() -> "frozenset[str]":
    """Every credential value or identity name those modules read (not the path selectors)."""
    return frozenset(n for names in credential_modules().values() for n in names
                     if n.endswith(SELECTOR_SUFFIXES))


@functools.lru_cache(maxsize=None)
def _patterns() -> "tuple[re.Pattern, re.Pattern]":
    counts: "dict[str, int]" = {}
    for p in REPO.rglob("*.py"):
        if "node_modules" not in p.parts:
            counts[p.name] = counts.get(p.name, 0) + 1
    refs, targets = [], []
    for p in credential_modules():
        rel = p.relative_to(REPO)
        # A file name shared by another module is only a reference with its directory.
        name = p.name if counts.get(p.name, 0) <= 1 else f"{p.parent.name}/{p.name}"
        refs.append(r"(?<![\w-])" + re.escape(name))
        targets.append(r"(?<![\w-])" + re.escape(name))
        if p.stem.isidentifier():
            stem = re.escape(p.stem)
            if counts.get(p.name, 0) <= 1:
                refs.append(rf"\bimport\s+{stem}\b|\bfrom\s+{stem}\s+import\b")
                targets.append(rf"^{stem}$")
            if rel.parts[0] == "packages":
                dotted = re.escape(".".join(rel.with_suffix("").parts[2:]))
                refs.append(rf"\b{dotted}\b")
                targets.append(rf"(^|\.){dotted}$")
    return re.compile("|".join(refs)), re.compile("|".join(targets))


def reference_pattern() -> "re.Pattern":
    """Matches a test's source that names a credential module."""
    return _patterns()[0]


def target_pattern() -> "re.Pattern":
    """Matches an imported module name, or a module loader's unparsed call, that runs one."""
    return _patterns()[1]
