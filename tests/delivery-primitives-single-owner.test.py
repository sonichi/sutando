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
    out: dict[str, int] = {}
    for path, text in sources.items():
        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError:
            continue
        # Aliases bind the SET of constructors referenced anywhere in the
        # RHS (any expression shape) — fail-closed over conditional selection.
        alias: dict[str, set] = {}
        pending: list = []

        def _refs(expr):
            names = set()
            for n in ast.walk(expr):
                nm = (n.id if isinstance(n, ast.Name) else
                      n.attr if isinstance(n, ast.Attribute) else None)
                if nm:
                    names.add(nm)
            return names

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                rhs = node.value
                tgts = [t for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                rhs = node.value
                tgts = [node.target] if isinstance(node.target, ast.Name) else []
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    if a.name in _CTORS and a.asname:
                        alias.setdefault(a.asname, set()).add(a.name)
                continue
            else:
                continue
            refs = _refs(rhs)
            direct = refs & set(_CTORS)
            indirect = refs - set(_CTORS)
            for t in tgts:
                if direct:
                    alias.setdefault(t.id, set()).update(direct)
                for src in indirect:
                    pending.append((t.id, src))
        changed = True
        while changed:
            changed = False
            for tgt_id, src_name in pending:
                if src_name in alias:
                    tgt_set = alias.setdefault(tgt_id, set())
                    if not alias[src_name] <= tgt_set:
                        tgt_set.update(alias[src_name])
                        changed = True

        def visit(node, enclosing):
            for child in ast.iter_child_nodes(node):
                enc = enclosing
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    enc = child.name
                if isinstance(child, ast.Call):
                    fn = child.func
                    name = fn.id if isinstance(fn, ast.Name) else (
                        fn.attr if isinstance(fn, ast.Attribute) else None)
                    ctors = ({name} if name in _CTORS
                             else alias.get(name or "", set()))
                    for ctor in sorted(ctors):
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
# qualified-attribute rebinding: Backend = dc.DesignCClaimBackend (reviewer P1 r3)
mutated5 = dict(prod_sources)
mutated5[_db] = prod_sources[_db] + (
    "\n\nimport ag2_sparrow.delivery_core.backend_c as _dc\n"
    "Backend = _dc.DesignCClaimBackend\n"
    "def _qualified_rebind_leg():\n"
    "    return Backend('improvised')\n")
mviol5 = instantiation_violations(scan_instantiations(mutated5),
                                  INSTANTIATION_OWNERS)
check(any("_qualified_rebind_leg::DesignCClaimBackend" in v for v in mviol5),
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
check(any("_chained_alias_leg::DesignCClaimBackend" in v for v in mviol6),
      "chained alias mutation FAILS the gate and names its site")
mutated7 = dict(prod_sources)
mutated7[_db] = prod_sources[_db] + (
    "\n\nimport ag2_sparrow.delivery_core.backend_c as _dc3\n"
    "BackendT: type = _dc3.DesignCClaimBackend\n"
    "def _annotated_alias_leg():\n"
    "    return BackendT('improvised')\n")
mviol7 = instantiation_violations(scan_instantiations(mutated7),
                                  INSTANTIATION_OWNERS)
check(any("_annotated_alias_leg::DesignCClaimBackend" in v for v in mviol7),
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

# ── vendored twins are byte-identical (drift = a second implementation) ────
for name in ("outbox.py", "outbox_adapter.py", "result_markers.py"):
    a, b = REPO / "src" / name, PKG / name
    check(a.exists() and b.exists() and a.read_bytes() == b.read_bytes(),
          f"vendored twin byte-identical: src/{name} == ag2_sparrow/{name}")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
