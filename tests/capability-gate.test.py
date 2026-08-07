#!/usr/bin/env python3
"""Capability-gate PreToolUse hook — hooks/capability-gate.py (RFC #2632 step 3).

Drives the hook as a real subprocess (its actual stdin-JSON -> permissionDecision
contract), covering: gate OFF by default (landing it never disrupts a core),
prohibited-overlay deny (financial move / credential entry, human-only), owner
write-irreversible needs-authorization deny (confirm-first), and fail-open
pass-through for anything not mapped to a gated capability.

Run: python3 tests/capability-gate.test.py
"""
import json
import os
import pathlib
import subprocess
import sys

HOOK = str(pathlib.Path(__file__).resolve().parents[1] / "hooks" / "capability-gate.py")
failures = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def run(tool_name, command, gate="1", tier=None):
    env = dict(os.environ)
    if gate is None:
        env.pop("SUTANDO_CAPABILITY_GATE", None)
    else:
        env["SUTANDO_CAPABILITY_GATE"] = gate
    if tier:
        env["SUTANDO_CAPABILITY_TIER"] = tier
    payload = json.dumps({"tool_name": tool_name, "tool_input": {"command": command}})
    p = subprocess.run([sys.executable, HOOK], input=payload, capture_output=True,
                       text=True, env=env)
    out = {}
    if p.stdout.strip():
        try:
            out = json.loads(p.stdout)
        except Exception:
            out = {}
    decision = (out.get("hookSpecificOutput") or {}).get("permissionDecision")
    return p.returncode, decision, p.stdout


# gate OFF -> never denies, even a financial move
rc, dec, _ = run("Bash", "wire transfer funds 500 usd to acct", gate=None)
check("gate OFF by default -> no deny (landing the hook can't disrupt a core)",
      rc == 0 and dec is None)

# prohibited overlay -> deny (human-only), gate ON
rc, dec, out = run("Bash", "wire transfer funds 500 usd to acct")
check("financial move -> deny (prohibited overlay, human-only)", dec == "deny", out)
rc, dec, out = run("Bash", "vault set OPENAI_API_KEY sk-xxx")
check("vault set (credential entry) -> deny (prohibited overlay)", dec == "deny", out)

# owner write-irreversible, no grant -> needs-authorization deny (confirm-first)
rc, dec, out = run("Bash", "gh pr merge 2729 --squash")
check("gh pr merge -> deny (owner write-irreversible needs authorization)", dec == "deny", out)
rc, dec, out = run("Bash", "rm -rf build/")
check("rm -rf -> deny (fs:delete, irreversible, needs authorization)", dec == "deny", out)

# non-gated command -> pass untouched (fail-open)
rc, dec, _ = run("Bash", "ls -la && git status")
check("ordinary command -> pass untouched (fail-open, no deny)", rc == 0 and dec is None)
rc, dec, _ = run("Read", "whatever")
check("non-Bash tool -> pass untouched", rc == 0 and dec is None)

# tier override: 'other' tier -> even a github:comment denies (deny cell)
rc, dec, out = run("Bash", "gh pr comment 1 --body hi", tier="other")
check("tier=other + github:comment -> deny (matrix deny cell)", dec == "deny", out)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    raise SystemExit(1)
print("ALL PASS")
