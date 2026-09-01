#!/usr/bin/env python3
"""A resolver failure must NOT create or read a repo-local fallback workspace.

Regression for the #2180 review blocker. Both executables used to catch every
exception from resolve_workspace() and substitute `repo_root() / "workspace"`.
That defeated configured workspace resolution: a malformed config or a resolver
regression silently created a SECOND telemetry store inside the checkout, which
then diverged from the real one with nothing reporting the split. Reviewer's
repro: sutando.config.local.json = {"workspace": {"path": 42}} makes
resolve_workspace() raise TypeError, and the OLD hook still wrote
<worktree>/workspace/state/skill-usage-log.jsonl while exiting 0.

Why the fallback could never be right: resolve_workspace() ALREADY defaults to
<repo>/workspace/ when nothing is configured, so a local fallback adds no
capability — it can only DISAGREE with the resolver when configuration exists,
which is exactly when it fires.

HOW this drives the failure, and why not via a fake checkout: my first attempt
built a throwaway repo with a malformed config and asserted "writes nothing". A
premise check caught that resolve_workspace() never raised there — it locates the
config relative to the MODULE's repo, not cwd, so it resolved the real workspace
and every assertion passed VACUOUSLY. Forcing the resolver to fail at the import
boundary is both simpler and actually exercises the branch.

Both directions are covered on purpose: "writes nothing" alone would also pass on
a hook that does nothing at all, so the happy path is asserted in the same run.

Run: python3 tests/skill-usage-report-workspace-isolation.test.py
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import types
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "skills" / "skill-usage-report" / "hooks" / "log-usage.py"

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def load_hook():
    spec = importlib.util.spec_from_file_location("log_usage", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = load_hook()

# --- resolver FAILS -----------------------------------------------------------
# Block the import the hook performs, which is the same observable state as any
# resolver exception: workspace_default missing, unimportable, or raising.
class _Blocker:
    """Make `from workspace_default import resolve_workspace` raise.

    PEP 451 `find_spec`, NOT the legacy `find_module`/`load_module` pair. Those
    were deprecated in 3.4 and REMOVED from the import system in 3.12, so a
    meta_path finder offering only them is simply never consulted there: the real
    workspace_default imports, the premise silently evaporates, and the hook
    performs the very repo-local write this file exists to forbid. Measured on
    3.12.13 — 3 checks failed, including the premise guard.

    Belt and braces: `sys.modules` is ALSO pre-seeded with a module whose
    `resolve_workspace` raises, which needs no import-system cooperation at all
    and so cannot be undone by a future protocol change. Either mechanism alone
    would do; together they are version-proof.
    """
    def find_spec(self, name, path=None, target=None):
        if name == "workspace_default":
            raise ImportError("blocked for test")
        return None


sys.modules.pop("workspace_default", None)
_stub = types.ModuleType("workspace_default")


def _raise(*_a, **_k):
    raise RuntimeError("resolver unavailable (blocked for test)")


_stub.resolve_workspace = _raise
sys.modules["workspace_default"] = _stub
sys.meta_path.insert(0, _Blocker())
try:
    ws = hook.workspace()
    # PREMISE: if this ever stops being None the assertions below are vacuous.
    check("premise: workspace() returns None when the resolver cannot answer",
          ws is None, f"got {ws!r}")

    repo_local = hook.repo_root() / "workspace" / "state" / "skill-usage-log.jsonl"
    before = repo_local.exists()
    lines_before = len(repo_local.read_text().splitlines()) if before else 0

    payload = json.dumps({"tool_name": "Skill", "tool_input": {"skill": "probe"}})
    import io
    real_stdin = sys.stdin
    sys.stdin = io.StringIO(payload)
    try:
        rc = hook.main()
    finally:
        sys.stdin = real_stdin

    check("hook: exits 0 on resolver failure (fail-open preserved)", rc == 0, f"rc={rc}")
    lines_after = len(repo_local.read_text().splitlines()) if repo_local.exists() else 0
    check("hook: wrote NOTHING to a repo-local fallback log",
          lines_after == lines_before, f"{lines_before} -> {lines_after}")
    check("hook: did not CREATE a repo-local log that was absent",
          before or not repo_local.exists())
finally:
    sys.meta_path.pop(0)
    sys.modules.pop("workspace_default", None)
    # Clean the fixture this block can create. When the premise breaks (as it did
    # on 3.12 before the find_spec fix) the hook DOES perform the repo-local
    # write, leaving an untracked workspace/state/skill-usage-log.jsonl in the
    # tree — a test that forbids a write must not leave that write behind when it
    # fails. Only remove what we created: if the file existed before, leave it.
    try:
        if not before and repo_local.exists():
            repo_local.unlink()
    except NameError:
        pass  # failed before `before`/`repo_local` were bound
    except OSError:
        pass

# --- resolver WORKS: the hook must still write, to the RESOLVED location -------
# Without this the block above would pass on a hook that never writes at all.
tmp = Path(tempfile.mkdtemp(prefix="sur-ws-ok-"))
try:
    hook.workspace = lambda: tmp                      # stand in for a resolved ws
    import io
    real_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps({"tool_name": "Skill",
                                        "tool_input": {"skill": "works"}}))
    try:
        rc = hook.main()
    finally:
        sys.stdin = real_stdin
    log = tmp / "state" / "skill-usage-log.jsonl"
    check("control: hook exits 0 when resolution works", rc == 0, f"rc={rc}")
    check("control: hook DOES write, to the RESOLVED location", log.exists())
    if log.exists():
        check("control: the record carries the skill slug", '"slug": "works"' in log.read_text())
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print(f"FAIL — {len(failures)} check(s): {failures}")
    sys.exit(1)
print("PASS — a resolver failure neither creates nor writes a repo-local workspace")
