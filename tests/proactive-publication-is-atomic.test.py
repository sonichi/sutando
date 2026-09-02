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

print("── delegation: no proactive producer writes results/ in place ──")
# Grepping for write_text would match the wrong sense; bind the call to a path
# expression that names a proactive result.
PRODUCERS = [
    REPO / "src" / "morning-briefing.py",
    REPO / "src" / "check-pending-questions.py",
    REPO / "skills" / "schedule-crons" / "scripts" / "codex-scheduler.py",
    REPO / "skills" / "deal-finder" / "scripts" / "scan.py",
]
# TypeScript producers write with writeFileSync; a final proactive name may
# only be reached through renameSync from a scratch name.
TS_PRODUCERS = [
    REPO / "src" / "task-bridge.ts",
    REPO / "src" / "live-agent-runtime.ts",
]


def ts_inplace_writes(path: Path) -> list[int]:
    src = path.read_text().splitlines()
    hits = []
    for i, line in enumerate(src, 1):
        m = re.search(r"writeFileSync\(\s*([A-Za-z_][A-Za-z0-9_]*)", line)
        if not m:
            continue
        var = m.group(1)
        if "proactive" not in var.lower():
            continue
        # The variable must be a scratch name (`...Tmp`) that a renameSync
        # then moves onto the final name.
        if var.lower().endswith("tmp"):
            continue
        hits.append(i)
    return hits


for prod in TS_PRODUCERS:
    check(f"{prod.name} publishes proactive files through a scratch name + renameSync",
          not ts_inplace_writes(prod), f"in-place writeFileSync at lines {ts_inplace_writes(prod)}")

# Agent-facing instructions are a producer too: an LLM told to "write
# results/proactive-{ts}.txt" writes the final name in place.
_discord = (REPO / "src" / "discord-bridge.py").read_text()
_bad = re.findall(r"[Ww]rite (?:a single proactive message to )?results/proactive-\{ts\}\.txt(?![^\n]*\.tmp)", _discord)
check("discord-bridge instructions publish proactive files via temp-and-rename", not _bad, str(_bad))


def inplace_writes(path: Path) -> list[int]:
    tree = ast.parse(path.read_text())
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in ("write_text", "write_bytes"):
            continue
        target = ast.unparse(node.func.value)
        if "proactive" in target.lower() or "result" in target.lower():
            hits.append(node.lineno)
    return hits


for prod in PRODUCERS:
    check(f"{prod.name} publishes through the owner", not inplace_writes(prod),
          f"in-place write at line(s) {inplace_writes(prod)}")

print("── control: the delegation scan CAN fire ──")
probe = Path(tempfile.mkdtemp(prefix="pub-")) / "probe.py"
probe.write_text("result_file.write_text('partial')\n")
check("a planted in-place proactive write is detected", inplace_writes(probe) == [1])

print(f"\n{'FAIL' if fails else 'OK'} — {len(fails)} failure(s)")
raise SystemExit(1 if fails else 0)
