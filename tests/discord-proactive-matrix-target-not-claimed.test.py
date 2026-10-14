#!/usr/bin/env python3
"""
Behavioral (#5368): Discord's `poll_proactive` must not CLAIM a proactive file
whose body redirects to a Matrix room — not even to release it again.

A claim renames `proactive-*.txt` out of the glob the AG2 Space gateway polls, so
claiming then releasing on every pass starves the gateway of its own file. The
sibling foreign-release test only sees the end state (file back on disk), which
the claim/release churn also produces; this test counts the claims and checks
what the gateway's glob can see while Discord holds one.

Drives the real coroutine, loaded with its real path so coverage attributes the
executed lines to src/discord-bridge.py.
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
# home, so a bridge loaded first would read the developer's own allowlist.
prior = os.environ.get("CLAUDE_CONFIG_DIR")
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-matrix-not-claimed-")
tmp = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"])
_cfg = tmp / "channels" / "discord"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / ".env").write_text("DISCORD_BOT_TOKEN=test-stub-token\n")
(_cfg / "access.json").write_text('{"allowFrom": [], "groups": {}}\n')
BRIDGE = REPO / "src" / "discord-bridge.py"
fail = 0


def check(cond, label):
    global fail
    print(("PASS: " if cond else "FAIL: ") + label)
    if not cond:
        fail = 1


def load_bridge():
    sib = REPO / "tests" / "discord-bridge-collaborator-tier.test.py"
    spec = importlib.util.spec_from_loader("sib_stub", loader=None)
    m = importlib.util.module_from_spec(spec)
    m.__file__ = str(sib)
    exec(compile(sib.read_text(), str(sib), "exec"), m.__dict__)
    m._install_discord_stub()
    spec = importlib.util.spec_from_loader("bridge_mx", loader=None)
    b = importlib.util.module_from_spec(spec)
    b.__file__ = str(BRIDGE)
    exec(compile(BRIDGE.read_text(), str(BRIDGE), "exec"), b.__dict__)
    return b


try:
    bridge = load_bridge()
    results, state = tmp / "results", tmp / "state"
    for d in (results, state, results / "undelivered"):
        d.mkdir(parents=True, exist_ok=True)
    bridge.RESULTS_DIR = results
    bridge.STATE_DIR = state
    # No last-owner-activity.json: activity routing defaults to Discord, which is
    # exactly the state in which the churn starts.

    fence = bridge._proactive_fence()
    real_claim = fence.claim
    claimed, gateway_saw = [], []

    def counting_claim(path):
        got = real_claim(path)
        if got is not None:
            claimed.append(path.name)
            # What the gateway's `_post_proactive` glob sees while Discord holds it.
            gateway_saw.append((path.name, sorted(p.name for p in results.glob("proactive-*.txt"))))
        return got

    fence.claim = counting_claim

    room = results / "proactive-1791739083.txt"
    room.write_text("[channel: !XLdYRwrweOuCCtaSnE:ag2.space]\nroom body\n")
    d7 = results / "proactive-d7.txt"
    d7.write_text("**[core: 2]**\n[channel: !XLdYRwrweOuCCtaSnE:ag2.space]\nroom body\n")

    async def one_pass():
        try:
            await asyncio.wait_for(bridge.poll_proactive(), timeout=0.5)
        except (asyncio.TimeoutError, Exception):
            pass

    for _ in range(5):
        asyncio.run(one_pass())

    print(f"  claims on Matrix-routed files over 5 passes: {len(claimed)}")
    for held, seen in gateway_saw[:2]:
        print(f"  while Discord held {held}, the gateway glob saw {seen}")
    check(claimed == [],
          "Discord never claims a file whose body redirects to a Matrix room")
    check(room.exists() and d7.exists(),
          "both Matrix-routed files stay visible as .txt for the gateway")
    check(not list(results.glob("*.sending*")), "no .sending claim is left behind")

    # Positive control: a Discord-addressed body is still claimed by Discord.
    native = results / "proactive-native.txt"
    native.write_text("[channel: 1530802402603700415]\nchannel body\n")
    asyncio.run(one_pass())
    check(claimed == ["proactive-native.txt"],
          "a DISCORD-addressed body is still claimed (the gate does not over-fire)")
finally:
    if prior is None:
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
    else:
        os.environ["CLAUDE_CONFIG_DIR"] = prior
    shutil.rmtree(tmp, ignore_errors=True)

if fail:
    print("FAIL: discord proactive Matrix-target claim gate")
    sys.exit(1)
print("PASS: Discord leaves Matrix-routed proactive files to the gateway.")
