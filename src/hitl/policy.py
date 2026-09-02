"""Manager-level auto-answer policy: the tail of permission requests that never
needs a human.

The Manager consults the policy on every create; a decided requirement is
recorded with `decided_by="policy"` and `chosen_action` already set, so the
producer (a blocking hook, a driver) proceeds exactly as after a card click —
and the projector never posts a card for it. One policy point serves every
producer (hook, TUI event, ACP), which is why it lives here and not in each.
Anything the policy cannot decide is a card; the policy never denies.
"""

from __future__ import annotations

import os
from typing import Iterable, Mapping, Optional

from .schema import HumanRequirement

ALLOW_TOOLS_ENV = "SUTANDO_HITL_ALLOW_TOOLS"
# Tools whose own effect is local and read-only. Task/Skill only START work whose
# tool calls are hooked individually; WebFetch is an egress channel and is NOT here.
DEFAULT_ALLOW_TOOLS = (
    "Read", "Glob", "Grep", "LS", "TodoWrite", "TodoRead",
    "WebSearch", "Task", "Skill", "NotebookRead",
)
AUTO_ALLOW_KIND = "allow_once"


class AllowlistPolicy:
    """Auto-allow a permission requirement whose subject tool is allowlisted."""

    def __init__(self, tools: Iterable[str]):
        self.tools = frozenset(t.strip() for t in tools if t and t.strip())

    def decide(self, req: HumanRequirement) -> Optional[str]:
        if req.kind != "permission":
            return None
        tool = str((req.subject or {}).get("tool") or "")
        if tool not in self.tools:
            return None
        for a in req.actions:
            if a.kind == AUTO_ALLOW_KIND:
                return a.id
        return None


def policy_from_env(env: Optional[Mapping[str, str]] = None) -> AllowlistPolicy:
    env = os.environ if env is None else env
    raw = env.get(ALLOW_TOOLS_ENV)
    tools = raw.split(",") if raw is not None else DEFAULT_ALLOW_TOOLS
    return AllowlistPolicy(tools)
