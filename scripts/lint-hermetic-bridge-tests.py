#!/usr/bin/env python3
"""Sutando lint: a test that imports a bridge MUST isolate CLAUDE_CONFIG_DIR first.

WHY
---
`src/discord-bridge.py` (and the slack/telegram siblings) resolve channel config at
**module level**, so the work happens during `exec_module`, before a test can intervene:

    src/discord-bridge.py:205   channels_env = claude_home_path("channels", "discord", ".env")
    src/discord-bridge.py:555   ACCESS_FILE  = channel_access_path("discord")

`channel_access_path()` reads `$CLAUDE_CONFIG_DIR` and falls back to the LEGACY real-home
`~/.claude/channels/<ch>/access.json` when the canonical path is missing. A test that does
not set `CLAUDE_CONFIG_DIR` therefore inherits whatever the developer happens to have, and
the symptom differs per machine:

  * clean box     -> legacy fallback + `[util_paths] DEPRECATION: using legacy ...`
  * operator box  -> silently imports that operator's REAL channel allowlist

Verified 2026-07-30 by re-running the import with `CLAUDE_CONFIG_DIR` popped from the env:
`ACCESS_FILE = /Users/<operator>/.claude/channels/discord/access.json`. Green everywhere,
trustworthy nowhere. Setting a bot token alone does NOT help — that only stops the `.env`
read, never the access resolution.

THE FIX a test must apply (before `exec_module`):

    os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-...")
    _cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
    _cfg.mkdir(parents=True, exist_ok=True)
    (_cfg / "access.json").write_text('{"allowFrom": []}')

DETECTION NOTES (both learned the hard way)
-------------------------------------------
1. Require an **assignment**, not a mention. A first pass keyed on the substring
   `CLAUDE_CONFIG_DIR` and wrongly exonerated three files that only name it in a comment —
   including `tests/bridge-audit-wiring.test.py`, whose comment *claimed* hermeticity while
   the test read live config. A comment is not isolation.
2. Recognize **post-import mitigation**. `tests/slack-bridge-tier-map.test.py` reassigns
   `mod.ACCESS_FILE` to a temp path after `exec_module`, deliberately, so its destructive
   write/unlink cannot touch the operator's real file. The import still resolves host config,
   so it is not clean — but it is not the same defect, and hard-failing the one author who
   thought about this is how lints get switched off. It reports as MITIGATED (non-fatal).

Usage:
  python3 scripts/lint-hermetic-bridge-tests.py           # scan whole tree (report + gate)
  python3 scripts/lint-hermetic-bridge-tests.py --diff    # scan only files added/modified vs BASE_REF
  python3 scripts/lint-hermetic-bridge-tests.py --list    # print current violators, exit 0

Exit 1 ONLY when a test outside KNOWN_UNISOLATED violates. A KNOWN_UNISOLATED entry that no
longer violates is reported as a NOTE, not a failure — hard-failing there is a footgun: the
moment a PR fixes a listed file, main goes red until someone edits this script. (Found while
testing this lint: #2428 fixes tests/bridge-audit-wiring.test.py, and a fatal stale-check
would have reddened main on its merge.)
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    ).stdout.strip()
    or "."
)

BRIDGE_IMPORT = re.compile(r"(discord|slack|telegram)-bridge\.py")
# An ASSIGNMENT to the config-dir env var — a comment mentioning it does not count.
ISOLATES = re.compile(
    r"""os\.environ\[\s*["'](?:CLAUDE_CONFIG_DIR|CLAUDE_HOME)["']\s*\]\s*=|"""
    r"""os\.environ\.setdefault\(\s*["'](?:CLAUDE_CONFIG_DIR|CLAUDE_HOME)["']|"""
    r"""monkeypatch\.setenv\(\s*["'](?:CLAUDE_CONFIG_DIR|CLAUDE_HOME)["']"""
)
# Equivalent isolation by other means.
ALT_ISOLATES = re.compile(
    r"""os\.environ\[\s*["']HOME["']\s*\]\s*=|util_paths\.|patch\([^)]*channel_access_path"""
)
# Post-import damage control: rebinding the resolved path to a temp location.
MITIGATES = re.compile(r"""\.\s*ACCESS_FILE\s*=|\.\s*channels_env\s*=""")

# Grandfathered: known-unisolated at the time this lint landed. Mini's shared-helper
# migration removes these; the stale-entry check below forces the list to shrink.
# Measured on origin/main @ 749f7e79 (2026-07-30). `tests/bridge-audit-wiring.test.py`
# is absent because PR #2428 fixes it.
KNOWN_UNISOLATED = frozenset(
    """
tests/audio-transcribe-skill.test.py
tests/bridge-audit-wiring.test.py
tests/bridge-restart-intercept.test.py
tests/bridges-sending-orphan-recovery.test.py
tests/discord-bridge-delivery-sentinel.test.py
tests/discord-bridge-dm-catchup.test.py
tests/discord-bridge-file-markers.test.py
tests/discord-bridge-task-write-instrument.test.py
tests/discord-chunker.test.py
tests/discord-task-source-invariance.test.py
tests/discord-writeside-attachments.test.py
tests/dm-result-multipart-upload.test.py
tests/health-check-fix-down-bridges.test.py
tests/slack-bridge-allowlist.test.py
tests/slack-bridge-chunking.test.py
tests/slack-bridge-download-html-guard.test.py
tests/slack-bridge-download-stream.test.py
tests/slack-bridge-orphan-recovery.test.py
tests/slack-bridge-pending-recovery.test.py
tests/slack-bridge-task-timeout.test.py
tests/slack-writeside-attachments.test.py
tests/telegram-bridge-forward-attribution.test.py
tests/telegram-bridge-proactive-owner-resolution.test.py
tests/telegram-bridge-progress-stream.test.py
tests/telegram-bridge-tofu-enroll.test.py
tests/telegram-writeside-attachments.test.py
""".split()
)

CLEAN, MITIGATED, VIOLATION = "clean", "mitigated", "violation"


# This lint's own test builds fixture strings containing `exec_module` and a bridge path,
# so a naive scan classifies the test file itself as in-scope. Exempt it, the same way
# scripts/lint-claude-home-path.sh exempts itself for quoting the pattern it forbids.
SELF_EXEMPT = {"tests/lint-hermetic-bridge-tests.test.py"}


def classify(path: Path) -> str | None:
    """Return a verdict, or None when the file is out of scope."""
    try:
        rel = path.resolve().relative_to(REPO.resolve()).as_posix()
    except (ValueError, OSError):
        rel = path.as_posix()
    if rel in SELF_EXEMPT:
        return None
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return None
    if "exec_module" not in text or not BRIDGE_IMPORT.search(text):
        return None
    if ISOLATES.search(text) or ALT_ISOLATES.search(text):
        return CLEAN
    return MITIGATED if MITIGATES.search(text) else VIOLATION


def scan(paths) -> dict[str, str]:
    out = {}
    for p in paths:
        verdict = classify(REPO / p)
        if verdict:
            out[p] = verdict
    return out


def tracked_tests() -> list[str]:
    r = subprocess.run(
        ["git", "ls-files", "--", "tests/*.py"], capture_output=True, text=True, cwd=REPO
    )
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def changed_tests(base: str) -> list[str]:
    r = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=AM", f"{base}...HEAD", "--", "tests/*.py"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def main() -> int:
    import os

    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode == "--diff":
        base = os.environ.get("BASE_REF", "origin/main")
        targets = changed_tests(base)
        if not targets:
            print("lint-hermetic-bridge-tests: no test files changed — nothing to scan")
            return 0
    else:
        targets = tracked_tests()

    results = scan(targets)

    if mode == "--list":
        for p, v in sorted(results.items()):
            print(f"{v:9} {p}")
        return 0

    new_violations = [p for p, v in results.items() if v == VIOLATION and p not in KNOWN_UNISOLATED]
    mitigated = [p for p, v in results.items() if v == MITIGATED]

    # The grandfather list must shrink, never rot: a listed file that now isolates
    # (or no longer imports a bridge) has to come off the list in the same PR.
    stale = []
    if mode != "--diff":
        for p in sorted(KNOWN_UNISOLATED):
            if not (REPO / p).exists() or results.get(p) != VIOLATION:
                stale.append(p)

    for p in mitigated:
        print(f"note: {p} — import still resolves host config; destructive path rebound post-import")

    if stale:
        # WARN, never fail. Hard-failing here is a footgun: the moment a PR fixes a listed
        # file, main goes red until someone edits this script. Caught while testing this
        # lint — #2428 fixes tests/bridge-audit-wiring.test.py, and a fatal stale-check
        # would have reddened main on its merge.
        print("\nlint-hermetic-bridge-tests: NOTE — KNOWN_UNISOLATED entries no longer violating")
        print("(remove them so the list keeps shrinking):\n")
        for p_ in stale:
            print(f"  {p_}")

    if not new_violations:
        print(
            f"lint-hermetic-bridge-tests: ok "
            f"({len(results)} bridge-importing tests scanned, "
            f"{len(KNOWN_UNISOLATED)} grandfathered, {len(mitigated)} mitigated)"
        )
        return 0

    if new_violations:
        print("\nlint-hermetic-bridge-tests: FAIL — test imports a bridge without isolating CLAUDE_CONFIG_DIR\n")
        for p in sorted(new_violations):
            print(f"  {p}")
        print(
            "\nThe bridge resolves channel config at import, so this reads the developer's real\n"
            "per-user channel allowlist. Set CLAUDE_CONFIG_DIR to a temp dir and seed\n"
            "channels/<ch>/access.json BEFORE exec_module. A token env var is not enough,\n"
            "and a comment saying 'hermetic' is not isolation."
        )

    return 1


if __name__ == "__main__":
    sys.exit(main())
