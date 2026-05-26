#!/usr/bin/env python3
"""Unit tests for the `reply_chunks` metric counter in `poll_results`.

Background: prior to this PR, `_emit_channel_metric(...)` computed
`reply_chunks` via `len(list(_chunk_for_discord(clean_text)))`. That walked
the chunker a second time per send purely to count for the metric. An
earlier revision used `len(_chunk_for_discord(clean_text))` directly,
which raised `TypeError: object of type 'generator' has no len()` AFTER
`channel.send` succeeded — the `except Exception` two lines down then
logged `Reply failed: <e>` on every successful reply, making the bridge
look broken in production while messages were actually delivering.

The fix counts chunks inside the existing send loop:

    reply_chunks = 0
    for chunk in _chunk_for_discord(clean_text):
        await channel.send(chunk, reference=ref)
        ...
        reply_chunks += 1

`poll_results` is an async method tightly coupled to discord.py runtime,
so testing it end-to-end would need a live `discord.Client`. Instead we
test the invariant the fix relies on: counting chunks during iteration
yields the same value as `len(list(_chunk_for_discord(text)))` for the
inputs the bridge actually sees, and the raw-generator `len(...)`
footgun stays broken so any future refactor that reintroduces it gets
caught here.
"""

import importlib.util
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Stub `discord` and materialize a placeholder token if missing — same
# rationale as tests/discord-chunker.test.py: discord-bridge.py touches
# both on import and would otherwise refuse to load in clean CI.
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
chunker = bridge._chunk_for_discord


def _count_via_loop(text: str, max_len: int = 1900) -> int:
    """Mirror exactly what `poll_results` does: increment a counter inside
    the existing for-loop over the chunker generator."""
    n = 0
    for _ in chunker(text, max_len=max_len):
        n += 1
    return n


def test_raw_generator_has_no_len():
    """Calling `len()` on the raw chunker output must keep raising
    `TypeError`. This is the footgun the fix routes around — if a future
    refactor accidentally makes the chunker return a list, the metric will
    silently start double-allocating again. Pin the contract."""
    gen = chunker("hello")
    try:
        len(gen)
    except TypeError:
        return  # expected
    raise AssertionError(
        "chunker no longer returns a generator; counter approach may "
        "now be wasteful or wrong — review _chunk_for_discord and the "
        "reply_chunks metric in poll_results together"
    )


def test_empty_input_yields_zero_chunks():
    """`if clean_text` short-circuits to `reply_chunks=0` for empty bodies
    (file-only replies hit this path). Confirm the loop-counter agrees."""
    assert _count_via_loop("") == 0
    # `len(list(chunker("")))` is the workaround the fix replaces — must
    # agree with the loop counter for the metric to stay consistent.
    assert len(list(chunker(""))) == 0


def test_short_input_is_single_chunk():
    """Typical short reply: one Discord message, `reply_chunks=1`."""
    text = "Hey — quick update, all good."
    assert _count_via_loop(text) == 1
    assert len(list(chunker(text))) == 1


def test_long_plain_text_counter_matches_list_len():
    """Multi-chunk plain text: the counter must equal `len(list(...))`
    so the metric value is unchanged by the refactor."""
    text = "\n".join("line " + str(i) * 40 for i in range(200))
    loop_count = _count_via_loop(text, max_len=300)
    list_count = len(list(chunker(text, max_len=300)))
    assert loop_count == list_count, (
        f"counter={loop_count} disagrees with list-len={list_count} — "
        "the metric value would change between the old and new code paths"
    )
    assert loop_count > 1  # sanity: this input must actually split


def test_fenced_code_block_counter_matches_list_len():
    """Code-fence-spanning input exercises the chunker's reopen-on-boundary
    path. The counter must still match `len(list(...))`."""
    text = "intro\n```python\n" + ("x = 1\n" * 400) + "```\nouter"
    loop_count = _count_via_loop(text, max_len=300)
    list_count = len(list(chunker(text, max_len=300)))
    assert loop_count == list_count
    assert loop_count >= 3  # opener-bearing chunk + reopened middle + closer


def test_counter_increments_once_per_yielded_chunk():
    """Direct invariant: the counter must equal the number of times the
    generator yields. (Guards against future loops that conditionally
    skip an iteration without decrementing.)"""
    text = "x" * 5000
    yielded = list(chunker(text, max_len=400))
    loop_count = _count_via_loop(text, max_len=400)
    assert loop_count == len(yielded)


def main():
    test_raw_generator_has_no_len()
    test_empty_input_yields_zero_chunks()
    test_short_input_is_single_chunk()
    test_long_plain_text_counter_matches_list_len()
    test_fenced_code_block_counter_matches_list_len()
    test_counter_increments_once_per_yielded_chunk()
    print("All reply_chunks counter tests passed.")


if __name__ == "__main__":
    main()
