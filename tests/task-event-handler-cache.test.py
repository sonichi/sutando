#!/usr/bin/env python3
"""resolve_task_event_handler's in-process cache: skip the python spawn on a
repeat call with nothing changed, but never skip it -- and never serve a stale
answer -- when a skill's manifest.json is actually added, edited, or removed.

This is the SAME-PROCESS property tests/task-event-handler-live-resolution
.test.py does not cover: that test drives task_event_handler() via a fresh
`bash -c` per call, which is a fresh process every time and so exercises no
cache regardless. The watcher (src/watch-tasks-stream.sh) sources this file
ONCE and calls resolve_task_event_handler repeatedly for the life of that one
process -- this test drives it the same way, several calls inside one bash -c
script, with a counting python3 stub standing in for SUTANDO_PY_BIN so a
skipped spawn is directly observable, not inferred from timing.

Run: python3 tests/task-event-handler-cache.test.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOOKUP = REPO / "src/agent/task-event-handler-lookup.sh"
FAILURES: list[str] = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


tmp = Path(tempfile.mkdtemp(prefix="teh-cache-"))
counter = tmp / "spawn-count"
counter.write_text("")
stub = tmp / "counting-python3"
stub.write_text(
    "#!/bin/sh\n"
    f"printf x >> {str(counter)!r}\n"
    "exec python3 \"$@\"\n"
)
stub.chmod(0o755)

skill_dir = REPO / "skills" / "zzz-teh-cache-test"
made = False
wp_manifest = REPO / "skills/worker-pool/manifest.json"
wp_hidden = REPO / "skills/worker-pool/manifest.json.hidden-for-cache-test"

try:
    (skill_dir / "scripts").mkdir(parents=True)
    made = True
    wp_manifest.rename(wp_hidden)
    handler_a = skill_dir / "scripts" / "handler_a.py"
    handler_a.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
    handler_a.chmod(0o755)
    manifest = skill_dir / "manifest.json"
    manifest.write_text(
        '{"config": {"SUTANDO_TASK_EVENT_HANDLER_SCRIPT": "scripts/handler_a.py"}}\n')

    # Two calls in ONE bash process, matching the real watcher's repeated
    # in-process calls across dispatches.
    script = (
        f". {str(LOOKUP)!r}\n"
        f'resolve_task_event_handler {str(REPO)!r}; echo \"rc1=$?\"\n'
        f'resolve_task_event_handler {str(REPO)!r}; echo \"rc2=$?\"\n'
    )
    env = {"PATH": "/usr/bin:/bin", "SUTANDO_PY_BIN": str(stub)}
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    lines = r.stdout.strip().splitlines()
    check("two repeat calls both resolve rc 0",
          "rc1=0" in lines and "rc2=0" in lines, f"stdout={r.stdout!r} stderr={r.stderr!r}")
    check("both calls print the SAME resolved handler",
          lines.count(str(handler_a.resolve())) == 2, f"stdout={r.stdout!r}")
    spawns_after_two = counter.read_text()
    check("the second, unchanged-manifest call did NOT spawn python again (cache hit)",
          spawns_after_two == "x", f"spawn count={len(spawns_after_two)} (expected 1)")

    # A manifest edit mid-run must force a real rescan, not a stale cache hit.
    counter.write_text("")
    handler_b = skill_dir / "scripts" / "handler_b.py"
    handler_b.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
    handler_b.chmod(0o755)
    script2 = (
        f". {str(LOOKUP)!r}\n"
        f'resolve_task_event_handler {str(REPO)!r}\n'
        f'sleep 1\n'  # ensure a distinguishable mtime on most filesystems
        f'echo {{\\"config\\": {{\\"SUTANDO_TASK_EVENT_HANDLER_SCRIPT\\": \\"scripts/handler_b.py\\"}}}} > {str(manifest)!r}\n'
        f'resolve_task_event_handler {str(REPO)!r}\n'
    )
    env2 = {"PATH": "/usr/bin:/bin", "SUTANDO_PY_BIN": str(stub)}
    r2 = subprocess.run(["bash", "-c", script2], capture_output=True, text=True, env=env2)
    out_lines = [l for l in r2.stdout.strip().splitlines() if l]
    check("same-process: editing the manifest mid-run is reflected on the VERY NEXT call, no restart",
          out_lines == [str(handler_a.resolve()), str(handler_b.resolve())],
          f"stdout={r2.stdout!r} stderr={r2.stderr!r}")
    check("same-process: the manifest edit forced a real rescan (python spawned twice, not cached)",
          counter.read_text() == "xx", f"spawn count={len(counter.read_text())} (expected 2)")

    # Removal, same-process: the cache must not keep serving handler_b after
    # the manifest disappears mid-run.
    counter.write_text("")
    script3 = (
        f". {str(LOOKUP)!r}\n"
        f'resolve_task_event_handler {str(REPO)!r}; echo \"rc=$?\"\n'
        f'rm {str(manifest)!r}\n'
        f'resolve_task_event_handler {str(REPO)!r}; echo \"rc=$?\"\n'
    )
    env3 = {"PATH": "/usr/bin:/bin", "SUTANDO_PY_BIN": str(stub)}
    r3 = subprocess.run(["bash", "-c", script3], capture_output=True, text=True, env=env3)
    check("same-process: removing the manifest mid-run reports rc 1 on the very next call",
          r3.stdout.count("rc=0") == 1 and r3.stdout.count("rc=1") == 1,
          f"stdout={r3.stdout!r} stderr={r3.stderr!r}")
finally:
    if wp_hidden.exists():
        wp_hidden.rename(wp_manifest)
    if made:
        shutil.rmtree(skill_dir, ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)

print(("FAILED -- " + ", ".join(FAILURES)) if FAILURES
      else "PASS -- the fingerprint cache skips redundant spawns and never survives a real manifest change")
sys.exit(1 if FAILURES else 0)
