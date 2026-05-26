#!/usr/bin/env python3
"""Tests for src/util_paths.py — personal_path, shared_personal_path, claude_home_path."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "src" / "util_paths.py"

spec = importlib.util.spec_from_file_location("util_paths", SCRIPT)
mod = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(REPO_ROOT / "src"))
spec.loader.exec_module(mod)

PASS = 0
FAIL = 0


def ok(label):
    global PASS; PASS += 1; print(f"PASS  {label}")


def fail(label, detail=""):
    global FAIL; FAIL += 1
    print(f"FAIL  {label}" + (f": {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# T1 — script exists
# ---------------------------------------------------------------------------
if SCRIPT.exists():
    ok("T1: util_paths.py exists")
else:
    fail("T1: util_paths.py exists")

# ---------------------------------------------------------------------------
# T2 — script has module docstring
# ---------------------------------------------------------------------------
content = SCRIPT.read_text()
body = "\n".join(l for l in content.splitlines() if not l.startswith("#!")).lstrip()
if body.startswith('"""') or body.startswith("'''"):
    ok("T2: script has module docstring")
else:
    fail("T2: script has module docstring")

# ---------------------------------------------------------------------------
# T3 — _memory_dir_env() returns SUTANDO_MEMORY_DIR when set
# ---------------------------------------------------------------------------
orig_new = os.environ.get("SUTANDO_MEMORY_DIR")
orig_old = os.environ.get("SUTANDO_PRIVATE_DIR")
try:
    os.environ["SUTANDO_MEMORY_DIR"] = "/tmp/test-mem-dir"
    os.environ.pop("SUTANDO_PRIVATE_DIR", None)
    result = mod._memory_dir_env()
    if result == "/tmp/test-mem-dir":
        ok("T3: _memory_dir_env() returns SUTANDO_MEMORY_DIR when set")
    else:
        fail("T3: returns SUTANDO_MEMORY_DIR", f"got={result!r}")
finally:
    if orig_new is None:
        os.environ.pop("SUTANDO_MEMORY_DIR", None)
    else:
        os.environ["SUTANDO_MEMORY_DIR"] = orig_new
    if orig_old is not None:
        os.environ["SUTANDO_PRIVATE_DIR"] = orig_old

# ---------------------------------------------------------------------------
# T4 — _memory_dir_env() falls back to SUTANDO_PRIVATE_DIR (with deprecation warning)
# ---------------------------------------------------------------------------
orig_new = os.environ.get("SUTANDO_MEMORY_DIR")
orig_old = os.environ.get("SUTANDO_PRIVATE_DIR")
import io
try:
    os.environ.pop("SUTANDO_MEMORY_DIR", None)
    os.environ["SUTANDO_PRIVATE_DIR"] = "/tmp/test-legacy-dir"
    stderr_buf = io.StringIO()
    orig_stderr = sys.stderr
    sys.stderr = stderr_buf
    result = mod._memory_dir_env()
    sys.stderr = orig_stderr
    deprecation_warned = "DEPRECATION" in stderr_buf.getvalue()
    if result == "/tmp/test-legacy-dir" and deprecation_warned:
        ok("T4: _memory_dir_env() falls back to SUTANDO_PRIVATE_DIR with deprecation warning")
    else:
        fail("T4: legacy fallback + deprecation", f"result={result!r} warned={deprecation_warned}")
finally:
    sys.stderr = orig_stderr if 'orig_stderr' in dir() else sys.stderr
    if orig_new is None:
        os.environ.pop("SUTANDO_MEMORY_DIR", None)
    else:
        os.environ["SUTANDO_MEMORY_DIR"] = orig_new
    if orig_old is None:
        os.environ.pop("SUTANDO_PRIVATE_DIR", None)
    else:
        os.environ["SUTANDO_PRIVATE_DIR"] = orig_old

