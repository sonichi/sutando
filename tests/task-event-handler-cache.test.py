#!/usr/bin/env python3
"""resolve_task_event_handler's in-process cache: resolve the manifest-scan
fallback ONCE per watcher process, never re-check after that.

This is a deliberate 2026-09-20 tightening over the #4498 per-call fingerprint
cache: that version still paid one `stat` per call to detect a manifest
change within the SAME process. This version pays nothing after the first
call -- and, as a direct consequence, a manifest change mid-process is no
longer picked up until the next watcher restart. That is the staleness
tests/task-event-handler-live-resolution.test.py's docstring warns about in
general, reintroduced here on purpose for the fallback path only: the
explicit SUTANDO_TASK_EVENT_HANDLER pin (proven unaffected below) still wins
instantly and is never cached.

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
env_base = {"PATH": "/usr/bin:/bin", "SUTANDO_PY_BIN": str(stub)}

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
    handler_b = skill_dir / "scripts" / "handler_b.py"
    handler_b.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
    handler_b.chmod(0o755)

    # (a) Same process, 3 calls, editing the manifest between call 2 and 3:
    # the third call must still report handler_a -- resolved once, by design.
    script = (
        f". {str(LOOKUP)!r}\n"
        f'resolve_task_event_handler {str(REPO)!r}\n'
        f'resolve_task_event_handler {str(REPO)!r}\n'
        f'echo {{\\"config\\": {{\\"SUTANDO_TASK_EVENT_HANDLER_SCRIPT\\": \\"scripts/handler_b.py\\"}}}} > {str(manifest)!r}\n'
        f'resolve_task_event_handler {str(REPO)!r}\n'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=dict(env_base))
    out_lines = [l for l in r.stdout.strip().splitlines() if l]
    check("same-process: all 3 calls report the FIRST-resolved handler, mid-run edit ignored",
          out_lines == [str(handler_a.resolve())] * 3, f"stdout={r.stdout!r} stderr={r.stderr!r}")
    check("same-process: python was spawned only ONCE across all 3 calls (resolve-once, not per-call)",
          counter.read_text() == "x", f"spawn count={len(counter.read_text())} (expected 1)")

    # (b) A FRESH process (new bash -c, re-sourcing the file) picks up the
    # manifest edit made above -- the restart half of the guarantee holds.
    counter.write_text("")
    manifest.write_text(
        '{"config": {"SUTANDO_TASK_EVENT_HANDLER_SCRIPT": "scripts/handler_b.py"}}\n')
    script2 = f". {str(LOOKUP)!r}\nresolve_task_event_handler {str(REPO)!r}\n"
    r2 = subprocess.run(["bash", "-c", script2], capture_output=True, text=True, env=dict(env_base))
    check("fresh process: picks up the manifest change made in the prior process",
          r2.stdout.strip() == str(handler_b.resolve()), f"stdout={r2.stdout!r} stderr={r2.stderr!r}")

    # (c) The explicit pin is rechecked live every call, in the SAME process,
    # even after the fallback cache above has already settled on something.
    pinned = "/bin/echo"
    script3 = (
        f". {str(LOOKUP)!r}\n"
        f'resolve_task_event_handler {str(REPO)!r} > /dev/null\n'  # settle the fallback cache first
        f'task_event_handler {str(REPO)!r}\n'
        f'SUTANDO_TASK_EVENT_HANDLER={pinned!r} task_event_handler {str(REPO)!r}\n'
    )
    r3 = subprocess.run(["bash", "-c", script3], capture_output=True, text=True, env=dict(env_base))
    lines3 = [l for l in r3.stdout.strip().splitlines() if l]
    check("pin: unpinned call still returns the (cached) fallback resolution",
          lines3[:1] == [str(handler_b.resolve())], f"stdout={r3.stdout!r}")
    check("pin: a pin set AFTER the fallback cache settled still wins instantly, same process",
          lines3[1:2] == [pinned], f"stdout={r3.stdout!r}")

    # (d) rc 2 (ambiguity) is still never cached -- each call re-detects it
    # and re-warns, rather than freezing the first warning silently.
    second_skill = REPO / "skills" / "zzz-teh-cache-test-2"
    (second_skill / "scripts").mkdir(parents=True)
    try:
        (second_skill / "scripts" / "handler_c.py").write_text(
            "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
        (second_skill / "scripts" / "handler_c.py").chmod(0o755)
        (second_skill / "manifest.json").write_text(
            '{"config": {"SUTANDO_TASK_EVENT_HANDLER_SCRIPT": "scripts/handler_c.py"}}\n')
        script4 = (
            f". {str(LOOKUP)!r}\n"
            f'resolve_task_event_handler {str(REPO)!r} >/dev/null 2>err1.txt; echo "rc=$?"\n'
            f'resolve_task_event_handler {str(REPO)!r} >/dev/null 2>err2.txt; echo "rc=$?"\n'
        )
        r4 = subprocess.run(["bash", "-c", script4], capture_output=True, text=True,
                             cwd=str(tmp), env=dict(env_base))
        check("ambiguity: rc 2 reported on BOTH calls, never cached to something else",
              r4.stdout.count("rc=2") == 2, f"stdout={r4.stdout!r} stderr={r4.stderr!r}")
        err1 = (tmp / "err1.txt").read_text() if (tmp / "err1.txt").exists() else ""
        err2 = (tmp / "err2.txt").read_text() if (tmp / "err2.txt").exists() else ""
        check("ambiguity: BOTH calls re-warn on stderr (not silenced after the first)",
              bool(err1) and bool(err2), f"err1={err1!r} err2={err2!r}")
    finally:
        shutil.rmtree(second_skill, ignore_errors=True)
finally:
    if wp_hidden.exists():
        wp_hidden.rename(wp_manifest)
    if made:
        shutil.rmtree(skill_dir, ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)

print(("FAILED -- " + ", ".join(FAILURES)) if FAILURES
      else "PASS -- fallback resolves once per process (by design); pin stays live; rc 2 never caches")
sys.exit(1 if FAILURES else 0)
