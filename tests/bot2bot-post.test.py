#!/usr/bin/env python3
"""Tests for skills/bot2bot-post/post.py —
VALID_KINDS set, USER_AGENT, load_token() (success + missing file), resolve_bot2bot_channel()
(role=bot2bot preferred, true-valued fallback, no-match exit), resolve_other_bot()
(channel-level allowFrom, sibling-bot preference, top-level fallback, no-other→None),
post() (HTTPError → sys.exit), main() (too-few args → exit). urllib.request.urlopen
monkeypatched; no Discord API calls needed."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "skills" / "bot2bot-post" / "post.py"

sys.path.insert(0, str(REPO_ROOT / "src"))

spec = importlib.util.spec_from_file_location("bot2bot_post", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

PASS = 0
FAIL = 0


def ok(label):
    global PASS; PASS += 1; print(f"PASS  {label}")


def fail(label, detail=""):
    global FAIL; FAIL += 1
    print(f"FAIL  {label}" + (f": {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# T1 — script exists
# ---------------------------------------------------------------------------
if SCRIPT.exists():
    ok("T1: post.py exists")
else:
    fail("T1: post.py exists")

# ---------------------------------------------------------------------------
# T2 — module docstring present
# ---------------------------------------------------------------------------
content = SCRIPT.read_text()
body = "\n".join(l for l in content.splitlines() if not l.startswith("#!")).lstrip()
if body.startswith('"""') or body.startswith("'''"):
    ok("T2: post.py has module docstring")
else:
    fail("T2: module docstring present")

# ---------------------------------------------------------------------------
# T3 — VALID_KINDS contains all five coordination verbs
# ---------------------------------------------------------------------------
expected_kinds = {"claim", "blocked", "done", "ping", "opinion"}
if mod.VALID_KINDS == expected_kinds:
    ok("T3: VALID_KINDS == {'claim','blocked','done','ping','opinion'}")
else:
    fail("T3: VALID_KINDS", f"got={mod.VALID_KINDS!r}")

# ---------------------------------------------------------------------------
# T4 — USER_AGENT contains "DiscordBot"
# ---------------------------------------------------------------------------
if "DiscordBot" in mod.USER_AGENT:
    ok("T4: USER_AGENT contains 'DiscordBot'")
else:
    fail("T4: USER_AGENT", f"got={mod.USER_AGENT!r}")

# ---------------------------------------------------------------------------
# T5 — load_token() returns token when ENV_FILE contains DISCORD_BOT_TOKEN
# ---------------------------------------------------------------------------
_orig_env_file = mod.ENV_FILE
with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
    f.write("DISCORD_BOT_TOKEN=testtoken12345\n")
    _tmp_env = Path(f.name)
mod.ENV_FILE = _tmp_env
result = mod.load_token()
mod.ENV_FILE = _orig_env_file
_tmp_env.unlink(missing_ok=True)
if result == "testtoken12345":
    ok("T5: load_token() returns token from ENV_FILE when DISCORD_BOT_TOKEN is present")
else:
    fail("T5: load_token() → token", f"got={result!r}")

# ---------------------------------------------------------------------------
# T6 — load_token() calls sys.exit when ENV_FILE does not exist
# ---------------------------------------------------------------------------
mod.ENV_FILE = Path("/tmp/nonexistent-bot2bot-env-99.env")
exited = False
try:
    mod.load_token()
except SystemExit:
    exited = True
finally:
    mod.ENV_FILE = _orig_env_file
if exited:
    ok("T6: load_token() calls sys.exit when ENV_FILE does not exist")
else:
    fail("T6: load_token() missing file → sys.exit", "did not raise SystemExit")

# ---------------------------------------------------------------------------
# T7 — resolve_bot2bot_channel() returns role=bot2bot channel (preferred path)
# ---------------------------------------------------------------------------
access_preferred = {
    "groups": {
        "C_ADMIN": {"role": "admin"},
        "C_BOT2BOT": {"role": "bot2bot", "extra": True},
    }
}
result = mod.resolve_bot2bot_channel(access_preferred)
if result == "C_BOT2BOT":
    ok("T7: resolve_bot2bot_channel() returns role=bot2bot entry over other entries")
else:
    fail("T7: resolve_bot2bot_channel() preferred", f"got={result!r}")

# ---------------------------------------------------------------------------
# T8 — resolve_bot2bot_channel() falls back to true-valued channel (legacy)
# ---------------------------------------------------------------------------
access_legacy = {
    "groups": {
        "C_MONITOR": {"role": "monitor"},
        "C_LEGACY": True,
    }
}
result = mod.resolve_bot2bot_channel(access_legacy)
if result == "C_LEGACY":
    ok("T8: resolve_bot2bot_channel() falls back to true-valued channel when no role=bot2bot entry")
else:
    fail("T8: resolve_bot2bot_channel() legacy fallback", f"got={result!r}")

