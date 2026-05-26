#!/usr/bin/env python3
"""Security-relevant tests for `_is_path_sendable` in src/discord-bridge.py.

`_is_path_sendable` is the gate between an agent-emitted `[file: /path]`
marker and `await channel.send(file=discord.File(fpath))`. If it's
permissive, an agent that's been prompt-injected — or a confused result
file — can exfiltrate arbitrary files (SSH keys, .env, browser cookies)
to Discord. PR #494 added the allowlist; PR #496 hardened it; this
test pins both improvements so a future refactor doesn't regress them
into a path-traversal/symlink-escape primitive.

Mirrors tests/discord-chunker.test.py conventions.
"""

import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

try:
    import discord  # noqa: F401
except ImportError:
    stub = types.ModuleType("discord")
    stub.Intents = type(
        "Intents",
        (),
        {"default": staticmethod(lambda: type("I", (), {"message_content": False})())},
    )
    stub.Client = type(
        "Client",
        (),
        {"__init__": lambda self, **kw: None, "event": staticmethod(lambda fn: fn)},
    )
    stub.File = type("File", (), {})
    stub.Message = type("Message", (), {})
    sys.modules["discord"] = stub

_channels_env = Path.home() / ".claude" / "channels" / "discord" / ".env"
if not _channels_env.exists():
    _channels_env.parent.mkdir(parents=True, exist_ok=True)
    _channels_env.write_text("DISCORD_BOT_TOKEN=test-token-not-real\n")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


bridge = _load("dbridge", REPO / "src" / "discord-bridge.py")
is_sendable = bridge._is_path_sendable


def _make_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    return path


def test_nonexistent_path_is_not_sendable():
    """Fail-closed on missing files. Pre-check before any allowlist match,
    so an attacker can't manipulate a not-yet-existing path."""
    assert not is_sendable("/tmp/sutando-does-not-exist-12345.png")


def test_directory_is_not_sendable():
    """`os.path.isfile` is the type gate — directories must reject, even
    if they sit under an allowed root. Otherwise `discord.File(dir)`
    fails downstream with a confusing error."""
    tmp = Path(tempfile.mkdtemp(prefix="/tmp/sutando-test-dir-"))
    try:
        assert not is_sendable(str(tmp))
    finally:
        tmp.rmdir()


def test_allowed_prefix_match():
    """Files under `/tmp/sutando-*` are explicitly allowed. This is the
    standard path for screenshots, generated assets, etc."""
    with tempfile.NamedTemporaryFile(
        prefix="sutando-test-", suffix=".png", dir="/tmp", delete=False
    ) as f:
        f.write(b"x")
        path = f.name
    try:
        assert is_sendable(path), f"expected allowed prefix to match {path}"
    finally:
        os.unlink(path)


def test_disallowed_prefix_rejected():
    """Files under `/tmp/other-*` are NOT in `SEND_ALLOWED_PREFIXES`.
    Allowed prefixes are a deliberate small set; anything else fails."""
    with tempfile.NamedTemporaryFile(
        prefix="other-not-sutando-", suffix=".txt", dir="/tmp", delete=False
    ) as f:
        f.write(b"x")
        path = f.name
    try:
        assert not is_sendable(path), f"expected reject for {path}"
    finally:
        os.unlink(path)


def test_arbitrary_path_rejected():
    """Generic existing file (e.g., `/etc/hosts`) must reject. Files
    outside the allowlist are the attacker's exfil target — verify the
    fail-closed default."""
    # /etc/hosts exists on every macOS / Linux system. NOT under any
    # SEND_ALLOWED_ROOTS / SEND_ALLOWED_PREFIXES.
    assert not is_sendable("/etc/hosts")


def test_symlink_pointing_outside_allowed_root_rejected():
    """Path-injection guard: an attacker who can write a symlink under an
    allowed root must not be able to use it to exfil files outside.
    `_is_path_sendable` calls `realpath` before the prefix comparison
    precisely to defeat this. Regression guard for the original allowlist
    motivation (PR #494)."""
    # Create a real file outside the allowlist
    target = Path("/tmp/sutando-symlink-target-outside.txt")
    target.write_text("secret")
    # Create a symlink under an allowed prefix pointing at it
    link = Path("/tmp/sutando-symlink-pointer.txt")
    if link.exists() or link.is_symlink():
        link.unlink()
    # Wait — the link itself starts with `/tmp/sutando-` which IS an
    # allowed prefix. The fix is `realpath`-then-compare. Make the link
    # point at a file outside the allowed prefixes:
    outside = Path("/tmp/escaped-not-sutando.txt")
    outside.write_text("would be exfil")
    try:
        os.symlink(outside, link)
        assert not is_sendable(str(link)), (
            f"symlink {link} → {outside} bypassed the allowlist — "
            "realpath collapse is broken"
        )
    finally:
        if link.exists() or link.is_symlink():
            link.unlink()
        outside.unlink()
        if target.exists():
            target.unlink()


def test_path_traversal_dotdot_rejected():
    """`/tmp/sutando-../etc/passwd` evaluates by `realpath` to
    `/etc/passwd`, which fails the allowlist. Guards the same
    path-injection class as the symlink test, just via the textual
    `..` segment instead of a symlink."""
    # The realpath of /tmp/sutando-X/../passwd is /tmp/passwd. So we just
    # check that a `..` traversal that lands somewhere not on the
    # allowlist is rejected:
    rogue_target = Path("/tmp/passwd-not-sutando-no-write")
    if not rogue_target.exists():
        rogue_target.write_text("not actual passwd")
    try:
        traversal = f"/tmp/sutando-x/../passwd-not-sutando-no-write"
        # Even though the textual path STARTS with /tmp/sutando-,
        # realpath collapses to /tmp/passwd-not-sutando-no-write, which
        # does NOT start with /tmp/sutando-.
        assert not is_sendable(traversal), (
            "path traversal via .. bypassed the allowlist — "
            "realpath collapse is broken"
        )
    finally:
        if rogue_target.exists():
            rogue_target.unlink()


def test_allowed_prefixes_are_the_documented_set():
    """Architectural assertion: the allowed prefixes must stay a small,
    deliberate set. A future refactor that adds `/tmp/` (no suffix) or
    `/Users/` would massively widen the attack surface; this test
    forces a deliberate update of the test if the allowlist grows."""
    documented = {
        "/tmp/sutando-",
        "/private/tmp/sutando-",
        "/tmp/echo-",
        "/private/tmp/echo-",
    }
    actual = set(bridge.SEND_ALLOWED_PREFIXES)
    assert actual == documented, (
        f"SEND_ALLOWED_PREFIXES has changed unexpectedly. "
        f"Removed: {documented - actual}, Added: {actual - documented}. "
        "Update this test deliberately to confirm the new exposure is intended."
    )


def main():
    test_nonexistent_path_is_not_sendable()
    test_directory_is_not_sendable()
    test_allowed_prefix_match()
    test_disallowed_prefix_rejected()
    test_arbitrary_path_rejected()
    test_symlink_pointing_outside_allowed_root_rejected()
    test_path_traversal_dotdot_rejected()
    test_allowed_prefixes_are_the_documented_set()
    print("All _is_path_sendable security tests passed.")


if __name__ == "__main__":
    main()
