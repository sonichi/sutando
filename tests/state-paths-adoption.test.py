#!/usr/bin/env python3
"""Adoption test for the SUTANDO_WORKSPACE contract.

## Why this test exists

Many of the recent commits in this repo are fixes of the same shape:
"module X writes/reads to `tasks/` / `results/` / `state/` / `notes/`
without going through the canonical workspace resolver, so on hosts
where `SUTANDO_WORKSPACE` is set the module writes one place while
another component reads another — split-brain that strands owner DMs
/ loses voice-agent state / pollutes `git status`."

The historic anti-pattern called out in CLAUDE.md is *"bridges fell
back to the script's repo root via `Path(__file__).resolve().parent.parent`"*.

A first version of this test (PR #991) checked only that a file
*imports* the resolver, then trusted it. @qingyun-wu's post-merge
review correctly pointed out that a file can import the resolver for
path X and still hand-roll the fallback for path Y — and that the
recommended fix-string pointed at a `state_paths` module that doesn't
exist in this repo. This rewrite addresses all five of her points.

## What this test does (per @qingyun-wu's recommendation)

For every `src/*.py` and `src/*.{ts,tsx}` source file, three checks:

  1. **Positive anti-pattern check (new, @qingyun-wu obs #2).** Fail
     when a non-allowlisted file USES a hand-rolled fallback to compose
     a runtime-state path — i.e. `<fallback-var> / "tasks"` or
     `REPO_DIR / "results"` etc., where the fallback-var is locally
     defined via `Path(__file__).parent.parent` (or similar). This is
     the documented incident shape — and it fires whether or not the
     resolver is also imported.

  2. **Runtime-state reference check (broadened regex, @qingyun-wu
     obs #3).** Fail when a file references runtime-state path tokens
     AND doesn't import the canonical resolver. Now matches single-
     quoted bare names (`'tasks'`), template literals
     (`` `${ws}/tasks/${id}` ``), and all `.ts/.tsx` files (the runner
     used to skip `.tsx`, @qingyun-wu obs #4).

  3. **Sanity assertions.** Resolver module exists; allowlist entries
     all exist on disk; the regexes match their documented forms (a
     guard against accidental loosening).

Per @qingyun-wu obs #1, the failure messages only point at
`workspace_default` / `resolve_workspace` — both real modules in this
repo. Per obs #5, the dead `_allowlisted_or_missing` placeholder was
removed.

Read-only static analysis; no fixtures, no networking. Runs in <200ms.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"

# Tokens that name runtime-state directories. A reference to any of
# these in a non-allowlisted file is a signal it touches workspace
# paths and should go through `resolve_workspace()`.
#
# Note: `data` is NOT in this list — too generic, matches `'data'`
# event-listener strings + many other unrelated uses. The original
# (v1) regex used it too but only in slash-wrapped form, so we follow
# the same scoping.
_RUNTIME_TOKENS_BARE = ("tasks", "results", "state", "notes", "logs")
_RUNTIME_TOKENS_SLASH = ("tasks", "results", "state", "notes", "data", "logs")

_BARE = "|".join(_RUNTIME_TOKENS_BARE)
_SLASH = "|".join(_RUNTIME_TOKENS_SLASH)
RUNTIME_STATE_REGEX = re.compile(
    r'(?:'
    # `"tasks"` / `'tasks'` — quoted bare name (excl. `data`)
    rf'["\']\s*(?:{_BARE})\s*["\']|'
    # `'/tasks/'` / `"/tasks/"` — slash-wrapped literal (incl. `data`)
    rf'["\']/(?:{_SLASH})/["\']|'
    # `(REPO|REPO_DIR|WORKSPACE_DIR|workspace|repo) / "tasks"` —
    # Python `/` operator OR JS path-join (per @qingyun-wu obs #2's
    # incident shape: the runtime-state path is composed off the
    # fallback var).
    rf'(?:REPO|REPO_DIR|WORKSPACE_DIR|workspace|repo)\s*/\s*["\'](?:{_SLASH})["\']|'
    # Template literal: anywhere a `${...}` is followed by `/tasks/`
    # inside a backtick string.
    rf'`[^`]*?\$\{{[^}}]+\}}\s*/(?:{_SLASH})\b[^`]*?`|'
    rf'`[^`]*?/(?:{_SLASH})\s*[/`]'
    r')'
)

# Hand-rolled fallback: matches a runtime-state token IMMEDIATELY
# composed off a fallback root (the documented incident shape, per
# @qingyun-wu obs #2). We check this on per-line basis AND require the
# line to NOT be a pure comment — file-level header doc comments
# legitimately mention `~/.sutando/workspace` in prose without
# composing a path off it.
HAND_ROLLED_COMPOSITION = re.compile(
    r'(?:'
    # Python: `Path(__file__)[.resolve()].parent.parent / "<token>"` /
    # `Path(__file__).parent.parent / "results"` etc.
    rf'Path\(__file__\)(?:\.resolve\(\))?\.parent\.parent\s*/\s*["\'](?:{_SLASH})["\']|'
    # Python/JS: literal `.sutando/workspace/<token>` in a string —
    # bypasses resolve_workspace() entirely.
    rf'\.sutando/workspace/(?:{_SLASH})\b|'
    # JS: `path.join(REPO_ROOT, '<token>')` style where REPO_ROOT names
    # itself as a fallback. The variable names (`REPO_ROOT` / `repoRoot`
    # / `FALLBACK_ROOT`) are strong-enough signals on their own — a
    # legitimate workspace root in this codebase would be named
    # `WORKSPACE_DIR` / `workspace` / `repo` and is matched by the
    # earlier alternation; these three names are reserved by convention
    # for fallback roots.
    rf'(?:REPO_ROOT|repoRoot|FALLBACK_ROOT)\s*[,/]\s*["\'](?:{_SLASH})["\']'
    r')'
)

# Canonical accessors. A file that references runtime-state must use
# one. Per @qingyun-wu obs #1, the recommended fix-string only points
# at modules that actually exist in this repo: `workspace_default`.
PY_CANONICAL = re.compile(
    r'(?:'
    r'from\s+workspace_default\s+import|'
    r'import\s+workspace_default|'
    r'resolve_workspace\s*\('
    r')'
)
TS_CANONICAL = re.compile(
    r"(?:"
    r"from\s+['\"]\./workspace_default(?:\.js)?['\"]|"
    r"resolveWorkspace\s*\("
    r")"
)

# Files that legitimately reference these strings without runtime-state
# semantics, or that legitimately define `__file__.parent.parent` for
# non-workspace purposes (e.g. walking the checkout for git operations).
# Each entry is justified, not silently allowed.
ALLOWLIST = {
    # The canonical resolver itself — names the strings literally and
    # IS the place where the fallback shapes legitimately live.
    "src/workspace_default.py",
    "src/workspace_default.ts",
    # util_paths reads personal-asset paths only — never writes
    # runtime-state.
    "src/util_paths.py",
    # core_heartbeat is intentionally dep-free per its own comment —
    # must run before any other Sutando module is loaded, so it inlines
    # the workspace resolution rather than importing workspace_default.
    "src/core_heartbeat.py",
    # task_archive.py is a pure locator helper — it takes tasks_dir as a
    # parameter from the caller and never resolves workspace itself. The
    # flagged token appears only in the module docstring (example usage),
    # not in runnable code.
    "src/task_archive.py",
}


def _is_comment_line(line: str, suffix: str) -> bool:
    """Best-effort comment detection — used to skip lines that mention
    the fallback shape in prose (docstrings, header comments)."""
    s = line.strip()
    if suffix == ".py":
        return s.startswith("#") or s.startswith('"""') or s.startswith("'''")
    if suffix in (".ts", ".tsx"):
        return s.startswith("//") or s.startswith("*") or s.startswith("/*")
    return False


