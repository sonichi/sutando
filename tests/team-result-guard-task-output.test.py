#!/usr/bin/env python3
"""The task-scoped output allowance (⑤a-cap) in the guarded egress.

A Signal Room task may announce a generated file as a standalone
`[file: <path>]` line, and ONLY when that path's realpath sits under the task's
own `<results>/<task_id>/`. Everything the guard withheld before it still
withholds: out-of-root paths, inline mentions, the send/attach aliases, redirects,
relative paths, symlink escapes, and any body where even one marker fails.

Part 1 drives the policy module directly. Part 2 drives agent-api's
`_guard_result_by_tier`, which passes the allowance for Signal Room tasks ONLY —
a team task from any other lane sees the unchanged guard.

Run: python3 tests/team-result-guard-task-output.test.py
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import policy.egress.result as guard  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


class _Scan:
    detected = False
    secret_types = ()


def _clean(_body):
    return _Scan()


ws = Path(tempfile.mkdtemp(prefix="task-output-guard-"))
results = ws / "results"
root = results / "task-signal-1-abcd"
root.mkdir(parents=True)
(root / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\n")
sibling = results / "task-signal-1-abcd0"          # prefix-confusable sibling
sibling.mkdir()
(sibling / "x.png").write_bytes(b"x")
outside = ws / "secret.png"
outside.write_bytes(b"x")
(root / "escape.png").symlink_to(outside)
in_root = str(root / "chart.png")


def run(body, task_output_root=root, tier="team"):
    return guard.guard_result_for_tier(body, tier, REPO, secret_filter=_clean,
                                       task_output_root=task_output_root)


print("== part 1: the policy ==")
cases = [
    ("in-root standalone [file:] survives", f"Here is the chart.\n[file: {in_root}]\n", False),
    ("in-root marker with surrounding blank lines survives", f"\n\n[file: {in_root}]\n\nDone.", False),
    ("two in-root markers survive", f"[file: {in_root}]\n[file: {in_root}]", False),
    ("in-root marker with indented whitespace survives", f"  [file: {in_root}]", False),
    ("out-of-root standalone marker withheld", f"[file: {outside}]", True),
    ("prefix-confusable sibling dir withheld", f"[file: {sibling / 'x.png'}]", True),
    ("symlink under root escaping the root withheld", f"[file: {root / 'escape.png'}]", True),
    ("relative path withheld", "[file: chart.png]", True),
    ("dot-dot escape withheld", f"[file: {root}/../../secret.png]", True),
    ("inline in-root mention withheld", f"see [file: {in_root}] here", True),
    ("[attach:] alias withheld even in-root", f"[attach: {in_root}]", True),
    ("[send:] alias withheld even in-root", f"[send: {in_root}]", True),
    ("mixed in-root + out-of-root withheld", f"[file: {in_root}]\n[file: {outside}]", True),
    ("redirect alongside an in-root marker withheld", f"[channel: 123]\n[file: {in_root}]", True),
    ("plain prose still passes", "no markers here", False),
]
for name, body, expect_withheld in cases:
    out, why = run(body)
    withheld = why is not None
    check(name, withheld == expect_withheld, f"why={why!r}")
    if not withheld and out != body:
        check(name + " (body unaltered)", False, "body changed")
    if withheld and out != guard.TEAM_LEAK_RESULT_MARKER:
        check(name + " (marker sentinel)", False, f"got {out!r}")

out, why = run(f"[file: {in_root}]", task_output_root=None)
check("no allowance (root=None): in-root marker still withheld", why is not None)
out, why = run(f"[file: {outside}]", tier="owner")
check("owner tier unchanged: any marker passes", why is None and out == f"[file: {outside}]")

# The lower-level entry points accept the keyword too (one policy, three doors).
try:
    guard.scan_team_result(f"[file: {in_root}]", REPO, _clean, task_output_root=root)
    check("scan_team_result accepts task_output_root", True)
except guard.TeamResultLeakError:
    check("scan_team_result accepts task_output_root", False, "raised")
verdict = guard.classify_result_for_tier(f"[file: {outside}]", "team", REPO, _clean,
                                         task_output_root=root)
check("classify_result_for_tier withholds out-of-root", verdict.kind == guard.VERDICT_LEAK)

# A marker whose only "standalone" appearance is fenced code is SHOWN, not issued:
# the full parse ignores it, so the count mismatch keeps the body a withhold.
out, why = run(f"```\n[file: {in_root}]\n```\n[file: {outside}]")
check("fenced example does not launder an out-of-root marker", why is not None)


print("== part 2: /result passes the allowance for Signal Room tasks only ==")
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["SUTANDO_WORKSPACE"] = str(ws)
spec = importlib.util.spec_from_file_location("agent_api", REPO / "src" / "agent-api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)
api.TASK_DIR = ws / "tasks"
api.RESULT_DIR = results
api.TASK_DIR.mkdir(exist_ok=True)
guard.load_team_result_scanner = lambda repo: _clean

(api.TASK_DIR / "task-signal-1-abcd.txt").write_text(
    "id: task-signal-1-abcd\nsource: signal-room\naccess_tier: team\n"
    "source_room_id: !a:hs\ntask: draw it\n")
body = f"[file: {in_root}]\nA chart."
check("Signal Room task: in-root marker survives /result's guard",
      api._guard_result_by_tier("task-signal-1-abcd", body) == body)
check("Signal Room task: out-of-root marker withheld",
      api._guard_result_by_tier("task-signal-1-abcd", f"[file: {outside}]")
      == guard.TEAM_LEAK_RESULT_MARKER)
check("Signal Room task: another task's dir withheld",
      api._guard_result_by_tier("task-signal-1-abcd", f"[file: {sibling / 'x.png'}]")
      == guard.TEAM_LEAK_RESULT_MARKER)

# A team task from any other lane: no allowance, even for a path under results/<id>/.
other = results / "task-777"
other.mkdir()
(other / "img.png").write_bytes(b"x")
(api.TASK_DIR / "task-777.txt").write_text(
    "id: task-777\nsource: discord\naccess_tier: team\ntask: draw it\n")
check("non-Signal-Room team task: marker under its own results dir still withheld",
      api._guard_result_by_tier("task-777", f"[file: {other / 'img.png'}]")
      == guard.TEAM_LEAK_RESULT_MARKER)

# Metadata gone (archived past the window): the prefix alone still identifies the lane.
(api.TASK_DIR / "task-signal-1-abcd.txt").unlink()
check("Signal Room task without metadata: allowance keyed on the lane's id prefix",
      api._guard_result_by_tier("task-signal-1-abcd", body) == body)

print()
if failures:
    print(f"  {len(failures)} FAILURE(S): {', '.join(failures)}")
    sys.exit(1)
print("PASS — task-scoped output allowance")
