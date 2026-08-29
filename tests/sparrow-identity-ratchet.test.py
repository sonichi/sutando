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
    ("agent-api.py", "handle_twilio_voice"): 1,
    ("agent-api.py", "handle_twilio_sms"): 1,
    ("agent-api.py", "handle_twilio_transcription"): 1,
    ("agent-api.py", "do_POST"): 1,
    ("cron_task_id.py", "task_id"): 1,
    # wall-clock mint behind a constant prefix, pre-existing; visible only
    # since the scanner resolves module constants (this PR's widening).
    ("task_workstreams.py", "_maybe_enqueue_classifier_task_locked"): 1,
    ("discord-bridge.py", "_dedup_recover"): 1,
    ("discord-bridge.py", "_handle_discord_message"): 1,
    ("discord-bridge.py", "poll_results"): 1,
    ("github-webhook.py", "do_POST"): 1,
    ("health-check.py", "emit_task_for_failures"): 2,
    ("remote-gateway-bridge.test.py", "main"): 4,
    ("slack-bridge.py", "_dedup_recover"): 1,
    ("slack-bridge.py", "_write_task"): 1,
    ("telegram-bridge.py", "_dedup_recover"): 1,
    ("telegram-bridge.py", "main"): 1,
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
        # A mint literal can hide behind a module constant (TASK_PREFIX = "task-cron-"),
        # so resolve one level — else a delegating refactor hides the construction.
        const_names = {
            t.id for node in tree.body if isinstance(node, ast.Assign)
            for t in node.targets if isinstance(t, ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and node.value.value.startswith("task-")
        }

        def _leads_task(node) -> bool:
            if _is_task_literal(node):
                return True
            if isinstance(node, ast.Name) and node.id in const_names:
                return True
            if (isinstance(node, ast.FormattedValue)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in const_names):
                return True
            return False

        def record():
            key = (rel, stack[-1])
            counts[key] = counts.get(key, 0) + 1

        class V(ast.NodeVisitor):
            def visit_FunctionDef(self, n):
                stack.append(n.name)
                self.generic_visit(n)
                stack.pop()
            visit_AsyncFunctionDef = visit_FunctionDef

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
                if (isinstance(f, ast.Attribute) and f.attr == "format"
                        and _is_task_literal(f.value)):
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


# Names from the canonical package that actually MINT a delivery identity.
# Importing a sibling type (TaskId) says nothing about delivery ids.
_DELIVERY_CTORS = frozenset({"DeliveryId", "delivery_id", "parse_delivery_id",
                             "identity"})


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
        return out

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
        exempt = _canonical_scopes(tree, bindings)
        stack = ["<module>"]

        def record():
            # Site-aware: only the scopes that actually call a canonical
            # constructor are exempt, never the whole file.
            if stack[-1] in exempt:
                return
            key = (rel, stack[-1])
            counts[key] = counts.get(key, 0) + 1

        class V(ast.NodeVisitor):
            def _scoped(self, n):
                if n.name == _DELIVERY_NAME:
                    record()
                stack.append(n.name)
                self.generic_visit(n)
                stack.pop()
            visit_FunctionDef = _scoped
            visit_AsyncFunctionDef = _scoped
            visit_ClassDef = _scoped

            def visit_Name(self, n):
                if n.id == _DELIVERY_NAME:
                    record()
                self.generic_visit(n)

            def visit_Attribute(self, n):
                if n.attr == _DELIVERY_NAME:
                    record()
                self.generic_visit(n)

            def visit_arg(self, n):
                if n.arg == _DELIVERY_NAME:
                    record()
                self.generic_visit(n)

            def visit_keyword(self, n):
                if n.arg == _DELIVERY_NAME:
                    record()
                self.generic_visit(n)

            def visit_alias(self, n):
                # Importing the canonical constructor is ADOPTION, not a legacy
                # site — counting it would penalise the migration it exists for.
                name = n.asname or n.name
                if name == _DELIVERY_NAME and name not in bindings:
                    record()
                self.generic_visit(n)

            def visit_Constant(self, n):
                if isinstance(n.value, str) and _DELIVERY_NAME in n.value:
                    record()
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

    def test_catches_quote_styles_nesting_and_spellings(self):
        cases = {
            "top-level double-quoted": ("a.py", 'x = f"task-{ts}"\n'),
            "top-level single-quoted": ("a.py", "x = f'task-{ts}'\n"),
            "nested file": ("runtime-api/a.py",
                            'def f():\n    return f"task-{ts}"\n'),
            "str.format": ("a.py", 'x = "task-{}".format(ts)\n'),
            "concatenation": ("a.py", 'x = "task-" + str(ts)\n'),
            "percent-format": ("a.py", 'x = "task-%s" % ts\n'),
        }
        for name, (rel, src) in cases.items():
            self.assertEqual(sum(self._scan_fixture(rel, src).values()), 1,
                             f"scanner missed the {name} mint shape")

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
