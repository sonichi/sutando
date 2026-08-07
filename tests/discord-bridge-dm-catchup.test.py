#!/usr/bin/env python3
"""Regression guard for restart-safety #2: Discord REST-catch-up of
missed DMs after a gateway IDENTIFY reconnect.

## The bug (the one that hit Vasiliy on 2026-05-21)

Discord gateway disconnect that outlasts the RESUME window forces
discord.py into a full IDENTIFY reconnect. IDENTIFY does NOT replay
`MESSAGE_CREATE` events that arrived during the gap — they're lost.

Real incident: 21:14 PT, owner sent "B + A in that order" via Discord
DM during a >75-minute disconnect. Next morning the bridge had no
record of it; the message was only recoverable via manual REST fetch.

## The fix

Track the last DM message ID we successfully observed per channel in
`state/discord-dm-checkpoint.json`. On every `on_ready` (full
reconnect after gateway IDENTIFY), call `_catchup_missed_dms()`
which REST-fetches messages with `after=<last_seen_id>` from each
checkpointed channel and replays them through `_handle_discord_message`.
Discord message IDs are Snowflake-monotonic so `after=<id>` is
reliable.

## What this test covers

The checkpoint storage (read/write/advance semantics) is pure I/O and
fully testable. The catch-up loop itself requires discord.py
mocking which is more involved — we exercise the pure parts directly
and source-grep-assert the wiring.
"""

import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Set workspace BEFORE importing the bridge — it captures state-file
# paths at module-load time.
_WORKSPACE_TMP = tempfile.mkdtemp(prefix="sutando-dm-catchup-test-")
os.environ["SUTANDO_WORKSPACE"] = _WORKSPACE_TMP
os.environ["SUTANDO_TEST_MODE"] = "1"  # v0.8: opt-in env-honor
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
(Path(_WORKSPACE_TMP) / "state").mkdir(parents=True, exist_ok=True)


def _load(name: str, path: Path):
    if "discord" not in sys.modules:
        stub = types.ModuleType("discord")
        stub.Intents = type("Intents", (), {"default": staticmethod(lambda: type("I", (), {"message_content": False})())})
        stub.Client = type("Client", (), {"__init__": lambda self, **kw: None, "event": staticmethod(lambda fn: fn)})
        stub.File = type("File", (), {})
        stub.DMChannel = type("DMChannel", (), {})
        stub.Object = lambda id: type("Object", (), {"id": id})()
        sys.modules["discord"] = stub
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


bridge = _load("discord_bridge", REPO / "src" / "discord-bridge.py")


def _clear_checkpoint():
    """Remove the checkpoint file between tests."""
    f = bridge.DM_CHECKPOINT_FILE
    if f.exists():
        f.unlink()


def test_load_returns_empty_when_file_missing():
    """Fail-open: a missing checkpoint file returns `{}`. Catch-up
    becomes a no-op (no channels to scan), but the bridge starts."""
    _clear_checkpoint()
    assert bridge._load_dm_checkpoint() == {}


def test_load_returns_empty_on_malformed_json():
    """Same fail-open shape: corrupt file → empty checkpoint, not crash."""
    _clear_checkpoint()
    bridge.DM_CHECKPOINT_FILE.write_text("{ this is not json")
    assert bridge._load_dm_checkpoint() == {}


def test_load_returns_empty_on_non_dict_root():
    """`null`, lists, strings at the root level — all fail-open."""
    _clear_checkpoint()
    bridge.DM_CHECKPOINT_FILE.write_text('["not", "a", "dict"]')
    assert bridge._load_dm_checkpoint() == {}


def test_update_advances_forward_only():
    """Checkpoint advances monotonically — older IDs are ignored
    (handles the catch-up replay case where messages are processed
    in any order but the checkpoint only moves forward)."""
    _clear_checkpoint()
    bridge._update_dm_checkpoint(channel_id=12345, message_id=100)
    bridge._update_dm_checkpoint(channel_id=12345, message_id=200)
    bridge._update_dm_checkpoint(channel_id=12345, message_id=150)  # backwards
    bridge._update_dm_checkpoint(channel_id=12345, message_id=50)   # way backwards
    cp = bridge._load_dm_checkpoint()
    assert cp.get("12345") == "200", (
        f"checkpoint should be at 200 (highest seen), got {cp.get('12345')!r}"
    )


def test_update_per_channel_independent():
    """Multiple channels track independently — checkpoint shape is
    `{channel_id: last_msg_id}` so two channels don't shadow each
    other."""
    _clear_checkpoint()
    bridge._update_dm_checkpoint(channel_id=111, message_id=1000)
    bridge._update_dm_checkpoint(channel_id=222, message_id=2000)
    bridge._update_dm_checkpoint(channel_id=111, message_id=1500)
    cp = bridge._load_dm_checkpoint()
    assert cp.get("111") == "1500"
    assert cp.get("222") == "2000"


