"""
Tests for the PERSONAL_CLAUDE.md compaction re-inject hook:
  - src/personal-claude-compact-hint.sh (hint output)
  - scripts/install-personal-claude-hook.sh (settings.json wiring)

Discovered by CI's Python test runner alongside other *.test.py files.

Covers:
  - Hint emits valid additionalContext JSON containing the file content
  - Per-host hosts/<host>/PERSONAL_CLAUDE.md wins over workspace root
  - Missing file → no output, exit 0 (silent no-op)
  - COMPACT-CORE-END marker → only the core is injected + pointer to the tail
  - Installer: fresh install registers under matcher "compact"
  - Installer: idempotent (no duplicate on re-run)
"""

import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HINT = os.path.join(REPO, "src", "personal-claude-compact-hint.sh")
INSTALLER = os.path.join(REPO, "scripts", "install-personal-claude-hook.sh")

sys.path.insert(0, REPO)
from src.util_paths import _host_label  # noqa: E402

_pass = 0
_fail = 0


def ok(label: str) -> None:
    global _pass
    print(f"  PASS: {label}")
    _pass += 1


def fail(label: str, detail: str = "") -> None:
    global _fail
    detail_str = f" — {detail}" if detail else ""
    print(f"  FAIL: {label}{detail_str}", file=sys.stderr)
    _fail += 1


def run_hint(workspace: str) -> subprocess.CompletedProcess:
    """Run the hint script with workspace resolution pinned to a temp dir.

    Uses the test-only escape hatch in src/sutando_config.py:
    SUTANDO_TEST_MODE=1 makes the resolver honor $SUTANDO_WORKSPACE.
    """
    env = dict(os.environ)
    env["SUTANDO_TEST_MODE"] = "1"
    env["SUTANDO_WORKSPACE"] = workspace
    env["SUTANDO_CORE_SESSION"] = "1"  # pass the core-session scope gate
    return subprocess.run(
        ["bash", HINT], capture_output=True, text=True, env=env, timeout=30
    )


# ── Test 1: workspace-root file is injected as additionalContext ─────────────
with tempfile.TemporaryDirectory() as ws:
    content = "## My rules\n- always test before shipping\n"
    with open(os.path.join(ws, "PERSONAL_CLAUDE.md"), "w") as f:
        f.write(content)
    r = run_hint(ws)
    try:
        payload = json.loads(r.stdout)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        if (
            r.returncode == 0
            and payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
            and "always test before shipping" in ctx
            and "re-injected after context compaction" in ctx
        ):
            ok("workspace-root PERSONAL_CLAUDE.md injected as additionalContext")
        else:
            fail("workspace-root inject", f"rc={r.returncode} ctx={ctx[:120]!r}")
    except (json.JSONDecodeError, KeyError) as e:
        fail("workspace-root inject", f"bad output {r.stdout[:200]!r} ({e}) stderr={r.stderr[:200]!r}")

# ── Test 2: per-host file wins over workspace root ────────────────────────────
with tempfile.TemporaryDirectory() as ws:
    host_dir = os.path.join(ws, "hosts", _host_label())
    os.makedirs(host_dir)
    with open(os.path.join(host_dir, "PERSONAL_CLAUDE.md"), "w") as f:
        f.write("PER-HOST-RULES\n")
    with open(os.path.join(ws, "PERSONAL_CLAUDE.md"), "w") as f:
        f.write("ROOT-RULES\n")
    r = run_hint(ws)
    try:
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        if "PER-HOST-RULES" in ctx and "ROOT-RULES" not in ctx:
            ok("per-host file wins over workspace root")
        else:
            fail("per-host precedence", f"ctx={ctx[:160]!r}")
    except (json.JSONDecodeError, KeyError) as e:
        fail("per-host precedence", f"bad output {r.stdout[:200]!r} ({e})")

# ── Test 3: missing file → silent no-op, exit 0 ───────────────────────────────
with tempfile.TemporaryDirectory() as ws:
    r = run_hint(ws)
    if r.returncode == 0 and r.stdout.strip() == "":
        ok("missing PERSONAL_CLAUDE.md → no output, exit 0")
    else:
        fail("missing-file no-op", f"rc={r.returncode} out={r.stdout[:120]!r}")

# ── Test 4: COMPACT-CORE-END marker splits core from reference tail ───────────
with tempfile.TemporaryDirectory() as ws:
    with open(os.path.join(ws, "PERSONAL_CLAUDE.md"), "w") as f:
        f.write("CORE-RULES\n\n<!-- COMPACT-CORE-END -->\n\nTAIL-REFERENCE\n")
    r = run_hint(ws)
    try:
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        if "CORE-RULES" in ctx and "TAIL-REFERENCE" not in ctx and "omitted to save tokens" in ctx:
            ok("marker: core injected, tail omitted with pointer")
        else:
            fail("marker split", f"ctx={ctx[:200]!r}")
    except (json.JSONDecodeError, KeyError) as e:
        fail("marker split", f"bad output {r.stdout[:200]!r} ({e})")

