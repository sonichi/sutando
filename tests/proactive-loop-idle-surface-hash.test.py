#!/usr/bin/env python3
"""Tests for idle-surface-hash.py — the one instrument in this set with no guard.

It runs on EVERY proactive pass (the substantive/noop record and the held-set
hash), and its two halves fail differently:

  * the HASH must be stable under re-wording and order, and must MOVE when the
    blocker actually changes. A guard that re-hashes on a re-description
    surfaces every pass; one that never moves goes permanently quiet.
  * `record_outcome` maintains counters that CANNOT use last-writer-wins. Its
    own docstring names the failure: "two processes that both read total=5
    would both write 6 and one pass would vanish." That is a claim about a
    lock, so the test drives the PRODUCTION writer from real concurrent
    processes rather than asserting the lock exists.

Run: python3 tests/proactive-loop-idle-surface-hash.test.py
"""
import importlib.util
import json
import pathlib
import subprocess
import sys as _sys
import tempfile
import time

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "proactive-loop" / "scripts"
spec = importlib.util.spec_from_file_location("ish", SCRIPTS / "idle-surface-hash.py")
ish = importlib.util.module_from_spec(spec); spec.loader.exec_module(ish)

fails, ran = [], 0
def check(name, cond, detail=""):
    global ran; ran += 1
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail and not cond else ""))
    if not cond: fails.append(name)

def raises(fn, *a):
    try:
        fn(*a); return False
    except ValueError:
        return True

print("idle-surface-hash")

# --- canonical form: what must NOT move the key ------------------------------
A = [["3166", "owner"], ["3274", "owner"]]
check("order does not move the key", ish.held_hash(A) == ish.held_hash(list(reversed(A))))
check("duplicates collapse", ish.held_hash(A) == ish.held_hash(A + A))
check("dict and pair forms agree",
      ish.held_hash(A) == ish.held_hash([{"id": "3166", "gated_on": "owner"},
                                         {"id": "3274", "gated_on": "owner"}]))
check("case and whitespace do not move the key",
      ish.held_hash(A) == ish.held_hash([["  3166 ", "OWNER"], ["3274", "  owner  "]]))
check("RE-DESCRIBING one blocker does not move the key",
      ish.held_hash(A) == ish.held_hash([["3166", "owner: still waiting on a decision"],
                                         ["3274", "owner  (asked twice)"]]),
      "gated_on must reduce to its leading token")

# --- and what MUST move it ---------------------------------------------------
check("a CHANGED blocker moves the key",
      ish.held_hash(A) != ish.held_hash([["3166", "ci"], ["3274", "owner"]]))
check("adding an item moves the key",
      ish.held_hash(A) != ish.held_hash(A + [["3999", "owner"]]))
check("removing an item moves the key",
      ish.held_hash(A) != ish.held_hash(A[:1]))
check("a DIFFERENT id moves the key — id is NOT reduced",
      ish.held_hash([["3166", "owner"]]) != ish.held_hash([["3166-b", "owner"]]))

# --- refusals: a silently-empty key is the dangerous failure -----------------
check("an entry with no id RAISES", raises(ish.canonical_lines, [["", "owner"]]))
check("an entry with no gated_on RAISES", raises(ish.canonical_lines, [["3166", ""]]))
check("a gated_on of only punctuation RAISES", raises(ish.canonical_lines, [["3166", ":"]]))
check("CONTROL: a valid pair does not raise", not raises(ish.canonical_lines, A))

check("hash is 16 hex chars", len(ish.held_hash(A)) == 16 and
      all(c in "0123456789abcdef" for c in ish.held_hash(A)))
check("canonical_lines is sorted and deduped",
      ish.canonical_lines([["b", "ci"], ["a", "owner"], ["b", "ci"]]) == ["a:owner", "b:ci"])

# --- record_outcome: streak + totals -----------------------------------------
def fresh(**kw):
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"streak": 0, "noop_total": 0, "substantive_total": 0, **kw}, f); f.close()
    return pathlib.Path(f.name)

p = fresh()
d = ish.record_outcome(p, "noop")
check("noop increments streak and noop_total", d["streak"] == 1 and d["noop_total"] == 1)
d = ish.record_outcome(p, "noop")
check("a second noop keeps counting", d["streak"] == 2 and d["noop_total"] == 2)
d = ish.record_outcome(p, "substantive")
check("substantive RESETS streak and increments its own total",
      d["streak"] == 0 and d["substantive_total"] == 1 and d["noop_total"] == 2)
check("an unrelated key survives the write",
      ish.record_outcome(fresh(last_surfaced_hash="keepme"), "noop")
        .get("last_surfaced_hash") == "keepme")

# --- the documented concurrency failure, driven for real ---------------------

# subprocess, not multiprocessing: macOS spawns, so a module-level Process()
# never starts and the counter assertions pass against an untouched file.
SCRIPT = str(SCRIPTS / "idle-surface-hash.py")
N = 12

def run_concurrent(cmds):
    ps = [subprocess.Popen(c, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for c in cmds]
    return [x.wait() for x in ps]

p2 = fresh()
rcs = run_concurrent([[_sys.executable, SCRIPT, "--state", str(p2), "--pass-outcome", "noop"]
                      for _ in range(N)])
check(f"all {N} concurrent CLI invocations exited 0 (the run really happened)",
      rcs == [0] * N, f"rcs={rcs}")
final = json.loads(p2.read_text())
check(f"{N} CONCURRENT processes lose no count (the lock is load-bearing)",
      final["noop_total"] == N, f"noop_total={final['noop_total']} expected {N}")
check("streak matches the same count under concurrency",
      final["streak"] == N, f"streak={final['streak']} expected {N}")

# CONTROL: this harness must be ABLE to observe a lost count, or the assertion
# above is satisfied by a race that never happens. Same shape, no lock.

# A shared wall-clock barrier FORCES the overlap: interpreter startup on a
# loaded runner can otherwise exceed the window and serialise every process.
UNLOCKED = r"""
import json, pathlib, sys, time
q, start_at = pathlib.Path(sys.argv[1]), float(sys.argv[2])
while time.time() < start_at:
    time.sleep(0.002)
doc = json.loads(q.read_text())
doc["noop_total"] = int(doc.get("noop_total") or 0) + 1
time.sleep(0.20)
q.write_text(json.dumps(doc))
"""
p3 = fresh()
start_at = time.time() + 3.0
rcs = run_concurrent([[_sys.executable, "-c", UNLOCKED, str(p3), repr(start_at)]
                      for _ in range(N)])
lost = json.loads(p3.read_text())["noop_total"]
check("CONTROL: the unlocked variant also ran (rc=0 everywhere)", rcs == [0] * N, f"rcs={rcs}")
check("CONTROL: it incremented at least once — the harness is live, not inert",
      lost >= 1, f"unlocked total={lost}; 0 means no process ran and the check above is vacuous")
check("CONTROL: and it LOSES counts, so the locked assertion is discriminating",
      lost < N, f"unlocked total={lost}; equal to {N} means the barrier did not "
                f"overlap the windows and this harness cannot see the bug")

print(f"\nidle-surface-hash: {ran - len(fails)}/{ran} passed")
if fails:
    print("FAILED: " + ", ".join(fails))
    raise SystemExit(1)
print("all passed")
