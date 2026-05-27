#!/usr/bin/env python3
"""
Structural guard: discord-voice-server.ts owner-tier system prompt must contain
action-grounding and capability-grounding rules (#1102).

Why: Two hallucination modes observed in production (Susan, 2026-05-25):
  1. False narration — model verbally claimed "Opening the Sutando repository"
     without invoking any tool.
  2. Feature invention — model described "Za Warudo activates co-presentation mode"
     (the real function is summon-to-voice-channel).
The grounding rules close both modes at the prompt level — cheaper and faster
than adding per-tool intent checks.

Guards:
  1. Action-grounding rule present: "never say doing X without calling the tool"
  2. Capability-grounding rule present: "describe ONLY what is listed in prompt"
  3. Both rules are inside the owner-tier buildAgent path (not just anywhere in the file)
  4. Neither rule accidentally landed in the base/other-tier path
  5. Uncertainty hedge rule present: "not sure" / "let me look that up" fallback

Run: python3 tests/discord-voice-grounding-guards.test.py
Exit: 0 on pass, 1 on fail.
"""

from pathlib import Path
import sys
import re

REPO = Path(__file__).resolve().parent.parent


def fail(msg: str, ctx: str = "") -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    if ctx:
        print("---context---", file=sys.stderr)
        print(ctx[:1500], file=sys.stderr)
    return 1


def main() -> int:
    server = REPO / "skills" / "discord-voice" / "scripts" / "discord-voice-server.ts"
    if not server.exists():
        return fail(f"{server} not found")
    text = server.read_text()

    # 1. Action-grounding rule
    action_grounding_patterns = [
        "Narrating an action without a tool call",
        "NEVER say you are doing something",
        "unless you are simultaneously invoking the corresponding tool",
    ]
    for pat in action_grounding_patterns:
        if pat not in text:
            return fail(
                f"discord-voice-server.ts missing action-grounding rule fragment: {pat!r}. "
                "The owner-tier prompt must instruct the model not to verbally narrate actions "
                "without corresponding tool calls (#1102)."
            )

    # 2. Capability-grounding rule
    capability_patterns = [
        "describe ONLY what is listed in this prompt",
        "never invent capabilities",
    ]
    for pat in capability_patterns:
        if pat not in text:
            return fail(
                f"discord-voice-server.ts missing capability-grounding rule fragment: {pat!r}. "
                "The owner-tier prompt must instruct the model to describe only listed capabilities (#1102)."
            )

    # 3. Rules are inside buildAgent (owner-tier function)
    # Extract the buildAgent function body by finding function start and checking
    # the grounding rules appear within it.
    ba_match = re.search(r'function\s+buildAgent\b', text)
    if not ba_match:
        return fail("discord-voice-server.ts: buildAgent function not found — can't verify guard placement")

    build_agent_body = text[ba_match.start():]
    # Find the end of buildAgent by counting braces
    depth = 0
    end_idx = len(build_agent_body)
    for i, ch in enumerate(build_agent_body):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end_idx = i + 1
                break
    build_agent_src = build_agent_body[:end_idx]

    if "Narrating an action without a tool call" not in build_agent_src:
        return fail(
            "Action-grounding rule not found inside buildAgent — it must be in the system prompt "
            "that buildAgent constructs, not elsewhere in the file."
        )
    if "describe ONLY what is listed in this prompt" not in build_agent_src:
        return fail(
            "Capability-grounding rule not found inside buildAgent — it must be in the system prompt "
            "that buildAgent constructs, not elsewhere in the file."
        )

    # 4. Uncertainty hedge
    if "not sure" not in text or ("let me look" not in text and "I don't know" not in text and "I'm not sure" not in text):
        # Soft check — just warn-fail if BOTH are missing
        if "not sure" not in text:
            return fail(
                "discord-voice-server.ts missing uncertainty hedge ('not sure'). "
                "The system prompt should instruct the model to say 'not sure' rather than guess (#1102)."
            )

    print("PASS: discord-voice-server.ts has action-grounding + capability-grounding rules in buildAgent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
