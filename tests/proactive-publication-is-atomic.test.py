#!/usr/bin/env python3
"""Proactive publication is atomic, so a claim can never retire a partial body.

A poller claims results/proactive-*.txt on sight: it hard-links the inode and
unlinks the name. If a producer writes in place, the claim can link a body that
is still being written, send the prefix, and destroy the name the rest would
have arrived at — the notification is unrecoverable, not merely late.

Run: python3 tests/proactive-publication-is-atomic.test.py   (stdlib only)
"""
from __future__ import annotations

import ast
import re
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import delivery.publication as pub  # noqa: E402
from delivery.readiness import read_ready_result  # noqa: E402

publish_result = pub.publish_result

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f" — {detail}"))
    if not cond:
        fails.append(label)


print("── the destination name appears only once the body is whole ──")
# Racing a poller is not a control: one write_text is one syscall, so the
# partial state is unobservable and the assertion could never go red.
d = Path(tempfile.mkdtemp(prefix="pub-"))
dest = d / "proactive-race.txt"
BODY = "[channel: discord]\n" + ("x" * 200_000) + "\ntail-marker\n"
seen = {}
real_replace, real_mkstemp = os.replace, tempfile.mkstemp


def spy_mkstemp(*a, **kw):
    fd, name = real_mkstemp(*a, **kw)
    seen["scratch"] = name
    return fd, name


def spy_replace(src, dst):
    # At the instant of publication: the body must already be whole, and the
    # deliverable name must not yet exist for a poller to claim.
    seen["staged_bytes"] = Path(src).stat().st_size
    seen["dest_existed"] = Path(dst).exists()
    return real_replace(src, dst)


pub.tempfile.mkstemp, pub.os.replace = spy_mkstemp, spy_replace
try:
    publish_result(dest, BODY)
finally:
    pub.tempfile.mkstemp, pub.os.replace = real_mkstemp, real_replace

check("the body is staged elsewhere first", "scratch" in seen,
      "no scratch file — the body was written at the deliverable name")
check("the whole body is on disk before the name appears",
      seen.get("staged_bytes") == len(BODY.encode()),
      f"staged {seen.get('staged_bytes')} of {len(BODY.encode())} bytes")
check("the deliverable name did not exist before that instant",
      seen.get("dest_existed") is False, seen.get("dest_existed"))
check("and it holds the whole body afterwards", dest.read_text() == BODY)
check("nothing partial is left behind",
      not [q for q in d.iterdir() if q.name.startswith(".")],
      sorted(q.name for q in d.iterdir()))

print("── the scratch name is invisible to a proactive glob ──")
d2 = Path(tempfile.mkdtemp(prefix="pub-"))
scratch_names = []
real_mk = tempfile.mkstemp


def record_mk(*a, **kw):
    fd, name = real_mk(*a, **kw)
    scratch_names.append(Path(name).name)
    return fd, name


pub.tempfile.mkstemp = record_mk
try:
    publish_result(d2 / "proactive-glob.txt", "body\n")
finally:
    pub.tempfile.mkstemp = real_mk
check("the scratch name matches no proactive-*.txt glob",
      scratch_names and not list(d2.glob("proactive-*.txt.tmp"))
      and all(n.startswith(".") for n in scratch_names),
      scratch_names)

print("── readiness agrees the published body is deliverable ──")
d3 = Path(tempfile.mkdtemp(prefix="pub-"))
p3 = publish_result(d3 / "proactive-ready.txt", "the answer\n")
check("read_ready_result returns the body", read_ready_result(p3) == "the answer")

print("── delegation: no proactive producer writes results/ in place (repo census) ──")
# A fixed list is a census of what someone remembered; the scan enumerates
# every file under the roots so a new producer cannot ship outside the check.
SCAN_ROOTS = [REPO / "src", REPO / "skills", REPO / "scripts",
              REPO / "packages" / "ag2-sparrow" / "ag2_sparrow"]


def census(suffixes):
    for root in SCAN_ROOTS:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            sp = str(p)
            if not p.is_file() or p.suffix not in suffixes:
                continue
            if "/tests/" in sp or ".test." in p.name or "node_modules" in sp or "/__pycache__/" in sp:
                continue
            yield p


def _walk_scope(stmt):
    """ast.walk that does not descend into nested function/class bodies."""
    stack = [stmt]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            stack.append(child)


def _proactive_taint(scope) -> set:
    """Names assigned from an expression naming `proactive` inside `scope`,
    nested functions excluded (a `path` in one function says nothing about a
    `path` in another)."""
    names = set()
    for stmt in getattr(scope, "body", []):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for node in _walk_scope(stmt):
            if isinstance(node, ast.Assign) and "proactive" in ast.unparse(node.value).lower():
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _write_target(node) -> "str | None":
    """The path expression a write_text/write_bytes/open(...,'w'|'a') call
    writes to, else None."""
    if isinstance(node.func, ast.Attribute) and node.func.attr in ("write_text", "write_bytes"):
        return ast.unparse(node.func.value)
    if isinstance(node.func, ast.Name) and node.func.id == "open" and node.args:
        mode = ""
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            mode = str(node.args[1].value)
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode = str(kw.value.value)
        if any(m in mode for m in "wax"):
            return ast.unparse(node.args[0])
    return None