def test_update_persists_atomically():
    """Atomic-write contract: file is never empty/corrupt mid-write.
    Exercise the tmp+rename path indirectly by writing and reading."""
    _clear_checkpoint()
    bridge._update_dm_checkpoint(channel_id=42, message_id=9999)
    # Direct file read (bypassing the loader) to confirm the on-disk
    # content is valid JSON.
    raw = bridge.DM_CHECKPOINT_FILE.read_text()
    parsed = json.loads(raw)
    assert parsed.get("42") == "9999"


def test_update_handles_string_message_id():
    """Defensive: message ids from Discord arrive as ints. If a
    future caller passes a string, the int comparison must still work."""
    _clear_checkpoint()
    bridge._update_dm_checkpoint(channel_id=42, message_id=100)
    # _update_dm_checkpoint signature uses int(message_id) → str
    # internally. Pin that re-passing the same id is idempotent.
    bridge._update_dm_checkpoint(channel_id=42, message_id=100)
    cp = bridge._load_dm_checkpoint()
    assert cp.get("42") == "100"


def test_load_filters_malformed_entries():
    """Hand-edited checkpoint with bad shape: keep the good entries,
    drop the bad ones — don't crash, don't lose all state to one
    bad row."""
    _clear_checkpoint()
    bridge.DM_CHECKPOINT_FILE.write_text(json.dumps({
        "good_channel": "12345",
        "another_good": "67890",
        "bad_channel": None,           # not str/int
        "another_bad": {"nested": "no"},  # not str/int
    }))
    cp = bridge._load_dm_checkpoint()
    assert cp.get("good_channel") == "12345"
    assert cp.get("another_good") == "67890"
    assert "bad_channel" not in cp
    assert "another_bad" not in cp


def test_source_wires_catchup_into_on_ready():
    """Architectural assertion: the catch-up coroutine must be
    scheduled from `on_ready` so it fires on every reconnect (full
    IDENTIFY). Without this wiring, the checkpoint advances but
    nothing replays after a gap. Source-grep so a future refactor
    that drops the wire fails loudly."""
    src = (REPO / "src" / "discord-bridge.py").read_text()
    # The exact wiring shape: `client.loop.create_task(_catchup_missed_dms())`
    # inside the `on_ready` function.
    import re
    on_ready_block = re.search(
        r"async def on_ready\(\):(.*?)(?=^(?:async )?def )",
        src, re.MULTILINE | re.DOTALL,
    )
    assert on_ready_block, "could not locate on_ready in discord-bridge.py"
    body = on_ready_block.group(1)
    assert "_catchup_missed_dms" in body, (
        "on_ready does NOT schedule _catchup_missed_dms — the catch-up "
        "won't fire on reconnect, leaving the original bug (lost DMs "
        "during IDENTIFY-reconnect) open."
    )
    assert "create_task" in body, (
        "_catchup_missed_dms must be scheduled via create_task so it "
        "runs in parallel with the other poll loops (not awaited)."
    )


def test_source_wires_checkpoint_update_into_handler():
    """Architectural: `_handle_discord_message` must call
    `_update_dm_checkpoint` for DMs. Without this, the checkpoint
    never advances and every reconnect tries to replay messages
    from time 0 (or hits the 50-message limit and loses everything
    older). Pin via grep."""
    src = (REPO / "src" / "discord-bridge.py").read_text()
    import re
    handler_block = re.search(
        r"async def _handle_discord_message\(.*?\):(.*?)(?=^(?:async )?def )",
        src, re.MULTILINE | re.DOTALL,
    )
    assert handler_block, "could not locate _handle_discord_message"
    body = handler_block.group(1)
    assert "_update_dm_checkpoint" in body, (
        "_handle_discord_message does NOT call _update_dm_checkpoint — "
        "the catch-up checkpoint will never advance, defeating the fix."
    )


def test_self_authored_dms_advance_the_checkpoint():
    """The SELF-AUTHOR early return must advance the checkpoint before dropping.

    `test_source_wires_checkpoint_update_into_handler` above greps the whole
    handler body, so it passes as long as *any* call exists — and it did, while
    this branch still returned without one. That is why this test is scoped to
    the branch rather than the function.

    Why it matters: `channel.history()` returns our own replies, and in a DM
    channel with the owner most messages ARE ours. `if message.author ==
    client.user: return` sits ABOVE the main checkpoint advance, so the
    checkpoint froze at the last message we did not write and every reconnect
    re-fetched the same window. Observed on two hosts as
    `[dm-catchup] replayed N missed DM(s)` with an identical N across restarts.

    The starvation this guards: catch-up fetches `limit=50, oldest_first=True`.
    Once >50 messages sit after a frozen checkpoint, an owner DM at position 51+
    is never fetched, and the checkpoint still cannot advance, so no later
    restart reaches it — silent permanent loss of the exact message catch-up
    exists to rescue.
    """
    # Parsed by INDENT, not regex. A non-greedy regex here silently over-matches
    # past this branch's `return` and swallows the legitimate checkpoint advance
    # further down the function — which makes the assertion pass on the very code
    # it is supposed to reject. (That is not hypothetical: the first version of
    # this test did exactly that and passed against the unfixed bridge.)
    lines = (REPO / "src" / "discord-bridge.py").read_text().splitlines()
    start = next(
        (i for i, l in enumerate(lines)
         if l.strip() == "if message.author == client.user:"),
        None,
    )
    assert start is not None, "could not locate the self-author early-return branch"
    if_indent = len(lines[start]) - len(lines[start].lstrip())
    branch_lines = []
    for l in lines[start + 1:]:
        if not l.strip():
            branch_lines.append(l)
            continue
        indent = len(l) - len(l.lstrip())
        if indent <= if_indent:          # dedented out of the branch
            break
        branch_lines.append(l)
        if l.strip() == "return":        # the branch's own exit — stop here
            break
    branch = "\n".join(branch_lines)
    assert branch.rstrip().endswith("return"), (
        f"branch extraction did not terminate on the early return; got:\n{branch}"
    )
    assert "_update_dm_checkpoint" in branch, (
        "the self-authored-DM branch returns WITHOUT advancing the checkpoint — "
        "our own replies then pin the checkpoint forever and catch-up re-fetches "
        "the same window on every restart (see docstring for the starvation path)."
    )
    assert "DMChannel" in branch, (
        "the self-author checkpoint advance must be gated on DMChannel — the "
        "checkpoint is per-DM-channel and guild messages have no place in it."
    )