# ── Test 5: installer fresh install registers matcher "compact" ───────────────
with tempfile.TemporaryDirectory() as tmp:
    env = dict(os.environ)
    env["SUTANDO_CLAUDE_WORKING_DIR"] = tmp
    r = subprocess.run(
        ["bash", INSTALLER], capture_output=True, text=True, env=env, timeout=30
    )
    settings_path = os.path.join(tmp, ".claude", "settings.json")
    try:
        with open(settings_path) as f:
            data = json.load(f)
        entries = data["hooks"]["SessionStart"]
        entry = next(
            (
                e
                for e in entries
                if any("personal-claude-compact-hint.sh" in h.get("command", "") for h in e.get("hooks", []))
            ),
            None,
        )
        if r.returncode == 0 and entry is not None and entry.get("matcher") == "compact":
            ok('installer registers hook under matcher "compact"')
        else:
            fail("installer fresh", f"rc={r.returncode} entry={entry!r} err={r.stderr[:200]!r}")
    except (OSError, json.JSONDecodeError, KeyError) as e:
        fail("installer fresh", f"{e} out={r.stdout[:200]!r} err={r.stderr[:200]!r}")

    # ── Test 6: idempotent re-run — no duplicate entry ────────────────────────
    r2 = subprocess.run(
        ["bash", INSTALLER], capture_output=True, text=True, env=env, timeout=30
    )
    with open(settings_path) as f:
        data2 = json.load(f)
    cmds = [
        h["command"]
        for e in data2["hooks"]["SessionStart"]
        for h in e.get("hooks", [])
        if "personal-claude-compact-hint.sh" in h.get("command", "")
    ]
    if r2.returncode == 0 and "already installed" in r2.stdout and len(cmds) == 1:
        ok("installer idempotent — single entry after re-run")
    else:
        fail("installer idempotent", f"rc={r2.returncode} n={len(cmds)} out={r2.stdout[:120]!r}")


# ── Test 7: hook is cwd-independent (hooks run from the session cwd) ──────────
# Regression: util_paths' internal `from workspace_default import ...` needs
# repo/src on sys.path; without it resolution fell back to cwd-dependent
# `git rev-parse` and silently no-op'd from a non-repo cwd (live-test finding).
with tempfile.TemporaryDirectory() as ws:
    with open(os.path.join(ws, "PERSONAL_CLAUDE.md"), "w") as f:
        f.write("CWD-INDEPENDENT-RULES\n")
    env = dict(os.environ)
    env["SUTANDO_TEST_MODE"] = "1"
    env["SUTANDO_WORKSPACE"] = ws
    env["SUTANDO_CORE_SESSION"] = "1"
    r = subprocess.run(
        ["bash", HINT], capture_output=True, text=True, env=env, timeout=30,
        cwd="/",  # decidedly not a git checkout
    )
    try:
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        if "CWD-INDEPENDENT-RULES" in ctx:
            ok("hook resolves workspace from a non-repo cwd")
        else:
            fail("cwd independence", f"ctx={ctx[:120]!r}")
    except (json.JSONDecodeError, KeyError) as e:
        fail("cwd independence", f"bad output {r.stdout[:200]!r} ({e})")

# ── Test 8b: installer skips (not crashes) on a clean Mac with no dev tools ───
# "No CLT" fixture per tests/python-binary-sh.test.sh: fake xcode-select (exit 2).
with tempfile.TemporaryDirectory() as tmp:
    noclt = os.path.join(tmp, "noclt")
    os.makedirs(noclt)
    with open(os.path.join(noclt, "xcode-select"), "w") as f:
        f.write("#!/bin/sh\nexit 2\n")
    os.chmod(os.path.join(noclt, "xcode-select"), 0o755)

    cwd = os.path.join(tmp, "cwd")
    os.makedirs(cwd)
    env = dict(os.environ)
    env["SUTANDO_CLAUDE_WORKING_DIR"] = cwd
    env["PATH"] = f"{noclt}:/usr/bin:/bin"
    env["OSTYPE"] = "darwin25"
    env.pop("SUTANDO_PY", None)
    r = subprocess.run(
        ["bash", INSTALLER], capture_output=True, text=True, env=env, timeout=30
    )
    settings_path = os.path.join(cwd, ".claude", "settings.json")
    if r.returncode == 0 and not os.path.exists(settings_path):
        ok("installer skips cleanly on a clean Mac with no developer tools")
    else:
        fail(
            "installer clean-mac stub skip",
            f"rc={r.returncode} settings_exists={os.path.exists(settings_path)} "
            f"out={r.stdout[:160]!r} err={r.stderr[:160]!r}",
        )

# ── Test 8: scope gate — non-core session (no SUTANDO_CORE_SESSION) is silent ──
with tempfile.TemporaryDirectory() as ws:
    with open(os.path.join(ws, "PERSONAL_CLAUDE.md"), "w") as f:
        f.write("SHOULD-NOT-APPEAR\n")
    env = dict(os.environ)
    env["SUTANDO_TEST_MODE"] = "1"
    env["SUTANDO_WORKSPACE"] = ws
    env.pop("SUTANDO_CORE_SESSION", None)
    r = subprocess.run(
        ["bash", HINT], capture_output=True, text=True, env=env, timeout=30
    )
    if r.returncode == 0 and r.stdout.strip() == "":
        ok("scope gate: no SUTANDO_CORE_SESSION → no output, exit 0")
    else:
        fail("scope gate", f"rc={r.returncode} out={r.stdout[:120]!r}")


print(f"\n{_pass} passed, {_fail} failed")
sys.exit(1 if _fail else 0)
