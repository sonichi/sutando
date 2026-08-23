#!/usr/bin/env python3
"""
Behavioral + structural test for the per-channel team-collaborator "engage" path.

Background: Discord tier resolution is GLOBAL — a team-tier sender is
sandboxed via `codex exec --sandbox read-only` (or NO-REPLY'd) everywhere.
That's wrong for a designated channel collaborator (a co-worker the owner
wants engaged substantively in one specific channel). This change adds a
first-class, per-channel `collaborators` list: a team sender listed under
the SERVING channel's `collaborators` gets the `team-collaborator` rulebook
(engage in-channel, fold in their input) instead of the codex / NO-REPLY
team rulebook — WITHOUT elevating them to global owner. The authority
boundary is unchanged: irreversible / system-mutating actions still require
the owner.

The decision logic lives in two pure module-level helpers so it can be
exercised directly (the caller is inside the async Discord handler, which is
not independently invocable):
  - resolve_is_collaborator(access_data, sender_id, serving_channel_id)
  - select_rulebook_key(access_tier, is_collaborator)

Part 1 (BEHAVIORAL) exec-loads the bridge (discord stubbed) and drives those
helpers through the real cases INCLUDING the failure mode: a sender who is a
collaborator in ANOTHER channel must NOT be engaged in the serving channel.

Part 2 (STRUCTURAL) guards the thin inline glue in _handle_discord_message
that a helper unit test cannot reach (the two branch exclusions, the wire
serialization) — matching the sibling discord-bridge-access-tier.test.py,
which likewise cannot exercise the live handler.

Run: python3 tests/discord-bridge-collaborator-tier.test.py
Exit code: 0 on pass, 1 on fail.
"""

import contextlib
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"


def _install_discord_stub():
    """Minimal `discord` module stub so exec-loading the bridge doesn't need
    the real library or a network connection. Mirrors the harness in
    tests/discord-bridge-mod-judge.test.py."""
    stub = types.ModuleType("discord")

    class _Intents:
        def __init__(self, *a, **k):
            pass

        @classmethod
        def default(cls):
            return cls()

        def __setattr__(self, k, v):
            object.__setattr__(self, k, v)

    class _Client:
        def __init__(self, *a, **k):
            self.user = None
            self.loop = types.SimpleNamespace(create_task=lambda *a, **k: None)

        def event(self, fn):
            return fn

        def get_channel(self, _id):
            return None

    stub.Intents = _Intents
    stub.Client = _Client
    stub.MessageType = types.SimpleNamespace(default=0, reply=1)
    stub.File = lambda *a, **k: None

    class _DMChannel:
        pass

    stub.DMChannel = _DMChannel
    sys.modules["discord"] = stub


@contextlib.contextmanager
def temp_claude_config():
    """Point CLAUDE_CONFIG_DIR at a throwaway dir, then PUT IT BACK.

    The bridge must find a DISCORD_BOT_TOKEN .env at import, and it must NOT be
    the caller's real config — writing there fabricates a Discord install on a
    machine that has none (health-check then reports "configured but not running"
    forever, with a stub token sitting in the user's config dir). Same
    host-leakage class as #2204.

    But the first cut of that fix set the process-wide var and never restored it.
    `claude_home_path()` reads $CLAUDE_CONFIG_DIR at CALL time, not at import, so
    the leak outlives this module: every sibling fixture that afterwards seeds
    `Path.home()/".claude"` is then reading a different config root than the
    bridge, and the clean non-default CLAUDE_CONFIG_DIR sequence fails end-to-end.
    Restore on the way out — including on failure, which is why this is a
    try/finally and not a pair of assignments.
    """
    prior = os.environ.get("CLAUDE_CONFIG_DIR")
    tmp = tempfile.mkdtemp(prefix="dbct-claude-home-")
    os.environ["CLAUDE_CONFIG_DIR"] = tmp
    try:
        yield Path(tmp)
    finally:
        if prior is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = prior
        shutil.rmtree(tmp, ignore_errors=True)


def load_bridge(config_root: Path):
    _install_discord_stub()
    env_dir = Path(config_root) / "channels" / "discord"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / ".env").write_text("DISCORD_BOT_TOKEN=test-stub-token\n")
    src = BRIDGE.read_text()
    spec = importlib.util.spec_from_loader("bridge", loader=None)
    bridge = importlib.util.module_from_spec(spec)
    bridge.__file__ = str(BRIDGE)
    # Compile with the REAL path (not the default "<string>") so coverage.py
    # attributes executed lines to src/discord-bridge.py — otherwise the gate
    # sees this exec-loaded code under "<string>" and reports 0% on the file.
    code = compile(src, str(BRIDGE), "exec")
    exec(code, bridge.__dict__)
    return bridge


