#!/usr/bin/env python3
""""Is this the owner?" has one answer for a local agent's cards and for the core's switch cards.

A local agent's card desk (packages/ag2-sparrow/ag2_sparrow/card_desk.py) asks
`agent_card.owner_may_act`; the core's switch-card intercept (src/switch_card_intercept.py) compared the
tier alone, so a task marked `collaborator`, or one whose sender is not a Matrix id, was the owner to
one and not to the other.

  a) over a table of senders, the intercept takes a switch request exactly when owner_may_act says owner
  b) the cases that differed before: a collaborator header, and a sender that is not a Matrix id
  c) both callers go through owner_may_act: neither compares `access_tier` itself

Run: python3 tests/owner-check-is-one.test.py
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import agent_card  # noqa: E402
import switch_card_intercept as sci  # noqa: E402
import task_envelope  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def task(*, tier="owner", kind="dm", sender="@owner:example.org", extra="") -> str:
    return ("id: task-abc\nsource: ag2space\nchannel_id: !dm:example.org\ntask_layout: mid\n"
            f"task: switch account\nroom_name: DM\nchannel_kind: {kind}\nsource_message_id: $evt\n"
            f"user_id: {sender}\ninteraction_type: message\naccess_tier: {tier}\n{extra}")


CASES = (
    ("the owner in their DM", {}, True),
    ("the owner in a room", {"kind": "room"}, True),
    ("a team sender", {"tier": "team"}, False),
    ("a guest", {"tier": "guest"}, False),
    ("no tier at all", {"tier": ""}, False),
    ("an owner-tier task marked collaborator", {"extra": "collaborator: true\n"}, False),
    ("a sender that is not a Matrix id", {"sender": "owner"}, False),
)


def main() -> int:
    ws = Path(tempfile.mkdtemp(prefix="owner-check-"))
    task_envelope.load_or_create_key(ws)
    for name, over, owner in CASES:
        text = task_envelope.stamp_text(task(**over), ws)
        headers = dict(task_envelope.attested_task_headers(text, ws).headers)
        desk = agent_card.owner_may_act(headers, dm_only=False) is None
        core = sci.request(text, ws) is not None
        check(desk == core == owner, f"a/b) {name}: cards say {desk}, the core's switch cards say {core}, want {owner}")
    for path in ("src/switch_card_intercept.py", "packages/ag2-sparrow/ag2_sparrow/card_desk.py"):
        source = (REPO / path).read_text(encoding="utf-8")
        check("owner_may_act(" in source, f"c) {path} asks owner_may_act")
        check(not re.search(r"access_tier[^\n]{0,40}[!=]=\s*[\"']owner", source),
              f"c) {path} does not compare access_tier itself")
    print("\n%d failure(s)" % len(FAILS) if FAILS else "\nall passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
