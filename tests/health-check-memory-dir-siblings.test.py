#!/usr/bin/env python3
"""Tests for check_memory_dir_siblings().

check_memory_dir_override() catches only the SUTANDO_MEMORY_DIR flavour of
"health-check reports on a directory the agent isn't writing". It returns None
when the var is unset, and the slug-split flavour needs no env var at all: two
project-slug derivations produce two sibling project dirs under one claude-home,
each with its own memory/.

Field-observed on two hosts, one per mechanism — one had the env override
(caught), one had the slug split (silent: memory-dir reported "ok, 66 .md files"
about a corpus the session had never written to, while its live corpus held 42).

The load-bearing case here is the symlink twin: two slug strings very often
point at ONE inode (a compatibility symlink bridging two derivation rules). If
that read as a split, the check would warn on every healthy install and get
ignored — so it must resolve before comparing.

Run: python3 tests/health-check-memory-dir-siblings.test.py
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def load_hc(claude_home: Path, memory_dir: Path):
    """Import health-check fresh with CLAUDE_CONFIG_DIR / SUTANDO_MEMORY_DIR pinned.

    Both are read at module import time (MEMORY_DIR is a module-level constant),
    so each case needs its own load rather than mutating os.environ afterwards.
    """
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_home)
    os.environ["SUTANDO_MEMORY_DIR"] = str(memory_dir)
    spec = importlib.util.spec_from_file_location("hc_sib", REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hc_sib"] = mod
    spec.loader.exec_module(mod)
    return mod


def seed(dirpath: Path, n: int) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (dirpath / f"mem_{i}.md").write_text("x")
    return dirpath


with tempfile.TemporaryDirectory() as td:
    home = Path(td) / "claude"
    projects = home / "projects"

    live = seed(projects / "-repo-slug" / "memory", 42)
    hc = load_hc(home, live)

    # 1. Only one populated corpus -> nothing to report.
    check("single corpus is silent", hc.check_memory_dir_siblings() is None)

    # 2. A genuine second corpus under a different slug -> warn.
    seed(projects / "-app-support-slug" / "memory", 66)
    res = hc.check_memory_dir_siblings()
    check("sibling corpus is flagged", res is not None and res["status"] == "warn")
    check(
        "names the sibling and its size",
        res is not None and "-app-support-slug" in res["detail"] and "66 .md" in res["detail"],
        (res or {}).get("detail", ""),
    )
    check(
        "reports which corpus the check itself covers",
        res is not None and "42 .md" in res["detail"],
        (res or {}).get("detail", ""),
    )

    # 3. An EMPTY sibling is not a split — a bare project dir is normal.
    (projects / "-empty-slug" / "memory").mkdir(parents=True)
    res_empty = hc.check_memory_dir_siblings()
    check(
        "empty sibling does not inflate the count",
        res_empty is not None and "-empty-slug" not in res_empty["detail"],
        (res_empty or {}).get("detail", ""),
    )

with tempfile.TemporaryDirectory() as td:
    # 4. THE ONE THAT MATTERS: a symlinked twin is one corpus, not two.
    home = Path(td) / "claude"
    projects = home / "projects"
    live = seed(projects / "-repo-slug" / "memory", 42)
    twin = projects / "-repo.slug.with.dots"
    twin.symlink_to(projects / "-repo-slug", target_is_directory=True)

    hc2 = load_hc(home, live)
    res_twin = hc2.check_memory_dir_siblings()
    check(
        "symlinked twin is NOT reported as a split",
        res_twin is None,
        f"got: {(res_twin or {}).get('detail', '')}",
    )

with tempfile.TemporaryDirectory() as td:
    # 5. No projects/ dir at all (fresh install) -> silent, not a crash.
    home = Path(td) / "claude"
    home.mkdir(parents=True)
    hc3 = load_hc(home, home / "projects" / "-x" / "memory")
    check("missing projects/ dir is silent", hc3.check_memory_dir_siblings() is None)

with tempfile.TemporaryDirectory() as td:
    # 6. A project dir with no memory/ subdir is skipped, not counted.
    #    (Every project dir Claude Code creates starts this way.)
    home = Path(td) / "claude"
    projects = home / "projects"
    live = seed(projects / "-repo-slug" / "memory", 5)
    (projects / "-no-memory-subdir").mkdir(parents=True)
    hc4 = load_hc(home, live)
    check("project dir without memory/ is skipped", hc4.check_memory_dir_siblings() is None)

with tempfile.TemporaryDirectory() as td:
    # 7. The live dir itself does not exist yet, but a sibling is populated.
    #    Must still report, with a live count of 0 rather than crashing — this is
    #    exactly the shape of a misconfigured MEMORY_DIR, the case worth catching.
    home = Path(td) / "claude"
    projects = home / "projects"
    seed(projects / "-populated-elsewhere" / "memory", 9)
    hc5 = load_hc(home, projects / "-does-not-exist" / "memory")
    res5 = hc5.check_memory_dir_siblings()
    check("missing live dir still reports the sibling", res5 is not None and "9 .md" in res5["detail"])
    check(
        "missing live dir reports 0 for itself",
        res5 is not None and "(0 .md)" in res5["detail"],
        (res5 or {}).get("detail", ""),
    )

print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("all checks passed")
