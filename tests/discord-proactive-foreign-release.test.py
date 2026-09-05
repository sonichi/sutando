#!/usr/bin/env python3
"""
Behavioral: `poll_proactive` must RELEASE a foreign-targeted file, not park it.

The sibling structural test pins the ordering in source. This one drives the real
coroutine so the release actually executes — a source-shape assertion cannot tell
whether the branch runs, and the branch is the whole fix.

Loaded with the real path in `compile()` so coverage attributes the executed lines
to src/discord-bridge.py rather than to "<string>".
"""
import asyncio
import importlib.util
import os
import pathlib
import shutil
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent

# Isolate BEFORE any bridge import: channel_access_path() falls back to the real
# home, so a test that loads a bridge first reads the developer's own allowlist.
prior = os.environ.get("CLAUDE_CONFIG_DIR")
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-foreign-release-")
_cfg = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / ".env").write_text("DISCORD_BOT_TOKEN=test-stub-token\n")
(_cfg / "access.json").write_text('{"allowFrom": [], "groups": {}}\n')
tmp = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"])
BRIDGE = REPO / "src" / "discord-bridge.py"
fail = 0


def check(cond, label):
    global fail
    print(("PASS: " if cond else "FAIL: ") + label)
    if not cond:
        fail = 1


def _stub_discord():
    """Reuse the sibling test's stub rather than a thinner copy — a partial stub
    fails at import with a TypeError that looks nothing like the real defect."""
    sib = REPO / "tests" / "discord-bridge-collaborator-tier.test.py"
    spec = importlib.util.spec_from_loader("sib_stub", loader=None)
    m = importlib.util.module_from_spec(spec)
    m.__file__ = str(sib)
    exec(compile(sib.read_text(), str(sib), "exec"), m.__dict__)
    m._install_discord_stub()


def load_bridge(config_root: pathlib.Path):
    _stub_discord()
    env_dir = config_root / "channels" / "discord"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / ".env").write_text("DISCORD_BOT_TOKEN=test-stub-token\n")
    spec = importlib.util.spec_from_loader("bridge_fr", loader=None)
    b = importlib.util.module_from_spec(spec)
    b.__file__ = str(BRIDGE)
    # Real path, so the executed lines are attributed to the source file.
    exec(compile(BRIDGE.read_text(), str(BRIDGE), "exec"), b.__dict__)
    return b


try:
    bridge = load_bridge(tmp)

    results = tmp / "results"
    state = tmp / "state"
    for d in (results, state, results / "undelivered"):
        d.mkdir(parents=True, exist_ok=True)
    bridge.RESULTS_DIR = results
    bridge.STATE_DIR = state

    foreign = results / "proactive-foreign.txt"
    foreign.write_text("[channel: !PrxhizfLysTYrYDcnw:ag2.space]\nroom body\n")

    # D7 header form: parse_markers peels `**[core: N]**` before the marker, so a
    # raw first-line regex misses it and Discord claims another bridge's file.
    d7 = results / "proactive-d7.txt"
    d7.write_text("**[core: 2]**\n[channel: !PrxhizfLysTYrYDcnw:ag2.space]\nroom body\n")

    async def one_pass():
        try:
            await asyncio.wait_for(bridge.poll_proactive(), timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            pass

    asyncio.run(one_pass())

    check(foreign.exists(),
          "the foreign-targeted file is RELEASED back to .txt for its own bridge")
    check(not list((results / "undelivered").glob("proactive-foreign*")),
          "it is NOT parked in undelivered/")
    check(not list(results.glob("proactive-foreign.sending*")),
          "no .sending claim is left behind")
    check(d7.exists(),
          "a D7 `**[core: N]**` header before the marker is ALSO released")

    # Positive control: the check must not over-fire on a real Discord target.
    native = results / "proactive-native.txt"
    native.write_text("[channel: 1530802402603700415]\nchannel body\n")
    asyncio.run(one_pass())
    check(not native.exists(),
          "a DISCORD-shaped target is still claimed (the check does not over-fire)")

    # CONFLICT: a .to-discord FILENAME outranks the body's Matrix redirect —
    # releasing it re-strands a file every other bridge refuses by its tag.
    conflict = results / "proactive-c.to-discord.txt"
    conflict.write_text("[channel: !PrxhizfLysTYrYDcnw:ag2.space]\nroom body\n")
    asyncio.run(one_pass())
    check(not conflict.exists(),
          "a destined .to-discord file with a Matrix body is NOT released — "
          "the filename decision carries through the delivery guard")
finally:
    if prior is None:
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
    else:
        os.environ["CLAUDE_CONFIG_DIR"] = prior
    shutil.rmtree(tmp, ignore_errors=True)

if fail:
    print("FAIL: discord proactive foreign release")
    sys.exit(1)
print("PASS: poll_proactive releases a foreign target instead of parking it.")