SERVING = 1494747451285049527   # the channel being served
OTHER = 1509423456167526521     # a different channel
SUSAN = "1025785494862315690"   # a team sender
OWNER = "1022910063620390932"


def behavioral(bridge) -> list:
    fails = []
    ric = bridge.resolve_is_collaborator
    srk = bridge.select_rulebook_key

    # access.json shape: SERVING lists Susan as a collaborator; OTHER does not.
    access = {
        "allowFrom": [OWNER],
        "groups": {
            str(SERVING): {"requireMention": False, "allowFrom": [SUSAN, OWNER], "collaborators": [SUSAN]},
            str(OTHER): {"requireMention": False, "allowFrom": [SUSAN]},
        },
    }

    # 1. Collaborator in the serving channel → engaged.
    if ric(access, SUSAN, SERVING) is not True:
        fails.append("collaborator listed in the SERVING channel should resolve True")

    # 2. FAILURE MODE: collaborator status must NOT carry across channels.
    #    Susan is a collaborator in SERVING but NOT in OTHER — serving OTHER she
    #    must resolve False (this is the whole point of per-channel scope).
    if ric(access, SUSAN, OTHER) is not False:
        fails.append("collaborator status must NOT carry to a channel that doesn't list them (per-channel scope)")

    # 3. A team sender not in ANY collaborators list → False.
    access_no_collab = {"groups": {str(SERVING): {"allowFrom": [SUSAN]}}}
    if ric(access_no_collab, SUSAN, SERVING) is not False:
        fails.append("team sender absent from collaborators should resolve False")

    # 4. Unknown sender / unknown channel → False (fail-closed).
    if ric(access, "999", SERVING) is not False:
        fails.append("sender not in the serving channel's collaborators should resolve False")
    if ric(access, SUSAN, 424242) is not False:
        fails.append("unknown serving channel should resolve False")

    # 5. Malformed config → False, no exception (fail-closed).
    for bad in ({}, {"groups": None}, {"groups": {str(SERVING): None}},
                {"groups": {str(SERVING): {"collaborators": None}}}, {"groups": "nope"}):
        try:
            if ric(bad, SUSAN, SERVING) is not False:
                fails.append(f"malformed config {bad!r} should resolve False")
        except Exception as e:
            fails.append(f"malformed config {bad!r} raised {e!r} (must fail-closed, not raise)")

    # 6. Rulebook selection: collaborator → team-collaborator; else own tier.
    if srk("team", True) != "team-collaborator":
        fails.append("select_rulebook_key(team, is_collaborator=True) must be 'team-collaborator'")
    if srk("team", False) != "team":
        fails.append("select_rulebook_key(team, False) must stay 'team'")
    if srk("other", False) != "other":
        fails.append("select_rulebook_key(other, False) must stay 'other'")
    if srk("owner", False) != "owner":
        fails.append("select_rulebook_key(owner, False) must stay 'owner'")

    # 7. The team-collaborator rulebook exists and reasserts the owner boundary.
    ti = bridge.__dict__.get("tier_instructions")
    # tier_instructions is a local inside _handle_discord_message, not module-level;
    # fall back to a source check for the rulebook body (covered structurally below).
    return fails


