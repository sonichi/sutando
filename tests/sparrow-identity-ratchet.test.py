#!/usr/bin/env python3
"""The identity ratchet (B slice 2): src/ may not grow new identity mints.

Two one-way gates over EVERY .py under src/ (recursive), pinned to today's
shipped sites:

R-A  Wall-clock task minting. The census showed a replayed provider event
     becomes a NEW task wherever the id comes from the clock; new code must
     use ag2_sparrow.identity.ingress_task_id. Detection is AST-based, so a
     mint is caught regardless of quote style or nesting: an f-string whose
     leading literal starts with "task-" and interpolates a value, a
     "task-..."​.format(...) call, and "task-..." + expr / "task-..." % expr.
     Sites are pinned per (file, enclosing function) so a removal elsewhere
     in the file cannot cancel a new mint. Above a pin is a new site (red);
     below means a site was strangled — lower the pin in the same change.

R-B  delivery_id constructor exclusivity: any src/ file (recursive) that
     names delivery_id must import the canonical constructors from
     ag2_sparrow.identity, or have every site pinned below. Both halves are
     AST-based: a comment or string cannot satisfy the import, a shadowed or
     privately-aliased binding is rejected, and the legacy exemption is per
     (file, function) site rather than whole-file — so a private constructor
     added to a legacy file is a new site, not an exempt one.

Positive controls at the bottom run the SAME scanners against hostile
fixtures (nested files, single quotes, .format, concatenation; comment and
string import spoofs, a shadowed import, a private alias, and a private
constructor smuggled into a pinned legacy file) so a silent scanner
regression reds this suite, not slice 3.

Run: python3 tests/sparrow-identity-ratchet.test.py   (stdlib only)
"""
from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"

# Shipped mint sites at the ratchet's introduction: (relpath, function) -> n.
TASK_MINT_PIN = {
    ("agent-api.py", "Handler.handle_twilio_voice"): 1,
    ("agent-api.py", "Handler.handle_twilio_sms"): 1,
    ("agent-api.py", "Handler.handle_twilio_transcription"): 1,
    ("agent-api.py", "Handler.do_POST"): 1,
    ("cron_task_id.py", "task_id"): 1,
    # wall-clock mint behind a constant prefix, pre-existing; visible only
    # since the scanner resolves module constants (this PR's widening).
    ("task_workstreams.py", "_maybe_enqueue_classifier_task_locked"): 1,
    ("discord-bridge.py", "_dedup_recover"): 1,
    ("discord-bridge.py", "_handle_discord_message"): 1,
    ("discord-bridge.py", "poll_results"): 1,
    ("github-webhook.py", "WebhookHandler.do_POST"): 1,
    ("health-check.py", "emit_task_for_failures"): 2,
    # +1 from main: a deterministic `task-PRIO<LEVEL>` fixture asserting priority is
    # serialized above `task:`. A fixed test id, not a runtime mint.
    ("remote-gateway-bridge.test.py", "main"): 5,
    ("slack-bridge.py", "_dedup_recover"): 1,
    ("slack-bridge.py", "_write_task"): 1,
    ("telegram-bridge.py", "_dedup_recover"): 1,
    ("telegram-bridge.py", "main"): 1,
    # Origination, not ingress: a client submits text over the local socket, so
    # there is no provider_event_id for ingress_task_id to be injective over.
    ("runtime-api/tasks_view.py", "TasksView.submit"): 1,
    # Same shape: the Signal Room originates a task from room speech; nothing
    # upstream carries a provider_event_id, so ingress_task_id has no domain.
    ("signal_room_tasks.py", "submit_signal_room_task"): 1,
}

# Pre-canonical delivery_id sites, pinned per (file, function) like
# TASK_MINT_PIN. A whole-file exemption here would hide a new constructor.
DELIVERY_ID_LEGACY_SITES = {
    ("slack-bridge.py", "result_watcher"): 5,
    ("slack_proactive_receipts.py", "_receipt_path"): 2,
    ("slack_proactive_receipts.py", "mark_delivered"): 3,
    ("slack_proactive_receipts.py", "was_delivered"): 2,
}

_DELIVERY_NAME = "delivery_id"
_IDENTITY_MODULE = "ag2_sparrow.identity"


def _scope_key(stack) -> str:
    """Qualified scope identity. A bare `stack[-1]` lets a removal in `A.mint`
    cancel a new site in `B.mint`, since both census as `mint`."""
    return ".".join(stack[1:]) or "<module>"


def _is_task_literal(node) -> bool:
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value.startswith("task-"))


