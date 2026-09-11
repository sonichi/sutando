#!/usr/bin/env python3
"""Convergence gate ② prerequisite (issue #3279, construction layer):
delivery primitives have exactly one owner definition each.

An adapter that re-defines DeliveryOutcome, a ClaimBackend, or the marker
parser has forked delivery semantics — the private-copy drift class that
shipped real bugs (marker parsers that leaked control text; sparrow copies
whose drift surfaced as coverage failures). This test enumerates DEFINITION
sites by AST across src/ and the sparrow package: the owner set is pinned,
any new site fails by path, and the vendored twins must stay byte-identical
so "the copy" can never quietly become "a second implementation".
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "packages" / "ag2-sparrow" / "ag2_sparrow"

failures: list[str] = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


def rel(p: Path) -> str:
    return str(p.relative_to(REPO))


# ── enumerate definition sites over every production module ────────────────
scan_files = [p for p in list((REPO / "src").rglob("*.py")) + list(PKG.rglob("*.py"))
              if ".test" not in p.name]

sites: dict[str, set[str]] = {"DeliveryOutcome": set(), "ClaimBackend": set(),
                              "parse_markers": set()}
for f in scan_files:
    try:
        tree = ast.parse(f.read_text(), filename=str(f))
    except SyntaxError:
        failures.append(f"unparseable production module: {rel(f)}")
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            if node.name == "DeliveryOutcome":
                sites["DeliveryOutcome"].add(rel(f))
            if node.name.endswith("ClaimBackend"):
                sites["ClaimBackend"].add(rel(f))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "parse_markers":
                sites["parse_markers"].add(rel(f))

OWNERS = {
    "DeliveryOutcome": {
        "src/outbox.py",
        "packages/ag2-sparrow/ag2_sparrow/outbox.py",
        "packages/ag2-sparrow/ag2_sparrow/delivery_core/contract.py",
    },
    "ClaimBackend": {
        "packages/ag2-sparrow/ag2_sparrow/delivery_core/contract.py",
        "packages/ag2-sparrow/ag2_sparrow/delivery_core/backend_a.py",
        "packages/ag2-sparrow/ag2_sparrow/delivery_core/backend_c.py",
    },
    "parse_markers": {
        "src/result_markers.py",
        "packages/ag2-sparrow/ag2_sparrow/result_markers.py",
    },
}

for prim, owners in OWNERS.items():
    found = sites[prim]
    extra = found - owners
    missing = owners - found
    check(not extra,
          f"{prim}: no definition outside its owners (new: {sorted(extra)})")
    check(not missing,
          f"{prim}: every pinned owner still defines it (gone: {sorted(missing)})")

# ── claim-backend INSTANTIATION ratchet: site identity is (path, enclosing
#    function, constructor) with multiplicity — file-level sanction is too wide ──
_CTORS = ("DesignAClaimBackend", "DesignCClaimBackend")


def scan_instantiations(sources: dict) -> dict:
    """Reference ratchet: every mention of a constructor name counts, keyed
    path::enclosing_function::ctor with multiplicity. No aliasing analysis —
    default params, attributes, subscripts, factory args all count, because
    a call cannot exist without a reference (kewei r6 fail-closed rule)."""
    out: dict[str, int] = {}
    for path, text in sources.items():
        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError:
            continue
        recognized = set(_CTORS)
        # Local spelling -> the ctor it is BOUND to: a name equal to the OTHER
        # ctor must resolve by binding, or C-as-A reports A while building C.
        bound = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for a in node.names:
                    if a.name in _CTORS:
                        bound[a.asname or a.name] = a.name
                        recognized.add(a.asname or a.name)

        def hits(node):
            if isinstance(node, ast.Name):
                if node.id in bound:
                    yield bound[node.id]
                elif node.id in recognized and node.id in _CTORS:
                    yield node.id
            elif isinstance(node, ast.Attribute) and node.attr in _CTORS:
                yield node.attr

        def visit(node, enclosing):
            for child in ast.iter_child_nodes(node):
                enc = enclosing
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    enc = child.name
                if not isinstance(child, (ast.Import, ast.ImportFrom)):
                    for ctor in hits(child):
                        key = f"{path}::{enclosing}::{ctor}"
                        out[key] = out.get(key, 0) + 1
                visit(child, enc)

        visit(tree, "<module>")
    return out


def instantiation_violations(found: dict, owners: dict) -> list[str]:
    bad = [f"{k} x{n}" for k, n in sorted(found.items()) if k not in owners]
    bad += [f"{k} x{n} (pinned x{owners[k]})" for k, n in sorted(found.items())
            if k in owners and n > owners[k]]
    return bad


prod_sources = {rel(f): f.read_text() for f in scan_files}
INSTANTIATION_OWNERS = {
    "packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py::_delivery_core::DesignAClaimBackend": 1,
    # Discord's proactive leg is pinned migration debt, not precedent.
    "src/discord-bridge.py::_proactive_fence::DesignAClaimBackend": 1,
    # The one outbox coordinator for the Discord result leg: return annotation
    # plus the cached construction. A third reference here must fail the gate.
    "src/discord_result_delivery.py::result_backend::DesignAClaimBackend": 2,
}
viol = instantiation_violations(scan_instantiations(prod_sources),
                                INSTANTIATION_OWNERS)
check(not viol,
      f"claim backends instantiated only at pinned (file,function) sites: {viol}")

# ── CONTROL (kewei P1): a SECOND leg inside an already-sanctioned file must
#    fail and name the site — file-level sanction cannot absorb it ──────────
_db = "src/discord-bridge.py"
mutated = dict(prod_sources)
mutated[_db] = prod_sources[_db] + (
    "\n\ndef _second_leg_control():\n"
    "    return DesignCClaimBackend('improvised')\n")
mviol = instantiation_violations(scan_instantiations(mutated),
                                 INSTANTIATION_OWNERS)
check(any("_second_leg_control::DesignCClaimBackend" in v for v in mviol),
      "same-file second-leg mutation FAILS the gate and names its site")
# multiplicity control: a SECOND call in the SAME sanctioned function trips x-count
mutated2 = dict(prod_sources)
mutated2[_db] = prod_sources[_db] + (
    "\n\ndef _proactive_fence():\n"
    "    DesignAClaimBackend('one'); DesignAClaimBackend('two')\n")
mviol2 = instantiation_violations(scan_instantiations(mutated2),
                                  INSTANTIATION_OWNERS)
check(any("_proactive_fence::DesignAClaimBackend x3 (pinned x1)" in v for v in mviol2),
      "multiplicity ratchet: extra calls under a pinned key exceed its count")
# alias control (reviewer P1, permanent): a local rebinding is still a construction
mutated3 = dict(prod_sources)
mutated3[_db] = prod_sources[_db] + (
    "\n\ndef _alias_leg_control():\n"
    "    Backend = DesignCClaimBackend\n"
    "    return Backend('improvised')\n")
mviol3 = instantiation_violations(scan_instantiations(mutated3),
                                  INSTANTIATION_OWNERS)
check(any("_alias_leg_control::DesignCClaimBackend" in v for v in mviol3),
      "same-file aliased constructor mutation FAILS the gate and names its site")
# import-as control: `from … import DesignCClaimBackend as DC` + DC(...) counts
mutated4 = dict(prod_sources)
mutated4[_db] = prod_sources[_db] + (
    "\n\ndef _import_alias_leg():\n"
    "    from ag2_sparrow.delivery_core import DesignCClaimBackend as DC\n"
    "    return DC('improvised')\n")
mviol4 = instantiation_violations(scan_instantiations(mutated4),
                                  INSTANTIATION_OWNERS)
check(any("_import_alias_leg::DesignCClaimBackend" in v for v in mviol4),
      "import-as aliased constructor mutation FAILS the gate and names its site")
# kewei r7 P1a: alias whose LOCAL name equals the OTHER constructor. Identifying a
# ctor by local spelling reported A while constructing C at the A-sanctioned site.
_rgb = "packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py"
mutated4a = dict(prod_sources)
mutated4a[_rgb] = prod_sources[_rgb].replace(
    "DesignAClaimBackend", "DesignCClaimBackend as DesignAClaimBackend", 1)
_scan4a = scan_instantiations(mutated4a)
check(any(k.startswith(_rgb) and k.endswith("::DesignCClaimBackend")
          for k in _scan4a),
      "alias collision resolves to the IMPORTED ctor, not the local spelling")
check(not any(k.startswith(_rgb) and k.endswith("::DesignAClaimBackend")
              for k in _scan4a),
      "alias collision no longer reports the shadowed A name")
# qualified-attribute rebinding: Backend = dc.DesignCClaimBackend (reviewer P1 r3)
mutated5 = dict(prod_sources)
mutated5[_db] = prod_sources[_db] + (
    "\n\nimport ag2_sparrow.delivery_core.backend_c as _dc\n"
    "Backend = _dc.DesignCClaimBackend\n"
    "def _qualified_rebind_leg():\n"
    "    return Backend('improvised')\n")
mviol5 = instantiation_violations(scan_instantiations(mutated5),
                                  INSTANTIATION_OWNERS)
check(any("<module>::DesignCClaimBackend" in v for v in mviol5),
      "qualified-attribute rebinding mutation FAILS the gate and names its site")
# chained + annotated aliases (reviewer P1 r4): both permanent controls
mutated6 = dict(prod_sources)
mutated6[_db] = prod_sources[_db] + (
    "\n\nimport ag2_sparrow.delivery_core.backend_c as _dc2\n"
    "Backend = _dc2.DesignCClaimBackend\n"
    "Selected = Backend\n"
    "def _chained_alias_leg():\n"
    "    return Selected('improvised')\n")
mviol6 = instantiation_violations(scan_instantiations(mutated6),
                                  INSTANTIATION_OWNERS)
check(any("<module>::DesignCClaimBackend" in v for v in mviol6),
      "chained alias mutation FAILS the gate and names its site")
mutated7 = dict(prod_sources)
mutated7[_db] = prod_sources[_db] + (
    "\n\nimport ag2_sparrow.delivery_core.backend_c as _dc3\n"
    "BackendT: type = _dc3.DesignCClaimBackend\n"
    "def _annotated_alias_leg():\n"
    "    return BackendT('improvised')\n")
mviol7 = instantiation_violations(scan_instantiations(mutated7),
                                  INSTANTIATION_OWNERS)
check(any("<module>::DesignCClaimBackend" in v for v in mviol7),
      "annotated alias mutation FAILS the gate and names its site")
# conditional-selection alias (reviewer P1 r5, kewei's exact mutation shape):
# an IfExp choosing between BOTH constructors binds both — either leg counts.
mutated8 = dict(prod_sources)
mutated8[_db] = prod_sources[_db] + (
    "\n\nfrom ag2_sparrow.delivery_core import DesignAClaimBackend, "
    "DesignCClaimBackend\n"
    "def _selected_backend_leg(use_c):\n"
    "    Backend = DesignCClaimBackend if use_c else DesignAClaimBackend\n"
    "    return Backend('improvised')\n")
mviol8 = instantiation_violations(scan_instantiations(mutated8),
                                  INSTANTIATION_OWNERS)
check(any("_selected_backend_leg::DesignCClaimBackend" in v for v in mviol8),
      "conditional-selection mutation FAILS the gate (C leg named)")
check(any("_selected_backend_leg::DesignAClaimBackend" in v for v in mviol8),
      "conditional-selection mutation FAILS the gate (A leg named)")
# kewei r6 permanent controls: reference ratchet catches forms alias
# analysis cannot — default parameter, instance attribute, subscript, factory.
mutated9 = dict(prod_sources)
mutated9[_db] = prod_sources[_db] + (
    "\n\nfrom ag2_sparrow.delivery_core import DesignCClaimBackend\n"
    "def _default_backend_leg(Backend=DesignCClaimBackend):\n"
    "    return Backend('improvised')\n")
mviol9 = instantiation_violations(scan_instantiations(mutated9),
                                  INSTANTIATION_OWNERS)
check(any("_default_backend_leg::DesignCClaimBackend" in v for v in mviol9),
      "default-parameter mutation FAILS the gate and names its site")
mutated10 = dict(prod_sources)
mutated10[_db] = prod_sources[_db] + (
    "\n\nimport ag2_sparrow.delivery_core.backend_c as _dc9\n"
    "class _Holder:\n"
    "    def __init__(self):\n"
    "        self.Backend = _dc9.DesignCClaimBackend\n"
    "    def make(self):\n"
    "        return self.Backend('improvised')\n")
mviol10 = instantiation_violations(scan_instantiations(mutated10),
                                   INSTANTIATION_OWNERS)
check(any("__init__::DesignCClaimBackend" in v for v in mviol10),
      "instance-attribute mutation FAILS the gate at the binding site")
mutated11 = dict(prod_sources)
mutated11[_db] = prod_sources[_db] + (
    "\n\nimport ag2_sparrow.delivery_core.backend_c as _dc10\n"
    "def _registry_leg(kind):\n"
    "    REG = {'c': _dc10.DesignCClaimBackend}\n"
    "    return REG[kind]('improvised')\n")
mviol11 = instantiation_violations(scan_instantiations(mutated11),
                                   INSTANTIATION_OWNERS)
check(any("_registry_leg::DesignCClaimBackend" in v for v in mviol11),
      "registry-subscript mutation FAILS the gate and names its site")
mutated12 = dict(prod_sources)
mutated12[_db] = prod_sources[_db] + (
    "\n\nimport ag2_sparrow.delivery_core.backend_c as _dc11\n"
    "def _factory_leg(make):\n"
    "    return make('improvised')\n"
    "def _caller_leg():\n"
    "    return _factory_leg(_dc11.DesignCClaimBackend)\n")
mviol12 = instantiation_violations(scan_instantiations(mutated12),
                                   INSTANTIATION_OWNERS)
check(any("_caller_leg::DesignCClaimBackend" in v for v in mviol12),
      "factory-argument mutation FAILS the gate at the passing site")


# ── kewei r7: module-import ratchet — dynamic getattr needs the module, and
#    importing a concrete backend MODULE outside its package home fails closed ─
def _path_parts(dotted):
    return tuple(dotted.split("."))

def _is_backend_mod(dotted):
    parts = _path_parts(dotted)
    return ("delivery_core" in parts and
            (parts[-1] in ("backend_a", "backend_c")))

def _is_facade_mod(dotted):
    return _path_parts(dotted)[-1] == "delivery_core"

def _resolve_from(path, node):
    """Absolute dotted module for an ImportFrom, relative forms included.

    `node.module` is None for `from . import x`, so the raw value hides the
    facade entirely — the relative-facade false negative.
    """
    mod = node.module or ""
    if not getattr(node, "level", 0):
        return mod
    base = tuple(str(path).split("/")[:-1])
    if node.level > 1:
        base = base[:-(node.level - 1)] or ()
    return ".".join(base + ((mod,) if mod else ()))


# The owner package's own home. A substring test would exempt any nested
# src/**/delivery_core/, letting a new adapter opt itself out of the ratchet.
DELIVERY_CORE_HOME = "packages/ag2-sparrow/ag2_sparrow/delivery_core/"


def scan_backend_module_imports(sources: dict) -> list[str]:
    """Module ratchet: outside the owner package, importing a concrete backend
    module OR holding the facade as a module object fails closed — getattr
    needs a module object, and names must arrive by from-import instead."""
    out = []
    for path, text in sources.items():
        if path.startswith(DELIVERY_CORE_HOME):
            continue
        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if _is_backend_mod(a.name):
                        out.append(f"{path} imports {a.name}")
                    elif _is_facade_mod(a.name):
                        out.append(f"{path} imports facade module {a.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = _resolve_from(path, node)
                if _is_backend_mod(mod):
                    out.append(f"{path} imports from {mod}")
                elif _is_facade_mod(mod):
                    for a in node.names:
                        if a.name in ("backend_a", "backend_c"):
                            out.append(f"{path} imports {mod}.{a.name}")
                else:
                    # `from <pkg> import delivery_core` binds a MODULE object,
                    # the same getattr escape as a plain module import.
                    for a in node.names:
                        if a.name == "delivery_core":
                            out.append(f"{path} imports facade module "
                                       f"{mod}.{a.name}" if mod else
                                       f"{path} imports facade module {a.name}")
                        elif a.name in ("backend_a", "backend_c"):
                            out.append(f"{path} imports {mod}.{a.name}")
    return sorted(out)

# node.module is None for `from . import x`, so the unresolved value hid the
# facade entirely and construction through it escaped both scanners.
_rel = "packages/ag2-sparrow/ag2_sparrow/relative_delivery_leg.py"
mutated4r = dict(prod_sources)
mutated4r[_rel] = ("from . import delivery_core as dc\n"
                   "def leg():\n"
                   "    return getattr(dc, 'Design' + 'CClaimBackend')()\n")
check(any(_rel in v and "delivery_core" in v
          for v in scan_backend_module_imports(mutated4r)),
      "relative facade import FAILS the module ratchet and names its file")

mod_viol = scan_backend_module_imports(prod_sources)
check(not mod_viol,
      f"no file outside delivery_core imports a concrete backend module: {mod_viol}")

# kewei's exact dynamic-lookup mutation: getattr over the imported module —
# invisible to name scanning, caught at the import line by the module ratchet.
mutated13 = dict(prod_sources)
mutated13[_db] = prod_sources[_db] + (
    "\n\nimport ag2_sparrow.delivery_core.backend_c as dc\n"
    "def _dynamic_backend_leg():\n"
    "    Backend = getattr(dc, 'DesignC' + 'ClaimBackend')\n"
    "    return Backend('improvised')\n")
mv13 = scan_backend_module_imports(mutated13)
check(any(_db in v and "backend_c" in v for v in mv13),
      "dynamic getattr mutation FAILS the module-import ratchet at its import")
mutated14 = dict(prod_sources)
mutated14[_db] = prod_sources[_db] + (
    "\n\nfrom ag2_sparrow.delivery_core import backend_c as bc\n")
mv14 = scan_backend_module_imports(mutated14)
check(any(_db in v for v in mv14),
      "from-package submodule import FAILS the module-import ratchet")
# kewei r8: the same getattr through the PUBLIC FACADE module object —
# caught at the facade import line; names must arrive by from-import.
mutated15 = dict(prod_sources)
mutated15[_db] = prod_sources[_db] + (
    "\n\nimport ag2_sparrow.delivery_core as dcf\n"
    "def _facade_dynamic_leg():\n"
    "    Backend = getattr(dcf, 'DesignC' + 'ClaimBackend')\n"
    "    return Backend('improvised')\n")
mv15 = scan_backend_module_imports(mutated15)
check(any(_db in v and "facade module" in v for v in mv15),
      "facade-getattr mutation FAILS the ratchet at its module-object import")
mutated16 = dict(prod_sources)
mutated16[_db] = prod_sources[_db] + (
    "\n\nfrom ag2_sparrow import delivery_core as dcf2\n")
mv16 = scan_backend_module_imports(mutated16)
check(any(_db in v and "facade module" in v for v in mv16),
      "from-package facade module import FAILS the ratchet")
# path-component equality: a future sibling like backend_adapter is NOT a hit
mutated17 = dict(prod_sources)
mutated17[_db] = prod_sources[_db] + (
    "\n\nimport ag2_sparrow.delivery_core_backend_adapter_x as bax  # noqa\n")
mv17 = [v for v in scan_backend_module_imports(mutated17) if "backend_adapter" in v]
check(not mv17, "sibling-name module is NOT a false positive (path components)")

# the exemption is the package HOME, not any path containing delivery_core:
# a nested src/**/delivery_core/ must NOT be able to opt out of the ratchet
_nested = "src/observability/delivery_core/adapter.py"
mutated18 = dict(prod_sources)
mutated18[_nested] = "import ag2_sparrow.delivery_core as dcn  # noqa\n"
mv18 = scan_backend_module_imports(mutated18)
check(any(_nested in v and "facade module" in v for v in mv18),
      "nested src/**/delivery_core/ does NOT escape the module ratchet")
check(not scan_backend_module_imports(
          {DELIVERY_CORE_HOME + "adapter.py":
           "import ag2_sparrow.delivery_core as dch  # noqa\n"}),
      "the owner package's own home is still exempt (no false positive)")

# ── vendored twins are byte-identical (drift = a second implementation) ────
for name in ("outbox.py", "outbox_adapter.py", "result_markers.py"):
    a, b = REPO / "src" / name, PKG / name
    check(a.exists() and b.exists() and a.read_bytes() == b.read_bytes(),
          f"vendored twin byte-identical: src/{name} == ag2_sparrow/{name}")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
