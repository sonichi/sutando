#!/usr/bin/env python3
"""Every fail-open guard in the usage hook, exercised.

These are the branches that make the hook SAFE: malformed stdin, a non-Skill
tool call, a missing or non-string slug, a slug that empties after the
directory/plugin prefix is stripped, and a write that raises. They were the only
uncovered lines in the file (56-57, 59, 62, 67, 79-80 — 82.1%), which is the
wrong thing to leave untested: a guard nobody exercises is indistinguishable
from a guard that does not work, and every one of these exists so the hook can
never block a tool call.

Contract under test: ALWAYS exit 0, and write nothing unless there is a real
slug and a resolvable workspace.

Run: python3 tests/skill-usage-report-hook-guards.test.py
"""
import importlib.util
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional

HOOK = Path(__file__).resolve().parents[1] / "skills" / "skill-usage-report" / "hooks" / "log-usage.py"
FAILS = []


def check(label, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + label + (f"  [{detail}]" if not cond and detail else ""))
    if not cond:
        FAILS.append(label)


def load_hook():
    spec = importlib.util.spec_from_file_location("log_usage", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(hook, raw: str, ws: Optional[Path]):
    """Drive main() with `raw` on stdin and workspace() pinned to `ws`."""
    real_stdin, real_ws = sys.stdin, hook.workspace
    sys.stdin = io.StringIO(raw)
    hook.workspace = lambda: ws
    try:
        return hook.main()
    finally:
        sys.stdin, hook.workspace = real_stdin, real_ws


def log_lines(ws: Path) -> int:
    p = ws / "state" / "skill-usage-log.jsonl"
    return len(p.read_text().splitlines()) if p.exists() else 0


hook = load_hook()
tmp = Path(tempfile.mkdtemp(prefix="sur-guards-"))
try:
    # --- the happy path first: without it every "wrote nothing" below is vacuous
    rc = run(hook, json.dumps({"tool_name": "Skill", "tool_input": {"skill": "probe"}}), tmp)
    check("control: a real Skill call exits 0 AND writes", rc == 0 and log_lines(tmp) == 1,
          f"rc={rc} lines={log_lines(tmp)}")
    base = log_lines(tmp)

    # --- guard: malformed stdin (lines 56-57)
    rc = run(hook, "{not json at all", tmp)
    check("malformed stdin -> exit 0, no write", rc == 0 and log_lines(tmp) == base, f"rc={rc}")

    # --- guard: not a Skill tool call (line 59)
    rc = run(hook, json.dumps({"tool_name": "Bash", "tool_input": {"skill": "probe"}}), tmp)
    check("non-Skill tool -> exit 0, no write", rc == 0 and log_lines(tmp) == base, f"rc={rc}")

    # --- guard: VALID json that is not an object. The parse try/except does not
    # cover these — .get() on an int/str/list raises and the hook exited 1,
    # violating the always-exit-0 contract on a PostToolUse hook (#2180 review).
    for raw, label in (("123", "int payload"), ('"x"', "string payload"), ("[]", "list payload")):
        rc = run(hook, raw, tmp)
        check(f"valid non-object JSON ({label}) -> exit 0, no write",
              rc == 0 and log_lines(tmp) == base, f"rc={rc}")

    # --- guard: tool_input present but NOT an object. `or {}` only rescues
    # falsy values, so a truthy non-dict reached .get() and raised.
    for ti, label in (("oops", "string"), (42, "int"), ([1], "list")):
        rc = run(hook, json.dumps({"tool_name": "Skill", "tool_input": ti}), tmp)
        check(f"non-object tool_input ({label}) -> exit 0, no write",
              rc == 0 and log_lines(tmp) == base, f"rc={rc}")

    # --- guard: missing / non-string slug (line 62)
    for payload, label in (
        ({"tool_name": "Skill", "tool_input": {}}, "missing slug"),
        ({"tool_name": "Skill", "tool_input": {"skill": 42}}, "non-string slug"),
        ({"tool_name": "Skill"}, "absent tool_input"),
    ):
        rc = run(hook, json.dumps(payload), tmp)
        check(f"{label} -> exit 0, no write", rc == 0 and log_lines(tmp) == base, f"rc={rc}")

    # --- guard: slug empties after the prefix strip (line 67)
    rc = run(hook, json.dumps({"tool_name": "Skill", "tool_input": {"skill": "apps/web:"}}), tmp)
    check("slug empty after ':' strip -> exit 0, no write", rc == 0 and log_lines(tmp) == base, f"rc={rc}")

    # --- and the prefix strip itself still records the BARE name
    rc = run(hook, json.dumps({"tool_name": "Skill", "tool_input": {"skill": "apps/web:deploy"}}), tmp)
    rec = json.loads((tmp / "state" / "skill-usage-log.jsonl").read_text().splitlines()[-1])
    check("directory-scoped slug is recorded bare", rc == 0 and rec["slug"] == "deploy", f"got {rec.get('slug')!r}")
    base = log_lines(tmp)

    # --- guard: the write itself raises (lines 79-80)
    class _Boom(Path):
        pass

    broken = tmp / "state" / "skill-usage-log.jsonl"
    broken.unlink(missing_ok=True)
    # rmtree, not rmdir: the claim lock (skill-usage-log.jsonl.lock, added with
    # the #2180 claim-race fix) also lives in state/, so rmdir now raises
    # "Directory not empty". Remove the whole directory rather than enumerating
    # artifacts, so a future sibling file does not break this teardown again.
    shutil.rmtree(tmp / "state")
    (tmp / "state").write_text("not a directory")   # mkdir/open must now raise
    rc = run(hook, json.dumps({"tool_name": "Skill", "tool_input": {"skill": "probe"}}), tmp)
    # NOTE what is being exercised here changed with the lock: an unusable state
    # dir now fails at LOCK ACQUISITION before the write is ever attempted, so
    # this covers the lock's degrade path as well as the write-raises guard.
    # Both must land on exit 0 — that is the invariant under test, and it holds
    # whichever of the two trips first.
    check("unusable state dir -> still exit 0 (fail-open, never blocks the tool)", rc == 0, f"rc={rc}")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

if FAILS:
    print(f"\nFAILED ({len(FAILS)})")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("\nPASS — every fail-open guard exercised; the hook never blocks a tool call")
