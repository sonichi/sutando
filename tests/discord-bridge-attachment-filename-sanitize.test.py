#!/usr/bin/env python3
"""Security regression guard for Discord attachment filename sanitization.

`src/discord-bridge.py` saves Discord attachments to /tmp/discord-inbox/
with the filename interpolated raw:

    local_path = INBOX_DIR / f"{int(time.time()*1000)}_{att.filename}"

Discord lets users upload arbitrary filenames including spaces, quotes,
semicolons, backticks, and `$`. Several downstream sites then glob
`/tmp/discord-inbox/*` and embed the resulting path in a shell command,
e.g. `skills/phone-conversation/scripts/conversation-server.ts`
(pre-fix):

    execSync(`bash .../prepend-image.sh "${image}" "${video}" 3`)

A filename like `x"; touch /tmp/pwn; #.jpg` would close the quoted shell
argument and execute attacker-supplied commands when the owner triggers
the concat fast path during a phone call. RCE via Discord attachment
filename.

The fix is two layers:
  1. `_safe_attachment_basename()` sanitizes filenames at the save site
     (defense at the boundary — pinned by this test).
  2. conversation-server.ts switches the fast path from execSync to
     execFileSync (defense at the use — pinned by a separate .ts test).
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_WORKSPACE_TMP = tempfile.mkdtemp(prefix="sutando-discord-filename-test-")
os.environ["SUTANDO_WORKSPACE"] = _WORKSPACE_TMP
# `discord-bridge.py` reads its token from `~/.claude/channels/discord/.env`
# (file path), not from `os.environ["DISCORD_BOT_TOKEN"]`, and `exit(1)`s
# at module load when the file is missing. CI has no such file. Stub the
# home dir + write a fake token so the module loads cleanly without
# requiring a real bot credential in CI.
_HOME_TMP = tempfile.mkdtemp(prefix="sutando-discord-filename-test-home-")
os.environ["HOME"] = _HOME_TMP
_token_dir = Path(_HOME_TMP) / ".claude" / "channels" / "discord"
_token_dir.mkdir(parents=True, exist_ok=True)
(_token_dir / ".env").write_text("DISCORD_BOT_TOKEN=test-token-not-real\n")
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")


def _load(name: str, path: Path):
    if "discord" not in sys.modules:
        import types
        stub = types.ModuleType("discord")
        stub.Intents = type("Intents", (), {"default": staticmethod(lambda: type("I", (), {"message_content": False})())})
        stub.Client = type("Client", (), {"__init__": lambda self, **kw: None, "event": staticmethod(lambda fn: fn)})
        stub.File = type("File", (), {})
        sys.modules["discord"] = stub
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


bridge = _load("discord_bridge", REPO / "src" / "discord-bridge.py")
sanitize = bridge._safe_attachment_basename


def test_normal_filename_preserved():
    assert sanitize("photo.jpg") == "photo.jpg"
    assert sanitize("Screenshot 2026-05-22.png") == "Screenshot_2026-05-22.png"
    assert sanitize("report-final-v2.pdf") == "report-final-v2.pdf"


def test_shell_injection_strip():
    """The core bug fix — characters that break out of quoted shell
    arguments must be stripped."""
    result = sanitize('x"; touch /tmp/pwn; #.jpg')
    assert '"' not in result, f"double quote survived: {result!r}"
    assert ';' not in result, f"semicolon survived: {result!r}"
    assert '#' not in result, f"hash survived: {result!r}"
    assert result.endswith(".jpg"), f"extension lost: {result!r}"


def test_backtick_and_dollar_strip():
    result = sanitize("evil`whoami`.png")
    assert '`' not in result
    result = sanitize("evil$IFS.png")
    assert '$' not in result


def test_path_traversal_strip():
    result = sanitize("../../../etc/passwd")
    assert '/' not in result, f"slash survived: {result!r}"
    assert '..' not in result, f"dotdot survived: {result!r}"


def test_empty_filename_falls_back():
    assert sanitize("") != ""
    assert sanitize(";;;;") != ""
    assert sanitize("...") != ""


def test_extension_preserved_when_short():
    for ext in ("jpg", "png", "pdf", "mp4", "mov", "txt", "md"):
        result = sanitize(f"file.{ext}")
        assert result.endswith(f".{ext}"), f"extension .{ext} not preserved: {result!r}"


def test_unicode_replaced():
    result = sanitize("файл.jpg")
    assert all(ord(c) < 128 for c in result), f"non-ASCII survived: {result!r}"
    assert result.endswith(".jpg")


def test_overlong_filename_capped():
    result = sanitize("a" * 10000 + ".jpg")
    assert len(result) <= 100, f"length not capped: len={len(result)}"


def main():
    failures = []
    for fn in (
        test_normal_filename_preserved,
        test_shell_injection_strip,
        test_backtick_and_dollar_strip,
        test_path_traversal_strip,
        test_empty_filename_falls_back,
        test_extension_preserved_when_short,
        test_unicode_replaced,
        test_overlong_filename_capped,
    ):
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"  ✗ {fn.__name__}")
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print(f"All {8} attachment-filename-sanitize tests passed.")


if __name__ == "__main__":
    main()
