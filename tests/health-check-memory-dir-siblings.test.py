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


def load_hc_no_env(claude_home: Path):
    """Same, but with SUTANDO_MEMORY_DIR UNSET — the slug-split flavour needs no
    env var at all, and every other case here sets the override, so without this
    the no-env path this check exists for was never exercised (#2353 review)."""
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_home)
    os.environ.pop("SUTANDO_MEMORY_DIR", None)
    spec = importlib.util.spec_from_file_location("hc_sib_noenv", REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hc_sib_noenv"] = mod
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
    seed(projects / "-repo.slug" / "memory", 66)
    res = hc.check_memory_dir_siblings()
    check("sibling corpus is flagged", res is not None and res["status"] == "warn")
    check(
        "names the sibling and its size",
        res is not None and "-repo.slug" in res["detail"] and "66 .md" in res["detail"],
        (res or {}).get("detail", ""),
    )
    check(
        "reports which corpus the check itself covers",
        res is not None and "42 .md" in res["detail"],
        (res or {}).get("detail", ""),
    )

    # 3. An EMPTY sibling is not a split — a bare project dir is normal.
    (projects / "-repo_slug" / "memory").mkdir(parents=True)
    res_empty = hc.check_memory_dir_siblings()
    check(
        "empty sibling does not inflate the count",
        res_empty is not None and "-repo_slug" not in res_empty["detail"],
        (res_empty or {}).get("detail", ""),
    )

with tempfile.TemporaryDirectory() as td:
    # 4. THE ONE THAT MATTERS: a symlinked twin is one corpus, not two.
    home = Path(td) / "claude"
    projects = home / "projects"
    live = seed(projects / "-repo-slug" / "memory", 42)
    twin = projects / "-repo.slug"
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
    # both names must be derivations of ONE path, or the scope filter (correctly)
    # drops the sibling before this case can exercise the missing-live-dir path.
    seed(projects / "-repo.slug" / "memory", 9)
    hc5 = load_hc(home, projects / "-repo-slug" / "memory")
    res5 = hc5.check_memory_dir_siblings()
    check("missing live dir still reports the sibling", res5 is not None and "9 .md" in res5["detail"])
    check(
        "missing live dir reports 0 for itself",
        res5 is not None and "(0 .md)" in res5["detail"],
        (res5 or {}).get("detail", ""),
    )

# --- #2353 review: an UNRELATED project must not trigger the warning ----------
# Warning on any populated corpus fires on every normal multi-project home. That
# is a permanent false warning, and a health signal people learn to ignore is
# worse than the split it was meant to surface.
with tempfile.TemporaryDirectory() as td2:
    root = Path(td2)
    home = root / "claude"
    live_slug = "-Users-me-Library-Application-Support-space-ag2-app-engine-sutando"
    live_mem = seed(home / "projects" / live_slug / "memory", 5)

    # (a) unrelated project alongside -> SILENT
    seed(home / "projects" / "-Users-me-Documents-some-other-repo" / "memory", 9)
    hc = load_hc(home, live_mem)
    r = hc.check_memory_dir_siblings()
    check("unrelated project does NOT warn", r is None,
          f"got: {r['detail'][:110] if r else None}")

    # (b) control: a genuine alternate DERIVATION of the same path -> still warns.
    #     Without this the test above passes for a check that never warns at all.
    seed(home / "projects" / "-Users-me-Library-Application-Support-space.ag2.app-engine-sutando" / "memory", 7)
    hc2 = load_hc(home, live_mem)
    r2 = hc2.check_memory_dir_siblings()
    check("genuine slug split still warns (control)", r2 is not None and r2["status"] == "warn")
    check("the warning names the alternate derivation, not the unrelated repo",
          bool(r2) and "space.ag2.app" in r2["detail"] and "some-other-repo" not in r2["detail"],
          f"got: {r2['detail'][:130] if r2 else None}")

# --- #2353 review: exercise the NO-ENV path (no SUTANDO_MEMORY_DIR) -----------
with tempfile.TemporaryDirectory() as td3:
    root = Path(td3)
    home = root / "claude"
    seed(home / "projects" / "-Users-me-only-project" / "memory", 3)
    hc3 = load_hc_no_env(home)
    r3 = hc3.check_memory_dir_siblings()
    check("no-env path runs without raising and is silent on a single corpus", r3 is None,
          f"got: {r3['detail'][:110] if r3 else None}")

print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("all checks passed")
