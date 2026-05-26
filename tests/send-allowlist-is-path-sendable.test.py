#!/usr/bin/env python3
"""Behavioral tests for `src/send_allowlist.py::is_path_sendable`.

The companion `send-allowlist-shared.test.py` covers the architectural
invariants (module exists, the right imports are present, constants are
documented). This suite exercises the *runtime behavior* of
`is_path_sendable()` — what it actually allows and blocks.

Security-critical properties guarded here:
  - Symlink-traversal: a symlink whose *name* starts with an allowed
    prefix but whose realpath resolves outside the allowlist is rejected.
  - Directory rejection: directories are never sendable (callers must
    receive regular files only).
  - Fail-closed default: anything not explicitly allowed returns False.
  - No path-traversal bypass: `..` segments in the path are neutralised
    by realpath before the allowlist check.
  - Root-prefix false-match guard: `/tmp/sutando` (a dir) does NOT
    qualify as a prefix match for `/tmp/sutando-recording-xyz.mov` —
    the `-` in the recorded prefix is load-bearing.

Run: python3 tests/send-allowlist-is-path-sendable.test.py
Exit: 0 on pass, non-zero on fail.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SRC = REPO / "src"

# -----------------------------------------------------------------------
# Module loader — import fresh with a temp workspace so the module-level
# SEND_ALLOWED_ROOTS computation doesn't reference live workspace data.
# -----------------------------------------------------------------------
_WORKSPACE = tempfile.mkdtemp(prefix="sutando-test-ws-")
os.environ["SUTANDO_WORKSPACE"] = _WORKSPACE
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Remove any previously cached copies so exec_module re-runs the
# module-level code with the env we just set.
for _mod_name in ("send_allowlist", "workspace_default", "util_paths"):
    sys.modules.pop(_mod_name, None)

_spec = importlib.util.spec_from_file_location("send_allowlist", _SRC / "send_allowlist.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

is_path_sendable = _mod.is_path_sendable

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

PASS = 0
FAIL = 0


def ok(label: str) -> None:
    global PASS
    PASS += 1
    print(f"PASS  {label}")


def fail(label: str, detail: str = "") -> None:
    global FAIL
    FAIL += 1
    msg = f"FAIL  {label}"
    if detail:
        msg += f": {detail}"
    print(msg)


def _tmp_file(prefix: str = "sutando-test-", suffix: str = ".txt", dir: str = "/tmp") -> str:
    """Create a real regular file and return its path."""
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=dir)
    os.close(fd)
    return path


def _with_patched_roots(roots: tuple, fn):
    """Run fn() with SEND_ALLOWED_ROOTS temporarily replaced."""
    orig = _mod.SEND_ALLOWED_ROOTS
    _mod.SEND_ALLOWED_ROOTS = roots
    try:
        return fn()
    finally:
        _mod.SEND_ALLOWED_ROOTS = orig


# -----------------------------------------------------------------------
# T1 — rejects nonexistent path (fail-closed)
# -----------------------------------------------------------------------
if not is_path_sendable("/nonexistent/path/file.txt"):
    ok("T1: nonexistent path returns False")
else:
    fail("T1: nonexistent path returns False", "got True for a path that doesn't exist")

# -----------------------------------------------------------------------
# T2 — rejects empty string (fail-closed)
# -----------------------------------------------------------------------
if not is_path_sendable(""):
    ok("T2: empty string returns False")
else:
    fail("T2: empty string returns False", "got True for empty string")

# -----------------------------------------------------------------------
# T3 — rejects a directory even if named with allowed prefix
# -----------------------------------------------------------------------
with tempfile.TemporaryDirectory(prefix="sutando-test-dir-", dir="/tmp") as d:
    if not is_path_sendable(d):
        ok("T3: directory returns False even with sutando- name")
    else:
        fail("T3: directory returns False", f"directory {d} was accepted")

# -----------------------------------------------------------------------
# T4 — accepts file whose realpath starts with /tmp/sutando-
# -----------------------------------------------------------------------
_f = _tmp_file(prefix="sutando-recording-", suffix=".mov")
try:
    if is_path_sendable(_f):
        ok("T4: /tmp/sutando-*.mov accepted (prefix match)")
    else:
        fail("T4: /tmp/sutando-*.mov accepted", f"realpath={os.path.realpath(_f)}")
finally:
    os.unlink(_f)

# -----------------------------------------------------------------------
# T5 — accepts file whose realpath starts with /tmp/echo-
# -----------------------------------------------------------------------
_f = _tmp_file(prefix="echo-screenshot-", suffix=".png")
try:
    if is_path_sendable(_f):
        ok("T5: /tmp/echo-*.png accepted (prefix match)")
    else:
        fail("T5: /tmp/echo-*.png accepted", f"realpath={os.path.realpath(_f)}")
finally:
    os.unlink(_f)

# -----------------------------------------------------------------------
# T6 — rejects /tmp file whose name does NOT match any allowed prefix
# -----------------------------------------------------------------------
_f = _tmp_file(prefix="notallowed-xyz-", suffix=".txt")
try:
    if not is_path_sendable(_f):
        ok("T6: /tmp/notallowed-* rejected (no prefix match)")
    else:
        fail("T6: /tmp/notallowed-* rejected", f"file {_f} was unexpectedly accepted")
finally:
    os.unlink(_f)

# -----------------------------------------------------------------------
# T7 — symlink traversal: name starts with sutando- but realpath escapes
#
# A symlink at /tmp/sutando-evil.txt → /etc/passwd.
# realpath resolves to /etc/passwd which does NOT start with /tmp/sutando-
# or /private/tmp/sutando-, so is_path_sendable must return False.
# -----------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    link = Path(td) / "sutando-evil.txt"
    link.symlink_to("/etc/passwd")
    # The symlink is in a non-prefix dir, so it's rejected for two reasons:
    # (a) realpath = /etc/passwd — not in prefix, (b) not under any root.
    if not is_path_sendable(str(link)):
        ok("T7: symlink→/etc/passwd rejected via realpath collapse")
    else:
        fail("T7: symlink→/etc/passwd rejected", "symlink to /etc/passwd was accepted")

# -----------------------------------------------------------------------
# T8 — symlink FROM /tmp/sutando-* → outside allowed area rejected
#
# More targeted: the symlink's name is in the allowed prefix namespace
# (/tmp/sutando-*), but the realpath resolves outside the allowlist.
# This is the direct attack vector: name the symlink sutando-*, hope the
# check only inspects the name.
# -----------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    evil_target = Path(td) / "secret.txt"
    evil_target.write_text("secret")
    # Symlink inside /tmp at a sutando- prefix name
    fd, link_path = tempfile.mkstemp(prefix="sutando-", suffix=".txt", dir="/tmp")
    os.close(fd)
    os.unlink(link_path)
    try:
        os.symlink(str(evil_target), link_path)
        real = os.path.realpath(link_path)
        # realpath → td/secret.txt which is outside all allowed prefixes/roots
        if not is_path_sendable(link_path):
            ok("T8: /tmp/sutando-<link>→outside accepted only by name — realpath defeats it")
        else:
            fail("T8: realpath-defeats-prefix-name-symlink", f"realpath={real}, was accepted")
    finally:
        try:
            os.unlink(link_path)
        except OSError:
            pass

# -----------------------------------------------------------------------
# T9 — path traversal via `..` does NOT bypass the allowlist
#
# Construct a path like /tmp/sutando-<dir>/../../../etc/passwd.
# realpath collapses the `..` so the check sees /etc/passwd, not the
# sutando-* prefix path.
# -----------------------------------------------------------------------
with tempfile.TemporaryDirectory(prefix="sutando-traversal-", dir="/tmp") as td:
    traversal = td + "/../../../etc/passwd"
    real = os.path.realpath(traversal)
    if not is_path_sendable(traversal):
        ok(f"T9: path traversal ../../../etc/passwd rejected (realpath={real})")
    else:
        fail("T9: path traversal rejected", f"traversal to {real} was accepted")

# -----------------------------------------------------------------------
# T10 — root-based match: file directly in an allowed root → accepted
# -----------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    allowed_root = Path(td) / "results"
    allowed_root.mkdir()
    test_file = allowed_root / "task-12345.txt"
    test_file.write_text("result content")

    def _t10():
        return is_path_sendable(str(test_file))

    if _with_patched_roots((str(allowed_root),), _t10):
        ok("T10: file directly in allowed root accepted")
    else:
        fail("T10: file directly in allowed root accepted", f"file={test_file}")

# -----------------------------------------------------------------------
# T11 — root-based match: file in *subdirectory* of allowed root → accepted
# -----------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    allowed_root = Path(td) / "results"
    sub = allowed_root / "2026-05-26"
    sub.mkdir(parents=True)
    test_file = sub / "task-multi.txt"
    test_file.write_text("content")

    def _t11():
        return is_path_sendable(str(test_file))

    if _with_patched_roots((str(allowed_root),), _t11):
        ok("T11: file in subdirectory of allowed root accepted")
    else:
        fail("T11: subdirectory of allowed root accepted", f"file={test_file}")

# -----------------------------------------------------------------------
# T12 — root-based: the root directory itself is NOT a file → rejected
# -----------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    allowed_root = Path(td) / "results"
    allowed_root.mkdir()

    def _t12():
        return is_path_sendable(str(allowed_root))

    if not _with_patched_roots((str(allowed_root),), _t12):
        ok("T12: the root directory itself (not a file) is rejected")
    else:
        fail("T12: root directory rejected", "directory was accepted as sendable")

# -----------------------------------------------------------------------
# T13 — false-prefix guard: /tmp/sutando (no trailing `-`) does NOT
#        accidentally match /tmp/sutando-recording-xyz
#
# The SEND_ALLOWED_PREFIXES are /tmp/sutando- (WITH the dash). A bare
# /tmp/sutando directory must not grant access to its own name.
# -----------------------------------------------------------------------
with tempfile.TemporaryDirectory(prefix="notallowed-prefix-", dir="/tmp") as td:
    # File is in /tmp/notallowed-prefix-xxx/sutando-results.txt
    # realpath won't start with /tmp/sutando- or /private/tmp/sutando-
    test_file = Path(td) / "sutando-results.txt"
    test_file.write_text("data")
    if not is_path_sendable(str(test_file)):
        ok("T13: file in non-prefix /tmp subdir rejected (starts with 'sutando' but wrong dir)")
    else:
        fail("T13: prefix substring safety", f"file={test_file} real={os.path.realpath(test_file)}")

# -----------------------------------------------------------------------
# T14 — root-based: sibling directory is NOT included (trailing-sep check)
#
# Allowed root = /tmp/xxx/results — file in /tmp/xxx/results-extra/f.txt
# must NOT match (would be a false positive if only startswith() without
# the trailing-sep guard were used).
# -----------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    allowed_root = Path(td) / "results"
    allowed_root.mkdir()
    sibling = Path(td) / "results-extra"
    sibling.mkdir()
    sibling_file = sibling / "file.txt"
    sibling_file.write_text("data")

    def _t14():
        return is_path_sendable(str(sibling_file))

    if not _with_patched_roots((str(allowed_root),), _t14):
        ok("T14: sibling dir results-extra/ not matched by results/ root (trailing-sep guard)")
    else:
        fail("T14: sibling dir trailing-sep guard", f"sibling {sibling_file} was accepted under root {allowed_root}")

# -----------------------------------------------------------------------
# T15 — symlink within allowed root pointing to real file also in root → accepted
#
# Legitimate use-case: agent creates a symlink alias inside results/.
# The realpath of the symlink resolves to another file also under results/
# so the allowlist check should accept it.
# -----------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    allowed_root = Path(td) / "results"
    allowed_root.mkdir()
    real_file = allowed_root / "task-123.txt"
    real_file.write_text("result")
    link_file = allowed_root / "task-123-alias.txt"
    link_file.symlink_to(real_file)

    def _t15():
        return is_path_sendable(str(link_file))

    if _with_patched_roots((str(allowed_root),), _t15):
        ok("T15: symlink→file within same allowed root accepted")
    else:
        fail("T15: intra-root symlink accepted", f"symlink {link_file}→{real_file}")

# -----------------------------------------------------------------------
# T16 — symlink within allowed root pointing OUTSIDE root → rejected
# -----------------------------------------------------------------------
with tempfile.TemporaryDirectory() as outer, tempfile.TemporaryDirectory() as td:
    allowed_root = Path(td) / "results"
    allowed_root.mkdir()
    outside_file = Path(outer) / "secret.txt"
    outside_file.write_text("secret")
    evil_link = allowed_root / "escape-link.txt"
    evil_link.symlink_to(outside_file)

    def _t16():
        return is_path_sendable(str(evil_link))

    if not _with_patched_roots((str(allowed_root),), _t16):
        ok("T16: symlink within root→outside root rejected via realpath")
    else:
        fail("T16: intra-root escape-symlink rejected", f"link {evil_link}→{outside_file} was accepted")

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
print(f"\n{PASS}/{PASS + FAIL} tests passed")
if FAIL:
    sys.exit(1)
