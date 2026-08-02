#!/usr/bin/env python3
"""The split-config sequence: distinct HOME and CLAUDE_CONFIG_DIR must stay green.

Regression for the #2357 review blocker (john-the-dev, 2026-07-30T02:24). Three
fixtures seeded the bridge's token at `Path.home() / ".claude" / channels/...`,
which is the WRONG root whenever CLAUDE_CONFIG_DIR is set — that is, in every real
split-config install. They passed anyway because a sibling fixture set
CLAUDE_CONFIG_DIR to a temp dir and never restored it, so later tests in the same
standalone run inherited a config root that happened to hold a token.

⚠ Those three failures were NOT introduced by fixing the leak — they were always
wrong about the root, and the leak hid it. Newly exposed is not newly introduced.

The reviewer's reproduction, as an actual test rather than a paragraph: run the
four fixtures with FRESH, DISTINCT HOME and CLAUDE_CONFIG_DIR, require exit 0 from
each, then assert the caller's config root came back UNTOUCHED.

Both halves matter, and the second one changed in the second round of review.
Exit 0 pins ROUTING — the bridge resolves its token via CLAUDE_CONFIG_DIR at import,
so a fixture seeding the wrong root cannot load. The directory assertions pin
ISOLATION. The first version of this test asserted the token landed under the
caller's CONFIG ROOT, which pinned routing but mandated the pollution: a fixture
run against a real config dir left a stub credential there, fabricating a Discord
install on a machine that has none — the same symptom this PR exists to fix.

Run: python3 tests/discord-bridge-split-config-root.test.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Order matters: collaborator-tier runs FIRST because it is the fixture whose
# unrestored CLAUDE_CONFIG_DIR used to mask the other three. Reversing the order
# would hide the very interaction under test.
SEQUENCE = [
    "discord-bridge-collaborator-tier.test.py",
    "discord-bridge-reply-directive.test.py",
    "discord-bridge-mod-judge-trackers.test.py",
    "discord-bridge-mod-judge-codex.test.py",
]

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


home = Path(tempfile.mkdtemp(prefix="dbsc-home-"))
config = Path(tempfile.mkdtemp(prefix="dbsc-config-"))
try:
    env = dict(os.environ, HOME=str(home), CLAUDE_CONFIG_DIR=str(config))
    for name in SEQUENCE:
        path = REPO / "tests" / name
        if not path.exists():
            check(f"{name} exists", False, "fixture missing — sequence incomplete")
            continue
        proc = subprocess.run([sys.executable, str(path)], env=env,
                              capture_output=True, text=True)
        detail = (proc.stderr or proc.stdout)[-160:].replace("\n", " ")
        check(f"split-root: {name} exits 0", proc.returncode == 0,
              f"rc={proc.returncode} {detail}")

    # ISOLATION is the contract, not routing. Each fixture builds its OWN throwaway
    # config root, so after the sequence the caller's CLAUDE_CONFIG_DIR and HOME must
    # be exactly as handed over: no fabricated Discord install, no stub credential.
    #
    # ⚠ The previous version of this block asserted the token landed IN `config`.
    # That verified routing while REQUIRING the pollution — it is why the fixtures
    # could pass with a stub left in the caller's real config dir (john-the-dev,
    # #2357 review 2026-07-31T07:36). Routing is still pinned, by the exit-0 checks
    # above: the bridge resolves its token through CLAUDE_CONFIG_DIR at import, so a
    # fixture that seeded the wrong root would fail to load and exit non-zero.
    in_config = (config / "channels" / "discord" / ".env").exists()
    in_home = (home / ".claude" / "channels" / "discord" / ".env").exists()
    leftovers = sorted(x.name for x in config.iterdir())
    check("caller's $CLAUDE_CONFIG_DIR has no fabricated Discord install",
          not in_config, "a fixture seeded the caller's real config root")
    check("caller's $CLAUDE_CONFIG_DIR left entirely unchanged (still empty)",
          not leftovers, f"unexpected entries: {leftovers}")
    check("caller's HOME/.claude left clean", not in_home,
          "a fixture is still hardcoding Path.home()/'.claude'")
finally:
    shutil.rmtree(home, ignore_errors=True)
    shutil.rmtree(config, ignore_errors=True)

print()
if failures:
    print(f"FAIL — {len(failures)} check(s): {failures}")
    sys.exit(1)
print("PASS — split-config-root sequence is green and leaves the caller's config root untouched")
