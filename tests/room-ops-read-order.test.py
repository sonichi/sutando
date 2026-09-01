#!/usr/bin/env python3
"""`read_room` states its order, and `--oldest-first` makes `| tail` show the latest.

The default is newest-first, so `| tail` — the natural "show me the latest" idiom —
returns the OLDEST rows. A live miss on 2026-09-01: a delivery check piped a
newest-first read through `tail`, did not see the message it had just sent (index 0,
cut by the pipe), concluded the send had failed, and re-sent a duplicate to the owner.
The order was never wrong; nothing in the payload said what it was.
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "agent-room-ops"))
_spec = importlib.util.spec_from_file_location("rr", REPO / "skills" / "agent-room-ops" / "read.py")
rr = importlib.util.module_from_spec(_spec)
sys.modules["rr"] = rr
_spec.loader.exec_module(rr)

failures = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


NEWEST_FIRST = [{"ts": 300, "body": "newest"}, {"ts": 200, "body": "middle"},
                {"ts": 100, "body": "oldest"}]


def fake(monkey_msgs, **kw):
    """Drive read_room's tail without a gateway: patch the fetch loop's inputs."""
    rr_result = rr._result
    return rr_result(True, monkey_msgs, room_id="!r", complete=True, **kw)


# 1. the envelope always names its order
r = fake(NEWEST_FIRST)
check("default result declares order", r.get("order") == "newest_first", f"got {r.get('order')!r}")
r2 = fake(list(reversed(NEWEST_FIRST)), order="oldest_first")
check("oldest_first result declares order", r2.get("order") == "oldest_first")

# 2. error paths still carry the key, so a caller can read it unconditionally
err = rr._result(False, reason="boom", room_id="!r")
check("error envelope carries order too", "order" in err, f"keys: {sorted(err)}")

# 3. the CLI exposes the flag
ro_src = (REPO / "skills" / "agent-room-ops" / "room_ops.py").read_text()
check("CLI exposes --oldest-first", "--oldest-first" in ro_src)
check("CLI threads it to read_room", "oldest_first=a.oldest_first" in ro_src)

# 4. THE POINT: `| tail` on oldest-first yields the LATEST message.
#    Control: on newest-first the same pipe yields the OLDEST — the live failure.
newest_first_tail = NEWEST_FIRST[-1]["body"]
oldest_first_tail = list(reversed(NEWEST_FIRST))[-1]["body"]
check("CONTROL: tail of newest-first is the OLDEST (the trap)",
      newest_first_tail == "oldest", f"got {newest_first_tail}")
check("tail of oldest-first is the NEWEST (the fix)",
      oldest_first_tail == "newest", f"got {oldest_first_tail}")

print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("All read-order checks passed.")
