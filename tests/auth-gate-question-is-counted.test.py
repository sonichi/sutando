#!/usr/bin/env python3
"""The boot-abort question the auth gate records must actually be COUNTED.

`src/auth-preflight-gate.sh` asks the owner, through scripts/ask-owner.py, when it
stops a boot on a logged-out CLI. The record of why the machine did not come up is
the worst one to lose, and a writer can look successful in every cheap way — so this
test counts through the SHIPPED reader (`src/pending_questions_reader.py`), never
by grepping what was written. The reader reaches the repo's pending-questions skill
(the one declared adapter); with no room reachable from the fixture, the record is the
workspace outbox entry, which the adapter lists once as not yet in the room.

Run:  python3 tests/auth-gate-question-is-counted.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import concurrent.futures as _cf
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "src" / "auth-preflight-gate.sh"
sys.path.insert(0, str(REPO / "src"))

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)
        if detail:
            print(f"        {detail[:400]}")


def waiting(ws: Path) -> list:
    """Count via the SHIPPED reader, not a local re-implementation."""
    spec = importlib.util.spec_from_file_location("pq_reader", REPO / "src" / "pending_questions_reader.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    import skill_roots  # noqa: PLC0415
    # The repo's skill declares the adapter; with no room capability it lists the outbox alone.
    return m.waiting(ws, skill_roots.declared(m.DECLARATION, roots=REPO / "skills"))


def extract_writer() -> str:
    """Pull the recording block out of the gate and make it runnable standalone.

    The gate exits 2 and shells out to scutil/osascript, so it cannot be run
    directly. Extracting the block exercises the REAL lines rather than a
    paraphrase — a grep would pass against any rewrite.
    """
    src = GATE.read_text()
    start = src.index('if [ -n "$_ws" ] && [ -n "$_host" ]; then')
    end = src.index("\nexit 2", start)
    return src[start:end]


print("auth-gate pending-question recording")

body = GATE.read_text()

try:
    block = extract_writer()
except ValueError as e:
    block = ""
    check("the recording block was extracted from the real gate", False, str(e))
check("the recording block was extracted from the real gate",
      "BOOT ABORTED" in block and "ask-owner.py" in block, f"got {len(block)} bytes")

# --- 1. delegation: the gate records only through ask-owner --------------------
check("the gate records only via scripts/ask-owner.py (no file write, no second DM file)",
      'python3 "$REPO/scripts/ask-owner.py"' in block
      and "pending-questions.md" not in body
      and not re.search(r'>\s*"\$_ws/results/proactive-', body),
      "the gate is back to writing the retired file or its own proactive file")

if block:
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td) / "ws"
        (ws / "results").mkdir(parents=True)
        env = dict(os.environ)
        env.pop("SUTANDO_INSTANCE_ID", None)
        env.update({"REPO": str(REPO), "_ws": str(ws), "_host": "TestHost",
                    "_remedy": "run `claude login`", "SUTANDO_HOST_LABEL": "TestHost"})
        before = waiting(ws)
        check("control: the fixture workspace starts with nothing waiting", before == [], str(before))

        r = subprocess.run(["bash", "-c", "set -e\n" + block],
                           capture_output=True, text=True, env=env, timeout=120)
        check("the extracted block runs cleanly", r.returncode == 0,
              f"rc={r.returncode} err={r.stderr[-300:]}")
        check("ask-owner said where it recorded the question", "recorded:" in r.stdout, r.stdout[-300:])

        after = waiting(ws)
        # THE assertion: the shipped reader counts it.
        check("the gate's question is COUNTED by the shipped reader", len(after) == 1,
              f"{len(before)} -> {len(after)}")
        check("...with the boot-abort title and the remedy in its body",
              bool(after) and "BOOT ABORTED" in after[0]["title"]
              and "run `claude login`" in after[0]["body"], json.dumps(after)[:300])
        check("...marked not yet in the room (no room is reachable from the fixture)",
              bool(after) and after[0]["in_room"] is False, json.dumps(after)[:300])

        held = list((ws / "state" / "pending-questions-outbox").glob("*.json"))
        check("the record is one durable outbox entry", len(held) == 1, str(held))
        dms = list((ws / "results").glob("proactive-*.txt"))
        check("exactly one DM was queued (ask-owner's; the gate adds none)", len(dms) == 1, str(dms))
        check("no scratch files are left behind",
              not list((ws / "state" / "pending-questions-outbox").glob(".*")), "temp files left")

        # --- 2. CONCURRENT gates must not lose an entry -------------------------
        def _fire(n: int):
            e = dict(env)
            e["_remedy"] = f"remedy-{n}"
            return subprocess.run(["bash", "-c", "set -e\n" + block],
                                  capture_output=True, text=True, env=e, timeout=180)

        N = 5
        with _cf.ThreadPoolExecutor(max_workers=N) as pool:
            results = list(pool.map(_fire, range(N)))
        check("every concurrent gate exits 0", all(x.returncode == 0 for x in results),
              str([x.returncode for x in results]))
        final = waiting(ws)
        present = [n for n in range(N) if any(f"remedy-{n}" in q["body"] for q in final)]
        check(f"all {N} concurrent boot-abort records are counted", len(present) == N and len(final) == 1 + N,
              f"only {present} present, {len(final)} listed")

print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("all checks passed — the boot-abort question is recorded where the reader counts it")
