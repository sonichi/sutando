#!/usr/bin/env python3
"""log_privacy_setting reports the BotFather privacy setting that Telegram never logs.

Privacy mode drops group messages server-side, so the failure produces no log line
and no error — it is only visible if the bridge asks getMe and says so.

The message must describe the GLOBAL setting and nothing more. Two ways it could
lie, both of which would misdirect the person reading it at 2am:
  * claiming a plain @username mention is delivered (it is not — only /command@bot,
    replies, inline messages, and general commands when the bot posted last);
  * implying the flag determines a given group's reach, when an ADMIN bot receives
    everything regardless — i.e. exactly after the remediation this line prompts.
"""
import importlib.util
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# The bridge resolves channel config at MODULE level, so isolation must happen
# BEFORE exec_module or the import reads the developer's real ~/.claude allowlist.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-tg-privacy-")
_cfg = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "telegram"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / "access.json").write_text('{"allowFrom": []}')
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")

spec = importlib.util.spec_from_file_location("telegram_bridge", ROOT / "src" / "telegram-bridge.py")
tb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tb)

failures = []


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


on = tb.log_privacy_setting(lambda: {"result": {"username": "echo_act_iv_pro_bot",
                                                "can_read_all_group_messages": False}})
off = tb.log_privacy_setting(lambda: {"result": {"username": "b",
                                                 "can_read_all_group_messages": True}})

check("privacy ON is reported as ON", "privacy mode ON" in on)
check("privacy OFF is reported as OFF", "privacy mode OFF" in off)
check("ON and OFF are distinguishable", on != off)
check("names the bot's own @username so the exempt form is copyable",
      "@echo_act_iv_pro_bot" in on)
check("names the real exempt cases", "replies" in on and "inline" in on and "/command" in on)

# CONTROL 1 — the plain-mention claim. Telegram does NOT deliver a bare @username
# mention under privacy mode; an earlier draft of this line said it did.
check("does NOT claim a plain @username mention is delivered",
      "is NOT delivered" in on and "mention entities are delivered" not in on)

# CONTROL 2 — the admin exception. An administrator bot receives everything
# regardless of this flag, so the line must not present it as per-group reach.
check("states the administrator exception", "administrator" in on)
check("says the flag does not describe a particular group's reach",
      "does not describe" in on and "reach" in on)
check("scopes the setting as bot-wide", "bot-wide" in on and "bot-wide" in off)

# CONTROL 3 — Telegram never delivers a bot-sent message, even to an admin bot
# or with privacy off; both branches must qualify their claim accordingly.
check("privacy-OFF branch does not claim ALL messages are delivered (bot senders excepted)",
      "from human senders are delivered" in off and "never delivers a message sent by" in off)
check("privacy-ON/administrator branch carries the same bot-sender exception",
      "never delivers a message sent by" in on)

# Failure modes must report unknown, never a clean OFF.
raised = tb.log_privacy_setting(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
check("getMe raising is reported as unknown, not OFF",
      "unknown" in raised and "privacy mode OFF" not in raised)
check("getMe raising does not propagate", isinstance(raised, str))
empty = tb.log_privacy_setting(lambda: {})
check("empty getMe result is unknown, not OFF",
      "unknown" in empty and "privacy mode OFF" not in empty)
missing = tb.log_privacy_setting(lambda: {"result": {"username": "b"}})
check("absent can_read_all_group_messages reads as ON (restricted), never OFF",
      "privacy mode ON" in missing)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all telegram privacy-setting log assertions passed")