# --- behavioural: the fix must EXECUTE, not merely be present in the source ---
# The two tests above parse the source. They fail correctly against the unfixed
# bridge, but they never run the branch, so they cannot catch a wiring change
# that leaves the call textually intact — and diff-coverage reported the fix at
# 0%, which is that gap stated as a number. These drive the real handler.


class _FakeDM(sys.modules["discord"].DMChannel):
    """A real subclass, so `isinstance(ch, discord.DMChannel)` is genuinely true
    rather than patched true."""

    def __init__(self, cid):
        self.id = cid


def _drive_self_authored(channel, msg_id=99999):
    """Run _handle_discord_message on a message the bot itself wrote."""
    import asyncio
    me = object()
    fake_client = type("_C", (), {"user": me})()
    msg = types.SimpleNamespace(author=me, channel=channel, id=msg_id)
    real_client = getattr(bridge, "client", None)
    bridge.client = fake_client
    try:
        asyncio.run(bridge._handle_discord_message(msg))
    finally:
        bridge.client = real_client


def _read_checkpoint() -> dict:
    """Checkpoint contents, or {} when the file was never written.

    Deliberately not `read_text()` directly: in the BROKEN state no file exists
    at all, and letting that surface as FileNotFoundError aborts the whole run
    (main() only catches AssertionError) so the diagnostic below never prints.
    A missing file is a legitimate outcome to assert against, not a crash.
    """
    f = bridge.DM_CHECKPOINT_FILE
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except (ValueError, OSError):
        return {}


def test_self_authored_dm_actually_advances_the_checkpoint():
    """The starvation fix, exercised end-to-end through the handler."""
    _clear_checkpoint()
    _drive_self_authored(_FakeDM(4242), msg_id=99999)
    data = _read_checkpoint()
    assert data.get("4242") == "99999", (
        f"a self-authored DM must advance the checkpoint; got {data!r}. "
        "Without this the catch-up fetch (limit=50, oldest_first) permanently "
        "strands an owner DM once 50 messages sit past the frozen checkpoint."
    )


def test_self_authored_guild_message_leaves_checkpoint_untouched():
    """Same path, non-DM channel: the checkpoint is per-DM-channel."""
    _clear_checkpoint()
    _drive_self_authored(type("_Guild", (), {"id": 4242})(), msg_id=99999)
    assert _read_checkpoint() == {}, (
        "a guild message must not write into the DM checkpoint; got "
        f"{_read_checkpoint()!r}"
    )


def test_checkpoint_write_failure_does_not_break_the_handler():
    """The update is best-effort: a failing write must not raise out of the
    handler, or a checkpoint problem becomes a message-handling outage."""
    _clear_checkpoint()
    orig = bridge._update_dm_checkpoint

    def boom(*a, **kw):
        raise OSError("disk full")

    bridge._update_dm_checkpoint = boom
    try:
        _drive_self_authored(_FakeDM(4242), msg_id=99999)  # must not raise
    finally:
        bridge._update_dm_checkpoint = orig


def main():
    failures = []
    for fn in (
        test_load_returns_empty_when_file_missing,
        test_load_returns_empty_on_malformed_json,
        test_load_returns_empty_on_non_dict_root,
        test_update_advances_forward_only,
        test_update_per_channel_independent,
        test_update_persists_atomically,
        test_update_handles_string_message_id,
        test_load_filters_malformed_entries,
        test_source_wires_catchup_into_on_ready,
        test_source_wires_checkpoint_update_into_handler,
        test_self_authored_dms_advance_the_checkpoint,
        test_self_authored_dm_actually_advances_the_checkpoint,
        test_self_authored_guild_message_leaves_checkpoint_untouched,
        test_checkpoint_write_failure_does_not_break_the_handler,
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
    print("All DM-catchup tests passed.")


if __name__ == "__main__":
    main()
