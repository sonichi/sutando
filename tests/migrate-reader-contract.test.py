#!/usr/bin/env python3
"""CI gate: migration CLASS_RULES must be compatible with each file's reader contract.

## Why this test exists

`sutando-migrate.sh`'s CLASS_RULES determine where a file lands after workspace
migration. The readers (`personal_path`, `status_read_path`, etc.) have fixed
resolution chains. If a rule sends a file to a location the reader never checks,
the reader silently falls back to a default — a "graceful degradation" that hides
data loss.

## Incident #1540

`stand-identity.json` was classified `rehome-state` (→ `<workspace>/state/`).
Its reader `personal_path()` resolves `machine-<host>/<file>` → workspace-root
`<file>`, never `state/`. After v0.8 migration every host lost its Stand name
and fell back to the string literal "Sutando" with no error or warning.

## What this test checks

For each file in CLASS_RULES, we classify it by its reader family and assert that
the migration destination is in the reader's search path:

  - **personal_path family** (stand-identity.json, pending-questions.md,
    PERSONAL_CLAUDE.md, session-state.md): must NOT be classified `rehome-state`
    (which lands in `state/`). personal_path only checks memory-dir and workspace
    root — it NEVER looks in state/.

  - **status_read_path family** (core-status.json, quota-state.json,
    dynamic-content.json, voice-state.json, contextual-chips.json): must NOT be
    classified with a root-only class (`newest-mtime`, `append`,
    `collision-keep-both`) at the top-level rule. These readers prefer
    `state/<name>` — a root-only classification would leave them in the legacy
    fallback path permanently.

Run: python3 tests/migrate-reader-contract.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import re
import sys
from fnmatch import fnmatch
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MIGRATE_SH = REPO / "scripts" / "sutando-migrate.sh"

# ---------------------------------------------------------------------------
# Reader-family classification
# Each entry: (canonical_filename, reader_family, notes)
# ---------------------------------------------------------------------------

# Files whose reader is personal_path() — resolves machine-dir then workspace ROOT.
# NEVER looks in state/.
PERSONAL_PATH_FILES = {
    "stand-identity.json",
    "pending-questions.md",
    "PERSONAL_CLAUDE.md",
    "session-state.md",
}

# Classes that send a file to <workspace>/state/ — incompatible with personal_path.
REHOME_TO_STATE_CLASSES = {"rehome-state"}

# Files whose reader is status_read_path() — prefers state/, falls back to root.
# Classifying at root-only is a regression (stays in legacy-fallback path forever).
STATUS_READ_PATH_FILES = {
    "core-status.json",
    "quota-state.json",
    "dynamic-content.json",
    "voice-state.json",
    "contextual-chips.json",
}

# Classes that keep a file at workspace root (not state/).
ROOT_ONLY_CLASSES = {"newest-mtime", "append", "collision-keep-both"}


# ---------------------------------------------------------------------------
# Parse CLASS_RULES
# ---------------------------------------------------------------------------

def parse_class_rules(script: Path) -> list[tuple[str, str]]:
    """Extract (glob, class) pairs from CLASS_RULES=( ... ) in the migrate script."""
    text = script.read_text()
    # Find the CLASS_RULES array body
    m = re.search(r'CLASS_RULES=\(\s*(.*?)\n\)', text, re.DOTALL)
    if not m:
        raise AssertionError(
            f"Could not find CLASS_RULES=(...) in {script.relative_to(REPO)} — "
            "parser may need updating if the array syntax changed."
        )
    body = m.group(1)
    rules: list[tuple[str, str]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        # Match quoted entries like "glob|class"  or "glob|class"  # comment
        m2 = re.match(r'"([^|"]+)\|([^"]+)"', line)
        if m2:
            rules.append((m2.group(1).strip(), m2.group(2).strip()))
    return rules


def classify_file(filename: str, rules: list[tuple[str, str]]) -> str | None:
    """Return the class that matches `filename` (first-match wins), or None."""
    for glob, cls in rules:
        if fnmatch(filename, glob):
            return cls
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_migrate_sh_exists() -> None:
    assert MIGRATE_SH.exists(), (
        f"{MIGRATE_SH.relative_to(REPO)} not found — is this branch based on "
        "staging-workspace-revamp?"
    )


def test_class_rules_parseable() -> None:
    rules = parse_class_rules(MIGRATE_SH)
    assert len(rules) >= 10, (
        f"parse_class_rules returned only {len(rules)} rules — parser may have broken."
    )


def test_personal_path_files_not_rehomed_to_state() -> None:
    """No personal_path file may be classified rehome-state (→ state/)."""
    rules = parse_class_rules(MIGRATE_SH)
    failures = []
    for filename in sorted(PERSONAL_PATH_FILES):
        cls = classify_file(filename, rules)
        if cls is None:
            failures.append(
                f"{filename}: no matching CLASS_RULE — file is unclassified "
                "(will hit catchall quarantine-unknown, which is wrong for a known file)"
            )
        elif cls in REHOME_TO_STATE_CLASSES:
            failures.append(
                f"{filename}: classified as '{cls}' (→ state/) but reader is "
                "personal_path() which only checks machine-dir and workspace ROOT — "
                "reader can never find the file after migration. "
                "Use 'newest-mtime' (keeps at root) instead. "
                "This is the exact class of bug that caused incident #1540."
            )
    if failures:
        raise AssertionError(
            "personal_path files mis-classified as rehome-state:\n"
            + "\n".join(f"  - {f}" for f in failures)
        )


def test_status_read_path_files_not_root_only() -> None:
    """No status_read_path file at root level may be classified with a root-only class.

    status_read_path prefers state/<name>. A root-only classification leaves the
    file at the legacy fallback path permanently — the state/ migration is lost.

    Note: these files may ALSO appear under state/ (state/core-status.json|structural)
    which is correct — this test only checks the root-level rule.
    """
    rules = parse_class_rules(MIGRATE_SH)
    failures = []
    for filename in sorted(STATUS_READ_PATH_FILES):
        cls = classify_file(filename, rules)
        if cls in ROOT_ONLY_CLASSES:
            failures.append(
                f"{filename}: top-level rule is '{cls}' (root-only) but reader is "
                "status_read_path() which prefers state/<name>. "
                f"Use 'rehome-state' or ensure a 'state/{filename}|structural' rule "
                "precedes this entry in CLASS_RULES (ORDER MATTERS — first match wins)."
            )
    if failures:
        raise AssertionError(
            "status_read_path files with root-only top-level rule:\n"
            + "\n".join(f"  - {f}" for f in failures)
        )


def test_stand_identity_specifically_not_rehome_state() -> None:
    """Targeted regression for incident #1540: stand-identity.json must not be rehome-state."""
    rules = parse_class_rules(MIGRATE_SH)
    cls = classify_file("stand-identity.json", rules)
    assert cls not in REHOME_TO_STATE_CLASSES, (
        f"stand-identity.json is still classified '{cls}' (→ state/). "
        "This is the exact bug from incident #1540 — personal_path() never looks "
        "in state/, so the Stand name falls back to 'Sutando' on every host that "
        "ran migration. Change to 'newest-mtime' (keeps at workspace root)."
    )