# ---------------------------------------------------------------------------
# T5 — _memory_dir_env() returns None when neither env var is set
# ---------------------------------------------------------------------------
orig_new = os.environ.get("SUTANDO_MEMORY_DIR")
orig_old = os.environ.get("SUTANDO_PRIVATE_DIR")
try:
    os.environ.pop("SUTANDO_MEMORY_DIR", None)
    os.environ.pop("SUTANDO_PRIVATE_DIR", None)
    result = mod._memory_dir_env()
    if result is None:
        ok("T5: _memory_dir_env() → None when neither env var is set")
    else:
        fail("T5: None when no env var", f"got={result!r}")
finally:
    if orig_new is not None:
        os.environ["SUTANDO_MEMORY_DIR"] = orig_new
    if orig_old is not None:
        os.environ["SUTANDO_PRIVATE_DIR"] = orig_old

# ---------------------------------------------------------------------------
# T6 — personal_path() returns workspace/file when SUTANDO_MEMORY_DIR not set + file absent
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "workspace"
    ws.mkdir()
    orig_new = os.environ.get("SUTANDO_MEMORY_DIR")
    orig_old = os.environ.get("SUTANDO_PRIVATE_DIR")
    try:
        os.environ.pop("SUTANDO_MEMORY_DIR", None)
        os.environ.pop("SUTANDO_PRIVATE_DIR", None)
        result = mod.personal_path("stand-identity.json", workspace=ws)
        if result == ws / "stand-identity.json":
            ok("T6: personal_path() → workspace/file when memory_dir unset + file absent")
        else:
            fail("T6: workspace fallback path", f"got={result}")
    finally:
        if orig_new is not None:
            os.environ["SUTANDO_MEMORY_DIR"] = orig_new
        if orig_old is not None:
            os.environ["SUTANDO_PRIVATE_DIR"] = orig_old

# ---------------------------------------------------------------------------
# T7 — personal_path() returns private machine-dir path when file exists there
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "workspace"
    ws.mkdir()
    mem_dir = Path(td) / "memory"
    mem_dir.mkdir()
    import socket
    host = socket.gethostname().split(".")[0]
    machine_dir = mem_dir / f"machine-{host}"
    machine_dir.mkdir()
    # Place the file in the private dir
    private_file = machine_dir / "stand-identity.json"
    private_file.write_text('{"machine": "test"}')

    orig_new = os.environ.get("SUTANDO_MEMORY_DIR")
    orig_old = os.environ.get("SUTANDO_PRIVATE_DIR")
    try:
        os.environ["SUTANDO_MEMORY_DIR"] = str(mem_dir)
        os.environ.pop("SUTANDO_PRIVATE_DIR", None)
        result = mod.personal_path("stand-identity.json", workspace=ws)
        if result == private_file:
            ok(f"T7: personal_path() → private machine-dir when file exists there")
        else:
            fail("T7: private machine-dir path", f"got={result} expected={private_file}")
    finally:
        if orig_new is None:
            os.environ.pop("SUTANDO_MEMORY_DIR", None)
        else:
            os.environ["SUTANDO_MEMORY_DIR"] = orig_new
        if orig_old is not None:
            os.environ["SUTANDO_PRIVATE_DIR"] = orig_old

# ---------------------------------------------------------------------------
# T8 — personal_path() falls through to workspace when private dir doesn't have file
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "workspace"
    ws.mkdir()
    mem_dir = Path(td) / "memory"
    mem_dir.mkdir()
    host = socket.gethostname().split(".")[0]
    machine_dir = mem_dir / f"machine-{host}"
    machine_dir.mkdir()
    # File exists in workspace only, not in private dir
    ws_file = ws / "stand-identity.json"
    ws_file.write_text('{"machine": "workspace"}')

    orig_new = os.environ.get("SUTANDO_MEMORY_DIR")
    orig_old = os.environ.get("SUTANDO_PRIVATE_DIR")
    try:
        os.environ["SUTANDO_MEMORY_DIR"] = str(mem_dir)
        os.environ.pop("SUTANDO_PRIVATE_DIR", None)
        result = mod.personal_path("stand-identity.json", workspace=ws)
        if result == ws_file:
            ok("T8: personal_path() falls through to workspace when private dir lacks file")
        else:
            fail("T8: workspace fallthrough", f"got={result} expected={ws_file}")
    finally:
        if orig_new is None:
            os.environ.pop("SUTANDO_MEMORY_DIR", None)
        else:
            os.environ["SUTANDO_MEMORY_DIR"] = orig_new
        if orig_old is not None:
            os.environ["SUTANDO_PRIVATE_DIR"] = orig_old

