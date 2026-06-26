#!/usr/bin/env python3
"""Unit tests for `_split_file_markers` in src/discord-bridge.py.

The `[file:|send:|attach:]` marker regex was duplicated inline at THREE
sites in `discord-bridge.py` — `poll_results` (channel replies),
`poll_proactive` (owner DMs), and the dm-fallback channel-redirect
path. Any future hardening to the pattern had to be applied three
times by hand. This file pins the behavior of the consolidated
`_split_file_markers` helper and the underlying `_FILE_MARKER_RE`
pattern so a future refactor that drifts one call site fails here
instead of in production.
"""

import importlib.util
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

try:
    import discord  # noqa: F401
except ImportError:
    stub = types.ModuleType("discord")
    stub.Intents = type("Intents", (), {"default": staticmethod(lambda: type("I", (), {"message_content": False})())})
    stub.Client = type("Client", (), {"__init__": lambda self, **kw: None, "event": staticmethod(lambda fn: fn)})
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
split = bridge._split_file_markers


def test_no_markers_returns_text_and_empty_list():
    """Common case: plain reply with no attachments."""
    clean, files = split("Hello world — no files here.")
    assert clean == "Hello world — no files here."
    assert files == []


def test_empty_input():
    """Empty body — both outputs must be safe for `if clean_text:` guards."""
    clean, files = split("")
    assert clean == ""
    assert files == []


def test_single_file_marker_extracted():
    """`[file: /path]` extracts the path and removes the marker."""
    clean, files = split("Here is the screenshot: [file: /tmp/sutando-x.png]")
    assert clean == "Here is the screenshot:"
    assert files == ["/tmp/sutando-x.png"]


def test_send_and_attach_aliases():
    """Three keywords share the same pattern — `file` / `send` / `attach`."""
    for keyword in ("file", "send", "attach"):
        clean, files = split(f"body [{keyword}: /tmp/sutando-x.png]")
        assert files == ["/tmp/sutando-x.png"], f"keyword={keyword} did not match"


def test_home_relative_path_matches():
    """`~/...` is a deliberate allowed form."""
    clean, files = split("body [file: ~/Downloads/report.pdf]")
    assert files == ["~/Downloads/report.pdf"]


def test_relative_path_does_not_match():
    """The pattern requires absolute paths (`/...` or `~/...`)."""
    for not_a_path in ("relative.txt", "./file.txt", "../escape.txt", "subdir/file.txt"):
        clean, files = split(f"body [file: {not_a_path}]")
        assert files == [], f"unexpectedly matched: {not_a_path!r}"


def test_multiple_markers_preserve_order():
    """Multiple markers in one body — extract in textual order."""
    clean, files = split(
        "first [file: /tmp/sutando-1.png] middle [send: /tmp/sutando-2.png] end"
    )
    assert files == ["/tmp/sutando-1.png", "/tmp/sutando-2.png"]


def test_colon_in_path_does_not_break_match():
    """`[reply: 12345]` (reply directive) doesn't match the file pattern."""
    clean, files = split("body [reply: 12345678901234567890]")
    assert files == []


def test_unknown_keyword_does_not_match():
    """Only `file`, `send`, `attach` are recognized."""
    for bad in ("path", "url", "attachment", "file2"):
        clean, files = split(f"body [{bad}: /tmp/sutando-x.png]")
        assert files == [], f"keyword {bad!r} unexpectedly matched"


def test_three_call_sites_use_the_helper():
    """Architectural assertion: the regex must be defined exactly once.
    A future refactor that re-introduces an inline `file_pattern =
    re.compile(...)` at a third call site would break this — keep the
    source of truth single."""
    src = (REPO / "src" / "discord-bridge.py").read_text()
    occurrences = src.count(r"\[(?:file|send|attach):")
    assert occurrences == 1, (
        f"expected exactly 1 occurrence of the marker regex pattern, found "
        f"{occurrences} — a call site has likely re-introduced an inline "
        f"`re.compile(...)` copy and will drift when the pattern is next "
        f"hardened"
    )


def main():
    test_no_markers_returns_text_and_empty_list()
    test_empty_input()
    test_single_file_marker_extracted()
    test_send_and_attach_aliases()
    test_home_relative_path_matches()
    test_relative_path_does_not_match()
    test_multiple_markers_preserve_order()
    test_colon_in_path_does_not_break_match()
    test_unknown_keyword_does_not_match()
    test_three_call_sites_use_the_helper()
    print("All file-marker tests passed.")


if __name__ == "__main__":
    main()
