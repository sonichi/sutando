#!/usr/bin/env python3
"""The taskify route persisted room message bodies to task files unfiltered.

Two routes in remote_gateway_bridge end in a task file. The delivery route
applies `filter_chat_secrets` before persisting; the events/taskify route copied
`content.body` verbatim into `tasks/task-taskify-<digest>.txt`. A task file is
durable and is then read back as an `access_tier: ambient` task, so this
persists a secret rather than merely handing one back.
"""
import glob
import os
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "packages" / "ag2-sparrow"
sys.path.insert(0, str(PKG))

from ag2_sparrow.chat_secret_filter import filter_chat_secrets  # noqa: E402
from ag2_sparrow.event_consumer import TaskifyHandler  # noqa: E402

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"       got {got!r}, want {want!r}")
        fails.append(name)


def promote(body):
    """Run one event through a threshold-1 handler; return the task file text."""
    d = tempfile.mkdtemp()
    h = TaskifyHandler(task_dir=d, agent_mxid="@me:ag2.space", threshold=1,
                       log=lambda *a: None)
    h.offer({"event_id": "e1", "type": "message.created", "room_id": "!r:ag2.space",
             "actor_id": "@someone:ag2.space", "cursor": 1,
             "content": {"body": body}})
    files = glob.glob(os.path.join(d, "*.txt"))
    return Path(files[0]).read_text() if files else ""


# 1. The reported leak: a vault-set line must not reach disk verbatim.
SECRET = "vault set OPENAI_API_KEY sk-proj-AbCdEf123456SUPERSECRET7890"
out = promote(SECRET)
check("the raw token is not persisted", "sk-proj-AbCdEf123456SUPERSECRET7890" in out, False)
check("the line is still promoted (not dropped)", "message.created" in out, True)

# 2. PARITY: assert against the filter's own runtime output, never a literal, so
#    a change to the redaction vocabulary cannot let the two routes diverge.
check("the taskify route matches what the delivery route would write",
      filter_chat_secrets(SECRET).text in out, True)

# 3. ORDERING: redact BEFORE the 120-char cut — truncating first can split a
#    token so no pattern matches, persisting a recognisable partial.
LONG = ("please rotate this for me when you get a sec, it is the prod one and "
        "I want it swapped today ok thanks -- sk-proj-AbCdEf123456SUPERSECRET7890abcdefghij")
assert len(LONG.splitlines()[0][:120]) == 120, "fixture must exceed the cut"
assert "sk-proj-AbCdEf" in LONG.splitlines()[0][:120], "fixture must straddle the cut"
out_long = promote(LONG)
check("a token straddling the 120-char cut leaks no partial",
      "sk-proj-AbCdEf" in out_long, False)

# 4. CONTROL — ordinary prose is untouched. Without this, "redact everything"
#    would satisfy every assertion above.
PROSE = "can you take a look at the deploy log when you have a minute"
out_prose = promote(PROSE)
check("ordinary prose survives verbatim", PROSE in out_prose, True)

# 5. The surrounding contract still holds: this is an ambient observation, and
#    the in-band block is what stops it being read as an instruction.
check("the promoted task is still guest-tier", "access_tier: guest" in out, True)
check("the promoted task carries origin: promoted", "origin: promoted" in out, True)
check("the in-band observation block survives", "SUTANDO SYSTEM INSTRUCTIONS" in out, True)

# 6. Empty and missing bodies must not raise — the filter sits on a path that
#    sees member.joined / reaction.added events carrying no text at all.
for label, body in (("empty string", ""), ("whitespace", "   ")):
    try:
        promote(body)
        check(f"a {label} body does not raise", True, True)
    except Exception as exc:  # noqa: BLE001
        check(f"a {label} body does not raise", repr(exc), True)

print(("FAILED: " + ", ".join(fails)) if fails else "taskify redaction: all checks passed")
sys.exit(1 if fails else 0)
