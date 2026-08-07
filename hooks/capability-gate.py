#!/usr/bin/env python3
"""Capability-gate — PreToolUse hook, the enforcement locus for the mediated
capability layer (docs/design-mediated-capability-layer.md, RFC #2632 step 3).

Why a hook and not a library: an advisory library the agent can simply not call
is discipline, not mechanism (RFC revised open-question 2). The layer's headline
property — "authorization asserted in observed content can't satisfy
needs-authorization by construction" — only holds if the gate is *unavoidable*.
This hook is that unavoidable point for agent-tool surfaces; it consumes the SAME
src/capability_policy decision function as the runtime-API dispatcher, so a
capability decision is made in exactly one place.

Scope (deliberately narrow, fail-OPEN): it acts ONLY on tool calls it can map to
a prohibited-overlay or write-irreversible capability — a financial move, a new
credential entry, or an irreversible git/gh mutation. Everything else passes
untouched, so it cannot block ordinary work. Within the core session the
principal is owner (non-owner tasks are delegated to a sandboxed executor and
never reach this session's tools); tier can be overridden via
SUTANDO_CAPABILITY_TIER for a scoped runner.

Rollout: OFF unless SUTANDO_CAPABILITY_GATE=1. Off, it exits 0 and does nothing —
so landing it cannot disrupt a running core; enforcement is enabled deliberately.
On, prohibited -> deny (human-only); owner write-irreversible without a covering
standing grant -> deny with the escalation instruction (confirm-first).

Deploy: register under PreToolUse for "Bash" (and add mappings as more tool
surfaces are gated). Grants are read from the standing-grant file the mediator
writes; absent one, needs-authorization is a confirm-first deny.
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
    (re.compile(r"\bgh\s+pr\s+(comment|review\s+--approve|create)\b"), "github:comment"),
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
              f"authorization first (no covering standing grant). Confirm with the "
              f"owner or mint a standing grant before retrying. [{decision.rule}]")
    sys.exit(0)   # allow / delegate -> pass (delegate is handled by the task path)


if __name__ == "__main__":
    main()