def _check_file(path: Path) -> list[str]:
    """Return list of failure messages for `path` (empty = passing)."""
    rel = path.relative_to(REPO).as_posix()
    if rel in ALLOWLIST:
        return []
    try:
        src = path.read_text()
    except Exception:
        return []

    failures = []
    suffix = path.suffix
    lines = src.split("\n")

    # Check 1: hand-rolled fallback COMPOSITION (positive anti-pattern,
    # @qingyun-wu obs #2). Fires per-line so we can skip pure comments.
    for lineno, line in enumerate(lines, 1):
        if _is_comment_line(line, suffix):
            continue
        if HAND_ROLLED_COMPOSITION.search(line):
            failures.append(
                f"{rel}:{lineno}: composes a runtime-state path off a "
                f"hand-rolled fallback ({line.strip()!r}). Use "
                f"`resolve_workspace()` (Python) or `resolveWorkspace()` "
                f"(TypeScript) — composing off `Path(__file__).parent.parent` "
                f"or a `.sutando/workspace` literal is the historic incident "
                f"shape documented in `src/workspace_default.py` and CLAUDE.md."
            )
            break  # one per file is enough — fix the pattern, not each line

    # Check 2: runtime-state reference without canonical resolver.
    if RUNTIME_STATE_REGEX.search(src):
        canonical_re = TS_CANONICAL if suffix in (".ts", ".tsx") else PY_CANONICAL
        if not canonical_re.search(src):
            for lineno, line in enumerate(lines, 1):
                if _is_comment_line(line, suffix):
                    continue
                if RUNTIME_STATE_REGEX.search(line):
                    failures.append(
                        f"{rel}:{lineno}: references runtime-state path "
                        f"({line.strip()!r}) without importing the canonical "
                        f"resolver. In Python use `from workspace_default "
                        f"import resolve_workspace`; in TypeScript use "
                        f"`import {{ resolveWorkspace }} from "
                        f"'./workspace_default.js'`. If this file legitimately "
                        f"references these tokens for non-runtime reasons, "
                        f"add {rel!r} to the ALLOWLIST in this test."
                    )
                    break

    return failures


