#!/usr/bin/env python3
"""Mediated capability layer — the mediator (src/capability_mediator.py, RFC #2632).

LIVE-path evidence (not just unit assertions), per owner ask "add enough live
test data to support the review". Each case drives the REAL mediator and prints
the REAL artifact it produced — audit JSONL rows and the pending-questions file —
so the PR carries captured output a reviewer can read. The escalation case
read-backs through the ACTUAL check-pending-questions.get_waiting_questions()
counting logic (above/below the `# Resolved` divider), which is the write-then-
assert delivery contract exercised end to end.

Hermetic: a temp workspace + temp pending-questions + temp audit log; a frozen
clock; no live-workspace resolution, no network.

Run: python3 tests/capability-mediator.test.py
"""
import importlib.util
import json
import os
import pathlib
import tempfile

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
import sys
sys.path.insert(0, str(_SRC))
import capability_policy as cp        # noqa: E402
import capability_mediator as cm      # noqa: E402

failures = []
LIVE = []  # captured live artifacts to print for the PR


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# frozen clock so expiry is deterministic
_CLOCK = {"t": 1_000_000.0}
def now():  # noqa: E306
    return _CLOCK["t"]


def _load_pending_reader(pq_path):
    """The REAL check-pending-questions reader, pointed at our temp file — so the
    read-back exercises production counting logic, not a test reimplementation."""
    spec = importlib.util.spec_from_file_location(
        "cpq_live", str(_SRC / "check-pending-questions.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.PQ_FILE = pathlib.Path(pq_path)          # override the live-resolved path
    return lambda: [str(q) for q in m.get_waiting_questions()]


tmp = pathlib.Path(tempfile.mkdtemp(prefix="cap-mediator-"))
AUDIT = str(tmp / "capability-audit.jsonl")
PQ = str(tmp / "pending-questions.md")
# seed a realistic pending-questions with a Resolved divider + archived entries
with open(PQ, "w", encoding="utf-8") as fh:
    fh.write("- [ ] an existing open question\n\n# Resolved\n- [x] an old answered one\n")

contexts = cm.ContextRegistry(now=now)
grants = cm.GrantStore(now=now)
audit = cm.AuditLog(AUDIT, now=now)
reader = _load_pending_reader(PQ)
med = cm.Mediator(contexts, grants, audit, pq_path=PQ, pq_reader=reader)


def envelope(tier):
    return {"access_tier": tier, "source": "ag2space", "user_id": "@rui:ag2.space"}


# ── 1. trust root: a principal is derived from the handle, never submitted.
h_owner = contexts.mint(envelope("owner"))
h_team = contexts.mint(envelope("team"))
p_owner = contexts.derive_principal(h_owner)
check("context handle derives the owner principal",
      contexts.derive_principal(h_owner).tier == cp.OWNER)
check("an unknown handle derives no principal (fail-closed)",
      contexts.derive_principal("cap-ctx-bogus") is None)

# ── 2. team + github:merge (write-irreversible) with no grant -> DENY? No:
#      team write-irreversible is needs-auth -> escalates. A team credential:read
#      is the hard DENY (boundary preserved).
r = med.mediate("credential:read", {"key": "OPENAI"}, h_team)
check("team credential:read -> deny (boundary preserved)", r.decision == cp.DENY)
LIVE.append(("team credential:read DENIED", r.audit))

# ── 3. LIVE escalation round-trip (write-then-assert through the REAL reader).
r = med.mediate("github:merge", {"repo": "sonichi/sutando", "pr": 1}, h_owner,
                scope="sonichi/sutando")
check("owner write-irreversible, no grant -> needs-authorization + escalated",
      r.decision == cp.NEEDS_AUTH and r.outcome == cm.ESCALATED)
check("escalation delivered==True (read back ABOVE the # Resolved divider via the real reader)",
      "delivered=True" in r.detail, r.detail)
with open(PQ, encoding="utf-8") as fh:
    pq_after = fh.read()
check("escalation entry landed above the divider (not in the archive)",
      "Authorize github:merge" in pq_after.split("# Resolved", 1)[0])
LIVE.append(("owner github:merge ESCALATED", r.audit))
LIVE.append(("pending-questions after escalation", pq_after))

# ── 4. covering fresh grant -> ALLOW, execute, VERIFIED outcome, grant consumed.
executed = {"n": 0}
def _exec(req):  # a real executor stand-in that "creates" something
    executed["n"] += 1
    return {"created_id": "cmt_123"}
def _verify(req, raw):  # independent postcondition: the id came back
    return isinstance(raw, dict) and bool(raw.get("created_id"))

g = grants.mint_fresh("github:merge", p_owner, {"repo": "sonichi/sutando", "pr": 2})
r = med.mediate("github:merge", {"repo": "sonichi/sutando", "pr": 2}, h_owner,
                executor=_exec, verifier=_verify, scope="sonichi/sutando")
check("covering fresh grant -> allow + executed + verified succeeded",
      r.decision == cp.ALLOW and r.outcome == cm.SUCCEEDED and executed["n"] == 1)
LIVE.append(("owner github:merge w/ grant SUCCEEDED", r.audit))

# grant is single-use: the SAME request again finds no grant -> escalates.
r2 = med.mediate("github:merge", {"repo": "sonichi/sutando", "pr": 2}, h_owner,
                 executor=_exec, verifier=_verify, scope="sonichi/sutando")
check("single-use grant consumed — second identical request re-escalates",
      r2.outcome == cm.ESCALATED and executed["n"] == 1)

# ── 5. verified-outcome: a truthy executor return is NOT success without the
#      verifier confirming the postcondition (motivating failure #3).
g2 = grants.mint_fresh("config:write", p_owner, {"rule": "x"})
def _swallow(req):   # returns {ok:true} but the write silently didn't happen
    return {"ok": True}
def _verify_false(req, raw):   # postcondition read-back finds nothing
    return False
r = med.mediate("config:write", {"rule": "x"}, h_owner,
                executor=_swallow, verifier=_verify_false)
check("truthy return + failing postcondition verifier -> FAILED, not success",
      r.outcome == cm.FAILED)
g3 = grants.mint_fresh("config:write", p_owner, {"rule": "y"})
r = med.mediate("config:write", {"rule": "y"}, h_owner, executor=_swallow)  # no verifier
check("no verifier -> UNKNOWN outcome, never success",
      r.outcome == cm.UNKNOWN)

# ── 6. prohibited overlay: human-only, no grant satisfies, even owner.
grants.mint_fresh("financial:move", p_owner, {"amt": 1})
r = med.mediate("financial:move", {"amt": 1}, h_owner, executor=_exec)
check("financial:move -> prohibited (human-only), executor NEVER ran",
      r.decision == cp.PROHIBITED and executed["n"] == 1)  # still 1 from case 4
LIVE.append(("owner financial:move PROHIBITED", r.audit))

# ── 7. delegate: ambient info-read -> delegate-sandboxed (no execution).
h_amb = contexts.mint(envelope("ambient"))
r = med.mediate("info:read", {"q": "status"}, h_amb, executor=_exec)
check("ambient info:read -> delegate-sandboxed (executor not run inline)",
      r.decision == cp.DELEGATE and executed["n"] == 1)

# ── 8. invalid handle -> deny, audited.
r = med.mediate("info:read", {}, "cap-ctx-expired-or-forged")
check("invalid context handle -> deny (no principal derivable)", r.decision == cp.DENY)

# ── 9. expiry: a handle past its TTL derives no principal.
h_short = contexts.mint(envelope("owner"), ttl_seconds=10)
_CLOCK["t"] += 11
check("expired context handle derives no principal",
      contexts.derive_principal(h_short) is None)
_CLOCK["t"] -= 11

# ── audit log is append-only JSONL with verified outcomes.
with open(AUDIT, encoding="utf-8") as fh:
    rows = [json.loads(ln) for ln in fh if ln.strip()]
check("audit log wrote a record per decision (append-only JSONL)", len(rows) >= 8)
check("audit records carry the VERIFIED outcome, not a self-reported ok",
      any(x["outcome"] == cm.SUCCEEDED for x in rows) and
      any(x["outcome"] == cm.FAILED for x in rows) and
      any(x["outcome"] == cm.ESCALATED for x in rows))
check("a prohibited action is audited with no execution",
      any(x["decision"] == cp.PROHIBITED and x["outcome"] == cm.PROHIBITED_OUT for x in rows))

# ── LIVE EVIDENCE dump (captured real artifacts for the PR body) ─────────────
# ── 10. branch completeness (close handle, standing grant, revoke, no-divider
#        escalation, no-executor allow, executor/verifier raising).
h_close = contexts.mint(envelope("owner"))
contexts.close(h_close)
check("a closed context handle derives no principal", contexts.derive_principal(h_close) is None)

st = grants.mint_standing("github:merge", p_owner, "sonichi/*")
rs = med.mediate("github:merge", {"repo": "sonichi/sutando", "pr": 9}, h_owner,
                 executor=_exec, verifier=_verify, scope="sonichi/sutando")
check("standing grant (scope pattern) satisfies a matching request -> allow",
      rs.decision == cp.ALLOW and rs.outcome == cm.SUCCEEDED)
# standing grant is NOT single-use: a second matching request still allowed
rs2 = med.mediate("github:merge", {"repo": "sonichi/sutando", "pr": 10}, h_owner,
                  executor=_exec, verifier=_verify, scope="sonichi/sutando")
check("standing grant is reusable (not consumed)", rs2.outcome == cm.SUCCEEDED)
grants.revoke(st.grant_id)
rs3 = med.mediate("github:merge", {"repo": "sonichi/sutando", "pr": 11}, h_owner, scope="sonichi/sutando")
check("after revoke, the standing grant no longer covers -> escalate",
      rs3.outcome == cm.ESCALATED)

# no-executor allow (a pure read the caller handles itself)
rna = med.mediate("info:read", {"q": "x"}, h_owner)
check("owner info:read, no executor -> allow/succeeded (allow-only)",
      rna.decision == cp.ALLOW and rna.outcome == cm.SUCCEEDED)

# executor raises -> FAILED ; verifier raises -> UNKNOWN
grants.mint_fresh("config:write", p_owner, {"rule": "boom"})
def _boom(req):
    raise RuntimeError("mutation blew up")
rboom = med.mediate("config:write", {"rule": "boom"}, h_owner, executor=_boom, verifier=_verify)
check("executor raising -> FAILED (never success)", rboom.outcome == cm.FAILED)
grants.mint_fresh("config:write", p_owner, {"rule": "vraise"})
def _vraise(req, raw):
    raise RuntimeError("verifier blew up")
rvr = med.mediate("config:write", {"rule": "vraise"}, h_owner, executor=lambda r: {"ok": 1}, verifier=_vraise)
check("verifier raising -> UNKNOWN (never success)", rvr.outcome == cm.UNKNOWN)

# escalation with NO divider in the file, reader=None (file-based read-back)
nd = str(tmp / "no-divider.md")
with open(nd, "w", encoding="utf-8") as fh:
    fh.write("just some notes, no divider\n")
check("escalation to a file with no divider still counts (file read-back)",
      cm.escalate_pending(nd, "Authorize something?") is True)

# ── 10b. GrantStore.consume_covering fail-closed identity branches (direct).
_gs = cm.GrantStore(now=now)
_alice = cp.Principal(tier="owner", user_id="alice", source="s")
_gs.mint_fresh("github:merge", _alice, {"pr": 1})
_req1 = cp.CapabilityRequest(verb="github:merge", args_digest=cm.digest_args({"pr": 1}))
check("consume: principal with no user_id -> None (fail-closed)",
      _gs.consume_covering(_req1, cp.Principal(tier="owner", user_id="")) is None)
check("consume: different tier -> None",
      _gs.consume_covering(_req1, cp.Principal(tier="team", user_id="alice")) is None)
check("consume: different user_id -> None (no bearer replay)",
      _gs.consume_covering(_req1, cp.Principal(tier="owner", user_id="mallory")) is None)
check("consume: matching identity -> grant returned + consumed",
      _gs.consume_covering(_req1, _alice) is not None and
      _gs.consume_covering(_req1, _alice) is None)

# ── 11. IDENTITY BINDING (qingyun-wu + bassilkhilo-ag2 P1): a grant bound to one
#        principal must NOT execute under a different principal, even same tier.
executed["n_before_mallory"] = executed["n"]
grants.mint_fresh("github:merge", p_owner, {"repo": "sonichi/sutando", "pr": 99})  # for @rui
h_mallory = contexts.mint({"access_tier": "owner", "source": "discord", "user_id": "mallory"})
rm = med.mediate("github:merge", {"repo": "sonichi/sutando", "pr": 99}, h_mallory,
                 executor=_exec, verifier=_verify, scope="sonichi/sutando")
check("mallory CANNOT execute @rui's grant (same tier, diff user_id) -> escalated, executor NOT run",
      rm.outcome == cm.ESCALATED and executed["n"] == executed["n_before_mallory"])
LIVE.append(("mallory replay of @rui's grant BLOCKED", rm.audit))

print("\n================ LIVE TEST DATA (captured real artifacts) ================")
for label, art in LIVE:
    print(f"\n--- {label} ---")
    if isinstance(art, dict):
        print(json.dumps(art, indent=2, sort_keys=True))
    else:
        print(art.rstrip())
print("\n--- full audit log (capability-audit.jsonl) ---")
for x in rows:
    print(json.dumps({k: x[k] for k in ("tier", "verb", "decision", "outcome", "rule")}, sort_keys=True))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    raise SystemExit(1)
print("ALL PASS")
