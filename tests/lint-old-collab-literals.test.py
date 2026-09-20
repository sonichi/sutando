#!/usr/bin/env python3
"""The old-spelling gate: a baseline of file -> count, refused only when it grows.

Drives the shipped script (imported by path, `main(argv, repo=...)`) against a
scratch git repo, because what it counts is `git ls-files` — an untracked file
must not count, a binary file must not crash it, and the baseline and the
script itself must never be evidence.

Run: python3 tests/lint-old-collab-literals.test.py  (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "lint-old-collab-literals.py"


def load():
    spec = importlib.util.spec_from_file_location("lint_old_collab_literals", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def scratch_repo(files: dict[str, bytes]) -> Path:
    d = Path(tempfile.mkdtemp(prefix="old-collab-"))
    subprocess.run(["git", "init", "-q", d], check=True)
    for rel, body in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    return d


def run(mod, repo: Path, *argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = mod.main(list(argv), repo=repo)
    return rc, out.getvalue()


failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "  FAIL") + name + (f": {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


mod = load()

# --- counting: tracked text only; the baseline and the script are not evidence
repo = scratch_repo({
    "a.py": b"room_doc = 1  # room-doc\nspace.ag2.doc.summon\nspace.ag2.document\n",
    "env.sh": b"export ROOM_DOC_TOKEN=x AG2_ROOM_DOC_URL=y\n",
    "docs/b.md": b"the room-doc tab\n",
    "scripts/lint-old-collab-literals.py": b"# room-doc room_doc room-doc\n",
    "scripts/old-collab-literals.baseline.json": b'{"room-doc": 1}\n',
    "blob.bin": bytes([0xFF, 0xFE, 0x00, 0x80]) + b"room-doc",
})
(repo / "untracked.py").write_text("room-doc room-doc\n", encoding="utf-8")
now = mod.counts(repo)
check("three spellings counted, the .document lookalike is not", now.get("a.py") == 3, repr(now))
check("markdown counts too", now.get("docs/b.md") == 1, repr(now))
check("the upper-case env aliases count", now.get("env.sh") == 2, repr(now))
check("the script and the baseline are skipped",
      "scripts/lint-old-collab-literals.py" not in now
      and "scripts/old-collab-literals.baseline.json" not in now, repr(now))
check("an untracked file does not count", "untracked.py" not in now, repr(now))
check("an undecodable file is skipped, not a crash", "blob.bin" not in now, repr(now))

# --- --update writes the baseline; a second run is then ok
rc, out = run(mod, repo, "--update")
check("--update exits 0 and reports the totals", rc == 0 and "3 files, 6 occurrences" in out, out)
written = json.loads((repo / "scripts" / "old-collab-literals.baseline.json").read_text())
check("baseline holds file -> count", written == {"a.py": 3, "docs/b.md": 1, "env.sh": 2}, repr(written))
rc, out = run(mod, repo)
check("unchanged tree is ok", rc == 0 and out.startswith("old-collab-literals: ok (6 occurrences in 3 files)"), out)

# --- growth in an existing file, and a new file, are each refused and named
(repo / "a.py").write_text("room_doc = 1\nroom-doc\nroom-doc\nroom-doc\n", encoding="utf-8")
(repo / "new.py").write_text("space.ag2.doc.invite\n", encoding="utf-8")
# Pathspec-limited on purpose: untracked.py stays untracked for the whole run.
subprocess.run(["git", "-C", str(repo), "add", "-A", "--", "a.py", "new.py"], check=True)
rc, out = run(mod, repo)
check("growth exits 1", rc == 1, out)
check("the grown file is named with baseline -> now", "  a.py: 3 -> 4" in out, out)
check("the new file is named from 0", "  new.py: 0 -> 1" in out, out)
check("the remedy names --update in the same commit", "--update in the same commit" in out, out)

# --- fewer occurrences is ok and invites locking the gain; a missing baseline means everything is new
(repo / "a.py").write_text("room_doc = 1\n", encoding="utf-8")
(repo / "new.py").unlink()
subprocess.run(["git", "-C", str(repo), "add", "-A", "--", "a.py", "new.py"], check=True)
rc, out = run(mod, repo)
check("shrinking is ok and says how much", rc == 0 and "2 fewer than the baseline" in out, out)
(repo / "scripts" / "old-collab-literals.baseline.json").unlink()
rc, out = run(mod, repo)
check("no baseline: every occurrence is growth", rc == 1 and "  a.py: 0 -> 1" in out, out)

# --- the real tree: the script's own root resolution and the committed baseline agree
rc, out = run(mod, mod.repo_root())
check("the repo's own baseline is current (run --update if this fails on purpose)", rc == 0, out)
check("repo_root is this checkout", mod.repo_root() == ROOT, str(mod.repo_root()))

if failures:
    print(f"lint-old-collab-literals: FAIL ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("lint-old-collab-literals: ok")
