#!/usr/bin/env python3
"""The watcher's task-event handler: resolved before identity, fail-closed, unshipped.

Three properties, one per defect:

1. ORDER. Both launchers computed the watcher's identity (`expected_version`) and then
   resolved the auto-discovered handler. So the resolved path was never in the identity:
   installing a publisher, removing one, or gaining a second left an ALREADY-RUNNING
   watcher untouched, because its version still matched. Resolution must precede the
   identity, and the identity must carry the resolved outcome.

2. FAIL-CLOSED. `resolve_task_event_handler` returns 2 for several publishers, and both
   launchers turned every non-zero into an empty handler and started anyway. That is
   fail-OPEN at an ownership boundary: with no router probe a worker-bound task falls
   through to the unrestricted core, which is the inheritance the handler exists to stop.

3. UNSHIPPED. `skills/worker-pool/task-event-handler` was tracked in git, so every install
   shipped a publisher and the key was exported on hosts with no pool at all. The owner's
   requirement is that an install which is not running a pool is unaffected even if the
   skill is deleted, so the publisher is created when a pool first exists and is ignored
   by git.

4. EXISTING-POOL SELF-HEAL. `register_worker` is the only writer of the publisher, and it
   only runs at worker creation -- so a pool created before property 3 landed loses its
   publisher the moment its checkout pulls past that commit (git deletes a file dropped
   from the tree) and nothing recreates it, silently reopening property 2's fall-through.
   Both launchers now call `ensure_task_event_handlers_published` before every resolution.

Properties 3 and 4 are executed against the real `pool_roster` functions; 1, 2 and the
ordering half of 4 are read off both launcher scripts, since driving a tmux launcher in a
unit test would assert on a mock."""
import ast
import json
import pathlib
import re
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
CLAUDE = REPO / "src/agent/claude/cli/start-cli.sh"
CODEX = REPO / "src/agent/codex/cli/start-cli.sh"
checks = {}

for name, path in (("claude", CLAUDE), ("codex", CODEX)):
    t = path.read_text()
    ensure_at = t.find("ensure_task_event_handlers_published \"$REPO\"")
    resolve_at = t.find("resolve_task_event_handler \"$REPO\"")
    ver_at = t.find("expected_version=")
    reuse_at = t.find("SUTANDO_NOTIFIER_VERSION=//p")
    checks[f"{name}: resolves the handler at all"] = resolve_at > 0
    checks[f"{name}: resolution precedes the identity computation"] = 0 < resolve_at < ver_at
    checks[f"{name}: resolution precedes the REUSE check, so a publisher change replaces a live watcher"] = \
        0 < resolve_at < reuse_at
    # An existing pool whose publisher went missing (untracked after shipping stopped)
    # never re-registers, so nothing else re-creates it -- self-heal before every resolve.
    checks[f"{name}: gives publishers a chance to self-heal before every resolution"] = \
        0 < ensure_at < resolve_at
    # The identity must actually carry the resolved outcome, or ordering alone buys nothing.
    ver_line = t[ver_at:t.find("\n", t.find("notifier_py", ver_at)) if name == "claude" else t.find("\"\n", ver_at) + 1]
    checks[f"{name}: the identity includes the resolved handler"] = "SUTANDO_TASK_EVENT_HANDLER" in ver_line
    checks[f"{name}: ambiguity (rc 2) is distinguished from 'none'"] = "handler_rc=$?" in t and '"$handler_rc" = 2' in t
    # Fail-closed: the rc-2 branch must return WITHOUT reaching new-session.
    m = re.search(r'if \[ "\$handler_rc" = 2 \]; then(.*?)\n  fi\n', t, re.S)
    checks[f"{name}: ambiguity refuses to start the watcher"] = bool(m) and "return 0" in m.group(1)
    checks[f"{name}: ambiguity also refuses REUSE (kills any live watcher session)"] = \
        bool(m) and "kill-session" in m.group(1)
    checks[f"{name}: the refusal tells the operator how to pin it"] = \
        bool(m) and "SUTANDO_TASK_EVENT_HANDLER" in m.group(1)

# Property 4a: the self-heal gate, against the canonical module (a copy
# misattributes coverage); publish is mocked so this touches no real file.
import importlib.util as _ilu
import unittest.mock as _mock
_spec = _ilu.spec_from_file_location(
    "pool_roster_canonical", REPO / "skills/worker-pool/scripts/pool_roster.py")
_canonical = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_canonical)
with tempfile.TemporaryDirectory() as _d:
    _ws_pool = pathlib.Path(_d) / "ws-pool"; (_ws_pool / "state").mkdir(parents=True)
    _canonical._write_atomic(_canonical.roster_path(_ws_pool),
                              {"version": 1, "workers": {"w1": {"state": "live", "label": "w1"}}})
    with _mock.patch.object(_canonical, "publish_task_event_handler") as _pub:
        _canonical.ensure_task_event_handler_published(_ws_pool)
        checks["canonical: a pool with a worker calls publish"] = _pub.called
    _ws_empty = pathlib.Path(_d) / "ws-empty"; (_ws_empty / "state").mkdir(parents=True)
    with _mock.patch.object(_canonical, "publish_task_event_handler") as _pub2:
        _canonical.ensure_task_event_handler_published(_ws_empty)
        checks["canonical: no pool never calls publish"] = not _pub2.called

