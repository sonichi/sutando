#!/usr/bin/env python3
"""Behavioral test for hooks/skill-usage-telemetry.py.

Runs the hook as a REAL subprocess (the way Claude Code invokes it) and verifies
the actual emission wiring by pointing it at a stub `telemetry.py` CLI that
records its argv to a file — no mocks of the hook's own code, no network. The
hook emits by spawning `telemetry.py feature_used skill:<name>` in a DETACHED
subprocess and returning immediately (CR #2254 — the hook must not block the tool
run on a network RTT), so the recorder is the detached child and the test waits
for it. Proves:
  - a Skill PostToolUse payload → spawns `feature_used skill:<name>`
  - the hook returns 0 promptly, without waiting on the send
  - a non-Skill tool → no spawn
  - missing / blank skill name → no spawn
  - malformed stdin → exit 0, no crash (fail-open), no spawn
  - the name is trimmed, slash-stripped, and length-bounded
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "skill-usage-telemetry.py"

# The detached child is spawned by the hook and races the assertion; poll for it.
_SETTLE_S = 0.5   # how long to wait to CONFIRM no spawn happened (negative cases)
_WAIT_S = 5.0     # max wait for the detached child to record (positive cases)


def _run(payload_obj, *, stub_root: Path, stdin: Optional[str] = None,
         expect: int = 1) -> tuple[int, list]:
    """Invoke the hook subprocess with a stub telemetry CLI on its src/ path.

    The hook spawns `python3 <stub>/src/telemetry.py feature_used skill:<name>`
    detached; the stub records argv[1:] to rec.jsonl. Returns
    (hook_exit_code, recorded_calls). `expect` is the number of recorded calls to
    wait for (0 → confirm none appear within the settle window)."""
    rec = stub_root / "rec.jsonl"
    src = stub_root / "src"
    src.mkdir(parents=True, exist_ok=True)
    # Stub telemetry CLI → append the received argv to rec.jsonl (behavioral
    # capture of the real spawn the hook performs).
    (src / "telemetry.py").write_text(
        "import json, sys\n"
        f"open(r'{rec}', 'a').write(json.dumps({{'argv': sys.argv[1:]}}) + '\\n')\n"
    )
    data = stdin if stdin is not None else json.dumps(payload_obj)
    env = {**os.environ, "SUTANDO_REPO_ROOT": str(stub_root)}
    t0 = time.monotonic()
    p = subprocess.run(
        [sys.executable, str(HOOK)], input=data, text=True,
        capture_output=True, env=env, timeout=10,
    )
    hook_wall = time.monotonic() - t0

    def _read() -> list:
        if not rec.exists():
            return []
        return [json.loads(x) for x in rec.read_text().splitlines() if x.strip()]

    if expect > 0:
        deadline = time.monotonic() + _WAIT_S
        while time.monotonic() < deadline and len(_read()) < expect:
            time.sleep(0.02)
    else:
        # No spawn is expected — give any (erroneous) child time to appear.
        time.sleep(_SETTLE_S)
    return p.returncode, _read(), hook_wall  # type: ignore[return-value]


def _calls_of(recorded: list) -> list:
    """Reduce recorded argv to the emitted feature name(s)."""
    return [r["argv"] for r in recorded]


def main() -> int:
    passed = 0

    # 1) Skill invocation → spawns `feature_used skill:<name>` (detached).
    with tempfile.TemporaryDirectory() as td:
        rc, calls, _ = _run(
            {"tool_name": "Skill", "tool_input": {"skill": "context-reconstruct"}},
            stub_root=Path(td),
        )
        assert rc == 0, rc
        assert _calls_of(calls) == [["feature_used", "skill:context-reconstruct"]], calls
        passed += 1
        print("ok   Skill use → spawns feature_used skill:<name>")

    # 1b) The hook returns promptly — it does NOT block on the send. The stub
    # child sleeps well past the hook's own runtime; the hook must still finish
    # fast (the whole point of CR #2254). We assert the hook's wall time is a
    # small fraction of a blocking send, proving it forks-and-returns.
    with tempfile.TemporaryDirectory() as td:
        rec = Path(td) / "rec.jsonl"
        src = Path(td) / "src"
        src.mkdir(parents=True)
        # Stub that BLOCKS 2s before recording — if the hook waited on it, the
        # hook's wall time would exceed 2s.
        (src / "telemetry.py").write_text(
            "import json, sys, time\n"
            "time.sleep(2)\n"
            f"open(r'{rec}', 'a').write(json.dumps({{'argv': sys.argv[1:]}}) + '\\n')\n"
        )
        env = {**os.environ, "SUTANDO_REPO_ROOT": td}
        t0 = time.monotonic()
        p = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"tool_name": "Skill", "tool_input": {"skill": "slow"}}),
            text=True, capture_output=True, env=env, timeout=10,
        )
        hook_wall = time.monotonic() - t0
        assert p.returncode == 0, p.returncode
        assert hook_wall < 1.5, f"hook blocked on the send ({hook_wall:.2f}s ≥ 1.5s)"
        # And the detached child still lands the event after the hook returned.
        deadline = time.monotonic() + _WAIT_S
        while time.monotonic() < deadline and not rec.exists():
            time.sleep(0.02)
        assert rec.exists(), "detached child never recorded the event"
        passed += 1
        print(f"ok   hook returns promptly ({hook_wall:.2f}s), send happens detached")

    # 2) A non-Skill tool → no spawn.
    with tempfile.TemporaryDirectory() as td:
        rc, calls, _ = _run(
            {"tool_name": "Bash", "tool_input": {"command": "ls"}},
            stub_root=Path(td), expect=0,
        )
        assert rc == 0 and calls == [], calls
        passed += 1
        print("ok   non-Skill tool → zero spawns")

    # 3) Missing / blank skill name → no spawn (nothing to attribute).
    for ti in ({"tool_input": {}}, {"tool_input": {"skill": ""}}, {"tool_input": {"skill": "   "}}):
        with tempfile.TemporaryDirectory() as td:
            rc, calls, _ = _run({"tool_name": "Skill", **ti}, stub_root=Path(td), expect=0)
            assert rc == 0 and calls == [], (ti, calls)
    passed += 1
    print("ok   missing/blank skill name → zero spawns")

    # 4) Malformed stdin → exit 0, no crash (fail-open, telemetry never breaks a tool).
    for bad in ("", "   ", "not json", "{"):
        with tempfile.TemporaryDirectory() as td:
            rc, calls, _ = _run(None, stub_root=Path(td), stdin=bad, expect=0)
            assert rc == 0 and calls == [], (repr(bad), rc, calls)
    passed += 1
    print("ok   malformed/empty stdin → exit 0, no spawn (fail-open)")

    # 4b) telemetry.py missing at the resolved src/ → exit 0, no crash, no spawn.
    with tempfile.TemporaryDirectory() as td:
        # SUTANDO_REPO_ROOT points at a dir with NO src/telemetry.py.
        env = {**os.environ, "SUTANDO_REPO_ROOT": td}
        p = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"tool_name": "Skill", "tool_input": {"skill": "x"}}),
            text=True, capture_output=True, env=env, timeout=10,
        )
        assert p.returncode == 0, (p.returncode, p.stderr)
        passed += 1
        print("ok   telemetry.py absent → exit 0, no crash")

    # 5) Name is trimmed, slash-stripped, and length-bounded (property hygiene).
    with tempfile.TemporaryDirectory() as td:
        rc, calls, _ = _run(
            {"tool_name": "Skill", "tool_input": {"skill": "  /morning-briefing  "}},
            stub_root=Path(td),
        )
        assert _calls_of(calls) == [["feature_used", "skill:morning-briefing"]], calls
        passed += 1
        print("ok   name trimmed + slash-stripped")
    with tempfile.TemporaryDirectory() as td:
        long = "x" * 200
        rc, calls, _ = _run(
            {"tool_name": "Skill", "tool_input": {"skill": long}},
            stub_root=Path(td),
        )
        assert calls[0]["argv"] == ["feature_used", "skill:" + "x" * 64], calls
        passed += 1
        print("ok   name length-bounded to 64 chars")

    print(f"\nALL PASS ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