# ---------------------------------------------------------------------------
# T9 — personal_path() checks assets/ subfolder for stand-avatar.png
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "workspace"
    ws.mkdir()
    assets_dir = ws / "assets"
    assets_dir.mkdir()
    avatar = assets_dir / "stand-avatar.png"
    avatar.write_bytes(b"\x89PNG")  # minimal PNG header

    orig_new = os.environ.get("SUTANDO_MEMORY_DIR")
    orig_old = os.environ.get("SUTANDO_PRIVATE_DIR")
    try:
        os.environ.pop("SUTANDO_MEMORY_DIR", None)
        os.environ.pop("SUTANDO_PRIVATE_DIR", None)
        result = mod.personal_path("stand-avatar.png", workspace=ws)
        if result == avatar:
            ok("T9: personal_path() checks assets/ subfolder for stand-avatar.png")
        else:
            fail("T9: avatar assets/ path", f"got={result} expected={avatar}")
    finally:
        if orig_new is not None:
            os.environ["SUTANDO_MEMORY_DIR"] = orig_new
        if orig_old is not None:
            os.environ["SUTANDO_PRIVATE_DIR"] = orig_old

# ---------------------------------------------------------------------------
# T10 — personal_path() returns preferred (private) path when nothing exists
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "workspace"
    ws.mkdir()
    mem_dir = Path(td) / "memory"
    mem_dir.mkdir()
    host = socket.gethostname().split(".")[0]
    expected_private = mem_dir / f"machine-{host}" / "stand-identity.json"

    orig_new = os.environ.get("SUTANDO_MEMORY_DIR")
    orig_old = os.environ.get("SUTANDO_PRIVATE_DIR")
    try:
        os.environ["SUTANDO_MEMORY_DIR"] = str(mem_dir)
        os.environ.pop("SUTANDO_PRIVATE_DIR", None)
        result = mod.personal_path("stand-identity.json", workspace=ws)
        if result == expected_private:
            ok("T10: personal_path() returns preferred private path when nothing exists")
        else:
            fail("T10: preferred path when nothing exists", f"got={result} expected={expected_private}")
    finally:
        if orig_new is None:
            os.environ.pop("SUTANDO_MEMORY_DIR", None)
        else:
            os.environ["SUTANDO_MEMORY_DIR"] = orig_new
        if orig_old is not None:
            os.environ["SUTANDO_PRIVATE_DIR"] = orig_old

# ---------------------------------------------------------------------------
# T11 — shared_personal_path() returns memory_dir/file when it exists
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "workspace"
    ws.mkdir()
    mem_dir = Path(td) / "memory"
    mem_dir.mkdir()
    shared_file = mem_dir / "notes"
    shared_file.mkdir()
    # shared_personal_path resolves to top-level mem_dir/<filename>
    note_file = mem_dir / "build_log.md"
    note_file.write_text("# Build log")

    orig_new = os.environ.get("SUTANDO_MEMORY_DIR")
    orig_old = os.environ.get("SUTANDO_PRIVATE_DIR")
    try:
        os.environ["SUTANDO_MEMORY_DIR"] = str(mem_dir)
        os.environ.pop("SUTANDO_PRIVATE_DIR", None)
        result = mod.shared_personal_path("build_log.md", workspace=ws)
        if result == note_file:
            ok("T11: shared_personal_path() → memory_dir/file when it exists")
        else:
            fail("T11: shared path returns memory_dir file", f"got={result} expected={note_file}")
    finally:
        if orig_new is None:
            os.environ.pop("SUTANDO_MEMORY_DIR", None)
        else:
            os.environ["SUTANDO_MEMORY_DIR"] = orig_new
        if orig_old is not None:
            os.environ["SUTANDO_PRIVATE_DIR"] = orig_old