# Property 3a: the publisher is not shipped.
tracked = subprocess.run(["git", "-C", str(REPO), "ls-files", "skills/worker-pool/task-event-handler"],
                         capture_output=True, text=True).stdout.strip()
checks["the publisher is NOT tracked in git (an install ships no publisher)"] = tracked == ""
ignored = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q",
                          "skills/worker-pool/task-event-handler"]).returncode == 0
checks["the publisher is git-ignored, so creating it never dirties a checkout"] = ignored

# Behavioral, against a COPY of the skill so the create-transition runs even on a
# host that already has a pool -- where the first version silently skipped it.
sys.path.insert(0, str(REPO / "src"))
try:
    import shutil
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        skill = root / "worker-pool"
        shutil.copytree(REPO / "skills/worker-pool", skill,
                        ignore=shutil.ignore_patterns("task-event-handler", "__pycache__"))
        link = skill / "task-event-handler"
        sys.path.insert(0, str(skill / "scripts"))
        import importlib.util
        spec = importlib.util.spec_from_file_location("pr_copy", skill / "scripts/pool_roster.py")
        pr = importlib.util.module_from_spec(spec)
        sys.modules["pool_roster"] = pr
        spec.loader.exec_module(pr)

        checks["a fresh install ships NO publisher (copytree excluded it, git agrees)"] = not link.exists()
        ws = root / "ws"; (ws / "state").mkdir(parents=True)
        checks["before any worker exists, still no publisher"] = not (link.is_symlink() or link.exists())
        pr.register_worker(ws, "w" * 32, "probe-worker")
        checks["register_worker publishes the handler once a pool exists"] = link.is_symlink()
        checks["the published handler points at this skill's router"] = \
            link.is_symlink() and link.readlink().name == "pool_route_handler.py"
        # Idempotent: a second registration must not fail or repoint it.
        pr.register_worker(ws, "x" * 32, "probe-worker-2")
        checks["a second registration leaves the publisher alone"] = \
            link.is_symlink() and link.readlink().name == "pool_route_handler.py"

        # Property 4: SELF-HEAL. An existing pool's publisher can go missing and
        # nothing re-registers on its own; ensure_task_event_handler_published republishes it.
        link.unlink()
        checks["fixture precondition: an existing pool's publisher can go missing"] = not link.exists()
        pr.ensure_task_event_handler_published(ws)
        checks["an existing pool's missing publisher is republished"] = \
            link.is_symlink() and link.readlink().name == "pool_route_handler.py"
        # Control: a workspace with NO pool at all must stay untouched -- the
        # unshipped-by-default property (3) must survive this new call path too.
        link.unlink()
        ws_nopool = root / "ws-nopool"; (ws_nopool / "state").mkdir(parents=True)
        pr.ensure_task_event_handler_published(ws_nopool)
        checks["control: a workspace with no pool gets no publisher from self-heal"] = not link.exists()
        # Restore for the sections below, which assume ws's publisher exists.
        pr.ensure_task_event_handler_published(ws)

        # A pool registered without a publisher reads to the launcher as NO pool,
        # so worker-bound tasks would reach the unrestricted core. Must abort.
        ws2 = root / "ws2"; (ws2 / "state").mkdir(parents=True)
        link.unlink()
        real_symlink = pathlib.Path.symlink_to

        def refuse(self, target, target_is_directory=False):
            raise OSError(30, "Read-only file system")

        pathlib.Path.symlink_to = refuse
        try:
            raised = None
            try:
                pr.register_worker(ws2, "y" * 32, "probe-worker-3")
            except Exception as exc:  # noqa: BLE001
                raised = exc
            checks["a failed publish RAISES instead of returning None"] = \
                raised is not None and type(raised).__name__ == "HandlerPublishError"
            checks["a failed publish leaves NO publisher behind"] = not link.exists()
            checks["a failed publish writes NO roster, so no pool exists without a handler"] = \
                not (pr.roster_path(ws2)).exists()
        finally:
            pathlib.Path.symlink_to = real_symlink
        # Control: the same call succeeds once symlink_to works again.
        pr.register_worker(ws2, "y" * 32, "probe-worker-3")
        checks["control: registration succeeds again once publishing can succeed"] = \
            link.is_symlink() and pr.roster_path(ws2).exists()
except Exception as e:  # noqa: BLE001
    checks[f"register_worker publish path is importable and runnable ({e})"] = False

fails = [k for k, ok in checks.items() if not ok]
for k, ok in checks.items():
    print(("ok   " if ok else "FAIL ") + k)
print("all checks pass" if not fails else f"FAILED ({len(fails)})")
sys.exit(1 if fails else 0)
