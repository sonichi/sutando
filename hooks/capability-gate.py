#!/usr/bin/env python3
"""Capability-gate — PreToolUse hook that consumes src/capability_policy.decide().

Opt-in DORMANT scaffolding, not a wired enforcement layer: OFF unless
SUTANDO_CAPABILITY_GATE=1, and nothing production-registers it yet. When enabled
it is a coarse confirm-first backstop over Bash — prohibited/irreversible/deny ->
deny; a needs-authorization action is always a confirm-first deny (a human
performs it: this hook does NOT honor grants). Fail-OPEN: anything it can't map,
or a missing policy module, passes untouched, so landing it can't disrupt a core.
Principal is owner by default (SUTANDO_CAPABILITY_TIER overrides for a scoped
runner). Design rationale lives in docs/design-mediated-capability-layer.md.
"""
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))
try:
    import capability_policy as cp
except Exception:  # policy module absent -> fail open (never break tool use)
    cp = None


# Bash command -> capability verb. Narrow + conservative: only the clearly
# irreversible / prohibited shapes. A miss is fail-open (None), never a guess.
_BASH_PATTERNS = (
    (re.compile(r"\bgh\s+pr\s+merge\b"), "github:merge"),
    (re.compile(r"\bgit\s+push\b.*--force|\bgit\s+push\s+-f\b"), "github:merge"),
    # Any `gh pr review` write is an authority action (approve/request-changes/
    # comment, PR number in any position) — gate it like an irreversible write.
    (re.compile(r"\bgh\s+pr\s+review\b"), "github:merge"),
    (re.compile(r"\bgh\s+pr\s+(comment|create)\b"), "github:comment"),
    (re.compile(r"\brm\s+-rf?\b|\brm\s+-fr?\b"), "fs:delete"),
    # financial moves / credential entry — prohibited overlay, human-only
    (re.compile(r"\b(transfer|withdraw|deposit|wire)\b.*\b(funds|money|usd|btc|eth)\b",
                re.IGNORECASE), "financial:move"),
    (re.compile(r"\bvault\s+set\b|\bsecurity\s+add-generic-password\b"), "credential:entry"),
)


def _verb_for(tool_name: str, tool_input: dict):
    """Map a tool call to a capability verb, or None (fail-open)."""
    if tool_name == "Bash":
        cmd = str((tool_input or {}).get("command", ""))
        for pat, verb in _BASH_PATTERNS:
            if pat.search(cmd):
                return verb
    return None


def _deny(reason: str):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


def main():
    if os.environ.get("SUTANDO_CAPABILITY_GATE", "").strip() not in {"1", "true", "on", "yes"}:
        sys.exit(0)   # gate OFF by default — landing this never disrupts a core
    if cp is None:
        sys.exit(0)   # no policy module -> fail open
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        sys.exit(0)
    verb = _verb_for(data.get("tool_name", ""), data.get("tool_input", {}))
    if verb is None:
        sys.exit(0)   # not a gated capability -> pass untouched

    tier = cp.normalize_tier(os.environ.get("SUTANDO_CAPABILITY_TIER") or "owner")
    req = cp.CapabilityRequest(verb=verb)
    decision = cp.decide(req, cp.Principal(tier=tier),
                         prohibited_overlay=cp.DEFAULT_PROHIBITED_OVERLAY)

    if decision.decision == cp.PROHIBITED:
        _deny(f"CAPABILITY GATE: {verb} is human-only (prohibited overlay) — "
              f"do it yourself; no agent path is authorized. [{decision.rule}]")
    if decision.decision == cp.DENY:
        _deny(f"CAPABILITY GATE: {verb} denied for tier {tier}. [{decision.rule}]")
    if decision.decision == cp.NEEDS_AUTH:
        _deny(f"CAPABILITY GATE: {verb} is irreversible and needs owner "
              f"authorization — a human must perform this action. [{decision.rule}]")
    sys.exit(0)   # allow / delegate -> pass (delegate is handled by the task path)


if __name__ == "__main__":
    main()
