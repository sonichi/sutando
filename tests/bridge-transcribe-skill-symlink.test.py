#!/usr/bin/env python3
"""_transcribe_via_skill must locate skills/ through a symlinked src/.

CLAUDE.md records this failure mode directly: "when invoked from an app-bundled
`src/` symlink" a repo-root derived from a non-resolved `__file__` points into the
bundle instead of the checkout. discord-bridge derived its transcribe skill path
with a bare `Path(__file__).parent.parent`, so under a bundled src/ it found no
skill and silently degraded to `[File attached:]` — while slack and telegram,
which resolve first, transcribed normally.

The inconsistency was also INTERNAL: discord-bridge's own
`_load_plugin_message_hooks` already does `here = Path(__file__).resolve()` before
appending `here.parent.parent / "skills"`. Two skills lookups in one module,
resolved two different ways.

This test does not re-implement the derivation — it EXTRACTS the real expression
text from each bridge's source and evaluates it against a symlink farm, so it
cannot drift from the code it guards.

Run: python3 tests/bridge-transcribe-skill-symlink.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGES = ("discord-bridge.py", "slack-bridge.py", "telegram-bridge.py")
SKILL_REL = ("skills", "audio-transcribe", "scripts", "transcribe.py")

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def extract_expr(src_file: Path) -> str | None:
    """Pull the right-hand side of the `skill_script = ...` assignment."""
    for line in src_file.read_text().splitlines():
        m = re.match(r"\s*skill_script\s*=\s*(.+?)\s*$", line)
        if m:
            return m.group(1)
    return None


# ── symlink farm: bundle/src -> real/src, skills/ only under real/ ──────────
tmp = Path(tempfile.mkdtemp(prefix="sutando-symlink-"))
real, bundle = tmp / "real", tmp / "bundle"
(real / "src").mkdir(parents=True)
real_skill = real.joinpath(*SKILL_REL)
real_skill.parent.mkdir(parents=True)
real_skill.write_text("# stand-in transcribe skill\n")
bundle.mkdir()
os.symlink(real / "src", bundle / "src")

check("CONTROL: the symlink farm is real (bundle/src resolves to real/src)",
      (bundle / "src").resolve() == (real / "src").resolve())
check("CONTROL: no skills/ exists under the bundle — an unresolved path CANNOT find it",
      not (bundle / "skills").exists())

for name in BRIDGES:
    src_file = REPO / "src" / name
    expr = extract_expr(src_file)
    # A silently-absent expression would let every assertion below vacuously pass.
    check(f"{name}: skill_script expression found in source", expr is not None)
    if expr is None:
        continue

    fake_file = str(bundle / "src" / name)
    (real / "src" / name).write_text("# placeholder\n")
    try:
        got = eval(expr, {"Path": Path, "os": os, "__file__": fake_file})  # noqa: S307
    except Exception as exc:                     # pragma: no cover
        check(f"{name}: expression evaluates", False, f"{type(exc).__name__}: {exc}")
        continue

    # Compare fully-resolved forms: on macOS /var is itself a symlink to
    # /private/var, so the two spellings of the SAME path differ textually.
    # Still discriminating — the unresolved expression lands under bundle/,
    # which normalises to a different real path (and does not exist).
    check(f"{name}: resolves through the symlink to the REAL skills/ script",
          Path(got).resolve() == real_skill.resolve(),
          f"got {str(Path(got).resolve())!r}, want {str(real_skill.resolve())!r}")
    check(f"{name}: the resolved script actually exists (would transcribe, not degrade)",
          Path(got).exists())

print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("all checks passed — every bridge finds the transcribe skill through a symlinked src/")
