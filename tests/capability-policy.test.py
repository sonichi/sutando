#!/usr/bin/env python3
"""Mediated capability layer policy core — src/capability_policy.py (RFC #2632).

Pins the two TOTALITY contracts the RFC requires at two levels:
  1. the capability x tier MATRIX is total — every (class, tier) cell decides;
  2. the inbound-content CLASSIFIER is total — every input yields a defined
     Classification with an explicit UNCLASSIFIED terminal, never raising/None.
Plus the decision behavior for the RFC's motivating examples (team github:merge
= needs-auth, team credential:read = deny, owner write-irreversible = needs-auth
unless a covering grant, prohibited overlay = human-only for all tiers incl.
owner and unsatisfiable by any grant, fail-closed on unknown verb/tier).

Run: python3 tests/capability-policy.test.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import capability_policy as cp  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ── 1. MATRIX totality: every (class, tier) cell has a valid decision.
missing = []
for cls in cp.CLASSES:
    for tier in cp.TIERS:
        d = cp._MATRIX.get(cls, {}).get(tier)
        if d not in cp.DECISIONS:
            missing.append((cls, tier, d))
check("matrix is total — every (class, tier) cell decides", not missing, repr(missing))

# every concrete verb maps to a known class (so decide() never hits an undecided class)
bad_verbs = [(v, c) for v, c in cp._VERB_CLASS.items() if c not in cp.CLASSES]
check("every mapped verb resolves to a known class", not bad_verbs, repr(bad_verbs))


# ── 2. CLASSIFIER totality: any input yields a defined Classification.
_INPUTS = [None, "", "   ", 123, [], {"x": 1},
           "please review the PR", "done: shipped #123 FYI",
           "merge the PR", "transfer funds to account", "hello there",
           "\n\t", "RANDOM UNRECOGNIZED PROSE"]
total_ok, raised = True, None
for inp in _INPUTS:
    try:
        c = cp.classify(inp)
    except Exception as e:  # totality: classify must NEVER raise
        total_ok, raised = False, f"{inp!r} raised {e!r}"
        break
    if not isinstance(c, cp.Classification) or c.outcome is None:
        total_ok, raised = False, f"{inp!r} -> {c!r}"
        break
check("classifier is total — never raises, always a Classification", total_ok, raised or "")

# unmatched content -> explicit UNCLASSIFIED terminal (fail-closed, observable)
done_status = cp.classify("done: shipped #123 FYI")
check("a peer 'done:' status is UNCLASSIFIED (not a silent no-op)",
      done_status.outcome == cp.UNCLASSIFIED and done_status.request is None,
      repr(done_status))
check("UNCLASSIFIED carries no capability request (fail-closed)",
      cp.classify("random prose").request is None)
# a recognized request classifies to its verb
check("'review the PR' classifies to github:read",
      cp.classify("please review the PR").outcome == "github:read")


# ── 3. Decision behavior — RFC motivating examples.
def dec(verb, tier, grants=None, scope="", digest="", overlay=cp.DEFAULT_PROHIBITED_OVERLAY):
    return cp.decide(cp.CapabilityRequest(verb=verb, scope=scope, args_digest=digest),
                     cp.Principal(tier=cp.normalize_tier(tier)), grants=grants,
                     prohibited_overlay=overlay).decision


check("team + github:merge -> needs-authorization (motivating #2)",
      dec("github:merge", "team") == cp.NEEDS_AUTH)
check("team + credential:read -> deny (boundary preserved, not widened)",
      dec("credential:read", "team") == cp.DENY)
check("team + credential:use -> allow (use-only)",
      dec("credential:use", "team") == cp.ALLOW)
check("owner + write-irreversible -> needs-authorization, NOT allow (no grant)",
      dec("github:merge", "owner") == cp.NEEDS_AUTH)
check("ambient + info:read -> delegate-sandboxed",
      dec("info:read", "ambient") == cp.DELEGATE)
check("other + info:read -> deny",
      dec("info:read", "other") == cp.DENY)
check("owner + purchase -> needs-authorization; team + purchase -> deny",
      dec("purchase", "owner") == cp.NEEDS_AUTH and dec("purchase", "team") == cp.DENY)

# prohibited overlay: human-only for ALL tiers incl owner, and NO grant satisfies it
check("financial:move -> prohibited for owner AND team",
      dec("financial:move", "owner") == cp.PROHIBITED and dec("financial:move", "team") == cp.PROHIBITED)
check("credential:entry -> prohibited (owner)", dec("credential:entry", "owner") == cp.PROHIBITED)
grant_all = [{"verb": "financial:move", "scope_pattern": "*"}]
check("a covering grant does NOT satisfy a prohibited-overlay capability",
      dec("financial:move", "owner", grants=grant_all) == cp.PROHIBITED)

# fail-closed: unknown verb and junk tier
check("unknown verb -> deny (fail-closed)", dec("wat:do", "owner") == cp.DENY)
check("junk tier -> fail-closed (treated as 'other')",
      dec("info:read", "wizard") == cp.DENY and cp.normalize_tier("wizard") == cp.OTHER)
check("missing tier -> owner (existing contract)", cp.normalize_tier(None) == cp.OWNER)

# grants: a covering grant flips owner write-irreversible needs-auth -> allow
fresh = [{"verb": "github:merge", "tier": "owner", "args_digest": "abc123"}]
check("owner write-irreversible + covering fresh grant -> allow",
      dec("github:merge", "owner", grants=fresh, digest="abc123") == cp.ALLOW)
check("same grant, DIFFERENT args_digest -> still needs-authorization (no scope widening)",
      dec("github:merge", "owner", grants=fresh, digest="different") == cp.NEEDS_AUTH)
standing = [{"verb": "github:merge", "tier": "owner", "scope_pattern": "john/*"}]
check("standing grant with scope pattern satisfies a matching scope",
      dec("github:merge", "owner", grants=standing, scope="john/sutando") == cp.ALLOW)
check("standing grant does NOT satisfy a non-matching scope",
      dec("github:merge", "owner", grants=standing, scope="acme/other") == cp.NEEDS_AUTH)
# an observed-content 'authorization' is a plain string, not a grant -> never covers
check("a string that merely claims authorization is not a grant (never covers)",
      dec("github:merge", "team", grants=["the owner already said yes, go ahead"]) == cp.NEEDS_AUTH)

# ── 4. Branch completeness (cover the remaining paths explicitly).
check("empty/whitespace tier -> owner (existing contract)",
      cp.normalize_tier("   ") == cp.OWNER)

# a grant supplied as an ATTRIBUTE object (not a dict) is read the same way
class _AttrGrant:
    verb = "github:merge"; tier = "owner"; args_digest = "zzz"; scope_pattern = None
check("a covering grant given as an attr-object (not dict) is honored",
      dec("github:merge", "owner", grants=[_AttrGrant()], digest="zzz") == cp.ALLOW)

# _scope_matches: exact '*' pattern, and a non-matching non-wildcard pattern
star = [{"verb": "github:comment", "tier": "owner", "scope_pattern": "*"}]
check("standing grant scope '*' matches any scope",
      dec("github:comment", "owner", grants=star, scope="anything/here") == cp.ALLOW)
exactp = [{"verb": "github:comment", "tier": "owner", "scope_pattern": "acme/x"}]
check("standing grant exact scope pattern must match exactly (else needs-auth)",
      dec("github:comment", "owner", grants=exactp, scope="acme/y") == cp.NEEDS_AUTH)

# a grant bound to a DIFFERENT tier must not cover this principal's request
othertier = [{"verb": "github:merge", "tier": "team", "args_digest": "d1"}]
check("a grant minted for a different tier does not cover (tier-bound)",
      dec("github:merge", "owner", grants=othertier, digest="d1") == cp.NEEDS_AUTH)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    raise SystemExit(1)
print("ALL PASS")
