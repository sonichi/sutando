#!/usr/bin/env python3
"""A launcher that has already booted must not freeze the resolved handler.

`task_event_handler()`'s own comment promises a handler installed, changed or
removed takes effect on the very next call, not just after a restart. That was
true in isolation, but both launchers (start-cli.sh, claude and codex) resolved
it ONCE at boot and forwarded the result into the notifier's env as
SUTANDO_TASK_EVENT_HANDLER -- the exact variable this function checks FIRST.
Every later call, for the life of that session, saw the frozen boot-time value
and never re-resolved (keweichen, PR #4472 review).

This drives the REAL shared function (src/agent/task-event-handler-lookup.sh),
not a copy, against temp skill dirs inside the real checkout -- the same
pattern watch-tasks-stream-handler-terminal-rc.test.py uses for the resolver
it wraps.

2026-09-20 UPDATE: each `call()` below is a fresh `bash -c` subprocess, so it
still passes -- but it no longer represents the real watcher, which sources
this file ONCE and calls resolve_task_event_handler repeatedly in ONE process.
Since that date the manifest-scan fallback resolves once per PROCESS and does
NOT re-check afterward (deliberate; see tests/task-event-handler-cache.test.py
and PR #4498's follow-up). The pin path below is unaffected either way -- it
was always, and remains, checked fresh on every call.

Run: python3 tests/task-event-handler-live-resolution.test.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOOKUP = REPO / "src/agent/task-event-handler-lookup.sh"
FAILURES: list[str] = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def call(env_handler, skill_name):
    """Invoke the shared task_event_handler() against REAL repo, with
    SUTANDO_TASK_EVENT_HANDLER set to `env_handler` (or unset if None).
    Returns (rc, stdout_path_or_empty)."""
    script = f". {str(LOOKUP)!r}\ntask_event_handler {str(REPO)!r}\n"
    env = {"PATH": "/usr/bin:/bin"}
    if env_handler is not None:
        env["SUTANDO_TASK_EVENT_HANDLER"] = env_handler
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    return r.returncode, r.stdout.strip()


skill_dir = REPO / "skills" / "zzz-live-resolution-test"
made = False
# worker-pool's own manifest also declares this, so it's hidden (the FILE,
# not the skill directory -- the resolver globs directories) for single-declarer cases below.
wp_manifest = REPO / "skills/worker-pool/manifest.json"
wp_hidden = REPO / "skills/worker-pool/manifest.json.hidden-for-test"
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

    rc1, out1 = call(None, "handler_a")
    check("no pin: resolves the manifest-declared handler", rc1 == 0 and out1 == str(handler_a.resolve()),
          f"rc={rc1} out={out1!r}")

    # The fix, proven live: re-point the same manifest with no restart and
    # no cached value anywhere -- just a fresh call, as a real task would make.
    handler_b = skill_dir / "scripts" / "handler_b.py"
    handler_b.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
    handler_b.chmod(0o755)
    manifest.write_text(
        '{"config": {"SUTANDO_TASK_EVENT_HANDLER_SCRIPT": "scripts/handler_b.py"}}\n')
    rc2, out2 = call(None, "handler_b")
    check("changed manifest: the very next call resolves the NEW handler, no restart needed",
          rc2 == 0 and out2 == str(handler_b.resolve()),
          f"rc={rc2} out={out2!r} -- still saw handler_a, exactly the staleness keweichen found")

    # Removed entirely: the next call reports "none" (rc 1), not a stale path.
    manifest.unlink()
    rc3, out3 = call(None, None)
    check("removed manifest: the very next call reports no handler (rc 1), not a stale path",
          rc3 == 1 and out3 == "", f"rc={rc3} out={out3!r}")

    # A genuine operator pin still wins over whatever the manifests say --
    # the fix must not turn an intentional override into dead weight.
    manifest.write_text(
        '{"config": {"SUTANDO_TASK_EVENT_HANDLER_SCRIPT": "scripts/handler_b.py"}}\n')
    pinned = "/bin/echo"
    rc4, out4 = call(pinned, "handler_b")
    check("an explicit operator pin still wins over manifest resolution",
          rc4 == 0 and out4 == pinned, f"rc={rc4} out={out4!r}")

    # A pin that names something non-executable is an operator error (rc 1),
    # not silently routed to whatever the manifest says instead.
    rc5, out5 = call(str(skill_dir / "does-not-exist"), "handler_b")
    check("a broken pin is an operator error (rc 1), not a silent fallback to resolution",
          rc5 == 1, f"rc={rc5} out={out5!r}")
finally:
    if wp_hidden.exists():
        wp_hidden.rename(wp_manifest)
    if made:
        shutil.rmtree(skill_dir, ignore_errors=True)

print(("FAILED -- " + ", ".join(FAILURES)) if FAILURES
      else "PASS -- the shared task_event_handler() re-resolves live on every call, pin still wins")
sys.exit(1 if FAILURES else 0)
