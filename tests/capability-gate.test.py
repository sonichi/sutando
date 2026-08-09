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
# review writes with the PR number BEFORE the flag must not bypass the gate
rc, dec, out = run("Bash", "gh pr review 2729 --approve")
check("gh pr review <n> --approve -> deny (no number-first bypass)", dec == "deny", out)
rc, dec, out = run("Bash", "gh pr review 2729 --request-changes -b nope")
check("gh pr review --request-changes -> deny (all review writes gated)", dec == "deny", out)
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


# ── Standing-grant honoring: a covering owner-minted grant flips an irreversible
#    needs-authorization deny->allow; missing/wrong-identity/expired still DENY.
import tempfile as _tf   # noqa: E402


def run_grant(command, grants_rows, user="owner-1", source="", tier="owner"):
    """Run the gate with a temp standing-grants file + a principal identity."""
    d = _tf.mkdtemp()
    gf = os.path.join(d, "grants.json")
    with open(gf, "w", encoding="utf-8") as fh:
        json.dump(grants_rows, fh)
    env = dict(os.environ)
    env["SUTANDO_CAPABILITY_GATE"] = "1"
    env["SUTANDO_CAPABILITY_GRANTS_FILE"] = gf
    if user:
        env["SUTANDO_CAPABILITY_USER"] = user
    if source:
        env["SUTANDO_CAPABILITY_SOURCE"] = source
    if tier:
        env["SUTANDO_CAPABILITY_TIER"] = tier
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    p = subprocess.run([sys.executable, HOOK], input=payload, capture_output=True,
                       text=True, env=env)
    out = json.loads(p.stdout) if p.stdout.strip() else {}
    return p.returncode, (out.get("hookSpecificOutput") or {}).get("permissionDecision"), p.stdout


_FAR = 9_999_999_999.0   # far-future expiry (live)
_cover = [{"verb": "github:merge", "tier": "owner", "user_id": "owner-1",
           "scope_pattern": "*", "single_use": False, "expires_at": _FAR}]

rc, dec, out = run_grant("gh pr merge 2729 --squash", _cover, user="owner-1")
check("covering standing grant + matching owner -> ALLOW (owner-approval path works)",
      dec is None and rc == 0, out)
rc, dec, out = run_grant("gh pr merge 2729 --squash", _cover, user="mallory")
check("covering grant but WRONG user -> still deny (identity preserved at the gate)",
      dec == "deny", out)
rc, dec, out = run_grant("gh pr merge 2729 --squash", _cover, user="")
check("covering grant but NO principal identity -> deny (fail-closed)", dec == "deny", out)
_expired = [dict(_cover[0], expires_at=1.0)]
rc, dec, out = run_grant("gh pr merge 2729 --squash", _expired, user="owner-1")
check("expired standing grant -> deny (fail-closed)", dec == "deny", out)
rc, dec, out = run_grant("gh pr merge 2729 --squash", [], user="owner-1")
check("empty grants file -> deny (no covering grant)", dec == "deny", out)
_wrongscope = [dict(_cover[0], verb="github:comment")]
rc, dec, out = run_grant("gh pr merge 2729 --squash", _wrongscope, user="owner-1")
check("standing grant for a DIFFERENT verb -> deny (no cover)", dec == "deny", out)
# a covering grant can NEVER satisfy a PROHIBITED-overlay action
_prohib = [{"verb": "financial:move", "tier": "owner", "user_id": "owner-1",
            "scope_pattern": "*", "single_use": False, "expires_at": _FAR}]
rc, dec, out = run_grant("wire transfer funds 500 usd to acct", _prohib, user="owner-1")
check("standing grant does NOT satisfy a prohibited-overlay action (still deny)",
      dec == "deny", out)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    raise SystemExit(1)
print("ALL PASS")
