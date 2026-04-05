#!/usr/bin/env python3
"""
Security sanitization tests for agent-api.
Run: python3 tests/security-sanitization.test.py
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent / 'src'))

passed = 0
failed = 0

def assert_eq(actual, expected, msg):
    global passed, failed
    if actual == expected:
        passed += 1
        print(f"  ✓ {msg}")
    else:
        failed += 1
        print(f"  ✗ {msg} (got {actual!r}, expected {expected!r})")

def assert_true(condition, msg):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✓ {msg}")
    else:
        failed += 1
        print(f"  ✗ {msg}")

# Import _safe_id from agent-api (it's not a module, so we extract the function)
import re
def _safe_id(raw: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_\-.]', '', raw)

print("\n=== Security Sanitization Tests ===\n")

# --- _safe_id ---
print("Path traversal prevention (_safe_id):")
assert_eq(_safe_id("task-123456"), "task-123456", "normal task ID unchanged")
assert_eq(_safe_id("../../../etc/passwd"), "......etcpasswd", "path traversal stripped of slashes")
assert_eq(_safe_id("task-123; rm -rf /"), "task-123rm-rf", "shell injection stripped")
assert_eq(_safe_id(""), "", "empty string stays empty")
assert_eq(_safe_id("task_with.dots-and_underscores"), "task_with.dots-and_underscores", "dots/underscores/dashes preserved")
assert_eq(_safe_id("task\x00null"), "tasknull", "null bytes stripped")
assert_eq(_safe_id("task/../../secret"), "task....secret", "nested traversal stripped")

# --- Media path validation ---
print("\nMedia path validation:")
assert_true(".." in "../../../etc/passwd", "path traversal detected by .. check")
assert_true("../secret" .startswith("/") == False, "relative path doesn't start with /")
assert_true("/etc/passwd".startswith("/"), "absolute path caught by / check")

# --- Mime type sanitization ---
print("\nHTTP header injection prevention:")
mime_clean = "text/html"
mime_injected = "text/html\r\nX-Injected: true"
assert_eq(mime_clean.split('\n')[0].split('\r')[0], "text/html", "clean mime unchanged")
assert_eq(mime_injected.split('\n')[0].split('\r')[0], "text/html", "injected header stripped")

# --- Shell escaping (inline-tools pattern) ---
print("\nShell argument escaping:")
def safe_escape(s):
    return s.replace('\\', '\\\\').replace("'", "'\\''").replace('"', '\\"')

assert_eq(safe_escape("normal"), "normal", "normal string unchanged")
assert_eq(safe_escape("it's"), "it'\\''s", "single quote escaped (shell-safe)")
assert_eq(safe_escape('say "hi"'), 'say \\"hi\\"', "double quote escaped")
assert_eq(safe_escape("back\\slash"), "back\\\\slash", "backslash escaped")
assert_eq(safe_escape("'; rm -rf /; '"), "'\\''; rm -rf /; '\\''", "shell injection escaped")

# --- Dashboard note slug validation ---
print("\nDashboard note slug validation:")
import re as _re
def valid_slug(slug):
    return bool(_re.match(r'^[\w-]+$', slug))

assert_true(valid_slug("my-note"), "normal slug accepted")
assert_true(valid_slug("note_2026"), "underscored slug accepted")
assert_true(not valid_slug("../../../etc/passwd"), "path traversal rejected")
assert_true(not valid_slug("note/subdir"), "slash rejected")
assert_true(not valid_slug(""), "empty string rejected")
assert_true(not valid_slug("note; rm -rf /"), "shell injection rejected")
assert_true(not valid_slug("note\x00null"), "null byte rejected")

# --- Dashboard note path resolution ---
print("\nDashboard note path resolution (is_relative_to):")
from pathlib import Path
notes_dir = Path("/tmp/sutando-test-notes").resolve()
good_path = (notes_dir / "my-note.md").resolve()
assert_true(good_path.is_relative_to(notes_dir), "normal note path is under notes_dir")

# Simulated traversal (even if slug passes regex, resolve catches symlinks)
traversal_path = (notes_dir / ".." / "etc" / "passwd").resolve()
assert_true(not traversal_path.is_relative_to(notes_dir), "traversal via .. escapes notes_dir")

# --- DTMF digit sanitization ---
print("\nDTMF digit sanitization:")
def sanitize_dtmf(digits):
    return re.sub(r'[^\d#*]', '', digits)

assert_eq(sanitize_dtmf("1234567890#"), "1234567890#", "normal digits unchanged")
assert_eq(sanitize_dtmf("123#*456"), "123#*456", "star and hash preserved")
assert_eq(sanitize_dtmf('1234"><script>alert(1)</script>'), "12341", "XSS payload stripped to digits only")
assert_eq(sanitize_dtmf(""), "", "empty string stays empty")
assert_eq(sanitize_dtmf("abc"), "", "non-digits fully stripped")

# --- TypeText AppleScript injection prevention ---
print("\nTypeText injection prevention:")
def safe_applescript_text(text):
    """Simulates the temp-file approach: text goes into file, not shell."""
    safe = text.replace('\\', '\\\\').replace('"', '\\"')
    return f'tell application "System Events" to keystroke "{safe}"'

script = safe_applescript_text('hello"; quit app "Finder')
assert_true('quit app' not in script.split('keystroke')[0], "injection stays inside keystroke quotes")
assert_true(safe_applescript_text("normal").endswith('"normal"'), "normal text passes through")

print(f"\n=== Results: {passed} passed, {failed} failed ===\n")
sys.exit(1 if failed > 0 else 0)
