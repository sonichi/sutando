#!/usr/bin/env python3
"""`discord-bridge.py send --reply-to` threads a CLI post onto an existing message.

The results path already defaults to quoting the triggering message
(`reply_to_id = source_message_anchor`). The `send` CLI had no reply target at
all, so every proactive post — digests, corrections, announcements — arrived
detached from the message it answered. These pin the REST payload shape, which
is the only place the reference actually reaches Discord.
"""

import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-reply-to-")
_cd = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_cd.mkdir(parents=True, exist_ok=True)
(_cd / "access.json").write_text('{"allowFrom": []}')
(_cd / ".env").write_text("DISCORD_BOT_TOKEN=test-token-not-real\n")

REPO = Path(__file__).resolve().parent.parent

try:
    import discord  # noqa: F401
except ImportError:
    stub = types.ModuleType("discord")
    stub.Intents = type("Intents", (), {"default": staticmethod(
        lambda: type("I", (), {"message_content": False})())})
    stub.Client = type("Client", (), {"__init__": lambda self, **kw: None,
                                      "event": staticmethod(lambda fn: fn)})
    stub.File = type("File", (), {})
    stub.Message = type("Message", (), {})
    sys.modules["discord"] = stub

_spec = importlib.util.spec_from_file_location("dbridge", REPO / "src" / "discord-bridge.py")
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)

failures = []


def check(cond, label):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


sys.path.insert(0, str(REPO / "src"))
from channels.discord.client import DiscordRestClient  # noqa: E402


def capture(message, **kw):
    """Run _send_via_rest through the PRODUCTION DiscordRestClient with a
    scripted transport; return the POSTed payloads the client built."""
    posted = []

    def transport(req, timeout):
        posted.append(json.loads(req.data))
        return 200, {"id": "1"}

    real = bridge._rest_client
    bridge._rest_client = lambda timeout=10: DiscordRestClient(
        "test-token-not-real", transport=transport)
    try:
        bridge._send_via_rest("123", message, **kw)
    finally:
        bridge._rest_client = real
    return posted


# --- the payload, which is what Discord actually sees -----------------------
one = capture("hello", reply_to="999888777666555444")
check(len(one) == 1, "single chunk produces one POST")
ref = one[0].get("message_reference")
check(ref is not None, "reply_to sets message_reference on the first chunk")
check((ref or {}).get("message_id") == "999888777666555444",
      "message_reference carries the id passed in")
check((ref or {}).get("fail_if_not_exists") is False,
      "fail_if_not_exists is False so a deleted target degrades to a plain send")

# The bug this guards: a reference on EVERY chunk renders N reply-headers for
# one answer. Only the first chunk may carry it.
multi = capture("y" * 1900 + "\n" + "z" * 1900, reply_to="111222333444555666")
check(len(multi) > 1, "long body actually chunks (fixture is exercising the path)")
check("message_reference" in multi[0], "first chunk of a chunked body carries the reference")
check(all("message_reference" not in c for c in multi[1:]),
      "later chunks carry NO reference")

# Regression guard: the default must stay byte-identical to pre-change behavior.
plain = capture("hello")
check("message_reference" not in plain[0], "no reply_to -> no message_reference (unchanged default)")

# --- argv parsing ------------------------------------------------------------
check(bridge._parse_send_argv(["--reply-to", "42", "body"]) == ("42", ["body"]),
      "--reply-to is consumed and the body survives")
check(bridge._parse_send_argv(["body", "--reply-to", "42"]) == ("", ["body", "--reply-to", "42"]),
      "leading position only: a body word reading --reply-to is not eaten")
check(bridge._parse_send_argv(["--body-file", "/tmp/x"]) == ("", ["--body-file", "/tmp/x"]),
      "--body-file still reaches _send_cli_body untouched")
try:
    bridge._parse_send_argv(["--reply-to", "42"])
    check(False, "a --reply-to with no body raises SystemExit")
except SystemExit:
    check(True, "a --reply-to with no body raises SystemExit")

print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("all reply-to assertions passed")
