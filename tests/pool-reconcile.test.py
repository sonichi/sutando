#!/usr/bin/env python3
"""pool_reconcile: the rules that decide what observed liveness implies.

Pinned here because each one was chosen against a failure mode rather than for
symmetry — see sonichi/sutando#4417:

  * no beat at all is UNKNOWN, never death, or the release that introduces
    beats abandons every worker running the release before it;
  * `retired` is owner intent and no observation may overwrite it;
  * a binding whose worker is not live is REPORTED, never rewritten;
  * live is not ready: a worker wedged on accepted work still beats.
"""
import importlib.util as u
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "skills" / "worker-pool" / "scripts" / "pool_reconcile.py"
spec = u.spec_from_file_location("pool_reconcile", SRC)
pr = u.module_from_spec(spec)
spec.loader.exec_module(pr)

NOW = 1_000_000.0
fresh = {"last_beat_at": NOW - 5}
stale = {"last_beat_at": NOW - 300}          # past live_s, inside abandon_s
ancient = {"last_beat_at": NOW - 100_000}    # past abandon_s

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok ' if ok else 'BAD'} {name}: {got!r}")
    if not ok:
        fails.append(f"{name}: got {got!r}, want {want!r}")


def codes(res):
    return sorted(a["code"] for a in res["anomalies"])


# --- classify_beat ---------------------------------------------------------
check("fresh beat is live", pr.classify_beat(fresh, NOW), pr.LIVE)
check("stale beat is recovering", pr.classify_beat(stale, NOW), pr.RECOVERING)
check("ancient beat is abandoned", pr.classify_beat(ancient, NOW), pr.ABANDONED)
check("absent beat is UNKNOWN, not dead", pr.classify_beat(None, NOW), pr.UNKNOWN)
check("beat with no timestamp is UNKNOWN", pr.classify_beat({}, NOW), pr.UNKNOWN)
check("future beat is UNKNOWN (clock fault, not freshness)",
      pr.classify_beat({"last_beat_at": NOW + 500}, NOW), pr.UNKNOWN)

# --- transitions -----------------------------------------------------------
r = pr.reconcile({"w1": {"state": "live"}}, {"w1": fresh}, NOW)
check("live + fresh -> no transition", r["transitions"], [])
check("live + fresh -> no anomaly", r["anomalies"], [])

r = pr.reconcile({"w1": {"state": "live"}}, {"w1": stale}, NOW)
check("live + stale -> live->recovering", r["transitions"],
      [{"worker_id": "w1", "from": "live", "to": "recovering"}])

r = pr.reconcile({"w1": {"state": "recovering"}}, {"w1": ancient}, NOW)
check("recovering + ancient -> abandoned", r["transitions"],
      [{"worker_id": "w1", "from": "recovering", "to": "abandoned"}])

# The migration-safety rule: today NO worker writes a beat, so a reconciler that
# treated absence as death would abandon the whole roster on first run.
r = pr.reconcile({"w1": {"state": "live"}}, {}, NOW)
check("no beat -> NO transition", r["transitions"], [])
check("no beat -> reported as MISSING", codes(r), [pr.MISSING])

# --- retired is owner intent ----------------------------------------------
r = pr.reconcile({"w1": {"state": "retired"}}, {}, NOW)
check("retired + no beat -> untouched", (r["transitions"], r["anomalies"]), ([], []))
r = pr.reconcile({"w1": {"state": "retired"}}, {"w1": fresh}, NOW)
check("retired + LIVE beat -> still untouched", r["transitions"], [])

# --- bindings are reported, never rewritten -------------------------------
r = pr.reconcile({"w1": {"state": "live"}}, {"w1": ancient},
                 NOW, bindings={"!room:x": "w1"})
check("binding to dead worker -> BINDING_UNAVAILABLE",
      pr.BINDING_UNAVAILABLE in codes(r), True)
check("reconcile never returns a binding mutation",
      "bindings" in r or "binding_updates" in r, False)
r = pr.reconcile({"w1": {"state": "live"}}, {"w1": fresh},
                 NOW, bindings={"!room:x": "w1"})
check("control: binding to LIVE worker -> no anomaly", r["anomalies"], [])
r = pr.reconcile({}, {}, NOW, bindings={"!room:x": "core"})
check("control: a binding to core is not an anomaly", r["anomalies"], [])

# --- unexpected worker is a notice ----------------------------------------
r = pr.reconcile({}, {"adhoc": fresh}, NOW)
check("live worker absent from desired -> UNEXPECTED", codes(r), [pr.UNEXPECTED])
check("...and it is a notice, not a failure",
      r["anomalies"][0]["severity"], "notice")

# --- readiness -------------------------------------------------------------
check("live + nothing accepted -> ready", pr.derive_readiness(pr.LIVE, [], hard_timeout_s=600),
      (True, None))
check("live + fresh accepted work -> ready",
      pr.derive_readiness(pr.LIVE, [30], hard_timeout_s=600), (True, None))
check("live + work accepted past the timeout -> NOT ready",
      pr.derive_readiness(pr.LIVE, [4000], hard_timeout_s=600), (False, pr.WEDGED))
check("not live -> not ready regardless",
      pr.derive_readiness(pr.RECOVERING, [], hard_timeout_s=600), (False, None))

# --- the measured production case -----------------------------------------
# Roster declared two workers live; zero processes existed; both rooms pinned.
r = pr.reconcile({"02e4302f": {"state": "live"}, "212e8040": {"state": "live"}},
                 {}, NOW,
                 bindings={"!YjpBVQgJCLoxmDKXoH:ag2.space": "02e4302f",
                           "!bKQkxfOrHZwejIyDLI:ag2.space": "212e8040"})
check("measured case: no silent transition", r["transitions"], [])
check("measured case: 2 MISSING + 2 BINDING_UNAVAILABLE",
      codes(r), sorted([pr.MISSING] * 2 + [pr.BINDING_UNAVAILABLE] * 2))

# --- discriminator ---------------------------------------------------------
# Every check above passes trivially if reconcile() returns nothing at all.
print("\n  -- control: the suite must be able to FAIL --")
empty = {"transitions": [], "anomalies": []}
would_pass = (pr.reconcile({"w1": {"state": "live"}}, {"w1": stale}, NOW) == empty)
print(f"  {'BAD' if would_pass else 'ok '} a do-nothing reconcile() does NOT satisfy these checks")
if would_pass:
    fails.append("suite passes against a no-op implementation")

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILURE(S)'}"
      f" — pool_reconcile ({26} checks)")
for f in fails:
    print("   ", f)
sys.exit(1 if fails else 0)