def scan_task_mints(root: Path) -> dict:
    """(relpath, enclosing function) -> count of task-mint construction sites
    across every .py under root, regardless of quote style or spelling."""
    counts = {}

    def scan_file(py: Path) -> None:
        rel = py.relative_to(root).as_posix()
        try:
            tree = ast.parse(py.read_text(errors="replace"))
        except SyntaxError:
            counts[(rel, "<unparseable>")] = 1
            return
        stack = ["<module>"]

        # A template may be bound in ANY lexical scope: resolve innermost
        # first; a name rebound here to a non-template shadows an outer one.
        def _bindings(scope) -> dict:
            out = {}
            def _bind(target, value):
                # MAY, not last-write-wins: mutually exclusive branches both reach a use,
                # so one branch binding a template must not be erased by the other.
                if isinstance(target, ast.Name):
                    out[target.id] = out.get(target.id, False) or _is_task_literal(value)
            def _own(node):
                # This scope's statements, not a nested scope's: a nested
                # function's local cannot bind a template for its parent.
                yield node
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef, ast.Lambda)):
                    return
                for ch in ast.iter_child_nodes(node):
                    yield from _own(ch)
            for child in ast.iter_child_nodes(scope):
                for sub in _own(child):
                    if sub is child and isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                         ast.ClassDef, ast.Lambda)):
                        continue
                    if isinstance(sub, ast.Assign):
                        for tg in sub.targets:
                            _bind(tg, sub.value)
                    elif isinstance(sub, ast.AnnAssign) and sub.value is not None:
                        _bind(sub.target, sub.value)
                    elif isinstance(sub, (ast.AugAssign, ast.For, ast.AsyncFor,
                                          ast.withitem, ast.NamedExpr)):
                        tg = getattr(sub, "target", None) or getattr(sub, "optional_vars", None)
                        if isinstance(tg, ast.Name):
                            # setdefault, not assignment: these forms bind a non-template, but
                            # under MAY they must not erase a template bound on another branch.
                            out.setdefault(tg.id, False)
            return out

        scopes = [_bindings(tree)]

        def _is_template(name: str) -> bool:
            for sc in reversed(scopes):
                if name in sc:
                    return sc[name]
            return False

        def _leads_task(node) -> bool:
            if _is_task_literal(node):
                return True
            if isinstance(node, ast.Name) and _is_template(node.id):
                return True
            if (isinstance(node, ast.FormattedValue)
                    and isinstance(node.value, ast.Name)
                    and _is_template(node.value.id)):
                return True
            return False

        def record():
            key = (rel, _scope_key(stack))
            counts[key] = counts.get(key, 0) + 1

        class V(ast.NodeVisitor):
            def visit_FunctionDef(self, n):
                stack.append(n.name)
                scopes.append(_bindings(n))
                self.generic_visit(n)
                scopes.pop()
                stack.pop()
            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_ClassDef(self, n):
                # The class must enter the SCOPE KEY too, or `A.mint` and `B.mint` census
                # identically and a removal in one cancels a new site in the other.
                stack.append(n.name)
                scopes.append(_bindings(n))
                self.generic_visit(n)
                scopes.pop()
                stack.pop()

            def visit_JoinedStr(self, n):
                # A glob metacharacter in the literal half makes this a MATCHER
                # over existing names, not a new identity — `.glob(f"{P}*.txt")`.
                if (n.values and _leads_task(n.values[0])
                        and any(isinstance(v, ast.FormattedValue)
                                for v in n.values)
                        and not any(isinstance(v, ast.Constant)
                                    and isinstance(v.value, str)
                                    and any(g in v.value for g in "*?[")
                                    for v in n.values)):
                    record()
                self.generic_visit(n)

            def visit_Call(self, n):
                f = n.func
                # format_map mints exactly what format does; a scanner that
                # names one spelling invites the other.
                if (isinstance(f, ast.Attribute)
                        and f.attr in ("format", "format_map")
                        and _leads_task(f.value)):
                    record()
                self.generic_visit(n)

            def visit_BinOp(self, n):
                if (isinstance(n.op, (ast.Add, ast.Mod))
                        and _leads_task(n.left)
                        and not isinstance(n.right, ast.Constant)):
                    record()
                self.generic_visit(n)

        V().visit(tree)

    for py in sorted(root.rglob("*.py")):
        scan_file(py)
    return counts


def _identity_imports(tree) -> dict:
    """import node -> names it binds from the canonical package. Comments and
    string literals are invisible to the AST, so neither can satisfy this."""
    found = {}
    for node in ast.walk(tree):
        names = set()
        if isinstance(node, ast.ImportFrom):
            if node.module == _IDENTITY_MODULE:
                names = {a.asname or a.name for a in node.names}
            elif node.module == "ag2_sparrow":
                names = {a.asname or a.name for a in node.names
                         if a.name == "identity"}
        elif isinstance(node, ast.Import):
            names = {a.asname or a.name.split(".")[0] for a in node.names
                     if a.name == _IDENTITY_MODULE}
        if names:
            found[node] = names
    return found


def _rebound_names(tree, import_nodes) -> set:
    """Every name the module binds by any means OTHER than those imports.
    A canonical name that also appears here is shadowed: the identifier at a
    use site may resolve to the local definition, so the import proves
    nothing about what the file actually calls."""
    out = set()
    for node in ast.walk(tree):
        if node in import_nodes:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            out.update(a.asname or a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.arg):
            out.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            out.update(node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx,
                                                       (ast.Store, ast.Del)):
            out.add(node.id)
    return out


# Only the pure derivations certify a delivery id: `DeliveryId(...)` wraps
# any string and `parse_delivery_id` reads one back, so neither proves derivation.
_PURE_DERIVATIONS = frozenset({"delivery_id", "legacy_delivery_id",
                               "resend_delivery_id"})
_DELIVERY_CTORS = _PURE_DERIVATIONS


