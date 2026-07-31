#!/usr/bin/env python3
"""Tests for scripts/lint-hermetic-bridge-tests.py.

Exercises the classifier and every main() mode IN-PROCESS (import + call, never a
subprocess) so the diff-coverage gate actually sees the lines — a subprocess run
executes the code in a child interpreter coverage.py is not tracing
([[reference_coverage_gate_needs_inprocess_tests]]).

The behaviours pinned here are the ones that were wrong in an earlier draft of the lint:
  * a COMMENT naming CLAUDE_CONFIG_DIR must not count as isolation (the bug that let
    tests/bridge-audit-wiring.test.py look hermetic while it read live config)
  * post-import `mod.ACCESS_FILE = <temp>` is MITIGATED, never a hard failure
  * a stale KNOWN_UNISOLATED entry is a NOTE, not exit 1 (a fatal check would redden
    main the moment a PR fixes a listed file)

Run: python3 tests/lint-hermetic-bridge-tests.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import ast
import importlib.util
import io
import contextlib
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location(
    "lint_hermetic", REPO / "scripts" / "lint-hermetic-bridge-tests.py"
)
lint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lint)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "  FAIL ") + name + (("" if cond else " — " + detail)))
    if not cond:
        failures.append(name)


def write(tmp: Path, body: str) -> Path:
    p = tmp / "sample.test.py"
    p.write_text(body)
    return p


IMPORTS_BRIDGE = """
import importlib.util, pathlib
spec = importlib.util.spec_from_file_location("db", "src/discord-bridge.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
"""

tmpdir = Path(tempfile.mkdtemp(prefix="lint-hermetic-test-"))

# --- rebinding: every module-level name tracker must honor it (round 19) ----
# One defect, three trackers. _rooted_segments was fixed in round 18; the
# reviewers found the second; probing the AXIS rather than the reported case
# found the other two. Each negative fixture writes only a decoy or points
# elsewhere, so a CLEAN verdict would mean the canonical discord access.json is
# absent at import and the bridge falls back to the operator's real allowlist.
_HEAD = (
    "import os, tempfile, pathlib\n"
    "_ccd = tempfile.mkdtemp()\n"
    'os.environ["CLAUDE_CONFIG_DIR"] = _ccd\n'
)
_SEED = (
    "p.mkdir(parents=True, exist_ok=True)\n"
    '(p / "access.json").write_text("{}")\n'
)
_VIA_ENV = 'p = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / CHANNEL\n'

check(
    "rebind via NAME drops the stale literal segment (john + qingyun, #2429)",
    lint.classify(write(tmpdir, _HEAD
        + 'OTHER = "slack"\nCHANNEL = "discord"\nCHANNEL = OTHER\n'
        + _VIA_ENV + _SEED + IMPORTS_BRIDGE)) == lint.VIOLATION,
)
check(
    "CONTROL reverse order: the alias resolves TO discord, so the seed is real",
    lint.classify(write(tmpdir, _HEAD
        + 'OTHER = "discord"\nCHANNEL = "slack"\nCHANNEL = OTHER\n'
        + _VIA_ENV + _SEED + IMPORTS_BRIDGE)) == lint.CLEAN,
)
check(
    "CONTROL an unrelated rebinding must not disturb a good binding",
    lint.classify(write(tmpdir, _HEAD
        + 'CHANNEL = "discord"\nUNRELATED = "slack"\nUNRELATED = CHANNEL\n'
        + _VIA_ENV + _SEED + IMPORTS_BRIDGE)) == lint.CLEAN,
)
check(
    "rebind to a non-literal drops the binding, failing CLOSED",
    lint.classify(write(tmpdir, _HEAD
        + 'CHANNEL = "discord"\nCHANNEL = os.environ.get("CH", "discord")\n'
        + _VIA_ENV + _SEED + IMPORTS_BRIDGE)) == lint.VIOLATION,
)
check(
    "rebinding the ROOT name revokes rootedness (_ccd_root_names)",
    # _HEAD already set the env from _ccd, so isolation is VALID here and the
    # verdict can only turn on rootedness — which is what makes this discriminating.
    lint.classify(write(tmpdir, _HEAD
        + '_ccd = "/tmp/somewhere-else"\n'
        + 'p = pathlib.Path(_ccd) / "channels" / "discord"\n'
        + _SEED + IMPORTS_BRIDGE)) == lint.VIOLATION,
)
check(
    "CONTROL a root name never rebound stays rooted",
    lint.classify(write(tmpdir, _HEAD
        + 'p = pathlib.Path(_ccd) / "channels" / "discord"\n'
        + _SEED + IMPORTS_BRIDGE)) == lint.CLEAN,
)
# The most severe of the three, because it defeats isolation outright rather than
# mis-attributing one path: a rebound name kept its temp-dir status, so a file
# pointing CLAUDE_CONFIG_DIR at the operator's REAL config dir read as isolated.
check(
    "rebound BEFORE the env assignment: not isolated (_isolation_line)",
    lint._isolation_line(ast.parse(
        "import os, tempfile\n"
        "d = tempfile.mkdtemp()\n"
        'd = "/Users/someone/.claude"\n'
        'os.environ["CLAUDE_CONFIG_DIR"] = d\n')) is None,
)
# The control that caught a false positive I nearly shipped: a FINAL-STATE set
# calls this unisolated, but the env var really was set to a temp dir and a later
# rebinding does not un-set it. Isolation is a question about the line it is on.
check(
    "CONTROL rebound AFTER the env assignment is still isolated",
    lint._isolation_line(ast.parse(
        "import os, tempfile\n"
        "_ccd = tempfile.mkdtemp()\n"
        'os.environ["CLAUDE_CONFIG_DIR"] = _ccd\n'
        '_ccd = "/tmp/elsewhere"\n')) is not None,
)
check(
    "CONTROL an isolated name never rebound still isolates",
    lint._isolation_line(ast.parse(
        "import os, tempfile\n"
        "d = tempfile.mkdtemp()\n"
        'os.environ["CLAUDE_CONFIG_DIR"] = d\n')) is not None,
)

# --- target selection must be structural, not textual ----------------------
# Selection used to regex the RAW file text, so a file that merely NAMED a bridge
# in prose had to be hermetic. It fired on a real merged test (#2458) whose module
# docstring mentions discord-bridge.py while it imports only src/discord-read.py.
check(
    "a prose-only mention of a bridge is not a bridge import",
    lint.classify(write(tmpdir,
        '"""Doc mentioning src/discord-bridge.py in prose."""\n'
        "import importlib.util\n"
        'spec = importlib.util.spec_from_file_location("d", "src/discord-read.py")\n'
        "m = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(m)\n")) is None,
)
check(
    "CONTROL a real bridge path in CODE is still selected",
    lint.classify(write(tmpdir, IMPORTS_BRIDGE)) == lint.VIOLATION,
)

# --- classify() ------------------------------------------------------------
check(
    "out of scope: no exec_module -> None",
    lint.classify(write(tmpdir, "import os\nx = 1\n")) is None,
)
check(
    "out of scope: exec_module but no bridge -> None",
    lint.classify(write(tmpdir, "spec.loader.exec_module(m)  # src/health-check.py\n")) is None,
)
check(
    "violation: imports bridge, no isolation",
    lint.classify(write(tmpdir, IMPORTS_BRIDGE)) == lint.VIOLATION,
)
# Was asserted CLEAN until review 8. It is not: the env var alone leaves
# channel_access_path() on its legacy real-home fallback. This assertion had encoded
# the very defect the gate exists to catch.
check(
    "clean: CLAUDE_CONFIG_DIR assignment PLUS a seeded access.json, both before the load",
    lint.classify(
        write(tmpdir,
'import os, tempfile, pathlib\nos.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()\n'
              '_c = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"\n'
              '_c.mkdir(parents=True, exist_ok=True)\n'
              '(_c / "access.json").write_text("{}")\n' + IMPORTS_BRIDGE)
    )
    == lint.CLEAN,
)
# The narrowing (qingyun, #2429 reviews 5-6): these shapes are NO LONGER clean, because
# none of them guarantees the import avoids an inherited CLAUDE_CONFIG_DIR.
# Review self-audit (#2429, found by me before review): `_isolation_line` proved an
# ASSIGNMENT happened, never that the value pointed anywhere safe. Setting the env var
# to the operator's REAL ~/.claude and seeding under it classified CLEAN — while writing
# into their live allowlist. That is worse than the fallback-READ this gate was built to
# stop, because it MUTATES host config. Isolation now requires a provably temporary dir.
check(
    "pointing CLAUDE_CONFIG_DIR at the operator's REAL ~/.claude is NOT isolation",
    lint.classify(
        write(tmpdir, 'import os, pathlib\nos.environ["CLAUDE_CONFIG_DIR"] = os.path.expanduser("~/.claude")\n'
              '_c = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"\n'
              '_c.mkdir(parents=True, exist_ok=True)\n(_c / "access.json").write_text("{}")\n'
              + IMPORTS_BRIDGE)
    )
    == lint.VIOLATION,
    "seeding under the real config dir overwrites the operator's live allowlist",
)
check(
    "HOME + literal suffix is NOT isolation",
    lint.classify(
        write(tmpdir, 'import os, pathlib\nos.environ["CLAUDE_CONFIG_DIR"] = os.environ["HOME"] + "/.claude"\n'
              '_c = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"\n'
              '_c.mkdir(parents=True, exist_ok=True)\n(_c / "access.json").write_text("{}")\n'
              + IMPORTS_BRIDGE)
    )
    == lint.VIOLATION,
)
check(
    "a bare literal path is NOT isolation (unprovable, so refused)",
    lint.classify(
        write(tmpdir, 'import os, pathlib\nos.environ["CLAUDE_CONFIG_DIR"] = "/tmp/x"\n'
              '_c = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"\n'
              '_c.mkdir(parents=True, exist_ok=True)\n(_c / "access.json").write_text("{}")\n'
              + IMPORTS_BRIDGE)
    )
    == lint.VIOLATION,
    "under-approximating CLEAN is this file's documented stance",
)
check(
    "a name bound to tempfile.mkdtemp() IS isolation",
    lint.classify(
        write(tmpdir, 'import os, tempfile, pathlib\n_d = tempfile.mkdtemp()\n'
              'os.environ["CLAUDE_CONFIG_DIR"] = _d\n'
              '_c = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"\n'
              '_c.mkdir(parents=True, exist_ok=True)\n(_c / "access.json").write_text("{}")\n'
              + IMPORTS_BRIDGE)
    )
    == lint.CLEAN,
    "the narrowing must not reject the ordinary two-step temp-dir shape",
)
check(
    "TemporaryDirectory().name IS isolation",
    lint.classify(
        write(tmpdir, 'import os, tempfile, pathlib\n_t = tempfile.TemporaryDirectory()\n'
              'os.environ["CLAUDE_CONFIG_DIR"] = _t.name\n'
              '_c = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"\n'
              '_c.mkdir(parents=True, exist_ok=True)\n(_c / "access.json").write_text("{}")\n'
              + IMPORTS_BRIDGE)
    )
    == lint.CLEAN,
)
check(
    "setdefault is NOT isolation — it is a no-op when the var is already inherited",
    lint.classify(
        write(tmpdir, 'import os\nos.environ.setdefault("CLAUDE_CONFIG_DIR", "/tmp/x")\n' + IMPORTS_BRIDGE)
    )
    == lint.VIOLATION,
)
check(
    "CLAUDE_HOME alone is NOT isolation — lower precedence than CLAUDE_CONFIG_DIR",
    lint.classify(write(tmpdir, 'import os\nos.environ["CLAUDE_HOME"] = "/tmp/x"\n' + IMPORTS_BRIDGE))
    == lint.VIOLATION,
)
check(
    "HOME alone is NOT isolation",
    lint.classify(write(tmpdir, 'import os\nos.environ["HOME"] = "/tmp/x"\n' + IMPORTS_BRIDGE))
    == lint.VIOLATION,
)
check(
    "a dict that is not os.environ is NOT isolation (receiver is checked)",
    lint.classify(write(tmpdir, 'cfg = {}\ncfg["CLAUDE_CONFIG_DIR"] = "/tmp/x"\n' + IMPORTS_BRIDGE))
    == lint.VIOLATION,
)
check(
    "isolation inside a dead branch is NOT isolation (module level required)",
    lint.classify(
        write(tmpdir, 'import os\nif False:\n    os.environ["CLAUDE_CONFIG_DIR"] = "/tmp/x"\n' + IMPORTS_BRIDGE)
    )
    == lint.VIOLATION,
)
check(
    "an EXPIRED patch context before the import is NOT isolation",
    lint.classify(
        write(
            tmpdir,
            'from unittest.mock import patch\nwith patch("util_paths.channel_access_path"):\n    pass\n'
            + IMPORTS_BRIDGE,
        )
    )
    == lint.VIOLATION,
    "patch had exited before exec_module ran",
)
check(
    "a never-called function containing the rebind is NOT mitigation",
    lint.classify(write(tmpdir, 'def unused():\n    m.ACCESS_FILE = "/tmp/a"\n' + IMPORTS_BRIDGE))
    == lint.VIOLATION,
)

# --- scope + seed bypasses (qingyun + Rui/john-the-dev, #2429 review 8) ----
SEED = ('import os, tempfile, pathlib\n'
        'os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()\n'
        'pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]).joinpath("channels", "discord").mkdir(parents=True, exist_ok=True)\n'
        '(pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord" / "access.json").write_text("{}")\n')
EXEC_LOAD = ('src = open("src/discord-bridge.py").read()\n'
             'import types\nbridge = types.ModuleType("b")\n'
             'exec(src, bridge.__dict__)\n')
check(
    "BYPASS 8: a bridge loaded via exec() is IN SCOPE (was silently unscanned)",
    lint.classify(write(tmpdir, EXEC_LOAD)) == lint.VIOLATION,
    "exec_module-only scope gate returned None = silent pass",
)
check(
    "exec()-loaded WITH env + seed before the load is clean",
    lint.classify(write(tmpdir, SEED + EXEC_LOAD)) == lint.CLEAN,
)
check(
    "BYPASS 9: CLAUDE_CONFIG_DIR set but access.json NOT seeded is NOT isolation",
    lint.classify(
        write(tmpdir, 'import os, tempfile\nos.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()\n' + IMPORTS_BRIDGE)
    )
    == lint.VIOLATION,
    "empty temp dir leaves channel_access_path() on its legacy real-home fallback",
)
check(
    "BYPASS 9b: access.json named only in a docstring is NOT a seed",
    lint.classify(
        write(tmpdir,
              '"""Seeds channels/discord/access.json somewhere else."""\n'
              'import os, tempfile\nos.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()\n' + IMPORTS_BRIDGE)
    )
    == lint.VIOLATION,
    "a mention is not a write",
)
check(
    "seed AFTER the load does not count",
    lint.classify(
        write(tmpdir, 'import os, tempfile, pathlib\nos.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()\n'
              + IMPORTS_BRIDGE + '\n(pathlib.Path("x") / "access.json").write_text("{}")\n')
    )
    == lint.VIOLATION,
)

# --- seed through a variable (#2429 review 9) -------------------------------
# The seed check first recognized "access.json" only INSIDE the write call's own
# receiver expression, so building the path into a name first read as no-seed.
# Binding a path to a variable is not a behavioral difference — a false VIOLATION
# is the sound direction, but it would have flagged correctly-seeded files and
# inflated the migration denominator.
_ENV = 'import os, tempfile, pathlib\nos.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()\n'
# A CLEAN fixture must also CREATE channels/<ch>/. A write into a fresh mkdtemp
# without it raises FileNotFoundError and creates nothing, so the canonical file is
# still absent at import (#2429 review 14, qingyun). Fixtures that omitted the mkdir
# were themselves instances of the bug under test.
_MKDIR = ('pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]).joinpath("channels", "discord")'
          '.mkdir(parents=True, exist_ok=True)\n')
_ENVDIR = _ENV + _MKDIR
check(
    "seed via an intermediate variable counts (p = chan / 'access.json'; p.write_text)",
    lint.classify(
        write(tmpdir, _ENVDIR + 'chan = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"\n'
              'p = chan / "access.json"\np.write_text("{}")\n' + IMPORTS_BRIDGE)
    )
    == lint.CLEAN,
)
check(
    "seed taint propagates two hops (ACCESS = 'access.json'; p = d / ACCESS)",
    lint.classify(
        write(tmpdir, _ENVDIR + 'ACCESS = "access.json"\n'
              'p = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord" / ACCESS\n'
              'p.write_text("{}")\n' + IMPORTS_BRIDGE)
    )
    == lint.CLEAN,
)
check(
    "a seed bound ONLY inside a function is not a module-level seed",
    lint.classify(
        write(tmpdir, _ENV + 'def s():\n    p = pathlib.Path("/tmp/x") / "access.json"\n'
              '    p.write_text("{}")\n' + IMPORTS_BRIDGE)
    )
    == lint.VIOLATION,
    "unbound-at-load is the reachability case the gate must not guess about",
)
check(
    "a variable that never carries access.json is not a seed",
    lint.classify(
        write(tmpdir, _ENV + 'p = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord" / ".env"\n'
              'p.write_text("{}")\n' + IMPORTS_BRIDGE)
    )
    == lint.VIOLATION,
    "taint must track the path, not any written variable",
)

# Review 10 (qingyun, #2429): the seed predicate proved only that SOME path expression
# contained "access.json" — not that the write targeted the configured
# $CLAUDE_CONFIG_DIR/channels/<ch>/access.json. Both shapes below wrote to an unrelated
# directory and classified CLEAN while the canonical file stayed absent, so the bridge
# still fell back to the operator's real-home allowlist. Rootedness is now required.
check(
    "unrelated METHOD write to some other access.json is NOT a seed",
    lint.classify(
        write(tmpdir, _ENV + 'p = pathlib.Path("/tmp/x") / "access.json"\n'
              'p.write_text("{}")\n' + IMPORTS_BRIDGE)
    )
    == lint.VIOLATION,
    "a seed must be rooted at the CONFIGURED config dir, not merely named access.json",
)
check(
    "unrelated HELPER write to some other access.json is NOT a seed",
    lint.classify(
        write(tmpdir, _ENV + 'write_private_text(pathlib.Path("/tmp/x") / "access.json", "{}")\n'
              + IMPORTS_BRIDGE)
    )
    == lint.VIOLATION,
    "the helper shape inherited the same false CLEAN and must be closed with it",
)
check(
    "canonical HELPER seed under the configured dir IS a seed",
    lint.classify(
        write(tmpdir, _ENVDIR + 'cfg = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"\n'
              'write_private_text(cfg / "access.json", "{}")\n' + IMPORTS_BRIDGE)
    )
    == lint.CLEAN,
    "closing the hole must not start false-flagging correctly-seeded helper writes",
)
check(
    "seed rooted via the NAME assigned into os.environ is clean",
    lint.classify(
        write(tmpdir, 'import os, tempfile, pathlib\n_ccd = tempfile.mkdtemp()\n'
              'os.environ["CLAUDE_CONFIG_DIR"] = _ccd\n'
              'p = pathlib.Path(_ccd) / "channels" / "discord" / "access.json"\n'
              'p.parent.mkdir(parents=True, exist_ok=True)\n'
              'p.write_text("{}")\n' + IMPORTS_BRIDGE)
    )
    == lint.CLEAN,
    "_ccd = mkdtemp(); os.environ[...] = _ccd is the same canonical path, spelled once",
)
check(
    "rooted but WITHOUT a channels segment is NOT a seed",
    lint.classify(
        write(tmpdir, _ENV + 'p = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "access.json"\n'
              'p.write_text("{}")\n' + IMPORTS_BRIDGE)
    )
    == lint.VIOLATION,
    "channel_access_path() reads channels/<ch>/access.json — the root alone is not that file",
)

# Review 11 (qingyun, #2429): the seed was not CHANNEL-SPECIFIC. It accepted any
# rooted path containing `channels` + `access.json` without proving the seeded
# channel matches the bridge being imported. `channel_access_path()` resolves per
# channel, so seeding discord does nothing for a telegram import — the canonical
# telegram file is still absent and the load falls back to the operator's real
# allowlist. The positive fixture just above had itself locked the shape in by
# seeding `slack` while IMPORTS_BRIDGE loads the DISCORD bridge; that is corrected.
_TELEGRAM_BRIDGE = IMPORTS_BRIDGE.replace("discord-bridge.py", "telegram-bridge.py")
_SLACK_BRIDGE = IMPORTS_BRIDGE.replace("discord-bridge.py", "slack-bridge.py")


def _seed(channel):
    return (_ENV + f'_c = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "{channel}"\n'
            '_c.mkdir(parents=True, exist_ok=True)\n'
            '(_c / "access.json").write_text("{}")\n')


# Review 13 (qingyun, #2429): `ast.walk()` flattens everything, so a canonical seed
# inside a never-called function was recorded as if it had run before the import.
# The file is never created, so the bridge still falls back to the real allowlist.
_INLINE_SEED = ('(pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"'
                ' / "access.json").write_text("{}")\n')

check(
    "seed inside a never-called function is NOT a seed",
    lint.classify(write(tmpdir, _ENV + 'def never_called():\n    ' + _INLINE_SEED + IMPORTS_BRIDGE))
    == lint.VIOLATION,
    "a def body does not run on import; the canonical file is never created",
)
check(
    "seed inside a class body is NOT a seed",
    lint.classify(write(tmpdir, _ENV + 'class C:\n    ' + _INLINE_SEED + IMPORTS_BRIDGE))
    == lint.VIOLATION,
)
check(
    "module-level inline seed IS still a seed",
    lint.classify(write(tmpdir, _ENVDIR + _INLINE_SEED + IMPORTS_BRIDGE)) == lint.CLEAN,
)
# Review 14 (qingyun, #2429): skipping def/class bodies was not enough — descending into
# every child of `if`/`try` admitted a seed under `if False:` or inside an `except` handler
# that never fires. Only bodies that run UNCONDITIONALLY on import may count.
# Review 15 (qingyun, round 8): a `try` BODY runs, but only until something
# definitely terminates it. A seed AFTER an unconditional `raise` never executes —
# yet the handler swallows the exception and the import proceeds, so the lint was
# reporting clean on a test that seeded nothing.
check(
    "seed AFTER an unconditional raise in a handled try is NOT a seed",
    lint.classify(
        write(tmpdir, _ENV + 'try:\n    raise RuntimeError("x")\n    ' + _INLINE_SEED
              + 'except Exception:\n    pass\n' + IMPORTS_BRIDGE)
    )
    == lint.VIOLATION,
    "everything after the raise is dead, but the import still happens",
)
check(
    "seed BEFORE the raise IS still a seed",
    lint.classify(
        write(tmpdir, _ENVDIR + 'try:\n    ' + _INLINE_SEED + '    raise RuntimeError("x")\n'
              + 'except Exception:\n    pass\n' + IMPORTS_BRIDGE)
    )
    == lint.CLEAN,
    "the terminator must not retroactively invalidate statements before it",
)
check(
    "seed under `if False:` is NOT a seed",
    lint.classify(write(tmpdir, _ENV + 'if False:\n    ' + _INLINE_SEED + IMPORTS_BRIDGE))
    == lint.VIOLATION,
    "no `if` branch is guaranteed to run on import",
)
check(
    "seed inside an `except` handler is NOT a seed",
    lint.classify(
        write(tmpdir, _ENV + 'try:\n    pass\nexcept Exception:\n    ' + _INLINE_SEED + IMPORTS_BRIDGE)
    )
    == lint.VIOLATION,
    "a handler only runs on exception",
)
check(
    "seed inside a `for` body is NOT a seed",
    lint.classify(write(tmpdir, _ENV + 'for _ in []:\n    ' + _INLINE_SEED + IMPORTS_BRIDGE))
    == lint.VIOLATION,
    "zero iterations is legal",
)
check(
    "seed in a `try` BODY IS a seed (runs unconditionally)",
    lint.classify(
        write(tmpdir, _ENVDIR + 'try:\n    ' + _INLINE_SEED + 'except Exception:\n    pass\n' + IMPORTS_BRIDGE)
    )
    == lint.CLEAN,
)
check(
    "seed inside a `with` block IS a seed (runs in module order)",
    lint.classify(
        write(tmpdir, _ENVDIR + 'with open("/dev/null"):\n    ' + _INLINE_SEED + IMPORTS_BRIDGE)
    )
    == lint.CLEAN,
    "refusing conditionals would reject the ordinary TemporaryDirectory seeding shape",
)
check(
    "seed inside try/except IS a seed",
    lint.classify(
        write(tmpdir, _ENVDIR + 'try:\n    ' + _INLINE_SEED + 'except Exception:\n    pass\n' + IMPORTS_BRIDGE)
    )
    == lint.CLEAN,
)
check(
    "MISMATCH: seed discord, import telegram -> violation",
    lint.classify(write(tmpdir, _seed("discord") + _TELEGRAM_BRIDGE)) == lint.VIOLATION,
    "telegram resolves channels/telegram/access.json; a discord seed leaves it absent",
)
check(
    "MISMATCH: seed telegram, import slack -> violation",
    lint.classify(write(tmpdir, _seed("telegram") + _SLACK_BRIDGE)) == lint.VIOLATION,
)
check(
    "MISMATCH via HELPER write: seed slack, import discord -> violation",
    lint.classify(
        write(tmpdir, _ENV + 'cfg = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "slack"\n'
              'write_private_text(cfg / "access.json", "{}")\n' + IMPORTS_BRIDGE)
    )
    == lint.VIOLATION,
    "the helper shape must carry the same channel requirement as the method shape",
)
check(
    "MATCH: seed telegram, import telegram -> clean",
    lint.classify(write(tmpdir, _seed("telegram") + _TELEGRAM_BRIDGE)) == lint.CLEAN,
)
check(
    "MATCH: seed slack, import slack -> clean",
    lint.classify(write(tmpdir, _seed("slack") + _SLACK_BRIDGE)) == lint.CLEAN,
)
# Review 12 (qingyun, #2429): the helper analysis scanned EVERY argument, so a
# canonical access path passed as the DATA argument satisfied the seed check while
# the write landed somewhere else entirely. Argument ROLE matters:
# `write_private_text(path, data)` writes to arg 0.
check(
    "HELPER: canonical path in the DATA argument is NOT a seed",
    lint.classify(
        write(tmpdir, _ENV + 'cfg = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"\n'
              'write_private_text(pathlib.Path("/tmp/elsewhere.txt"), str(cfg / "access.json"))\n'
              + IMPORTS_BRIDGE)
    )
    == lint.VIOLATION,
    "the write target is arg 0; a path in the payload seeds nothing",
)
check(
    "HELPER: path in arg 0 IS a seed (positive still holds)",
    lint.classify(
        write(tmpdir, _ENVDIR + 'cfg = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"\n'
              'write_private_text(cfg / "access.json", "{}")\n' + IMPORTS_BRIDGE)
    )
    == lint.CLEAN,
)
check(
    "HELPER: keyword form path=<canonical> IS a seed",
    lint.classify(
        write(tmpdir, _ENVDIR + 'cfg = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"\n'
              'write_private_text(path=cfg / "access.json", data="{}")\n' + IMPORTS_BRIDGE)
    )
    == lint.CLEAN,
    "restricting to arg 0 must not break the keyword call form",
)
check(
    "a test importing TWO bridges needs BOTH seeded",
    lint.classify(write(tmpdir, _seed("discord") + _TELEGRAM_BRIDGE + IMPORTS_BRIDGE)) == lint.VIOLATION,
    "one seed cannot cover two channels",
)

# --- receiver + ordering bypasses (qingyun, #2429 review 7) ----------------
# MITIGATED is non-fatal, so a false mitigation silently downgrades a real violation.
check(
    "BYPASS 4: an attribute merely NAMED environ is NOT os.environ",
    lint.classify(
        write(tmpdir, 'import fake\nfake.environ["CLAUDE_CONFIG_DIR"] = "/tmp/x"\n' + IMPORTS_BRIDGE)
    )
    == lint.VIOLATION,
)
check(
    "BYPASS 5: a shadowed bare `environ` dict is NOT os.environ",
    lint.classify(
        write(tmpdir, 'environ = {}\nenviron["CLAUDE_CONFIG_DIR"] = "/tmp/x"\n' + IMPORTS_BRIDGE)
    )
    == lint.VIOLATION,
)
check(
    "BYPASS 6: a rebind on an UNRELATED object is not mitigation (receiver checked)",
    lint.classify(write(tmpdir, IMPORTS_BRIDGE + '\ncfg = object()\ncfg.ACCESS_FILE = "/tmp/a"\n'))
    == lint.VIOLATION,
)
check(
    "BYPASS 7: a rebind BEFORE the import is not mitigation (import re-resolves after it)",
    lint.classify(write(tmpdir, 'm = object()\nm.ACCESS_FILE = "/tmp/a"\n' + IMPORTS_BRIDGE))
    == lint.VIOLATION,
)
check(
    "positive: rebinding the ACTUAL imported module AFTER the import is still mitigated",
    lint.classify(write(tmpdir, IMPORTS_BRIDGE + '\nm.ACCESS_FILE = "/tmp/a.json"\n'))
    == lint.MITIGATED,
)

# --- the two adversarial bypasses (qingyun, #2429 P1) ----------------------
# Both defeated the original regex predicate and are why detection is AST-based.
check(
    "violation: a COMMENT naming CLAUDE_CONFIG_DIR is NOT isolation",
    lint.classify(
        write(tmpdir, "# Hermetic: CLAUDE_CONFIG_DIR is handled, honest.\n" + IMPORTS_BRIDGE)
    )
    == lint.VIOLATION,
)
check(
    "BYPASS 1: an assignment-SHAPED comment is NOT isolation",
    lint.classify(
        write(
            tmpdir,
            '# os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()\n' + IMPORTS_BRIDGE,
        )
    )
    == lint.VIOLATION,
    "regex matched comment text; AST never sees comments",
)
check(
    "BYPASS 2: a REAL assignment AFTER exec_module is NOT isolation",
    lint.classify(
        write(
            tmpdir,
            IMPORTS_BRIDGE + '\nimport os, tempfile\nos.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()\n',
        )
    )
    == lint.VIOLATION,
    "isolation after the import leaves module-level resolution already done",
)
check(
    "ordering: a COMPLETE isolation (env + seed) placed BEFORE the load is clean",
    lint.classify(
        write(
            tmpdir,
            'import os, tempfile, pathlib\nos.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()\n'
            'pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]).joinpath("channels", "discord").mkdir(parents=True, exist_ok=True)\n'
            '(pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord" / "access.json").write_text("{}")\n' + IMPORTS_BRIDGE,
        )
    )
    == lint.CLEAN,
)
check(
    "unparseable file is a VIOLATION, never a silent pass",
    lint.classify(write(tmpdir, "def broken(:\n" + IMPORTS_BRIDGE)) == lint.VIOLATION,
)
check(
    "late HOME assignment is also rejected",
    lint.classify(write(tmpdir, IMPORTS_BRIDGE + '\nimport os\nos.environ["HOME"] = "/tmp/x"\n'))
    == lint.VIOLATION,
)
# BYPASS 3 (qingyun, #2429 second review): the old ALT_ISOLATES accepted any `util_paths.`
# substring, so a COMMENT promising isolation counted as isolation — the very hole this lint
# exists to close. The AST rewrite dropped that predicate; these pin it shut.
check(
    "BYPASS 3: a comment mentioning util_paths.channel_access_path is NOT isolation",
    lint.classify(
        write(
            tmpdir,
            "# util_paths.channel_access_path isolates this later\n" + IMPORTS_BRIDGE,
        )
    )
    == lint.VIOLATION,
)
check(
    "BYPASS 3b: same comment placed after the import is still NOT isolation",
    lint.classify(
        write(
            tmpdir,
            IMPORTS_BRIDGE + "\n# util_paths.channel_access_path isolates this later\n",
        )
    )
    == lint.VIOLATION,
)
check(
    "mitigated: post-import ACCESS_FILE rebind",
    lint.classify(write(tmpdir, IMPORTS_BRIDGE + '\nm.ACCESS_FILE = "/tmp/a.json"\n'))
    == lint.MITIGATED,
)
check(
    "mitigated: post-import channels_env rebind",
    lint.classify(write(tmpdir, IMPORTS_BRIDGE + '\nm.channels_env = "/tmp/e"\n'))
    == lint.MITIGATED,
)
check(
    "slack and telegram bridges are in scope too",
    lint.classify(write(tmpdir, IMPORTS_BRIDGE.replace("discord", "slack"))) == lint.VIOLATION
    and lint.classify(write(tmpdir, IMPORTS_BRIDGE.replace("discord", "telegram"))) == lint.VIOLATION,
)
check("unreadable path -> None", lint.classify(tmpdir / "does-not-exist.py") is None)

# --- Review 14 (qingyun, #2429): a WRITE is not a FILE ----------------------
# The predicate proved the test asked for the canonical path; it never proved the file
# came into existence. `mkdtemp()` returns an EMPTY directory, so `channels/<ch>/` does
# not exist and the write raises FileNotFoundError having created nothing. Swallow that
# and the bridge still imports, still falling back to the operator's real allowlist.
#
# qingyun sent ONE member of this family (swallowed missing parent). Probing the SHAPE
# rather than her repro turned up seven more, including the simplest one of all: a bare
# write with no mkdir anywhere. All eight classified CLEAN before this change.
_CCD_P = 'pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"])'
_WRITE = f'({_CCD_P} / "channels" / "discord" / "access.json").write_text("{{}}")\n'
_SWALLOW = "try:\n    " + _WRITE + "except FileNotFoundError:\n    pass\n"

for _name, _body in [
    # qingyun's exact repro.
    ("swallowed missing-parent write", _ENV + _SWALLOW),
    # Simpler than her repro and equally broken: nothing creates the directory at all.
    ("bare write with no mkdir anywhere", _ENV + _WRITE),
    # `channels/` is absent too, so a non-recursive mkdir raises exactly like the write.
    ("mkdir without parents=True does not vouch for the write",
     _ENV + "try:\n    " + _CCD_P + '.joinpath("channels", "discord").mkdir(exist_ok=True)\n'
     "except FileNotFoundError:\n    pass\n" + _SWALLOW),
    # Ordering is the whole point: creating the directory afterwards is too late.
    ("mkdir AFTER the write is too late",
     _ENV + _SWALLOW + _CCD_P + '.joinpath("channels", "discord").mkdir(parents=True, exist_ok=True)\n'),
    # A directory for a different channel proves nothing about this one.
    ("mkdir for a DIFFERENT channel does not vouch for discord",
     _ENV + _CCD_P + '.joinpath("channels", "slack").mkdir(parents=True, exist_ok=True)\n' + _SWALLOW),
    # The mkdir must clear the same reachability bar as the seed, or `if False:` and a
    # never-called `def` would each launder a write that cannot succeed.
    ("mkdir under `if False:` does not vouch for the write",
     _ENV + "if os.environ.get('NEVER'):\n    " + _CCD_P
     + '.joinpath("channels", "discord").mkdir(parents=True, exist_ok=True)\n' + _SWALLOW),
    ("mkdir inside a never-called def does not vouch for the write",
     _ENV + "def _setup():\n    " + _CCD_P
     + '.joinpath("channels", "discord").mkdir(parents=True, exist_ok=True)\n' + _SWALLOW),
    # A broad handler swallows the same failure; the family is the swallow, not the type.
    ("write swallowed by a broad `except Exception` is not a seed",
     _ENV + "try:\n    " + _WRITE + "except Exception:\n    pass\n"),
]:
    check(f"BYPASS 14: {_name}",
          lint.classify(write(tmpdir, _body + IMPORTS_BRIDGE)) == lint.VIOLATION,
          "the canonical access.json is never created, so the bridge still falls back")

# The other direction, so closing this does not switch the gate off: the two recursive
# creation idioms this repo actually uses must both keep reading CLEAN.
check(
    "mkdir(parents=True) before the write is still clean",
    lint.classify(write(tmpdir, _ENVDIR + _WRITE + IMPORTS_BRIDGE)) == lint.CLEAN,
)
check(
    "the `p.parent.mkdir(parents=True)` idiom is still clean",
    lint.classify(write(tmpdir, _ENV + f'_p = {_CCD_P} / "channels" / "discord" / "access.json"\n'
                        '_p.parent.mkdir(parents=True, exist_ok=True)\n_p.write_text("{}")\n'
                        + IMPORTS_BRIDGE)) == lint.CLEAN,
    "tests/discord-bridge-file-markers.test.py and friends seed exactly this way",
)
check(
    "os.makedirs before the write is still clean",
    lint.classify(write(tmpdir, _ENV + f'os.makedirs({_CCD_P} / "channels" / "discord", exist_ok=True)\n'
                        + _WRITE + IMPORTS_BRIDGE)) == lint.CLEAN,
    "makedirs is recursive by definition and needs no parents= keyword",
)

# --- false-CLEAN paths reported on 1e7794e9 (qingyun-wu, #2429) -------------
# Both are the same shape: a check that recognised a NAME instead of the PROPERTY
# it stands for. Neither fixture ever creates or writes the canonical file, and
# both classified `clean`, so a test could read/mutate the developer's real
# channel allowlist while the gate said it was isolated.
check(
    "false CLEAN 1: a fake `.makedirs` receiver does NOT vouch for the parent",
    lint.classify(write(tmpdir,
        'import os, tempfile, pathlib\n'
        'class Fake:\n'
        '    def makedirs(self, *a, **k): pass\n'
        'os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()\n'
        f'_p = {_CCD_P} / "channels" / "discord"\n'
        'Fake().makedirs(_p)\n'
        '(_p / "access.json").write_text("{}")\n'
        + IMPORTS_BRIDGE)) != lint.CLEAN,
    "a no-op makedirs cannot create the parent; the real write then raises and is swallowed",
)
check(
    "false CLEAN 2: rooted `notchannels/.../access.json.bak` is NOT the canonical seed",
    lint.classify(write(tmpdir,
        _ENV
        + f'_p = {_CCD_P} / "notchannels" / "discord"\n'
        'os.makedirs(_p, exist_ok=True)\n'
        '(_p / "access.json.bak").write_text("{}")\n'
        + IMPORTS_BRIDGE)) != lint.CLEAN,
    "substring matching accepted both the notchannels/ lookalike and the .bak suffix",
)
# Anti-vacuity: the two pins above would also hold if classify() simply stopped
# returning CLEAN. The genuine article — real os.makedirs, exact canonical path —
# must still read clean.
check(
    "control: the genuine os.makedirs + canonical access.json seed is STILL clean",
    lint.classify(write(tmpdir,
        _ENV
        + f'_p = {_CCD_P} / "channels" / "discord"\n'
        'os.makedirs(_p, exist_ok=True)\n'
        '(_p / "access.json").write_text("{}")\n'
        + IMPORTS_BRIDGE)) == lint.CLEAN,
)
# The alias forms must keep working, or the receiver check would just break the
# idioms it is meant to protect.
check(
    "alias: `import os as _os` -> _os.makedirs still counts",
    lint.classify(write(tmpdir,
        # Rooting via plain `os.environ` on purpose: aliasing the env read too would
        # test the CCD-root resolver (which only knows `os.environ`) instead of the
        # makedirs-receiver check this pins.
        'import os, tempfile, pathlib\n'
        'import os as _os\n'
        'os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()\n'
        f'_p = {_CCD_P} / "channels" / "discord"\n'
        '_os.makedirs(_p, exist_ok=True)\n'
        '(_p / "access.json").write_text("{}")\n'
        + IMPORTS_BRIDGE)) == lint.CLEAN,
)
check(
    "alias: `from os import makedirs` -> bare makedirs still counts",
    lint.classify(write(tmpdir,
        'import os, tempfile, pathlib\n'
        'from os import makedirs\n'
        'os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()\n'
        f'_p = {_CCD_P} / "channels" / "discord"\n'
        'makedirs(_p, exist_ok=True)\n'
        '(_p / "access.json").write_text("{}")\n'
        + IMPORTS_BRIDGE)) == lint.CLEAN,
)
check(
    "a bare `makedirs(...)` that was never imported from os does NOT count",
    lint.classify(write(tmpdir,
        'import os, tempfile, pathlib\n'
        'def makedirs(*a, **k): pass\n'
        'os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()\n'
        f'_p = {_CCD_P} / "channels" / "discord"\n'
        'makedirs(_p)\n'
        '(_p / "access.json").write_text("{}")\n'
        + IMPORTS_BRIDGE)) != lint.CLEAN,
)

# --- round-16 findings (qingyun-wu / john-the-dev, #2429) -------------------
# Both are the same shape AGAIN: a check that accepted a weaker property than the
# one it needs. Order-blindness accepted permutations; an imported NAME was
# trusted after it had been rebound to something else entirely.
check(
    "permutation `access.json/channels/discord` is NOT the canonical seed",
    lint.classify(write(tmpdir,
        _ENV
        + f'_wrong = {_CCD_P} / "access.json" / "channels" / "discord"\n'
        'os.makedirs(_wrong, exist_ok=True)\n'
        '(_wrong / "seed.json").write_text("{}")\n'
        + IMPORTS_BRIDGE)) != lint.CLEAN,
    "component ORDER is load-bearing: the canonical run is channels/<bridge>/access.json",
)
check(
    "an `os` alias REBOUND to a fake no longer vouches for the parent",
    lint.classify(write(tmpdir,
        'import os, tempfile, pathlib\n'
        'class Fake:\n'
        '    def makedirs(self, *a, **k): pass\n'
        'os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()\n'
        f'_p = {_CCD_P} / "channels" / "discord"\n'
        'os = Fake()\n'
        'try:\n'
        '    os.makedirs(_p)\n'
        '    (_p / "access.json").write_text("{}")\n'
        'except FileNotFoundError:\n'
        '    pass\n'
        + IMPORTS_BRIDGE)) != lint.CLEAN,
    "import-time recognition is not call-site truth",
)
# Anti-vacuity controls. Ordering and shadow-detection are both easy to overshoot
# into "nothing is ever clean" — these pin the legitimate idioms this repo uses.
check(
    "control: multi-assignment canonical seed is STILL clean",
    lint.classify(write(tmpdir,
        _ENV
        + f'_c = {_CCD_P} / "channels" / "discord"\n'
        'os.makedirs(_c, exist_ok=True)\n'
        '(_c / "access.json").write_text("{}")\n'
        + IMPORTS_BRIDGE)) == lint.CLEAN,
)
check(
    "control: the `p.parent.mkdir(parents=True)` idiom is STILL clean",
    lint.classify(write(tmpdir,
        _ENV
        + f'_p = {_CCD_P} / "channels" / "discord" / "access.json"\n'
        '_p.parent.mkdir(parents=True, exist_ok=True)\n'
        '_p.write_text("{}")\n'
        + IMPORTS_BRIDGE)) == lint.CLEAN,
    "`.parent` DROPS a component — an ordered suffix check has to model that",
)
check(
    "control: `os.environ[...] = ...` does NOT count as rebinding `os`",
    lint.classify(write(tmpdir,
        'import os, tempfile, pathlib\n'
        'import os as _os\n'
        'os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()\n'
        f'_c = {_CCD_P} / "channels" / "discord"\n'
        '_os.makedirs(_c, exist_ok=True)\n'
        '(_c / "access.json").write_text("{}")\n'
        + IMPORTS_BRIDGE)) == lint.CLEAN,
    "a Subscript target whose base is `os` is not a rebinding of the name `os`",
)

# --- round-18 (qingyun-wu, #2429): same-length rebinding ------------------
# `_rooted_segments()` kept a binding unless the new segment list was strictly
# LONGER, so rebinding a path variable to a same-length NON-canonical path was
# ignored. The file below writes only the SLACK allowlist, yet imports the
# Discord bridge — whose canonical access.json is absent, so it can still fall
# back to the operator's real one.
check(
    "same-length rebind (discord -> slack) is NOT a discord seed",
    lint.classify(write(tmpdir,
        _ENV
        + f'_cfg = {_CCD_P} / "channels" / "discord"\n'
        '_cfg.mkdir(parents=True, exist_ok=True)\n'
        + f'_cfg = {_CCD_P} / "channels" / "slack"\n'
        '(_cfg / "access.json").write_text("{}")\n'
        + IMPORTS_BRIDGE)) != lint.CLEAN,
    "last write wins: _cfg names the SLACK path by the time access.json is written",
)
# Controls — last-write-wins must not degrade into "rebinding always dirties".
check(
    "control: a genuine single-binding discord seed is STILL clean",
    lint.classify(write(tmpdir,
        _ENV
        + f'_cfg = {_CCD_P} / "channels" / "discord"\n'
        '_cfg.mkdir(parents=True, exist_ok=True)\n'
        '(_cfg / "access.json").write_text("{}")\n'
        + IMPORTS_BRIDGE)) == lint.CLEAN,
)
check(
    "control: rebind slack -> discord, seeding discord LAST, is clean",
    lint.classify(write(tmpdir,
        _ENV
        + f'_cfg = {_CCD_P} / "channels" / "slack"\n'
        '_cfg.mkdir(parents=True, exist_ok=True)\n'
        + f'_cfg = {_CCD_P} / "channels" / "discord"\n'
        '_cfg.mkdir(parents=True, exist_ok=True)\n'
        '(_cfg / "access.json").write_text("{}")\n'
        + IMPORTS_BRIDGE)) == lint.CLEAN,
    "order-sensitivity has to cut both ways or it is just a stricter gate",
)

# --- scan() ----------------------------------------------------------------
scanned = lint.scan(["tests/lint-hermetic-bridge-tests.test.py"])
check("scan(): skips out-of-scope files", scanned == {}, str(scanned))

# --- repo-wide invariants --------------------------------------------------
tracked = lint.tracked_tests()
check("tracked_tests(): finds the tests dir", len(tracked) > 20, f"got {len(tracked)}")
check(
    "KNOWN_UNISOLATED entries all exist on disk",
    all((lint.REPO / p).exists() for p in lint.KNOWN_UNISOLATED),
    str([p for p in lint.KNOWN_UNISOLATED if not (lint.REPO / p).exists()]),
)

# --- main() modes ----------------------------------------------------------
def run_main(argv: list[str]) -> tuple[int, str]:
    old = sys.argv
    sys.argv = argv
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = lint.main()
    finally:
        sys.argv = old
    return rc, buf.getvalue()


rc, out = run_main(["lint", "--list"])
check("--list exits 0", rc == 0, f"rc={rc}")
check("--list prints a verdict column", any(v in out for v in (lint.VIOLATION, lint.CLEAN)), out[:120])

rc, out = run_main(["lint"])
check("whole-tree run is green on the current tree", rc == 0, out[-400:])
check("whole-tree run reports the scan summary", "bridge-importing tests scanned" in out, out[-200:])

rc, out = run_main(["lint", "--diff"])
check("--diff exits 0 when nothing relevant changed or all changed files are clean", rc == 0, out[-300:])

# --- remaining early-return paths ------------------------------------------
check(
    "exec_module with a non-Name arg -> no module var -> cannot be mitigated",
    lint.classify(write(tmpdir, IMPORTS_BRIDGE.replace("exec_module(m)", "exec_module(mods[0])")))
    == lint.VIOLATION,
)
check(
    "mitigation helper returns None when there is no module var",
    lint._mitigation_line(__import__("ast").parse("x.ACCESS_FILE = 1\n"), 0, None) is None,
)
check(
    "isolation helper returns None when nothing qualifies",
    lint._isolation_line(__import__("ast").parse("x = 1\n")) is None,
)

# --diff with no changed test files takes the early-exit path.
import subprocess as _sp
_rc, _out = run_main(["lint", "--diff"])
check("--diff early-exit or clean run exits 0", _rc == 0, _out[-200:])

# MITIGATED note path: classify a real mitigation via scan() so main() prints the note.
_mit = tmpdir / "mitigated_sample.test.py"
_mit.write_text(IMPORTS_BRIDGE + '\nm.ACCESS_FILE = "/tmp/a.json"\n')
check("scan() reports a real mitigation", lint.scan([str(_mit)]) .get(str(_mit)) == lint.MITIGATED)

# --- failure branches ------------------------------------------------------
# Exercise the paths that only fire on a red tree, by swapping the grandfather list.
_real_known = lint.KNOWN_UNISOLATED
try:
    # Nothing grandfathered -> every existing offender counts as a NEW violation.
    lint.KNOWN_UNISOLATED = frozenset()
    rc, out = run_main(["lint"])
    check("empty grandfather list -> exit 1", rc == 1, f"rc={rc}")
    check("failure output names the FAIL reason", "FAIL — test imports a bridge" in out, out[:200])
    check("failure output lists offending files", "tests/" in out.split("FAIL")[-1], out[-300:])
    check(
        "failure output explains the fix",
        "Set CLAUDE_CONFIG_DIR to a temp dir" in out,
        out[-300:],
    )

    # A listed file that does not exist is stale -> NOTE, and must NOT fail the run.
    lint.KNOWN_UNISOLATED = frozenset(_real_known | {"tests/_definitely-not-here.test.py"})
    rc, out = run_main(["lint"])
    check("stale grandfather entry does NOT fail the run", rc == 0, f"rc={rc}")
    check("stale entry is reported as a NOTE", "NOTE — KNOWN_UNISOLATED entries" in out, out[-400:])
    check(
        "stale entry names the file",
        "tests/_definitely-not-here.test.py" in out,
        out[-400:],
    )
finally:
    lint.KNOWN_UNISOLATED = _real_known

check("grandfather list restored after patching", lint.KNOWN_UNISOLATED is _real_known)

# --- KNOWN LIMITATIONS: pinned false positives ----------------------------
# These two shapes are hermetic and SHOULD classify clean. They do not, and the
# reason is structural rather than a bug in any one predicate: `_isolation_line`
# only counts isolation that `_reachable_nodes` proves runs unconditionally on
# import, while the load side uses `ast.walk` and sees a load anywhere — including
# inside a function body. The two detectors therefore disagree about what "runs".
#
# Neither symmetric repair is acceptable, which is why this is pinned rather than
# fixed here:
#   * load side -> _reachable_nodes  : stops seeing loads inside test functions,
#                                      which is the COMMON shape. False CLEAN.
#   * isolation side -> ast.walk     : re-admits a seed under `if False:` or in an
#                                      except handler. That is exactly the hole
#                                      round 7 closed. False CLEAN.
# The real fix is per-scope ordering (isolation before load along the same path),
# which is a new analysis and does not belong in this PR's last round.
#
# They are pinned as EXPECTED-CURRENT-BEHAVIOUR, not endorsed. The gate's documented
# stance is to under-approximate CLEAN, so a false positive is the safe direction to
# be wrong in — it costs a grandfather entry, never a silent host-config mutation.
#
# WHEN EITHER OF THESE STARTS RETURNING CLEAN: that is the scope-aware fix landing.
# Delete the corresponding pin — do not "repair" it — and note it in the PR/issue
# that made it pass. A pin that is quietly flipped to CLEAN would hide the change.
check(
    "KNOWN LIMITATION: isolation + load inside ONE function reads as violation",
    lint.classify(
        write(tmpdir,
              'import os, tempfile, pathlib, importlib.util\n'
              'def test_it():\n'
              '    os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()\n'
              '    _c = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"\n'
              '    _c.mkdir(parents=True, exist_ok=True)\n'
              '    (_c / "access.json").write_text("{}")\n'
              '    spec = importlib.util.spec_from_file_location("db", "src/discord-bridge.py")\n'
              '    m = importlib.util.module_from_spec(spec)\n'
              '    spec.loader.exec_module(m)\n')
    ) == lint.VIOLATION,
    "started classifying CLEAN — the scope-aware fix landed; delete this pin",
)
# Anti-vacuity control for the pin above. A bare `== VIOLATION` assertion would also
# hold if classify() started returning violation for everything, which would make the
# pin pass while measuring nothing. Hoisting the SAME statements to module level must
# be clean, so function scope is provably the only variable.
check(
    "control: those same statements at module level ARE clean",
    lint.classify(
        write(tmpdir,
              'import os, tempfile, pathlib, importlib.util\n'
              'os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()\n'
              '_c = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"\n'
              '_c.mkdir(parents=True, exist_ok=True)\n'
              '(_c / "access.json").write_text("{}")\n'
              'spec = importlib.util.spec_from_file_location("db", "src/discord-bridge.py")\n'
              'm = importlib.util.module_from_spec(spec)\n'
              'spec.loader.exec_module(m)\n')
    ) == lint.CLEAN,
)
# The remaining two pins share one baseline, held here so each varies exactly one thing.
# Writing this control is what caught a mis-attribution: the first draft of the guard pin
# ALSO routed the temp dir through a variable, so its violation could have come from
# either cause. The 2x2 disambiguates — direct+unconditional clean, direct+guarded
# violation, indirect+unconditional violation — so these are two independent limitations,
# and the third one below was not previously documented anywhere.
_DIRECT_SETUP = ('import os, tempfile, pathlib\n'
                 'os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()\n'
                 '_c = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"\n')
_SEED = '_c.mkdir(parents=True, exist_ok=True)\n(_c / "access.json").write_text("{}")\n'

check(
    "control (shared baseline): direct temp assignment + unconditional seed IS clean",
    lint.classify(write(tmpdir, _DIRECT_SETUP + _SEED + IMPORTS_BRIDGE)) == lint.CLEAN,
)
check(
    "KNOWN LIMITATION: seed guarded by `if not ...exists()` reads as violation",
    lint.classify(
        write(tmpdir,
              _DIRECT_SETUP +
              'if not (_c / "access.json").exists():\n'
              '    _c.mkdir(parents=True, exist_ok=True)\n'
              '    (_c / "access.json").write_text("{}")\n' + IMPORTS_BRIDGE)
    ) == lint.VIOLATION,
    "started classifying CLEAN — the scope-aware fix landed; delete this pin",
)
# Third limitation, found by the control above rather than by reading the code:
# `_isolation_line` recognises the temp dir only when the mkdtemp() call is the
# assignment's own RHS. Routing it through a variable — which is ordinary style, and
# what a test needs in order to reuse the path — is not recognised as isolation at all,
# even with no conditional anywhere. This one is NOT scope-related, so the scope-aware
# fix will not remove it; it needs the isolation predicate to follow a local binding.
check(
    "KNOWN LIMITATION: temp dir routed through a variable reads as violation",
    lint.classify(
        write(tmpdir,
              'import os, tempfile, pathlib\n'
              '_cfg = pathlib.Path(tempfile.mkdtemp())\n'
              'os.environ["CLAUDE_CONFIG_DIR"] = str(_cfg)\n'
              '_c = _cfg / "channels" / "discord"\n' + _SEED + IMPORTS_BRIDGE)
    ) == lint.VIOLATION,
    "started classifying CLEAN — the binding-following fix landed; delete this pin",
)

print("\n" + ("FAIL — " + ", ".join(failures) if failures else "PASS — lint-hermetic-bridge-tests"))
sys.exit(1 if failures else 0)
