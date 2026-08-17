#!/usr/bin/env python3
"""
The team rulebook's NO-REPLY action must catch a peer's closing message.

A peer agent ends a thread in prose that restates technical detail
("Acknowledged. Disposition remains unchanged: mergeable, not merge-ready."),
which action 1 reads as "technical question, analysis" and answers. The reply
is itself a closing message, so the peer answers it, and neither side can stop:
observed 2026-08-17 as ~4 exchanges rendered as 8 messages between two agents.

NO-REPLY previously listed only empty / punctuation-only / meta-chatter, none of
which describes a paragraph of PR status. The criterion this pins is that the
discriminator is whether the sender ASKS for something, not whether the content
looks technical.

The dict lives inside `async def _handle_discord_message`, which a unit test
cannot invoke, so this reads the source — but it extracts the team block and
then action 3 WITHIN it, rather than grepping the whole file. A file-wide
substring check would pass on the prose in this docstring.
"""
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "discord-bridge.py"
fail = 0


def check(cond, label):
    global fail
    print(("PASS: " if cond else "FAIL: ") + label)
    if not cond:
        fail = 1


text = SRC.read_text()

# Extract the "team" rulebook value: from its key to the next tier key.
start = text.find('        "team": (')
end = text.find('        "other": (', start)
check(start != -1 and end > start, "team rulebook block is locatable")
team = text[start:end]

# Action 3 only — bounded by the "Rules:" footer that follows it.
m = re.search(r'"3\. NO-REPLY.*?(?="Rules:)', team, re.S)
check(m is not None, "action 3 (NO-REPLY) is locatable inside the team block")
noreply = m.group(0) if m else ""

# The criterion, scoped to action 3. Outside it, the same words would be prose.
check("ASKS FOR NOTHING" in noreply,
      "NO-REPLY names the ask-nothing criterion")
check("acknowledgement" in noreply and "loop closed" in noreply,
      "NO-REPLY names the concrete shapes (acknowledgement, 'loop closed')")
check("EVEN WHEN it restates technical detail" in noreply,
      "NO-REPLY overrides the technical-looking read that sends it to action 1")
check("not whether the content" in noreply and "looks technical" in noreply,
      "NO-REPLY states the discriminator, not just examples")

# Token must be unique to action 1: bare "RUN CODEX" is not, since action 3's
# own first bullet routes prose there by name.
check("1. RUN CODEX" not in noreply and "codex-bounded.sh" not in noreply,
      "extraction is scoped — action 1's body is outside the NO-REPLY slice")
check("ASKS FOR NOTHING" in team,
      "sanity: the criterion is inside the team rulebook, not another tier")

# It must NOT have been added to the tiers that have no such action, where it
# would be inert text in a prompt that never reaches this decision.
other_start = text.find('        "other": (')
check("ASKS FOR NOTHING" not in text[other_start:],
      "criterion is not pasted into the other/untrusted tier")

if fail:
    print("FAIL: discord-bridge team NO-REPLY ack criterion")
    sys.exit(1)
print("PASS: a peer's closing message is classified NO-REPLY, not analysis.")
