#!/usr/bin/env python3
"""The idle-surface dedup key is canonical, not a hash of the rendered prose.

Step 6.5 posts a held-list once per changed set. While the rule lived only in
skill prose the hash was built ad hoc each pass, and an agent handed "sha1 the
held-list" hashes the sentence it was about to send — so re-wording the same
items re-fires the guard every pass and it never dedups anything.

The load-bearing property: any rendering of the same (id, blocker) set gives ONE
hash, and a real change — item added or dropped, blocker changed — moves it.
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "proactive-loop" / "scripts" / "idle-surface-hash.py"
_spec = importlib.util.spec_from_file_location("ish", SCRIPT)
ish = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ish)

failures = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


BASE = [["3166", "owner"], ["3274", "owner"], ["hooks", "owner"]]
h0 = ish.held_hash(BASE)

# ── same set, different renderings → one hash ────────────────────────────────
for label, variant in {
    "reordered": [["3274", "owner"], ["hooks", "owner"], ["3166", "owner"]],
    "dict form": [{"id": i, "gated_on": g} for i, g in BASE],
    "case and whitespace": [["3166", "  OWNER "], ["3274", "Owner"], ["  hooks", "owner"]],
    # the rule the docstring states: gated_on names WHO, so re-describing one
    # blocker must not produce a second key.
    "prose descriptions of one blocker": [["3166", "owner: restart window"],
                                          ["3274", "owner: gateway credential"],
                                          ["hooks", "owner - waiting on a/b/c"]],
    "duplicate entry": BASE + [["3166", "owner"]],
}.items():
    check(f"stable across: {label}", ish.held_hash(variant) == h0, ish.held_hash(variant))

# ── a real change moves it ───────────────────────────────────────────────────
for label, variant in {
    "item dropped": BASE[:-1],
    "item added": BASE + [["3297", "ci"]],
    "blocker changed owner->ci": [["3166", "ci"], ["3274", "owner"], ["hooks", "owner"]],
}.items():
    check(f"moves on: {label}", ish.held_hash(variant) != h0)

# ── an entry with no id is refused, not silently keyed as ":blocker" ─────────
try:
    ish.held_hash([["", "owner"]])
    check("empty id is refused", False, "no ValueError raised")
except ValueError:
    check("empty id is refused", True)

# ── a missing gate is refused too: it reduced to "", so a caller using a wrong
# field name got a key that was add-sensitive but never moved when a blocker did
for name, bad in (("empty gate", [["#1", ""]]),
                  ("whitespace gate", [["#1", "   "]]),
                  ("wrong field name", [{"id": "#1", "blocker": "owner"}])):
    try:
        ish.held_hash(bad)
        check(f"{name} is refused", False, "no ValueError raised")
    except ValueError:
        check(f"{name} is refused", True)

# the exact defect: two DIFFERENT gates must not collapse to one empty gate
try:
    k = ish.canonical_key([{"id": "#1", "blocker": "owner"},
                           {"id": "#2", "blocker": "ci"}])
    check("two different gates cannot both reduce to empty", False, f"keyed as {k!r}")
except ValueError:
    check("two different gates cannot both reduce to empty", True)

# CONTROL: the well-formed spelling of that same pair still works, so the new
# refusal is rejecting the wrong KEY NAME and not the shape
ok = ish.canonical_key([{"id": "#1", "gated_on": "owner"},
                        {"id": "#2", "gated_on": "ci"}])
check("the correct field name still keys both entries",
      ok == "#1:owner\n#2:ci", f"got {ok!r}")

# ── state round trip: post once, then quiet, and --commit is what records ────
ws = Path(tempfile.mkdtemp())
state = ws / "state" / "idle-streak.json"
state.parent.mkdir()
state.write_text(json.dumps({"streak": 2, "last_surfaced_hash": "", "updated": "x"}))


def run(items, commit=False):
    """In-process so the production main() is measured, not a subprocess the
    coverage instrumentation cannot see. One real subprocess run is below."""
    argv = ["--state", str(state), "--items", json.dumps(items)] + (["--commit"] if commit else [])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ish.main(argv)
    return buf.getvalue().strip(), rc


first, rc = run(BASE)
check("first sight of a set says post", first.startswith("post") and rc == 0, first)
again, _ = run(BASE)
check("without --commit nothing is recorded, so it still says post",
      again.startswith("post"), again)
committed, _ = run(BASE, commit=True)
check("--commit says post on the transition", committed.startswith("post"), committed)
quiet, _ = run(BASE)
check("the same set afterwards is quiet", quiet.startswith("quiet"), quiet)
check("streak survives the state write",
      json.loads(state.read_text()).get("streak") == 2, state.read_text())
moved, _ = run(BASE + [["3297", "ci"]])
check("a changed set posts again", moved.startswith("post"), moved)

# ── malformed input is an error, never a silent 'quiet' ─────────────────────
err = io.StringIO()
with contextlib.redirect_stderr(err):
    rc_obj = ish.main(["--state", str(state), "--items", "{}"])
    rc_bad = ish.main(["--state", str(state), "--items", "not json"])
check("a JSON object instead of a list is rejected", rc_obj == 2, err.getvalue())
check("unparseable input is rejected", rc_bad == 2, err.getvalue())
check("an entry with no id is rejected at the CLI too",
      ish.main(["--state", str(state), "--items", '[["", "owner"]]']) == 2)

# a torn or corrupt state file must not suppress the surface
state.write_text("{not json")
posted, rc = run(BASE)
check("a corrupt state file is treated as no prior hash, so the surface posts",
      posted.startswith("post") and rc == 0, posted)
state.write_text(json.dumps({"streak": 2, "last_surfaced_hash": "", "updated": "x"}))
run(BASE, commit=True)          # restore the recorded hash the next check needs

# the shipped entry point runs as a real process with identical argv
proc = subprocess.run([sys.executable, str(SCRIPT), "--state", str(state),
                       "--items", json.dumps(BASE)], capture_output=True, text=True)
check("the script runs as a subprocess with the same verdict",
      proc.returncode == 0 and proc.stdout.strip().startswith("quiet"),
      f"{proc.returncode} {proc.stdout!r} {proc.stderr!r}")

# ── pass-outcome counters ───────────────────────────────────────────────────
# `streak` resets each substantive pass: a gauge, not a counter.
with tempfile.TemporaryDirectory() as d:
    st = Path(d) / "idle-streak.json"

    def outcome(kind):
        p = subprocess.run([sys.executable, str(SCRIPT), "--state", str(st),
                            "--pass-outcome", kind],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        return p.stdout.strip(), p.returncode

    out, rc = outcome("noop")
    check("a no-op pass records without a held-list on stdin",
          rc == 0 and "streak=1" in out and "noop_total=1" in out, out)
    outcome("noop")
    out, _ = outcome("substantive")
    check("a substantive pass resets the streak but not the totals",
          "streak=0" in out and "noop_total=2" in out and "substantive_total=1" in out, out)

    doc = json.loads(st.read_text())
    check("both cumulative totals persist, so the denominator is recoverable",
          doc.get("noop_total") == 2 and doc.get("substantive_total") == 1, doc)

    # The reason for the lock: last-writer-wins silently under-counts, and an
    # under-count is indistinguishable from a genuinely quiet stretch.
    st2 = Path(d) / "concurrent.json"
    procs = [subprocess.Popen([sys.executable, str(SCRIPT), "--state", str(st2),
                               "--pass-outcome", "noop"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              stdin=subprocess.DEVNULL) for _ in range(20)]
    for p in procs:
        p.wait()
    got = json.loads(st2.read_text()).get("noop_total")
    check("20 concurrent recorders lose no increments (the lock earns its place)",
          got == 20, f"noop_total={got}, expected 20")

# --pass-outcome is record-only and must return BEFORE touching stdin: under
# cron stdin is an open pipe nobody writes, and a read there never returns.
with tempfile.TemporaryDirectory() as d:
    st = Path(d) / "s.json"
    r, w = os.pipe()                      # open, silent, NOT closed
    p = subprocess.Popen([sys.executable, str(SCRIPT), "--state", str(st),
                          "--pass-outcome", "noop"],
                         stdin=r, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True)
    os.close(r)
    try:
        out, _ = p.communicate(timeout=10)
        hung = False
    except subprocess.TimeoutExpired:
        p.kill()
        out, hung = "", True
    os.close(w)
    check("record-only does not block on an open, silent stdin pipe",
          not hung and p.returncode == 0 and "noop_total=1" in out,
          "BLOCKED on stdin.read()" if hung else out)

    # It ignores --items rather than half-doing both, so the mode is unambiguous.
    p2 = subprocess.run([sys.executable, str(SCRIPT), "--state", str(st),
                         "--items", json.dumps(BASE), "--pass-outcome", "substantive"],
                        capture_output=True, text=True, stdin=subprocess.DEVNULL)
    check("record-only ignores --items and prints the outcome, not a hash",
          p2.returncode == 0 and p2.stdout.strip().startswith("substantive"), p2.stdout)
    check("...and leaves last_surfaced_hash untouched",
          "last_surfaced_hash" not in json.loads(st.read_text()), st.read_text())

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