def canonical_delivery_bindings(tree) -> set:
    """Names in force that construct a delivery id canonically. A renamed,
    private, or shadowed alias is unfollowable across files, so it binds
    nothing here; neither does an unrelated import from the same package."""
    imports = _identity_imports(tree)
    if not imports:
        return set()
    out = set()
    for node, names in imports.items():
        # A module handle (import ag2_sparrow.identity / from ag2_sparrow
        # import identity) reaches every constructor through attribute access.
        handle = not (isinstance(node, ast.ImportFrom)
                      and node.module == _IDENTITY_MODULE)
        for a in node.names:
            root = a.name.split(".")[0] if isinstance(node, ast.Import) else a.name
            if a.asname and a.asname != root:
                continue
            name = a.asname or root
            if name.startswith("_") or name not in names:
                continue
            if handle or name in _DELIVERY_CTORS:
                out.add(name)
    return out


def _assigned_here(node) -> set:
    """Names this scope rebinds directly, not counting nested scopes — a
    sibling function's local cannot shadow the import for this one."""
    out = set()
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
            out.add(child.name)
            continue
        for sub in ast.walk(child):
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.ClassDef)):
                break
            if isinstance(sub, ast.Name) and isinstance(sub.ctx,
                                                        (ast.Store, ast.Del)):
                out.add(sub.id)
            elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                # A private import is a binding too: rebinding a canonical
                # name makes every later call in this scope private.
                out.update(_import_bindings(sub))
    return out


def _import_bindings(node) -> set:
    """Names an import binds, excluding the canonical package's own —
    those are the bindings the gate exists to recognise, not shadows."""
    if isinstance(node, ast.ImportFrom) and node.module in (_IDENTITY_MODULE,
                                                            "ag2_sparrow"):
        return set()
    if isinstance(node, ast.Import):
        return {a.asname or a.name.split(".")[0] for a in node.names
                if a.name != _IDENTITY_MODULE}
    return {a.asname or a.name.split(".")[0] for a in node.names}


def _scope_nodes(body_node):
    """Nodes of ONE lexical scope in source order; nested scopes are pruned,
    so a sibling function's locals never certify this one's names."""
    for child in ast.iter_child_nodes(body_node):
        yield child
        # A nested scope has its own walk; an assignment's subtree is
        # handled at the assignment, value before targets.
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef, ast.Assign, ast.AnnAssign, ast.If,
                              ast.Try, ast.Match)):
            continue
        yield from _scope_nodes(child)



def _canonical_scopes(tree, bindings: set) -> set:
    """Scope names in which a canonical constructor is actually CALLED. A file
    that merely imports one does not thereby make every scope in it canonical."""
    if not bindings:
        return set()
    found, stack = set(), ["<module>"]

    def called(fn):
        while isinstance(fn, ast.Attribute):
            fn = fn.value
        return isinstance(fn, ast.Name) and fn.id in bindings

    class C(ast.NodeVisitor):
        def _scoped(self, n):
            stack.append(n.name)
            self.generic_visit(n)
            stack.pop()
        visit_FunctionDef = _scoped
        visit_AsyncFunctionDef = _scoped
        visit_ClassDef = _scoped

        def visit_Call(self, n):
            if called(n.func):
                found.add(stack[-1])
            self.generic_visit(n)

    C().visit(tree)
    # Drop any scope that rebinds the constructor name it appeared to call.
    shadowing = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            shadowing[node.name] = _assigned_here(node)
    shadowing.setdefault("<module>", _assigned_here(tree))
    return {s for s in found if not (bindings & shadowing.get(s, set()))}


def has_canonical_binding(tree) -> bool:
    """True only where a canonical constructor is actually CALLED in some
    unshadowed scope. An import alone is a claim, not an adoption."""
    return bool(_canonical_scopes(tree, canonical_delivery_bindings(tree)))


