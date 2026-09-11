#!/usr/bin/env python3
"""
`[reply: <message-id>]` in the shared marker grammar.

Before this, the marker was known only to a private regex inside
discord-bridge's TASK path. The proactive path parses through
`result_markers.parse_markers`, which had never heard of it, so a proactive
body carrying the marker had it printed to the user as literal text — the
leak this module exists to prevent. Observed in the owner's channel:

    [reply: 1544846123452211261]
    <@…> <@…> 47m, 1 waiting — …

Guards:
  1. a leading [reply:] emits a `reply` action and leaves the body clean
  2. ORDER INDEPENDENCE with [channel:] — the producer writes the redirect
     first, so an order-dependent parse misses the reply in the only case
     that actually occurs
  3. a value that is not a 17-20 digit snowflake is LEFT IN the body rather
     than silently eaten, so a malformed marker stays visible
  4. a plain body is untouched
  5. dm-only still suppresses only the channel redirect, not the reply
  6. the marker never survives into the delivered body once acted on

Run: python3 tests/result-markers-reply.test.py
Exit: 0 on pass, 1 on fail.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from result_markers import parse_markers  # noqa: E402

SNOWFLAKE = "1544846123452211261"
failures = []


def check(name, ok, detail=""):
    if ok:
        print(f"ok: {name}")
    else:
        print(f"FAIL: {name}{(' — ' + detail) if detail else ''}")
        failures.append(name)


def acts(text):
    r = parse_markers(text)
    return {(a.kind, a.value) for a in r.actions}, r.body


# 1 — leading marker, clean body
a, body = acts(f"[reply: {SNOWFLAKE}]\nhello there")
check("leading [reply:] emits a reply action", ("reply", SNOWFLAKE) in a, str(a))
check("…and the marker is stripped from the body", body.strip() == "hello there", repr(body))

# 2 — order independence, both ways
a, body = acts(f"[channel: 123]\n[reply: {SNOWFLAKE}]\nhello")
check("channel-first: both markers parsed",
      {("redirect", "123"), ("reply", SNOWFLAKE)} <= a, str(a))
check("channel-first: body clean", body.strip() == "hello", repr(body))

a, body = acts(f"[reply: {SNOWFLAKE}]\n[channel: 123]\nhello")
check("reply-first: both markers parsed",
      {("redirect", "123"), ("reply", SNOWFLAKE)} <= a, str(a))

# 3 — a non-snowflake is not a target; leave it visible rather than eat it
a, body = acts("[reply: 99]\nstays put")
check("short id emits no reply action", not any(k == "reply" for k, _ in a), str(a))
check("short id is left in the body", "[reply: 99]" in body, repr(body))

# 4 — untouched
a, body = acts("plain body with no markers")
check("plain body emits no reply action", not any(k == "reply" for k, _ in a), str(a))
check("plain body is unchanged", body.strip() == "plain body with no markers", repr(body))

# 5 — dm-only guards the redirect, not the reply
a, _ = acts(f"[dm-only]\n[channel: 123]\n[reply: {SNOWFLAKE}]\nhi")
check("dm-only still suppresses the channel redirect",
      not any(k == "redirect" for k, _ in a), str(a))
check("dm-only does not suppress the reply", ("reply", SNOWFLAKE) in a, str(a))

# 6 — the leak this exists to prevent
for label, text in (("reply only", f"[reply: {SNOWFLAKE}]\nbody"),
                    ("channel first", f"[channel: 9]\n[reply: {SNOWFLAKE}]\nbody"),
                    ("reply first", f"[reply: {SNOWFLAKE}]\n[channel: 9]\nbody")):
    _, body = acts(text)
    check(f"no literal marker survives into the body ({label})",
          "[reply:" not in body, repr(body))

print()
if failures:
    print(f"FAILED — {len(failures)} failure(s)")
    sys.exit(1)
print("PASSED")