def inplace_writes(path: Path) -> list[int]:
    """Lines that write a proactive name in place: a write on a target naming
    `proactive`, or on a variable assigned from such an expression in the
    same scope."""
    try:
        tree = ast.parse(path.read_text(errors="replace"))
    except SyntaxError:
        return []
    module_taint = _proactive_taint(tree)
    hits = set()
    scopes = [tree] + [n for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for scope in scopes:
        tainted = module_taint | (_proactive_taint(scope) if scope is not tree else set())
        for stmt in getattr(scope, "body", []):
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for node in _walk_scope(stmt):
                if not isinstance(node, ast.Call):
                    continue
                target = _write_target(node)
                if target is None:
                    continue
                if "proactive" in target.lower() or target in tainted:
                    hits.add(node.lineno)
    return sorted(hits)


SH_INPLACE = re.compile(r">>?\s*\"?[^\"\s]*results/proactive-[^\"\s]*\.txt\"?(?:\s|$)")


def sh_inplace_writes(path: Path) -> list[int]:
    return [i for i, line in enumerate(path.read_text(errors="replace").splitlines(), 1)
            if SH_INPLACE.search(line)]


def ts_inplace_writes(path: Path) -> list[int]:
    hits = []
    for i, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
        m = re.search(r"writeFileSync\(\s*([A-Za-z_][A-Za-z0-9_]*)", line)
        if not m:
            continue
        var = m.group(1)
        if "proactive" not in var.lower() or var.lower().endswith("tmp"):
            continue
        hits.append(i)
    return hits


for prod in census({".py"}):
    if "proactive" not in prod.read_text(errors="replace").lower():
        continue
    bad = inplace_writes(prod)
    check(f"{prod.relative_to(REPO)} publishes proactive files through the owner", not bad,
          f"in-place write at line(s) {bad}")
for prod in census({".sh"}):
    bad = sh_inplace_writes(prod)
    check(f"{prod.relative_to(REPO)} publishes proactive files via a scratch name + mv", not bad,
          f"redirect onto a final proactive name at line(s) {bad}")
for prod in census({".ts"}):
    bad = ts_inplace_writes(prod)
    check(f"{prod.relative_to(REPO)} publishes proactive files through a scratch name + renameSync",
          not bad, f"in-place writeFileSync at lines {bad}")

# Agent-facing instructions are producers too: an LLM told to "write
# results/proactive-{ts}.txt" writes the final name in place.
INSTRUCTION = re.compile(r"(?i)\b(?:write|writing|save|emit|echo|create)\b[^\n]{0,80}results/proactive-(?:[^\s`'\"()]|\([^)]*\))*\.txt")
COMPLIANT = re.compile(r"\.tmp|\bmv\b|publish_result")


def bad_instructions(path: Path) -> list[int]:
    return [i for i, line in enumerate(path.read_text(errors="replace").splitlines(), 1)
            if INSTRUCTION.search(line) and not COMPLIANT.search(line)]


INSTRUCTION_FILES = [REPO / "CLAUDE.md", REPO / "AGENTS.md"] + \
    sorted((REPO / "skills").rglob("*.md")) + list(census({".py"}))
for doc in INSTRUCTION_FILES:
    if not doc.is_file() or "node_modules" in str(doc):
        continue
    bad = bad_instructions(doc)
    check(f"{doc.relative_to(REPO)} instructs proactive publication via temp-and-rename", not bad,
          f"in-place instruction at line(s) {bad}")

print("── control: the delegation scan CAN fire ──")
probe = Path(tempfile.mkdtemp(prefix="pub-")) / "probe.py"
probe.write_text("proactive_file.write_text('partial')\n")
check("a planted in-place proactive write is detected", inplace_writes(probe) == [1])
probe.write_text("p = results / 'proactive-x.txt'\nwith open(p, 'w') as fh:\n    fh.write('partial')\n")
check("a planted open(...,'w') on a proactive name is detected", inplace_writes(probe) == [2])
sh = probe.with_suffix(".sh"); sh.write_text('echo hi > "$WS/results/proactive-1.txt"\nprintf x > "$out.tmp-$$" && mv "$out.tmp-$$" "$out"\n')
check("a planted shell redirect onto a final proactive name is detected", sh_inplace_writes(sh) == [1])
md = probe.with_suffix(".md"); md.write_text("Write the digest to results/proactive-x-$(date +%s).txt\nPublish results/proactive-y.txt: write results/.proactive-y.txt.tmp then `mv` it\n")
check("a planted in-place instruction is detected and the compliant form is not", bad_instructions(md) == [1])

print(f"\n{'FAIL' if fails else 'OK'} — {len(fails)} failure(s)")
raise SystemExit(1 if fails else 0)
