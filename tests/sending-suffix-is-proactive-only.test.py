#!/usr/bin/env python3
"""`.sending` is claimed on the proactive family ONLY — never on a task result.

`task-orphan-check/SKILL.md` step 2b classified `results/<id>.txt.sending` (a
TASK result) as a live mid-delivery state meaning DONE. No code produces it:
every claim-by-rename site gates on the proactive family before applying the
suffix. The branch was dead, and the dead branch was not free — on 2026-08-02 it
was cited as a real completion namespace while reviewing #2525, which would have
added handling for a case that cannot arise.

So the fix is not only the doc line. This pins the invariant the doc now states,
because a documented claim with no enforcement rots back: if someone starts
claiming task results by rename, this fails and points at SKILL.md step 2b.

Deliberately checks the ENCLOSING GATE, not the presence of a literal string. A
first pass at this grepped `startswith("proactive-")` and reported telegram as
ungated — it gates on the *variable* `PROACTIVE_PREFIXES`, which a literal-only
pattern cannot see. The check below resolves that name.

Run: python3 tests/sending-suffix-is-proactive-only.test.py
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"

_passed = 0
_failed = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failed += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def resolve_prefix_tuples(text: str) -> dict:
    """NAME = ("a-", "b-") assignments, so a gate on a variable is readable."""
    out = {}
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Tuple, ast.List)):
            vals = [e.value for e in node.value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if not vals:
                continue
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = vals
    return out


def _gates_in(block, prefixes_by_name) -> list:
    found = [m.group(2) for m in
             re.finditer(r'startswith\((["\'])([^"\']+)\1\)', block)]
    for name, vals in prefixes_by_name.items():
        if re.search(rf"startswith\(.*\b{re.escape(name)}\b", block) or \
                re.search(rf"\bin\s+{re.escape(name)}\b", block):
            found.extend(vals)
    return found


def gates_before(lines, claim_idx, prefixes_by_name) -> "list | None":
    """Prefix strings gating the nearest enclosing results-dir loop, or None."""
    for i in range(claim_idx, max(0, claim_idx - 80), -1):
        if re.search(r"for\s+\w+\s+in\s+.*(RESULTS_DIR|results_dir)", lines[i]):
            return _gates_in("\n".join(lines[i:claim_idx + 1]), prefixes_by_name)
    return None


def func_body_gates(lines, claim_idx, prefixes_by_name) -> "list | None":
    """Inline gate inside the enclosing function (a method claim site like the
    5b fence, called via attribute so caller resolution cannot see it)."""
    for i in range(claim_idx, max(0, claim_idx - 80), -1):
        if re.match(r"\s*def\s+\w+", lines[i]):
            return _gates_in("\n".join(lines[i:claim_idx + 1]), prefixes_by_name)
    return None


def real_claim_lines(text: str) -> list:
    """Line numbers of ACTUAL `.with_suffix(".sending")` calls.

    Must be AST, not a text scan: `src/proactive_routing.py` describes the
    rename in its module docstring, and a regex over raw source counts that
    prose as a fourth claim site. (Same inflation that turned a WIRE gate
    count of 6 into 16 earlier — docstrings are not code.)
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr == "with_suffix" and node.args:
            a = node.args[0]
            if isinstance(a, ast.Constant) and a.value == ".sending":
                out.append(node.lineno)
    return out


def enclosing_func(text: str, lineno: int) -> "str | None":
    """Innermost function containing `lineno`."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= lineno <= getattr(node, "end_lineno", node.lineno):
                if best is None or node.lineno > best[0]:
                    best = (node.lineno, node.name)
    return best[1] if best else None


def call_sites(fname: str) -> list:
    """(file, lineno, gates) for every call to `fname` across src/."""
    out = []
    for py in sorted(SRC.glob("*.py")):
        text = py.read_text()
        if fname not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        lines, prefixes = text.splitlines(), resolve_prefix_tuples(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == fname:
                out.append((py.name, node.lineno,
                            gates_before(lines, node.lineno - 1, prefixes)))
    return out


# A claim moved into a shared helper has no enclosing results-dir loop, so its
# gate lives at the CALLERS. Follow one hop rather than treating it as ungated —
# the invariant is "every claim is gated", not "every claim is gated inline".
claim_sites = []
delegating_callers = 0
for py in sorted(SRC.glob("*.py")):
    text = py.read_text()
    if ".sending" not in text:
        continue
    lines = text.splitlines()
    prefixes = resolve_prefix_tuples(text)
    for lineno in real_claim_lines(text):
        gates = gates_before(lines, lineno - 1, prefixes)
        label = py.name
        if gates is None:
            fn = enclosing_func(text, lineno)
            callers = [c for c in call_sites(fn) if c[0] != py.name] if fn else []
            # EVERY caller must be gated; one ungated caller means an ungated claim.
            if callers and all(c[2] for c in callers):
                gates = [g for c in callers for g in c[2]]
                label = f"{py.name} (via {len(callers)} caller(s) of {fn}())"
                delegating_callers += len(callers)
            elif not callers:
                # Method claim sites are invoked via attribute calls, which the
                # Name-based caller scan cannot see; their gate must be inline.
                gates = func_body_gates(lines, lineno - 1, prefixes)
        claim_sites.append((label, lineno, gates))

# A zero-site run would make every assertion below vacuously true. Centralising
# REMOVES inline sites by design, so count claim PATHS: inline + delegating.
ok("found at least one claim-by-rename site to check",
   len(claim_sites) + delegating_callers >= 3,
   f"found {len(claim_sites)} inline site(s) + {delegating_callers} delegating caller(s)")

for fname, lineno, gates in claim_sites:
    has_proactive_family = bool(gates) and any(
        g.startswith(("proactive-", "briefing-", "insight-", "friction-")) for g in gates)
    ok(f"{fname}:{lineno} gates .sending on the proactive family",
       has_proactive_family,
       f"gates seen: {gates!r} — if a TASK result can now be claimed by rename, "
       f"update task-orphan-check/SKILL.md step 2b and name this site there")
    ok(f"{fname}:{lineno} does NOT gate on a task-result prefix",
       not (gates and any(g.startswith("task") for g in gates)),
       f"gates seen: {gates!r}")

# The doc must not re-acquire the phantom row.
skill = (REPO / "skills" / "task-orphan-check" / "SKILL.md").read_text()
ok("SKILL.md states the task form does not occur",
   "does not occur" in skill and "`results/<id>.txt.sending`" in skill,
   "step 2b no longer names the dead branch explicitly")

print(f"sending-suffix-is-proactive-only: {_passed}/{_passed + _failed} passed"
      + (f" — {_failed} FAILED" if _failed else ""))
raise SystemExit(1 if _failed else 0)
