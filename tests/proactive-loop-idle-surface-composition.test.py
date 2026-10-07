#!/usr/bin/env python3
"""The documented step-6.5 sequence, end to end across both CLIs.

The component suites exercise `idle-held.py` and `idle-surface-hash.py` apart;
neither can catch a documented pipe that drops one side's persistence.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HELD = REPO / "skills" / "proactive-loop" / "scripts" / "idle-held.py"
HASH = REPO / "skills" / "proactive-loop" / "scripts" / "idle-surface-hash.py"

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + ("" if cond else f"   {detail}"))
    if not cond:
        fails.append(name)


def seed(state: Path) -> None:
    state.write_text(json.dumps({"held_item_ids": [["old", "owner"]]}))


def run(state: Path, held_extra: list[str], hash_extra: list[str]):
    a = subprocess.run([sys.executable, str(HELD), "--state", str(state),
                        "--remove", "old", "--reason", "superseded",
                        "--add", "new:ci", "--note", "owner/repo#42", *held_extra],
                       capture_output=True, text=True)
    b = subprocess.run([sys.executable, str(HASH), "--state", str(state), *hash_extra],
                       input=a.stdout, capture_output=True, text=True)
    return a, b, json.loads(state.read_text())


print("proactive-loop idle surface: the documented composition")

with tempfile.TemporaryDirectory() as d:
    st = Path(d) / "idle.json"
    seed(st)
    a, b, doc = run(st, ["--write"], ["--commit"])
    check("documented pipe: both CLIs exit 0", a.returncode == 0 and b.returncode == 0,
          f"{a.returncode}/{b.returncode} {a.stderr[:120]}{b.stderr[:120]}")
    check("documented pipe: the held EDIT persists",
          doc.get("held_item_ids") == [["new", "ci"]], json.dumps(doc.get("held_item_ids")))
    check("documented pipe: the note persists alongside it",
          (doc.get("held_item_notes") or {}).get("new") == "owner/repo#42",
          json.dumps(doc.get("held_item_notes")))

    # The next unchanged pass must be quiet: an FYI that repeats itself trains the
    # owner to ignore the surface that exists to interrupt them.
    a2 = subprocess.run([sys.executable, str(HELD), "--state", str(st)],
                        capture_output=True, text=True)
    b2 = subprocess.run([sys.executable, str(HASH), "--state", str(st)],
                        input=a2.stdout, capture_output=True, text=True)
    check("documented pipe: an unchanged next pass is quiet",
          b2.stdout.startswith("quiet"),
          f"held rc={a2.returncode} out={b2.stdout.strip()[:90]} err={b2.stderr[:90]}")

with tempfile.TemporaryDirectory() as d:
    st = Path(d) / "idle.json"
    seed(st)
    _, _, only_commit = run(st, [], ["--commit"])
    check("CONTROL: --commit alone loses the edit, which is why both flags are documented",
          only_commit.get("held_item_ids") == [["old", "owner"]],
          json.dumps(only_commit.get("held_item_ids")))

print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all passed'}")
sys.exit(1 if fails else 0)
