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

# ── HERMETIC ISOLATION — must precede any bridge load ───────────────────────
# The execution block at the bottom imports src/discord-bridge.py, whose
# module-level `channel_access_path()` reads $CLAUDE_CONFIG_DIR and falls back
# to the developer's REAL home. scripts/lint-hermetic-bridge-tests.py requires
# the earliest MODULE-LEVEL `os.environ["CLAUDE_CONFIG_DIR"] = ...` assignment,
# to a value that is not that real home — a patch, a setdefault, or an
# assignment nested inside a function or `with` block is deliberately NOT
# accepted, because those either no-op or execute too late.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-transcribe-skill-")

# The bridge sys.exit()s at import when no bot token is present, so seed a fake
# one INSIDE the isolated dir. It is never used to connect — the discord SDK is
# stubbed below — but the module refuses to load without it.
# Seed EVERY channel this file names, not just the one it imports, and seed them
# with LITERAL path segments. The lint keys the requirement off the bridges the
# test references (discord, slack and telegram all appear in BRIDGES below), and
# it tracks path segments statically — a `for _ch in (...)` loop is invisible to
# it, which is correct: a seed it cannot see is a seed a reader cannot verify.
_ccd_root = Path(os.environ["CLAUDE_CONFIG_DIR"])
(_ccd_root / "channels" / "discord").mkdir(parents=True, exist_ok=True)
(_ccd_root / "channels" / "slack").mkdir(parents=True, exist_ok=True)
(_ccd_root / "channels" / "telegram").mkdir(parents=True, exist_ok=True)
(_ccd_root / "channels" / "discord" / "access.json").write_text('{"allowFrom": []}\n')
(_ccd_root / "channels" / "slack" / "access.json").write_text('{"allowFrom": []}\n')
(_ccd_root / "channels" / "telegram" / "access.json").write_text('{"allowFrom": []}\n')

# The discord bridge additionally sys.exit()s at import without a bot token.
# Never used to connect — the SDK is stubbed below.
(_ccd_root / "channels" / "discord" / ".env").write_text(
    "DISCORD_BOT_TOKEN=not-a-real-token-test-only\n")

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



# ── EXECUTION: run _transcribe_via_skill so the derivation line actually runs ─
# Everything above evaluates the expression TEXT, which proves the semantics but
# never executes src/discord-bridge.py:432 — so diff-cover reported that line at
# 0%. A subprocess would not help: coverage.py does not trace a child
# interpreter, so the load has to be IN-PROCESS.
import importlib.util
import types


def _stub_discord() -> None:
    """Minimal stand-in for the discord SDK so the module imports offline."""
    stub = types.ModuleType("discord")

    class _Intents:
        @classmethod
        def default(cls):
            i = cls(); i.message_content = False; i.members = False; return i

    class _Client:
        def __init__(self, *a, **kw):
            self.user = None
            self.loop = types.SimpleNamespace(create_task=lambda *a, **kw: None)

        def event(self, fn):
            return fn

        def get_channel(self, _):
            return None

    stub.Intents, stub.Client = _Intents, _Client
    stub.AllowedMentions = type("AllowedMentions", (), {"__init__": lambda self, *a, **kw: None})
    for n in ("File", "Message", "DMChannel", "TextChannel", "Thread"):
        setattr(stub, n, type(n, (), {}))
    sys.modules.setdefault("discord", stub)
    sys.modules.setdefault("discord.errors", types.ModuleType("discord.errors"))


_stub_discord()
_spec = importlib.util.spec_from_file_location("_db_exec", REPO / "src" / "discord-bridge.py")
_db = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(_db)
except Exception as exc:                                    # pragma: no cover - env-dependent
    check("EXECUTION: discord-bridge imported for the real call", False, repr(exc))
else:
    seen: dict[str, object] = {}
    _db._run_optional_script_shared = lambda script, args, **kw: seen.setdefault("script", script)
    _db._transcribe_via_skill("/tmp/does-not-exist.ogg")
    got = Path(str(seen.get("script", "")))
    check("EXECUTION: the derivation line ran and produced a path", bool(seen))
    check("EXECUTION: it resolved to the REAL checkout's skills/, not the symlink parent",
          got.parts[-4:] == ("skills", "audio-transcribe", "scripts", "transcribe.py"),
          f"got {got}")
    # The property the fix exists for: os.path.realpath means the derived path
    # does not depend on __file__ being reached through a symlinked src/.
    check("EXECUTION: the resolved script exists on disk", got.exists(), f"got {got}")

print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("all checks passed — the transcribe skill path survives a symlinked src/")