def _certified_nodes(tree, bindings: set) -> set:
    """ids of AST nodes exempt from the delivery gate: the callee of a pure
    derivation, and any target/keyword/dict key whose value is that call or a
    local the same scope certified from one. Certification is per construction
    and per use — never per scope."""
    out = set()
    if not bindings:
        return out
    module_rebound = _assigned_here(tree)

    def pure_call(node, rebound) -> bool:
        if not isinstance(node, ast.Call):
            return False
        fn = node.func
        if isinstance(fn, ast.Name):
            return (fn.id in bindings and fn.id in _PURE_DERIVATIONS
                    and fn.id not in rebound)
        if isinstance(fn, ast.Attribute) and fn.attr in _PURE_DERIVATIONS:
            root = fn.value
            while isinstance(root, ast.Attribute):
                root = root.value
            return (isinstance(root, ast.Name) and root.id in bindings
                    and root.id not in rebound)
        return False

    def mark(node):
        out.update(id(n) for n in ast.walk(node))

    def walk_scope(body_node, outer_rebound):
        cert = set()
        # Shadowing is positional: a call before this scope rebinds the
        # constructor is canonical, the same call after it is private.
        shadowed = set()

        def rebound_now():
            return shadowed | outer_rebound

        def certified(v):
            return (pure_call(v, rebound_now())
                    or (isinstance(v, ast.Name) and v.id in cert))

        def shadow(name):
            shadowed.add(name)
            cert.discard(name)

        def visit_expr(expr):
            for node in ast.walk(expr):
                if isinstance(node, ast.Call) and pure_call(node, rebound_now()):
                    mark(node.func)
                elif isinstance(node, ast.keyword) and certified(node.value):
                    out.add(id(node))
                elif isinstance(node, ast.Dict):
                    for k, v in zip(node.keys, node.values):
                        if k is not None and certified(v):
                            mark(k)
                elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                        and node.id in cert:
                    out.add(id(node))

        def run(nodes):
            for node in nodes:
                step(node)

        def _arms_of(node):
            # Every construct whose branches are ALTERNATIVES, not a sequence. `finally`
            # is excluded on purpose: it runs on every path, so it is not an arm.
            if isinstance(node, ast.If):
                visit_expr(node.test)
                return [node.body, node.orelse or []]
            if isinstance(node, ast.Try):
                for h in node.handlers:
                    if h.type is not None:
                        visit_expr(h.type)
                return ([node.body + (node.orelse or [])]
                        + [h.body for h in node.handlers])
            visit_expr(node.subject)
            return [c.body for c in node.cases]

        def branch_merge(node):
            # Alternatives cannot both execute: certification survives only where EVERY
            # arm keeps it, while shadowing spreads from any arm so invalidation is never lost.
            base_c, base_s = set(cert), set(shadowed)
            arms = _arms_of(node)
            results = []
            for arm in arms:
                cert.clear(); cert.update(base_c)
                shadowed.clear(); shadowed.update(base_s)
                run(_scope_nodes(ast.Module(body=arm, type_ignores=[])))
                results.append((set(cert), set(shadowed)))
            merged_c = set.intersection(*(c for c, _ in results)) if results else base_c
            merged_s = set.union(*(sh for _, sh in results)) if results else base_s
            cert.clear(); cert.update(merged_c)
            shadowed.clear(); shadowed.update(merged_s)
            if isinstance(node, ast.Try) and node.finalbody:
                run(_scope_nodes(ast.Module(body=node.finalbody, type_ignores=[])))

        def step(node):
            if isinstance(node, (ast.If, ast.Try, ast.Match)):
                branch_merge(node)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for name in _import_bindings(node):
                    shadow(name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                shadow(node.name)
            elif isinstance(node, ast.arg):
                shadow(node.arg)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                ok = node.value is not None and certified(node.value)
                if node.value is not None:
                    visit_expr(node.value)
                for tg in targets:
                    if ok:
                        mark(tg)
                    for name in (n.id for n in ast.walk(tg) if isinstance(n, ast.Name)):
                        shadow(name)
                        if ok and isinstance(tg, ast.Name):
                            cert.add(name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                shadow(node.id)
            elif isinstance(node, ast.Call) and pure_call(node, rebound_now()):
                mark(node.func)
            elif isinstance(node, ast.keyword) and certified(node.value):
                out.add(id(node))
            elif isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if k is not None and certified(v):
                        mark(k)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                    and node.id in cert:
                out.add(id(node))

        run(_scope_nodes(body_node))

    # Each scope is walked once, lexically, with its own rebinding set.
    walk_scope(tree, set())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            walk_scope(node, module_rebound)
    return out


def scan_delivery_id_sites(root: Path) -> dict:
    """(relpath, enclosing function) -> count of delivery_id SITES, for every
    .py under root (recursive). A site is an identifier or string constant
    naming delivery_id — the places a delivery identity is defined, passed,
    or recorded. Files with a canonical binding contribute nothing."""
    counts = {}
    for py in sorted(root.rglob("*.py")):
        rel = py.relative_to(root).as_posix()
        try:
            tree = ast.parse(py.read_text(errors="replace"))
        except SyntaxError:
            counts[(rel, "<unparseable>")] = 1
            continue
        bindings = canonical_delivery_bindings(tree)
        exempt = _certified_nodes(tree, bindings)
        stack = ["<module>"]

        def record(node=None):
            # Per construction/use: only a node certified by a pure derivation
            # is exempt — never a scope, never a file.
            if node is not None and id(node) in exempt:
                return
            key = (rel, _scope_key(stack))
            counts[key] = counts.get(key, 0) + 1

        class V(ast.NodeVisitor):
            def _scoped(self, n):
                if n.name == _DELIVERY_NAME:
                    record(n)
                stack.append(n.name)
                self.generic_visit(n)
                stack.pop()
            visit_FunctionDef = _scoped
            visit_AsyncFunctionDef = _scoped
            visit_ClassDef = _scoped

            def visit_Name(self, n):
                if n.id == _DELIVERY_NAME:
                    record(n)
                self.generic_visit(n)

            def visit_Attribute(self, n):
                if n.attr == _DELIVERY_NAME:
                    record(n)
                self.generic_visit(n)

            def visit_arg(self, n):
                if n.arg == _DELIVERY_NAME:
                    record(n)
                self.generic_visit(n)

            def visit_keyword(self, n):
                if n.arg == _DELIVERY_NAME:
                    record(n)
                self.generic_visit(n)

            def visit_alias(self, n):
                # Importing the canonical constructor is ADOPTION, not a legacy
                # site — counting it would penalise the migration it exists for.
                name = n.asname or n.name
                if name == _DELIVERY_NAME and name not in bindings:
                    record(n)
                self.generic_visit(n)

            def visit_Constant(self, n):
                if isinstance(n.value, str) and _DELIVERY_NAME in n.value:
                    record(n)
                self.generic_visit(n)

        V().visit(tree)
    return counts


class WallClockTaskMintRatchet(unittest.TestCase):
    def test_no_new_mint_sites_and_pins_track_removals(self):
        counts = scan_task_mints(SRC)
        for key, n in sorted(counts.items()):
            pinned = TASK_MINT_PIN.get(key, 0)
            self.assertLessEqual(
                n, pinned,
                f"src/{key[0]} ({key[1]}) has {n} task-mint site(s), pin is "
                f"{pinned}. New task identities must come from "
                f"ag2_sparrow.identity.ingress_task_id (injective, "
                f"replay-stable), not the wall clock.")
            self.assertGreaterEqual(
                n, pinned,
                f"src/{key[0]} ({key[1]}) dropped below its pin "
                f"({n} < {pinned}) — a mint site was strangled. Lower "
                f"TASK_MINT_PIN in this test in the same change so the "
                f"ratchet records the progress.")
        for key in sorted(TASK_MINT_PIN):
            self.assertIn(key, counts,
                          f"pinned site src/{key[0]} ({key[1]}) has no mint "
                          f"sites left — remove its TASK_MINT_PIN entry.")


class DeliveryIdConstructorExclusivity(unittest.TestCase):
    def test_delivery_id_sites_are_canonical_or_pinned_legacy(self):
        counts = scan_delivery_id_sites(SRC)
        for key, n in sorted(counts.items()):
            pinned = DELIVERY_ID_LEGACY_SITES.get(key, 0)
            self.assertLessEqual(
                n, pinned,
                f"src/{key[0]} ({key[1]}) has {n} delivery_id site(s), pin is "
                f"{pinned}. A delivery identity may only come from "
                f"ag2_sparrow.identity — import the canonical constructors "
                f"(freeze doc R1/R3). A private constructor in a legacy file "
                f"is a new site, not an exempt one.")
            self.assertGreaterEqual(
                n, pinned,
                f"src/{key[0]} ({key[1]}) dropped below its pin "
                f"({n} < {pinned}) — a legacy site was migrated. Lower "
                f"DELIVERY_ID_LEGACY_SITES in this test in the same change.")
        for key in sorted(DELIVERY_ID_LEGACY_SITES):
            self.assertIn(key, counts,
                          f"pinned legacy site src/{key[0]} ({key[1]}) is "
                          f"gone — remove its DELIVERY_ID_LEGACY_SITES entry.")


class ScannerPositiveControls(unittest.TestCase):
    """The scanners must catch the shapes the ratchet exists to catch."""

    def _scan_fixture(self, relpath: str, source: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / relpath
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(source)
            return scan_task_mints(Path(tmp))

    def test_a_non_assign_binding_does_not_erase_a_branch_template(self):
        """`for T in ...` binds a non-template, but on a mutually exclusive branch it
        must not clobber a template bound by the other. Assign was made MAY-safe
        first; this is the same defect in the binding form that fix did not cover."""
        tmpl = ('def mint(flag, x):\n    if flag:\n%s\n    else:\n%s\n'
                '    return f"{T}{x}"\n')
        assign = '        T = "task-"'
        loop = '        for T in ["plain-"]:\n            pass'
        assign_first = self._scan_fixture("a.py", tmpl % (assign, loop))
        loop_first = self._scan_fixture("a.py", tmpl % (loop, assign))
        self.assertEqual(assign_first, loop_first,
                         "a For target erased a template bound on the other branch")
        self.assertEqual(loop_first, {("a.py", "mint"): 1})
        # Control: with no template on either branch there is nothing to count, so the
        # equality above cannot be satisfied by two empty censuses.
        self.assertEqual(
            self._scan_fixture("a.py", tmpl % ('        T = "plain-"', loop)), {})

    def test_scope_key_distinguishes_same_named_methods(self):
        """A bare scope name lets a removal in one class cancel a new site in
        another: both census as `mint`, so the net count never moves."""
        holder = ('class A:\n    def mint(self, x):\n%s\nclass B:\n'
                  '    def mint(self, x):\n%s\n')
        mints = '        T = "task-"\n        return f"{T}{x}"'
        plain = '        return x'
        in_a = self._scan_fixture("a.py", holder % (mints, plain))
        in_b = self._scan_fixture("a.py", holder % (plain, mints))
        self.assertNotEqual(in_a, in_b,
                            "moving the sole mint between classes left the census unchanged")
        self.assertEqual(in_a, {("a.py", "A.mint"): 1})
        self.assertEqual(in_b, {("a.py", "B.mint"): 1})
        # Control: a top-level function keeps its bare name, so qualifying the key does
        # not silently rewrite every existing pin.
        self.assertEqual(self._scan_fixture("a.py", 'def mint(x):\n    return f"task-{x}"\n'),
                         {("a.py", "mint"): 1})

    def test_branch_order_does_not_decide_whether_a_mint_is_seen(self):
        """Mutually exclusive branches both reach the use, so a template bound in
        either one must count regardless of which the source lists first."""
        tmpl = 'def mint(flag, x):\n    if flag:\n        T = "%s"\n    else:\n        T = "%s"\n    return f"{T}{x}"\n'
        task_first = self._scan_fixture("a.py", tmpl % ("task-", "plain-"))
        plain_first = self._scan_fixture("a.py", tmpl % ("plain-", "task-"))
        self.assertEqual(task_first, plain_first,
                         "source order changed the census of one program")
        self.assertEqual(plain_first, {("a.py", "mint"): 1},
                         "a task template on either branch must be counted")
        # Control: with no task template on either branch there is nothing to find,
        # so the equality above cannot be satisfied by two empty censuses.
        neither = self._scan_fixture("a.py", tmpl % ("plain-", "other-"))
        self.assertEqual(neither, {}, "control: neither branch binds a template")

    def test_catches_quote_styles_nesting_and_spellings(self):
        cases = {
            "top-level double-quoted": ("a.py", 'x = f"task-{ts}"\n'),
            "top-level single-quoted": ("a.py", "x = f'task-{ts}'\n"),
            "nested file": ("runtime-api/a.py",
                            'def f():\n    return f"task-{ts}"\n'),
            "str.format": ("a.py", 'x = "task-{}".format(ts)\n'),
            "str.format_map": ("a.py", 'x = "task-{ts}".format_map(d)\n'),
            "concatenation": ("a.py", 'x = "task-" + str(ts)\n'),
            "percent-format": ("a.py", 'x = "task-%s" % ts\n'),
        }
        for name, (rel, src) in cases.items():
            self.assertEqual(sum(self._scan_fixture(rel, src).values()), 1,
                             f"scanner missed the {name} mint shape")

    def test_catches_a_mint_behind_a_module_constant_template(self):
        # `T = "task-{stamp}"; T.format_map(d)` mints exactly what the literal
        # form does; the constant is one level of indirection, not a disguise.
        for src in ('T = "task-{stamp}"\nx = T.format_map(d)\n',
                    'T = "task-{}"\nx = T.format(ts)\n'):
            self.assertEqual(sum(self._scan_fixture("a.py", src).values()), 1,
                             src)
        self.assertEqual(self._scan_fixture("a.py", 'T = "task-static"\nx = T\n'),
                         {}, "a bare constant reference is not a mint")

    def test_catches_a_template_bound_one_lexical_scope_away(self):
        """A local template and a module-level annotated assignment both mint
        restart-unstable ids; a scanner that only reads top-level ast.Assign
        stays green on both. Positional shadowing: a name rebound in the
        function to a non-template does not resolve to the module template."""
        counts = self._scan_fixture("probe.py", (
            'import time\n'
            'TEMPLATE: str = "task-cron-{stamp}"\n'
            'OUTER = "task-{stamp}"\n'
            'def annotated():\n'
            '    return TEMPLATE.format(stamp=time.time())\n'
            'def local():\n'
            '    template = "task-{stamp}"\n'
            '    return template.format_map({"stamp": time.time()})\n'
            'def shadowed(name):\n'
            '    OUTER = name\n'
            '    return OUTER.format(stamp=time.time())\n'
            'def literal():\n'
            '    return f"task-{time.time()}"\n'
        ))
        self.assertEqual(counts, {("probe.py", "annotated"): 1,
                                  ("probe.py", "local"): 1,
                                  ("probe.py", "literal"): 1})

    def test_ignores_non_mint_task_strings(self):
        self.assertEqual(
            self._scan_fixture("a.py", 'x = "task-static"\ny = f"task-lit"\n'),
            {})

    def test_delivery_gate_sees_nested_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "runtime-api" / "b.py"
            f.parent.mkdir(parents=True)
            f.write_text("delivery_id = compute()\n")
            self.assertEqual(scan_delivery_id_sites(Path(tmp)),
                             {("runtime-api/b.py", "<module>"): 1})


class BranchCertificationIsPerPath(unittest.TestCase):
    """Mutually exclusive arms cannot both execute, so certification must hold on
    EVERY path to exempt a use, and an invalidation on any path must survive."""

    def _d(self, source: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "probe.py").write_text(source)
            return scan_delivery_id_sites(Path(tmp))

    HDR = "from ag2_sparrow.identity import delivery_id\n\n"
    CANON = '        k = delivery_id(item, ts)'
    RAW = "        k = f'{item}#{ts}'"
    TMPL = ("def mint(flag, item, ts, rec):\n    if flag:\n%s\n    else:\n%s\n"
            "    rec['delivery_id'] = k\n    return rec\n")

    def _arms(self, first, second):
        return self._d(self.HDR + self.TMPL % (first, second))

    def test_branch_order_does_not_decide_certification(self):
        self.assertEqual(self._arms(self.RAW, self.CANON),
                         self._arms(self.CANON, self.RAW),
                         "source order of the two arms changed the census")
        self.assertEqual(self._arms(self.RAW, self.CANON), {("probe.py", "mint"): 1},
                         "a raw value on either arm must leave the use counted")

    def test_every_alternative_construct_merges_per_path(self):
        """`if` is not the only place alternatives appear. try/except and match/case
        thread the same shared state, and `finally` runs on every path so it is not an arm."""
        C, R = "delivery_id(item, ts)", 'f"{item}#{ts}"'
        TRY = (self.HDR + 'def mint(item, ts, rec):\n    try:\n        k = %s\n'
               '    except Exception:\n        k = %s\n    rec["delivery_id"] = k\n    return rec\n')
        MAT = (self.HDR + 'def mint(x, item, ts, rec):\n    match x:\n        case 1:\n'
               '            k = %s\n        case _:\n            k = %s\n'
               '    rec["delivery_id"] = k\n    return rec\n')
        for name, tmpl in (("try/except", TRY), ("match/case", MAT)):
            with self.subTest(construct=name):
                self.assertEqual(self._d(tmpl % (C, R)), self._d(tmpl % (R, C)),
                                 f"{name}: arm order changed the census")
                self.assertEqual(self._d(tmpl % (R, C)), {("probe.py", "mint"): 1})
                self.assertEqual(self._d(tmpl % (C, C)), {},
                                 f"{name} control: every arm canonical stays exempt")
                self.assertEqual(self._d(tmpl % (R, R)), {("probe.py", "mint"): 1},
                                 f"{name} control: no arm canonical is counted")
        finally_rebinds = (self.HDR + 'def mint(item, ts, rec):\n    try:\n'
                           '        k = delivery_id(item, ts)\n    except Exception:\n'
                           '        k = delivery_id(item, ts)\n    finally:\n        k = "raw"\n'
                           '    rec["delivery_id"] = k\n    return rec\n')
        self.assertEqual(self._d(finally_rebinds), {("probe.py", "mint"): 1},
                         "`finally` runs on every path, so a rebind there revokes certification")

    def test_certification_still_works_and_invalidation_survives(self):
        # Without this pair, a change that simply stopped certifying anything would
        # pass the order test above -- that is exactly how the first attempt failed.
        self.assertEqual(self._arms(self.RAW, self.RAW), {("probe.py", "mint"): 1},
                         "control: neither arm is canonical, so the use is counted")
        self.assertEqual(self._arms(self.CANON, self.CANON), {},
                         "control: every arm canonical, so the use stays exempt")
        rebound = (self.HDR + 'def mint(flag, task):\n    d = delivery_id(task, "gw")\n'
                   '    if flag:\n        d = "raw"\n    return {"delivery_id": d}\n')
        self.assertEqual(self._d(rebound), {("probe.py", "mint"): 1},
                         "a rebind inside one arm must revoke certification made before it")


class DeliveryGateHostileControls(unittest.TestCase):
    """The exclusivity arm must not accept a *claimed* canonical import. Each
    fixture names delivery_id and would be gated; only a real, public,
    unshadowed import may clear it."""

    SPOOFS = {
        "comment_spoof.py":
            "# from ag2_sparrow.identity import delivery_id\n"
            'delivery_id = "d:" + str(1)\n',
        "string_spoof.py":
            'DOC = "from ag2_sparrow.identity import delivery_id"\n'
            'delivery_id = "d:" + str(1)\n',
        "real_import_shadowed.py":
            "from ag2_sparrow.identity import delivery_id\n"
            "def delivery_id(t, b):\n"
            '    return "d:%s@%s" % (t, b)\n'
            'x = delivery_id("t", "b")\n',
        "private_alias.py":
            "from ag2_sparrow.identity import delivery_id as _d\n"
            'delivery_id = "d:" + str(1)\n',
        "no_import_control.py": 'delivery_id = "d:" + str(1)\n',
    }

    def _canonical(self, source: str) -> bool:
        return has_canonical_binding(ast.parse(source))

    def test_a_sibling_scope_cannot_certify_this_scopes_local(self):
        # bad() records a private value under a local that good() later binds
        # canonically; certification is per scope and per binding order.
        src = ("from ag2_sparrow.identity import delivery_id\n"
               "def bad(d):\n"
               '    return {"delivery_id": d}\n'
               "def good(t):\n"
               '    d = delivery_id(t, "gw")\n'
               '    return {"delivery_id": d}\n')
        self.assertEqual(self._sites(src), {"bad": 1})
        # Same scope, but the use precedes the canonical binding.
        src = ("from ag2_sparrow.identity import delivery_id\n"
               "def f(t, d):\n"
               '    rec = {"delivery_id": d}\n'
               '    d = delivery_id(t, "gw")\n'
               "    return rec\n")
        self.assertEqual(self._sites(src), {"f": 1})

    def test_a_private_import_rebinding_the_constructor_is_not_canonical(self):
        src = ("from ag2_sparrow.identity import delivery_id\n"
               "def f(t):\n"
               '    d = delivery_id(t, "gw")\n'
               "    from private_mint import mint as delivery_id\n"
               "    d = delivery_id(t)\n"
               '    return {"delivery_id": d}\n')
        sites = self._sites(src)
        self.assertGreaterEqual(sites.get("f", 0), 1,
                                f"import shadow cleared the gate: {sites}")

    def test_no_spoof_or_shadow_clears_the_gate(self):
        for name, source in sorted(self.SPOOFS.items()):
            self.assertFalse(self._canonical(source),
                             f"{name} was accepted as canonical")

    def test_a_real_public_unshadowed_import_does_clear_the_gate(self):
        # Negative control for the controls: without this the arm could be
        # vacuously strict and nobody would notice.
        for source in (
            "from ag2_sparrow.identity import delivery_id\n"
            'x = delivery_id("t", "b")\n',
            "from ag2_sparrow import identity\n"
            'x = identity.delivery_id("t", "b")\n',
            "import ag2_sparrow.identity\n"
            'x = ag2_sparrow.identity.delivery_id("t", "b")\n',
        ):
            self.assertTrue(self._canonical(source), source)

    def _sites(self, source: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "probe.py").write_text(source)
            return {k[1]: v for k, v in scan_delivery_id_sites(Path(tmp)).items()}

    def test_an_unrelated_import_from_the_package_exempts_nothing(self):
        # The sibling type is CALLED here on purpose: an uncalled import is
        # already stopped by the per-scope rule, so it cannot test this one.
        self.assertEqual(
            self._sites("from ag2_sparrow.identity import TaskId\n"
                        "def f():\n"
                        '    t = TaskId("task-x")\n'
                        '    delivery_id = "d:" + str(1)\n'
                        "    return t, delivery_id\n"),
            {"f": 2}, "a sibling type cleared delivery_id sites")

    def test_exemption_is_per_scope_not_per_file(self):
        # One canonical call must not clear a sibling scope's private one.
        self.assertEqual(
            self._sites("from ag2_sparrow.identity import delivery_id\n"
                        "def good():\n"
                        '    return delivery_id("t", "g")\n'
                        "def legacy():\n"
                        '    delivery_id = "raw"\n'
                        "    return delivery_id\n"),
            {"legacy": 2}, "a canonical sibling scope cleared a legacy one")

    def test_adopting_the_canonical_constructor_is_not_itself_a_site(self):
        # Negative control: if adoption counted, migrating would red the
        # ratchet and the gate would punish the change it exists to drive.
        self.assertEqual(
            self._sites("from ag2_sparrow.identity import delivery_id\n"
                        "def good():\n"
                        '    return delivery_id("t", "g")\n'),
            {})

    # --- certification is per construction/use, and only pure derivations certify ---

    RAW_CTOR = ("from ag2_sparrow.identity import DeliveryId\n"
                "import time\n"
                "def f():\n"
                '    delivery_id = DeliveryId(f"d:task-{time.time_ns()}@gw")\n'
                "    return delivery_id\n")
    RAW_RECORD_BESIDE_PURE = ("from ag2_sparrow.identity import delivery_id\n"
                              "def f(task, rec):\n"
                              '    d = delivery_id(task, "gw")\n'
                              '    rec["delivery_id"] = "raw"\n'
                              "    return d\n")
    PARSER_IS_NOT_A_DERIVATION = ("from ag2_sparrow.identity import parse_delivery_id\n"
                                  "def f(s):\n"
                                  "    delivery_id = parse_delivery_id(s)\n"
                                  "    return delivery_id\n")
    CERTIFIED_FLOW = ("from ag2_sparrow.identity import delivery_id\n"
                      "def f(task, send):\n"
                      '    delivery_id = delivery_id(task, "gw")\n'
                      "    send(delivery_id=delivery_id)\n"
                      '    return {"delivery_id": delivery_id}\n')

    def test_raw_constructor_in_a_canonical_file_is_a_site(self):
        """R1's forbidden shape: a restart-derived string wrapped in the
        canonical type. The wrapper certifies nothing."""
        self.assertEqual(self._sites(self.RAW_CTOR), {"f": 2})

    def test_a_pure_call_does_not_clear_a_raw_record_beside_it(self):
        self.assertEqual(self._sites(self.RAW_RECORD_BESIDE_PURE), {"f": 1})

    def test_parsing_a_stored_value_is_not_a_derivation(self):
        self.assertEqual(self._sites(self.PARSER_IS_NOT_A_DERIVATION), {"f": 2})

    def test_a_certified_local_flows_into_keywords_and_records(self):
        """Positive control: adoption must stay free, including the uses of a
        value the same scope derived purely."""
        self.assertEqual(self._sites(self.CERTIFIED_FLOW), {})

    def test_private_constructor_in_a_legacy_file_reds_the_ratchet(self):
        """The exemption is per SITE, not per file: future delivery work
        cannot hide a private constructor in a pre-existing legacy file."""
        legacy_rel, legacy_fn = "slack-bridge.py", "result_watcher"
        self.assertIn((legacy_rel, legacy_fn), DELIVERY_ID_LEGACY_SITES)
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / legacy_rel
            f.write_text(
                "def result_watcher():\n"
                "    delivery_id = f.name\n"
                "    return delivery_id\n"
                "def _mint_delivery_id(task, boundary):\n"
                '    return "d:%s@%s" % (task, boundary)\n'
                "def _use():\n"
                '    delivery_id = _mint_delivery_id("t", "b")\n'
                "    return delivery_id\n")
            counts = scan_delivery_id_sites(Path(tmp))
        unpinned = {k: n for k, n in counts.items()
                    if n > DELIVERY_ID_LEGACY_SITES.get(k, 0)}
        self.assertTrue(
            unpinned,
            "a private delivery-id constructor added to a legacy file left "
            "the ratchet green")
        self.assertIn((legacy_rel, "_use"), unpinned)


if __name__ == "__main__":
    unittest.main()
