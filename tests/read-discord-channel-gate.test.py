#!/usr/bin/env python3
"""Serving-relative contextNotFrom gate for the agent's OWN channel reads
(src/read_discord_channel.py). Proves the SAME blacklist that gates the bridge
prefetch also gates a direct read — keyed on the SERVING channel, so serving a
private channel can still read it while serving a public channel cannot.

All ids below are FICTITIOUS placeholders (no real channels/guilds). The guild
resolver is stubbed, so no live Discord is needed. Run:
  python3 tests/read-discord-channel-gate.test.py
"""
import json, tempfile, os, importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("read_discord_channel", _ROOT / "src" / "read_discord_channel.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# --- fictitious fixtures (NOT real ids) ---
PUBLIC_CH = "900000000000000001"     # a public channel that blacklists the private guild
PRIVATE_GUILD = "900000000000000002"  # a private guild
PRIVATE_CH = "900000000000000003"     # a channel inside PRIVATE_GUILD
OTHER_PUB_CH = "900000000000000004"   # a channel in another, public guild
OTHER_PUB_GUILD = "900000000000000005"

# access.json: PUBLIC_CH's contextNotFrom blacklists the whole private guild (guild-level entry);
# PRIVATE_CH itself declares no contextNotFrom (it's served legitimately).
_acc = {"groups": {PUBLIC_CH: {"contextNotFrom": [PRIVATE_GUILD]}}}
_tf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
json.dump(_acc, _tf); _tf.close()
m.ACCESS_FILE = Path(_tf.name)

# stub guild resolution (no live Discord); token is irrelevant once stubbed
_GUILD_OF = {PRIVATE_CH: PRIVATE_GUILD, OTHER_PUB_CH: OTHER_PUB_GUILD}
m.resolve_guild = lambda target, token=None: _GUILD_OF.get(str(target))

try:
    # serve PUBLIC_CH -> read a private-guild channel => BLOCKED
    r = m.gate(PUBLIC_CH, PRIVATE_CH, "tok")
    assert r is not None, "serve public-ch reading a private-guild channel MUST be blocked"
    assert PRIVATE_GUILD in r, f"block reason should cite the guild: {r}"

    # serve PRIVATE_CH -> read PRIVATE_CH => ALLOW (its own contextNotFrom lacks itself)
    assert m.gate(PRIVATE_CH, PRIVATE_CH, "tok") is None, \
        "serving the private channel must still be able to read it"

    # serve PUBLIC_CH -> read another public channel => ALLOW
    assert m.gate(PUBLIC_CH, OTHER_PUB_CH, "tok") is None, \
        "serve public-ch reading another public channel must be allowed"

    # channel-level blacklist entry also blocks (not just guild-level)
    Path(_tf.name).write_text(json.dumps({"groups": {PUBLIC_CH: {"contextNotFrom": [PRIVATE_CH]}}}))
    r2 = m.gate(PUBLIC_CH, PRIVATE_CH, "tok")
    assert r2 is not None and "channel-level" in r2, f"channel-level entry should block: {r2}"

    # a channel with no contextNotFrom configured: nothing blocked
    assert m.gate("900000000000000099", PRIVATE_CH, "tok") is None, \
        "unconfigured serving channel blocks nothing"

    print("PASS: serving-relative contextNotFrom gate — public-ch cannot read a private-guild "
          "channel, the private channel can read itself; channel- and guild-level entries both enforce")
finally:
    os.unlink(_tf.name)