def test_core_status_in_state_rule() -> None:
    """state/core-status.json must have a structural or equivalent rule (not just root)."""
    rules = parse_class_rules(MIGRATE_SH)
    # The state/ path should have a dedicated rule before the root catchall
    state_cls = classify_file("state/core-status.json", rules)
    assert state_cls not in (None, "skip-unknown", "quarantine-unknown"), (
        f"state/core-status.json has no explicit rule (class={state_cls!r}). "
        "Per Mini #2, per-host status files in state/ must not hit newest-mtime "
        "(would drop one host's data on multi-host scan). Add 'state/core-status.json|structural'."
    )


def test_known_skip_classes_present() -> None:
    """Ephemeral files (.alive) and skip-unknown catchalls must be present."""
    rules = parse_class_rules(MIGRATE_SH)
    classes = {cls for _, cls in rules}
    assert "skip-ephemeral" in classes, "Missing 'skip-ephemeral' class — .alive heartbeat files not excluded"
    assert "skip-unknown" in classes or "quarantine-unknown" in classes, (
        "Missing 'skip-unknown' or 'quarantine-unknown' catchall — unknown files would be unhandled"
    )


# ---------------------------------------------------------------------------
# Self-check: the parser must detect the #1540 regression if re-introduced
# ---------------------------------------------------------------------------

def test_parser_catches_rehome_state_regression() -> None:
    """Self-test: if CLASS_RULES were reverted to rehome-state for stand-identity.json,
    the parser must catch it."""
    # Synthetic rules that reproduce the #1540 bug
    bad_rules = [
        ("stand-identity.json", "rehome-state"),
        ("*", "quarantine-unknown"),
    ]
    cls = classify_file("stand-identity.json", bad_rules)
    assert cls == "rehome-state", "Parser didn't return rehome-state from synthetic bad rules"
    # And the test_stand_identity_specifically_not_rehome_state logic would fire:
    assert cls in REHOME_TO_STATE_CLASSES, (
        "REHOME_TO_STATE_CLASSES doesn't include rehome-state — self-check broken"
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        test_migrate_sh_exists,
        test_class_rules_parseable,
        test_personal_path_files_not_rehomed_to_state,
        test_status_read_path_files_not_root_only,
        test_stand_identity_specifically_not_rehome_state,
        test_core_status_in_state_rule,
        test_known_skip_classes_present,
        test_parser_catches_rehome_state_regression,
    ]
    failures = []
    for fn in tests:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except (AssertionError, FileNotFoundError) as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"  ✗ {fn.__name__}")
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  {f}")
        return 1
    print("All migrate-reader-contract tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