# ---------------------------------------------------------------------------
# T12 — shared_personal_path() falls back to workspace when private file absent
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "workspace"
    ws.mkdir()
    mem_dir = Path(td) / "memory"
    mem_dir.mkdir()
    # File is in workspace but NOT in mem_dir
    ws_file = ws / "build_log.md"
    ws_file.write_text("# Build log")

    orig_new = os.environ.get("SUTANDO_MEMORY_DIR")
    orig_old = os.environ.get("SUTANDO_PRIVATE_DIR")
    try:
        os.environ["SUTANDO_MEMORY_DIR"] = str(mem_dir)
        os.environ.pop("SUTANDO_PRIVATE_DIR", None)
        result = mod.shared_personal_path("build_log.md", workspace=ws)
        if result == ws_file:
            ok("T12: shared_personal_path() falls back to workspace when private file absent")
        else:
            fail("T12: workspace fallback for shared path", f"got={result} expected={ws_file}")
    finally:
        if orig_new is None:
            os.environ.pop("SUTANDO_MEMORY_DIR", None)
        else:
            os.environ["SUTANDO_MEMORY_DIR"] = orig_new
        if orig_old is not None:
            os.environ["SUTANDO_PRIVATE_DIR"] = orig_old

# ---------------------------------------------------------------------------
# T13 — shared_personal_path() returns workspace path when SUTANDO_MEMORY_DIR not set
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "workspace"
    ws.mkdir()
    orig_new = os.environ.get("SUTANDO_MEMORY_DIR")
    orig_old = os.environ.get("SUTANDO_PRIVATE_DIR")
    try:
        os.environ.pop("SUTANDO_MEMORY_DIR", None)
        os.environ.pop("SUTANDO_PRIVATE_DIR", None)
        result = mod.shared_personal_path("build_log.md", workspace=ws)
        if result == ws / "build_log.md":
            ok("T13: shared_personal_path() → workspace/file when SUTANDO_MEMORY_DIR unset")
        else:
            fail("T13: workspace path when no env", f"got={result}")
    finally:
        if orig_new is not None:
            os.environ["SUTANDO_MEMORY_DIR"] = orig_new
        if orig_old is not None:
            os.environ["SUTANDO_PRIVATE_DIR"] = orig_old

# ---------------------------------------------------------------------------
# T14 — claude_home_path() returns ~/.claude by default
# ---------------------------------------------------------------------------
orig_claude = os.environ.get("CLAUDE_HOME")
try:
    os.environ.pop("CLAUDE_HOME", None)
    result = mod.claude_home_path()
    expected = Path.home() / ".claude"
    if result == expected:
        ok(f"T14: claude_home_path() → ~/.claude by default")
    else:
        fail("T14: ~/.claude default", f"got={result}")
finally:
    if orig_claude is not None:
        os.environ["CLAUDE_HOME"] = orig_claude

# ---------------------------------------------------------------------------
# T15 — claude_home_path(*subpath) joins subpath; respects $CLAUDE_HOME override
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    orig_claude = os.environ.get("CLAUDE_HOME")
    try:
        os.environ["CLAUDE_HOME"] = td
        result = mod.claude_home_path("channels", "discord", "access.json")
        expected = Path(td) / "channels" / "discord" / "access.json"
        if result == expected:
            ok("T15: claude_home_path(*subpath) joins correctly; respects $CLAUDE_HOME override")
        else:
            fail("T15: subpath join + CLAUDE_HOME override", f"got={result} expected={expected}")
    finally:
        if orig_claude is None:
            os.environ.pop("CLAUDE_HOME", None)
        else:
            os.environ["CLAUDE_HOME"] = orig_claude

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{PASS}/{PASS+FAIL} tests passed")
if __name__ == "__main__":
    pass
