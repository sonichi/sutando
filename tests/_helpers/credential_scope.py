"""Production modules that read a gateway credential name, found by scanning the tree.

A module under src/, skills/ or packages/ is in scope when its code (not a comment or docstring)
holds a GATEWAY_*, RELAY_*, REMOTE_TASK_* or AG2_REMOTE_* name, or a credential-file selector such as
AG2_DEVICE_ENV, as a string constant, or when it imports a module in scope (to a fixpoint). A test
that names such a module, by file, import or dotted path, reaches credential discovery.
"""
from __future__ import annotations

import ast
import functools
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOTS = ("src", "skills", "packages")
CREDENTIAL_NAME = re.compile(r"(?:GATEWAY|RELAY|REMOTE_TASK|AG2_REMOTE)_[A-Z0-9_]+|AG2_DEVICE_ENV")
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


def _modules() -> "list[Path]":
    return [p for root in ROOTS for p in sorted((REPO / root).rglob("*.py")) if not _is_test(p.relative_to(REPO))]


@functools.lru_cache(maxsize=None)
def _name_counts() -> "dict[str, int]":
    counts: "dict[str, int]" = {}
    for p in REPO.rglob("*.py"):
        if "node_modules" not in p.parts:
            counts[p.name] = counts.get(p.name, 0) + 1
    return counts


def _dotted(p: Path) -> "str | None":
    rel = p.relative_to(REPO).with_suffix("")
    return ".".join(rel.parts[2:]) if rel.parts[0] == "packages" else None


def _keys(p: Path) -> "set[str]":
    """The names an import of `p` can use: its stem when that file name is unique, its dotted path."""
    keys = {p.stem} if p.stem.isidentifier() and _name_counts().get(p.name, 0) <= 1 else set()
    dotted = _dotted(p)
    return keys | ({dotted} if dotted else set())


def imported_names(p: Path, source: str) -> "set[str]":
    """Every module name `p`'s code imports, relative imports resolved against its package."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    package = (_dotted(p) or "").split(".")[:-1]
    out: "set[str]" = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            base = n.module or ""
            if n.level:
                base = ".".join(package[:len(package) - n.level + 1] + ([base] if base else []))
            out |= {base} | {f"{base}.{a.name}" if base else a.name for a in n.names}
    return out


@functools.lru_cache(maxsize=None)
def credential_modules() -> "dict[Path, frozenset[str]]":
    """Each production module that reads a credential name or imports such a module, with the
    names it reads (empty when it is in scope only by import)."""
    sources = {p: p.read_text(errors="replace") for p in _modules()}
    found = {p: names for p, src in sources.items() for names in [code_names(src)] if names}
    imports = {p: imported_names(p, src) for p, src in sources.items()}
    while True:
        keys = set().union(*(_keys(p) for p in found))
        added = {p: frozenset() for p in sources if p not in found and imports[p] & keys}
        if not added:
            return found
        found.update(added)


def selector_names() -> "frozenset[str]":
    """Every credential value or identity name those modules read (not the path selectors)."""
    return frozenset(n for names in credential_modules().values() for n in names
                     if n.endswith(SELECTOR_SUFFIXES))


def _trie(words) -> str:
    """One regex matching exactly `words`, factored by shared prefix: a flat alternation of a
    hundred escaped names costs a try per name at every position of every scanned file."""
    tree: dict = {}
    for w in words:
        node = tree
        for ch in w:
            node = node.setdefault(ch, {})
        node[""] = {}
    def emit(node) -> str:
        end = "" in node
        branches = [re.escape(ch) + emit(sub) for ch, sub in sorted(node.items()) if ch]
        if not branches:
            return ""
        body = branches[0] if len(branches) == 1 else "(?:" + "|".join(branches) + ")"
        return f"(?:{body})?" if end else body
    return f"(?:{emit(tree)})"


@functools.lru_cache(maxsize=None)
def _patterns() -> "tuple[re.Pattern, re.Pattern]":
    counts = _name_counts()
    names, stems, dotted = set(), set(), set()
    for p in credential_modules():
        rel = p.relative_to(REPO)
        # A file name shared by another module is only a reference with its directory.
        names.add(p.name if counts.get(p.name, 0) <= 1 else f"{p.parent.name}/{p.name}")
        if p.stem.isidentifier():
            if counts.get(p.name, 0) <= 1:
                stems.add(p.stem)
            if rel.parts[0] == "packages":
                dotted.add(".".join(rel.with_suffix("").parts[2:]))
    refs = [r"(?<![\w-])" + _trie(names)]
    targets = [r"(?<![\w-])" + _trie(names)]
    if stems:
        refs.append(rf"\bimport\s+{_trie(stems)}\b|\bfrom\s+{_trie(stems)}\s+import\b")
        targets.append(rf"^{_trie(stems)}$")
    if dotted:
        refs.append(rf"\b{_trie(dotted)}\b")
        targets.append(rf"(^|\.){_trie(dotted)}$")
    return re.compile("|".join(refs)), re.compile("|".join(targets))


def reference_pattern() -> "re.Pattern":
    """Matches a test's source that names a credential module."""
    return _patterns()[0]


def target_pattern() -> "re.Pattern":
    """Matches an imported module name, or a module loader's unparsed call, that runs one."""
    return _patterns()[1]