# ---------------------------------------------------------------------------
# T9 — resolve_bot2bot_channel() calls sys.exit when no group matches
# ---------------------------------------------------------------------------
access_empty = {"groups": {"C_OTHER": {"role": "monitor"}}}
exited = False
try:
    mod.resolve_bot2bot_channel(access_empty)
except SystemExit:
    exited = True
if exited:
    ok("T9: resolve_bot2bot_channel() calls sys.exit when no bot2bot group found")
else:
    fail("T9: resolve_bot2bot_channel() no match → sys.exit", "did not raise SystemExit")

# ---------------------------------------------------------------------------
# T10 — resolve_other_bot() returns non-self ID from channel-level allowFrom
# ---------------------------------------------------------------------------
access_ch = {
    "groups": {
        "C_BOT2BOT": {
            "role": "bot2bot",
            "allowFrom": ["self_id_111", "other_id_222"],
        }
    },
    "allowFrom": ["self_id_111"],
}
result = mod.resolve_other_bot(access_ch, "self_id_111", "C_BOT2BOT")
if result == "other_id_222":
    ok("T10: resolve_other_bot() returns non-self ID from channel-level allowFrom")
else:
    fail("T10: resolve_other_bot() channel-level allowFrom", f"got={result!r}")

# ---------------------------------------------------------------------------
# T11 — resolve_other_bot() prefers ID not in global allowFrom (sibling bot)
# ---------------------------------------------------------------------------
access_pref = {
    "groups": {
        "C_BOT2BOT": {
            "role": "bot2bot",
            "allowFrom": ["owner_id_333", "bot_id_444"],
        }
    },
    "allowFrom": ["self_id_111", "owner_id_333"],
}
result = mod.resolve_other_bot(access_pref, "self_id_111", "C_BOT2BOT")
if result == "bot_id_444":
    ok("T11: resolve_other_bot() prefers non-global-allowFrom ID (sibling bot over owner)")
else:
    fail("T11: resolve_other_bot() bot preference", f"got={result!r}")

# ---------------------------------------------------------------------------
# T12 — resolve_other_bot() falls back to top-level allowFrom when no channel-level
# ---------------------------------------------------------------------------
access_tl = {
    "groups": {"C_BOT2BOT": True},
    "allowFrom": ["self_id_111", "legacy_other_555"],
}
result = mod.resolve_other_bot(access_tl, "self_id_111", "C_BOT2BOT")
if result == "legacy_other_555":
    ok("T12: resolve_other_bot() falls back to top-level allowFrom when no channel-level allowFrom")
else:
    fail("T12: resolve_other_bot() top-level fallback", f"got={result!r}")

# ---------------------------------------------------------------------------
# T13 — resolve_other_bot() returns None when only self_id in allowFrom
# ---------------------------------------------------------------------------
access_solo = {
    "groups": {"C_BOT2BOT": {"role": "bot2bot", "allowFrom": ["self_id_111"]}},
    "allowFrom": ["self_id_111"],
}
result = mod.resolve_other_bot(access_solo, "self_id_111", "C_BOT2BOT")
if result is None:
    ok("T13: resolve_other_bot() returns None when no other IDs beyond self_id")
else:
    fail("T13: resolve_other_bot() solo → None", f"got={result!r}")

# ---------------------------------------------------------------------------
# T14 — post() calls sys.exit on HTTPError from Discord API
# ---------------------------------------------------------------------------
_orig_urlopen = urllib.request.urlopen

def _raise_discord_error(req, **kwargs):
    raise urllib.error.HTTPError(
        "https://discord.com/api/v10/channels/C/messages",
        403, "Forbidden", {}, io.BytesIO(b"Missing Permissions"),
    )

urllib.request.urlopen = _raise_discord_error
exited = False
try:
    mod.post("C_CHANNEL", "test message", "fake_token")
except SystemExit:
    exited = True
finally:
    urllib.request.urlopen = _orig_urlopen
if exited:
    ok("T14: post() calls sys.exit on HTTPError from Discord API (403 Forbidden)")
else:
    fail("T14: post() HTTPError → sys.exit", "did not raise SystemExit")

# ---------------------------------------------------------------------------
# T15 — main() calls sys.exit(1) when fewer than 3 args (kind + text required)
# ---------------------------------------------------------------------------
_orig_argv = sys.argv[:]
sys.argv = ["post.py"]
exited = False
try:
    mod.main()
except SystemExit as e:
    exited = e.code == 1
finally:
    sys.argv = _orig_argv
if exited:
    ok("T15: main() calls sys.exit(1) when fewer than 3 args (kind + text required)")
else:
    fail("T15: main() too-few args → sys.exit(1)", "did not raise SystemExit(1)")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{PASS}/{PASS+FAIL} tests passed")
if __name__ == "__main__":
    pass
