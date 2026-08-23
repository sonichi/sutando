#!/usr/bin/env python3
"""log_group_reach reports the privacy-mode state that Telegram never logs.

Privacy mode drops group messages server-side, so the failure produces no log
line and no error — it is only visible if the bridge asks getMe and says so.
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
spec = importlib.util.spec_from_file_location("telegram_bridge", ROOT / "src" / "telegram-bridge.py")
tb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tb)

failures = []


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


on = tb.log_group_reach(lambda: {"result": {"username": "echo_act_iv_pro_bot",
                                            "can_read_all_group_messages": False}})
check("privacy ON is reported as ON", "privacy mode ON" in on)
check("privacy ON names the bot's own @username so the exempt form is copyable",
      "@echo_act_iv_pro_bot" in on)
check("privacy ON names all three exempt cases",
      "commands" in on and "replies" in on and "mention" in on)

off = tb.log_group_reach(lambda: {"result": {"username": "b", "can_read_all_group_messages": True}})
check("privacy OFF is reported as OFF", "privacy mode OFF" in off)

# The two states must not render alike — that is the whole point of the line.
check("ON and OFF are distinguishable", on != off and ("ON" in on) != ("ON" in off))

# A probe that cannot run must say so, never imply a clean state: an unknown
# rendering as OFF would recreate the invisibility this line exists to remove.
raised = tb.log_group_reach(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
check("getMe raising is reported as unknown, not as OFF",
      "unknown" in raised and "privacy mode OFF" not in raised)
check("getMe raising does not propagate", isinstance(raised, str))

empty = tb.log_group_reach(lambda: {})
check("empty getMe result is unknown, not OFF",
      "unknown" in empty and "privacy mode OFF" not in empty)

# Absent field must not read as OFF either — Bot API omits false-y fields.
missing = tb.log_group_reach(lambda: {"result": {"username": "b"}})
check("absent can_read_all_group_messages reads as ON (restricted), never OFF",
      "privacy mode ON" in missing)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all telegram group-reach log assertions passed")