def test_no_unauthorized_runtime_state_references():
    failures = []
    seen: set[str] = set()
    # Per @qingyun-wu obs #4: scan .py + .ts + .tsx.
    for pat in ("*.py", "*.ts", "*.tsx"):
        for path in sorted(SRC.rglob(pat)):
            if "/__pycache__/" in str(path) or "/node_modules/" in str(path):
                continue
            if str(path) in seen:
                continue
            seen.add(str(path))
            failures.extend(_check_file(path))
    if failures:
        msg = "state-paths adoption violations:\n" + "\n".join(f"  - {f}" for f in failures)
        raise AssertionError(msg)


def test_canonical_modules_themselves_are_present():
    """Sanity: the canonical resolver we require everyone else to use
    must exist."""
    assert (SRC / "workspace_default.py").is_file(), "src/workspace_default.py missing"


def test_allowlist_entries_actually_exist():
    """Guard: an ALLOWLIST entry that no longer exists is dead config."""
    for entry in ALLOWLIST:
        path = REPO / entry
        if not path.is_file():
            raise AssertionError(
                f"ALLOWLIST entry {entry!r} does not exist — remove it from the "
                f"test if the file was deleted/renamed, or add the file back."
            )


def test_hand_rolled_composition_detection_self_check():
    """Self-test: the anti-pattern regex must match the documented
    incident shapes. Catches a regex regression where someone weakens
    the pattern without realizing it."""
    cases = [
        'Path(__file__).parent.parent / "tasks"',
        "Path(__file__).resolve().parent.parent / 'results'",
        '"/.sutando/workspace/tasks"',
        "path.join(REPO_ROOT, 'tasks')",
        "path.join(repoRoot, 'state')",
    ]
    for c in cases:
        assert HAND_ROLLED_COMPOSITION.search(c), (
            f"HAND_ROLLED_COMPOSITION regex no longer matches the documented "
            f"incident shape: {c!r}"
        )


def test_hand_rolled_composition_negative_cases():
    """Self-test: must NOT match comment-prose mentions of the fallback
    path. These are legitimate documentation; the per-line check
    skips comments, but the regex itself shouldn't be hair-trigger."""
    negative_cases = [
        # Prose docstring mention — not a composition.
        '# (default ~/.sutando/workspace/), not the repo checkout.',
        '* SUTANDO_WORKSPACE — Per-user workspace dir',
        # Event listener strings — `data` is the event name, not a dir.
        "req.on('data', (c) => ...)",
        # File-level docstring just mentioning the path.
        "// helpers resolve to `$SUTANDO_WORKSPACE` (default `~/.sutando/workspace/`)",
    ]
    for c in negative_cases:
        # The regex itself may or may not match these (e.g. `~/.sutando/workspace/`
        # in a comment is intentionally NOT in the composition regex because
        # there's no trailing token); the per-line comment-skip handles the rest.
        # This test pins that NO false positive matches the composition shape.
        assert not HAND_ROLLED_COMPOSITION.search(c), (
            f"HAND_ROLLED_COMPOSITION unexpectedly matched a non-composition "
            f"form: {c!r}"
        )


def test_runtime_state_regex_self_check():
    """Self-test: the runtime-state regex must match common forms,
    including the single-quote + template-literal forms @qingyun-wu
    pointed out the original regex was missing."""
    cases = [
        # Original cases the v1 regex already caught:
        '"tasks"',
        "'/tasks/'",
        'REPO / "tasks"',
        # New cases per @qingyun-wu obs #3:
        "join(ws, 'tasks')",        # single-quoted bare
        "`${ws}/tasks/${id}`",       # template literal with /tasks/
        "`${repoRoot}/results`",     # template literal terminating at backtick
    ]
    for c in cases:
        assert RUNTIME_STATE_REGEX.search(c), (
            f"RUNTIME_STATE_REGEX no longer matches form: {c!r} — "
            f"per @qingyun-wu obs #3 these are common path-building shapes "
            f"that must trip the gate."
        )


def test_runtime_state_regex_no_data_false_positive():
    """Regression guard: `'data'` as an event-listener string or
    similar unrelated single-quoted bare name must NOT trip the gate
    (the per-line check excludes `data` from the bare-quoted
    alternation to avoid this false positive)."""
    assert not RUNTIME_STATE_REGEX.search(
        "req.on('data', (c: Buffer) => chunks.push(c));"
    ), "false positive on `'data'` as event-listener string"


def main():
    failures = []
    for fn in (
        test_no_unauthorized_runtime_state_references,
        test_canonical_modules_themselves_are_present,
        test_allowlist_entries_actually_exist,
        test_hand_rolled_composition_detection_self_check,
        test_hand_rolled_composition_negative_cases,
        test_runtime_state_regex_self_check,
        test_runtime_state_regex_no_data_false_positive,
    ):
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"  ✗ {fn.__name__}")
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print("All state-paths adoption tests passed.")


if __name__ == "__main__":
    main()
