#!/usr/bin/env python3
"""A blocking Stop must deliver the turn-gate's own text in `reason`.

The turn-gate branch of `src/check-pending-tasks.sh` put a fixed string in
`reason` and the gate's actual guidance in a TOP-LEVEL `additionalContext`.
The Stop event does not read that field, so every refusal reached the model as
a one-line verdict with no remedy: it said the turn could not end and never
said what would end it. Observed consequence -- an agent guessing at commands
until one returned zero, which is how a gate meant to prevent silence produced
a retry loop instead.

`additionalContext` is supported on Stop only under `hookSpecificOutput`; a
top-level key of that name is dropped. `reason` is the channel that reaches the
model, so the guidance rides it.

This pins the SEMANTICS (the remedy is in the field that is delivered).
`stop-hook-emits-valid-json.test.py` pins parseability and
`check-pending-tasks-workspace.test.sh` the wire shape; none of the three
implies the others.

Run: python3 tests/stop-hook-reason-carries-the-gate-text.test.py
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

# The hook's gate is session-scoped; unset so this suite drives its own ledger
# rather than whatever session happens to be running it.
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
# The watcher-coverage gate has its own suite; a temp inbox nobody watches would
# block before the ledger gate's text under test is reached.
os.environ["SUTANDO_STOP_HOOK_WATCHER_GATE"] = "0"

REPO = pathlib.Path(__file__).resolve().parent.parent
HOOK = REPO / "src" / "check-pending-tasks.sh"
RESOLVE = 'WORKSPACE="$(bash "$REPO_DIR/scripts/sutando-config.sh" workspace 2>/dev/null)"'
REPO_LINE = 'REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"'

sys.path.insert(0, str(REPO / "src"))
import turn_ledger  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(("ok   " if cond else "FAIL ") + label + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


def _decision(hook_src: str) -> dict:
    """Run `hook_src` against a fresh workspace whose gate is armed and silent."""
    src = HOOK.read_text()
    assert RESOLVE in src and REPO_LINE in src, "hook layout moved; update this test"
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp)
        (ws / "tasks").mkdir()
        (ws / "results").mkdir()
        # One recorded action arms the gate; one stop advances the boundary. The
        # NEXT stop is the silent turn under test.
        turn_ledger.record_no_send("arming the gate for this test", ws)
        turn_ledger.stop_gate(ws)
        stub = ws / "hook.sh"
        stub.write_text(
            hook_src.replace(REPO_LINE, f'REPO_DIR="{REPO}"').replace(RESOLVE, f'WORKSPACE="{ws}"')
        )
        out = subprocess.run(["/bin/bash", str(stub)], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL)
        assert out.returncode == 0, f"hook exited {out.returncode}: {out.stderr}"
        return json.loads(out.stdout or "{}")


def main() -> int:
    d = _decision(HOOK.read_text())

    # Fixture control: a decision that never blocked would pass every assertion
    # below by vacuity.
    check("the fixture makes the turn-gate fire", d.get("decision") == "block", repr(d))
    reason = d.get("reason") or ""

    # The gate's own text, from the gate itself -- not a copy of it here, which
    # would pass while the hook shipped something else entirely.
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp)
        (ws / "tasks").mkdir()
        (ws / "results").mkdir()
        turn_ledger.record_no_send("arming the gate for this test", ws)
        turn_ledger.stop_gate(ws)
        expected = turn_ledger.stop_gate(ws) or ""
    check("the gate produces a non-empty refusal to compare against", bool(expected.strip()))

    check("reason carries the gate's own text, not a fixed string",
          expected.strip() and expected.strip() in reason, f"reason={reason[:120]!r}")
    # The old constant also contained "no-send", so that substring discriminates
    # nothing. The COMMAND is what only the gate's text carries.
    check("reason names the command to run, which the fixed string never did",
          "room_ops.py" in reason, f"reason={reason[:120]!r}")
    check("no top-level additionalContext, which Stop drops",
          "additionalContext" not in d, f"keys={sorted(d)}")

    print("PASS" if not failures else f"FAILED ({len(failures)})")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