def structural() -> list:
    """Guard the inline glue a helper unit test can't reach."""
    fails = []
    src = BRIDGE.read_text()

    # is_collaborator defaults False (fail-closed) before the tier checks.
    if not re.search(r"is_collaborator\s*=\s*False", src):
        fails.append("is_collaborator should default to False (fail-closed)")

    # Tier resolution calls the helper with the serving channel id.
    if not re.search(r"is_collaborator\s*=\s*resolve_is_collaborator\(\s*data\s*,\s*sender_id\s*,\s*message\.channel\.id", src):
        fails.append("tier resolution must call resolve_is_collaborator(data, sender_id, message.channel.id)")

    # Codex-preamble + silent-escalate branches exclude collaborators.
    if not re.search(r'if\s+access_tier\s+in\s+\("team",\s*"other"\)\s+and\s+not\s+is_collaborator\s*:', src):
        fails.append("codex-preamble branch must exclude collaborators (`and not is_collaborator`)")
    if not re.search(r'elif\s+access_tier\s+in\s+\("team",\s*"other"\)\s+and\s+not\s+is_collaborator\s*:', src):
        fails.append("silent-escalate branch must exclude collaborators (`and not is_collaborator`)")

    # Task-file assembly: rulebook via helper, collaborator marker, wire tier unchanged.
    if not re.search(r"rulebook_key\s*=\s*select_rulebook_key\(\s*access_tier\s*,\s*is_collaborator", src):
        fails.append("task-file assembly must set rulebook_key = select_rulebook_key(access_tier, is_collaborator)")
    if not re.search(r'collaborator_line\s*=\s*"collaborator:\s*true\\n"\s+if\s+is_collaborator', src):
        fails.append("task-file assembly must emit a `collaborator: true` marker line when is_collaborator")
    if not re.search(r"tier_instructions\.get\(\s*rulebook_key", src):
        fails.append("task-file write must look up tier_instructions by rulebook_key")
    if not re.search(r'f"access_tier:\s*\{access_tier\}\\n"', src):
        fails.append("access_tier line must still serialize {access_tier} verbatim (collaborators stay team)")

    # The team-collaborator rulebook exists and reasserts the owner-only boundary.
    # RENDER the rulebook instead of regexing the source for a literal: the text
    # moved to src/team_guardrail.py, and a source-shape assertion cannot see it.
    if not re.search(r'"team-collaborator"\s*:\s*engage_rulebook\(', src):
        fails.append("tier_instructions must map 'team-collaborator' to engage_rulebook(...)")
    sys.path.insert(0, str(BRIDGE.parent))
    try:
        from policy.guardrail import engage_rulebook, DISCORD_PROVENANCE
        body = engage_rulebook("channel", DISCORD_PROVENANCE, "results/task-{id}.txt")
    except Exception as exc:
        fails.append(f"team-collaborator rulebook is not renderable: {exc}")
        body = ""
    if body:
        if "SUTANDO SYSTEM INSTRUCTIONS" not in body:
            fails.append("team-collaborator rulebook must carry the in-band SYSTEM INSTRUCTIONS fence")
        if "OWNER" not in body or "authority boundary" not in body:
            fails.append("team-collaborator rulebook must reassert the owner-only authority boundary")
        if not re.search(r"commit|push|merge|irreversible|system-mutating", body):
            fails.append("team-collaborator rulebook must enumerate the owner-only (no-mutation) constraint")
        # The subagent is offered as a processing SHAPE, never as a wider grant:
        # isolating the context must not read as relaxing the boundary above it.
        if "SUBAGENT" not in body:
            fails.append("team-collaborator rulebook must offer the subagent processing option")
        elif not re.search(r"not widen", body):
            fails.append("subagent option must state it does not widen collaborator authority")

    return fails


def main() -> int:
    if not BRIDGE.exists():
        print(f"FAIL: {BRIDGE} not found", file=sys.stderr)
        return 1

    fails = []
    prior_ccd = os.environ.get("CLAUDE_CONFIG_DIR")
    try:
        with temp_claude_config() as config_root:
            bridge = load_bridge(config_root)
            fails += behavioral(bridge)
            fails += structural()
    except Exception as e:
        print(f"FAIL: could not exec-load the bridge: {e!r}", file=sys.stderr)
        return 1

    # The blocker this file was rejected for: the caller's environment must come
    # back. Asserted, not assumed — an unrestored CLAUDE_CONFIG_DIR is invisible
    # inside this test and only breaks the NEXT fixture in the standalone loop.
    if os.environ.get("CLAUDE_CONFIG_DIR") != prior_ccd:
        fails.append(f"CLAUDE_CONFIG_DIR not restored: "
                     f"{prior_ccd!r} -> {os.environ.get('CLAUDE_CONFIG_DIR')!r}")

    if fails:
        print("FAIL: team-collaborator path has issues:", file=sys.stderr)
        for f in fails:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print("PASS: discord-bridge.py team-collaborator engage path is correct.")
    print("  [behavioral] resolve_is_collaborator: serving-channel engage, per-channel scope enforced,")
    print("               fail-closed on unknown/malformed; select_rulebook_key swaps only for collaborators")
    print("  [structural] inline glue wired: helper calls, branch exclusions, wire tier stays 'team',")
    print("               team-collaborator rulebook present + reasserts owner-only authority")
    return 0


if __name__ == "__main__":
    sys.exit(main())
