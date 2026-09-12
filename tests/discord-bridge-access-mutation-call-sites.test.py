#!/usr/bin/env python3
"""Real-line coverage for the thread-engage and pairing `mutate_access_file`
call sites in `src/discord-bridge.py`.

Neither existing test drives the ACTUAL call-site code: the thread-engage test
(`tests/discord-thread-engage-missing-access.test.py`) exercises a hand-copied
mutator against `access_store` directly, and the pairing test
(`tests/discord-bridge-pairing-dm.test.py`) only reaches the success path. This
file extracts the two real `If` blocks from `_handle_discord_message` via AST
(same technique as `tests/discord-access-backup.test.py` and
`tests/bridge-timeout-guards.test.py`'s `_compile_segment`, one level deeper —
a nested `If` instead of a top-level `FunctionDef`), wraps each in a synthetic
`async def` taking the block's free names as explicit parameters, and compiles
it against the real file path so coverage.py attributes execution to the real
production lines. This exercises the `_thread_seed_mutator`/`_pairing_mutator`
closure bodies and the `except Exception` branches around both
`mutate_access_file(...)` call sites.

Run: python3 tests/discord-bridge-access-mutation-call-sites.test.py
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import time as _time
import unittest
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"

# lint-hermetic-bridge-tests.py flags any exec(..., ns) naming a bridge path
# as a bridge load requiring isolation first, even for an AST slice like ours.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-discord-access-mut-")
_ccd_discord = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_ccd_discord.mkdir(parents=True, exist_ok=True)
(_ccd_discord / "access.json").write_text('{"allowFrom": []}')

sys.path.insert(0, str(REPO / "src"))
import discord  # noqa: E402
from access_store import (  # noqa: E402
    mutate_access_file as real_mutate_access_file,
    read_access_for_transaction as real_read_access_for_transaction,
)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def _find_if_matching(predicate) -> ast.If:
    tree = ast.parse(BRIDGE.read_text(), filename=str(BRIDGE))
    handle_fn = next(
        n for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_handle_discord_message"
    )
    for n in ast.walk(handle_fn):
        if isinstance(n, ast.If) and predicate(ast.unparse(n.test)):
            return n
    raise AssertionError("matching If block not found in _handle_discord_message")


def _wrap_as_async_function(node: ast.If, name: str, params: list[str], return_name: Optional[str]):
    """Wrap `node` as an async function body taking `params` as explicit locals.

    Line numbers are preserved on `node` (and everything under it), so executing
    the compiled function counts the ORIGINAL discord-bridge.py lines as covered.
    """
    ret_value = ast.Name(id=return_name, ctx=ast.Load()) if return_name else ast.Constant(value=None)
    fn = ast.AsyncFunctionDef(
        name=name,
        args=ast.arguments(
            posonlyargs=[], args=[ast.arg(arg=p) for p in params],
            vararg=None, kwonlyargs=[], kw_defaults=[], kwarg=None, defaults=[],
        ),
        body=[node, ast.Return(value=ret_value)],
        decorator_list=[], returns=None,
    )
    fn.lineno, fn.col_offset = node.lineno, 0
    fn.end_lineno, fn.end_col_offset = node.end_lineno, 0
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    ns: dict = {}
    exec(compile(module, str(BRIDGE), "exec"), ns)
    return ns[name]


THREAD_PARAMS = [
    "message", "client", "bot_mentioned", "role_mentioned", "require_mention",
    "ACCESS_FILE", "mutate_access_file", "_backup_access_to_disk",
    "read_access_for_transaction", "_has_sibling_bots",
    "_should_notify_owner_on_seed", "_format_seed_notice",
    "_maybe_notify_owner_of_seed", "discord",
]
async def _noop_notify(*_a, **_k):
    """Stub for the seed-notice wrapper: these cases assert mutation behaviour,
    not delivery, and the real one would need a DM-capable client."""
    return False


PAIRING_PARAMS = [
    "message", "sender_id", "username", "allowed", "channel_authorized", "policy",
    "ACCESS_FILE", "mutate_access_file", "_backup_access_to_disk",
    "_deliver_pairing_prompt", "time",
]


def _build_thread_harness():
    node = _find_if_matching(lambda t: t == "isinstance(message.channel, discord.Thread)")
    return _wrap_as_async_function(node, "_thread_engage_harness", THREAD_PARAMS, "require_mention")


def _build_pairing_harness():
    node = _find_if_matching(lambda t: t.startswith("policy == 'pairing'"))
    return _wrap_as_async_function(node, "_pairing_harness", PAIRING_PARAMS, None)


def _wrap_nodes_as_async_function(nodes: list[ast.stmt], name: str, params: list[str], sentinel: str):
    """Like `_wrap_as_async_function` but for a CONTIGUOUS RUN of sibling
    statements (not a single `If`) — appends a sentinel return so a caller can
    tell "one of the block's own `return`s fired" (early-drop) apart from
    "execution fell through to the end of the slice" (would have continued in
    the real function)."""
    body = list(nodes) + [ast.Return(value=ast.Constant(value=sentinel))]
    fn = ast.AsyncFunctionDef(
        name=name,
        args=ast.arguments(
            posonlyargs=[], args=[ast.arg(arg=p) for p in params],
            vararg=None, kwonlyargs=[], kw_defaults=[], kwarg=None, defaults=[],
        ),
        body=body, decorator_list=[], returns=None,
    )
    fn.lineno, fn.col_offset = nodes[0].lineno, 0
    fn.end_lineno, fn.end_col_offset = nodes[-1].end_lineno, 0
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    ns: dict = {}
    exec(compile(module, str(BRIDGE), "exec"), ns)
    return ns[name]


_GATE_SENTINEL = "__NOT_DROPPED__"


def _build_thread_engage_to_gate_harness():
    """Spans the thread-engage seed block through the `require_mention` gate
    that follows it in `_handle_discord_message` — the real production
    statements, in their original nested `if not is_dm:` block, sliced by
    matching the same `If` node the two single-block harnesses above already
    locate. Lets a test observe whether an unseeded, unmentioned message is
    actually dropped (bare `return` → None) before it can ever reach the
    downstream channel_authorized/pairing code (#3318 blocker 2)."""
    outer = _find_if_matching(lambda t: t == "not is_dm")
    start = next(
        i for i, s in enumerate(outer.body)
        if isinstance(s, ast.If) and ast.unparse(s.test) == "isinstance(message.channel, discord.Thread)"
    )
    end = next(
        i for i, s in enumerate(outer.body)
        if isinstance(s, ast.If) and ast.unparse(s.test).startswith("require_mention and")
    )
    nodes = outer.body[start:end + 1]
    return _wrap_nodes_as_async_function(
        nodes, "_thread_engage_to_gate_harness", THREAD_PARAMS + ["load_allowed"], _GATE_SENTINEL
    )


_THREAD_HARNESS = _build_thread_harness()
_PAIRING_HARNESS = _build_pairing_harness()
_THREAD_TO_GATE_HARNESS = _build_thread_engage_to_gate_harness()
# The sliced span now calls _mention_gate_triggers_ingest (added on main after
# this branch); stub it fail-closed so the drop path stays the one under test.
_THREAD_TO_GATE_HARNESS.__globals__["_mention_gate_triggers_ingest"] = lambda message: False


class _FakeThread(discord.Thread):
    """Never calls discord.Thread.__init__ — isinstance() is all the block needs.

    `.parent` is a read-only property on the real class and is only touched by
    the pragma-excluded owner-notice tail, which these scenarios never reach.
    """

    def __init__(self, thread_id, parent_id):
        self.id = thread_id
        self.parent_id = parent_id

    async def send(self, *args, **kwargs):
        return None


class _FakeAuthor:
    def __init__(self, author_id):
        self.id = author_id
        self.mention = f"<@{author_id}>"


class _FakeMessage:
    def __init__(self, channel, author):
        self.channel = channel
        self.author = author


def _raising_mutate(*_args, **_kwargs):
    raise RuntimeError("boom")


class ThreadEngageMutatorBody(unittest.TestCase):
    """Lines 3224-3251-ish: the mutator body and the fresh/already-seeded paths."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="thread-engage-cs-"))
        self.access_file = self.tmpdir / "access.json"
        self.client = type("C", (), {"user": type("U", (), {"id": 555})()})()

    def _run(self, **overrides):
        kwargs = dict(
            client=self.client, bot_mentioned=False, role_mentioned=False,
            require_mention=True, ACCESS_FILE=self.access_file,
            mutate_access_file=real_mutate_access_file,
            _backup_access_to_disk=lambda *_a, **_k: None,
            read_access_for_transaction=real_read_access_for_transaction,
            _has_sibling_bots=lambda *_a, **_k: False,
            _should_notify_owner_on_seed=lambda *_a, **_k: False,
            _format_seed_notice=lambda *_a, **_k: "unused",
            _maybe_notify_owner_of_seed=_noop_notify,
            discord=discord,
        )
        kwargs.update(overrides)
        return asyncio.run(_THREAD_HARNESS(**kwargs))

    def test_fresh_thread_unrecognized_sender_is_not_seeded(self):
        """#3318 blocker 2 (qingyun-wu): a fresh thread with no parent config
        must not manufacture a brand-new grant for an author who isn't already
        a recognized (allowFrom) sender — that self-authorized an unpaired,
        unmentioned stranger on their own first message. The message still
        reaches the normal allowlist/pairing gate downstream, just unseeded."""
        self.access_file.write_text(json.dumps({"groups": {}, "allowFrom": ["1111"]}))
        author = _FakeAuthor(2222)
        channel = _FakeThread(9001, 8001)
        message = _FakeMessage(channel, author)
        result = self._run(message=message)
        check("unrecognized-sender fresh thread leaves require_mention untouched", result is True)
        doc = json.loads(self.access_file.read_text())
        check(
            "unrecognized-sender fresh thread writes no groups entry",
            doc.get("groups", {}).get("9001") is None,
            str(doc),
        )

    def test_fresh_thread_recognized_sender_is_still_seeded(self):
        """The owner's own single-bot convenience (the reason this branch
        exists) must survive: a sender already in the top-level allowFrom
        still gets an engager-only grant even with no parent config."""
        self.access_file.write_text(json.dumps({"groups": {}, "allowFrom": ["2222"]}))
        author = _FakeAuthor(2222)
        channel = _FakeThread(9001, 8001)
        message = _FakeMessage(channel, author)
        result = self._run(message=message)
        check("recognized-sender fresh thread seed narrows require_mention to False", result is False)
        doc = json.loads(self.access_file.read_text())
        entry = doc.get("groups", {}).get("9001")
        check("recognized-sender fresh thread seed writes a groups entry", entry is not None, str(doc))
        check(
            "recognized-sender fresh thread entry defaults allowFrom to the author (no parent config)",
            entry == {"requireMention": False, "allowFrom": ["2222"]},
            str(entry),
        )

    def test_already_seeded_thread_is_a_no_op(self):
        self.access_file.write_text(json.dumps(
            {"groups": {"9001": {"requireMention": False}}, "allowFrom": []}
        ))
        before = self.access_file.read_text()
        author = _FakeAuthor(2222)
        channel = _FakeThread(9001, 8001)
        message = _FakeMessage(channel, author)
        result = self._run(message=message)
        check("already-seeded thread leaves require_mention untouched", result is True)
        check("already-seeded thread leaves access.json byte-identical",
              self.access_file.read_text() == before)

    def test_mutate_access_file_raising_is_caught_and_logged(self):
        author = _FakeAuthor(2222)
        channel = _FakeThread(9002, 8001)
        message = _FakeMessage(channel, author)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = self._run(message=message, mutate_access_file=_raising_mutate)
        check("exception path leaves require_mention untouched", result is True)
        check("exception path logs the failure",
              "[thread-engage] failed to update access.json" in buf.getvalue(), buf.getvalue())

    def test_corrupt_access_file_is_left_untouched_with_a_warning(self):
        self.access_file.write_text("{not valid json")
        author = _FakeAuthor(2222)
        channel = _FakeThread(9003, 8001)
        message = _FakeMessage(channel, author)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = self._run(message=message)
        check("corrupt-file path leaves require_mention untouched", result is True)
        check("corrupt-file path warns instead of overwriting",
              "WARNING: access.json unreadable; skipping seed, not overwriting" in buf.getvalue(),
              buf.getvalue())
        check("corrupt-file path never rewrites the file",
              self.access_file.read_text() == "{not valid json")


class ThreadEngageFailsClosedThroughGate(unittest.TestCase):
    """End-to-end (#3318 blocker 2, qingyun-wu): drives the REAL production
    statements from the thread-engage seed block through the require_mention
    gate that immediately follows it, proving an unpaired, unmentioned
    author's first message in a thread with NO access.json present is DROPPED
    (bare `return`, before channel_authorized is ever computed) rather than
    self-authorizing. A recognized sender is run as a control to prove the
    harness isn't just unconditionally returning early."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="thread-engage-gate-cs-"))
        self.access_file = self.tmpdir / "access.json"  # genuinely absent
        self.client = type("C", (), {"user": type("U", (), {"id": 555})()})()

    def _run(self, **overrides):
        kwargs = dict(
            client=self.client, bot_mentioned=False, role_mentioned=False,
            require_mention=True, ACCESS_FILE=self.access_file,
            mutate_access_file=real_mutate_access_file,
            _backup_access_to_disk=lambda *_a, **_k: None,
            read_access_for_transaction=real_read_access_for_transaction,
            _has_sibling_bots=lambda *_a, **_k: False,
            _should_notify_owner_on_seed=lambda *_a, **_k: False,
            _format_seed_notice=lambda *_a, **_k: "unused",
            _maybe_notify_owner_of_seed=_noop_notify,
            discord=discord,
            load_allowed=lambda: [],
        )
        kwargs.update(overrides)
        return asyncio.run(_THREAD_TO_GATE_HARNESS(**kwargs))

    def test_unpaired_unmentioned_author_dropped_when_access_file_missing(self):
        self.assertFalse(self.access_file.exists(), "precondition: access.json genuinely missing")
        author = _FakeAuthor(2222)
        channel = _FakeThread(9101, 8101)
        message = _FakeMessage(channel, author)
        result = self._run(message=message)
        check(
            "unpaired unmentioned author is dropped at the require_mention gate "
            "(never reaches channel_authorized/pairing)",
            result is None,
            f"got {result!r} instead of an early return",
        )
        check(
            "the dropped message did not self-grant a seed (access.json still absent)",
            not self.access_file.exists(),
        )

    def test_recognized_sender_falls_through_the_gate(self):
        self.access_file.write_text(json.dumps(
            {"dmPolicy": "pairing", "allowFrom": ["2222"], "groups": {}}
        ))
        author = _FakeAuthor(2222)
        channel = _FakeThread(9102, 8102)
        message = _FakeMessage(channel, author)
        result = self._run(message=message)
        check(
            "a recognized sender's seeded thread falls through the gate (not dropped)",
            result == _GATE_SENTINEL,
            f"got {result!r}",
        )
        doc = json.loads(self.access_file.read_text())
        check(
            "the recognized sender's thread was actually seeded",
            doc.get("groups", {}).get("9102") is not None,
            str(doc),
        )


class PairingMutatorExceptionBranch(unittest.TestCase):
    """Lines ~3439-3453: the except-branch around the pairing mutate_access_file call."""

    def test_mutate_access_file_raising_skips_pairing_and_warns(self):
        author = _FakeAuthor(3003)
        channel = type("Ch", (), {"id": 7001})()
        message = _FakeMessage(channel, author)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = asyncio.run(_PAIRING_HARNESS(
                message=message, sender_id="3003", username="newuser",
                allowed=[], channel_authorized=False, policy="pairing",
                ACCESS_FILE=Path(tempfile.mkdtemp(prefix="pairing-cs-")) / "access.json",
                mutate_access_file=_raising_mutate,
                _backup_access_to_disk=lambda *_a, **_k: None,
                _deliver_pairing_prompt=None,
                time=_time,
            ))
        check("pairing exception path returns without a code", result is None)
        out = buf.getvalue()
        check("pairing exception path logs the mutate failure",
              "[pairing] failed to update access.json" in out, out)
        check("pairing exception path warns the operator instead of overwriting",
              "access.json unreadable or write failed" in out, out)


if __name__ == "__main__":
    _r = unittest.main(exit=False)
    try:
        import coverage
        _cov = coverage.Coverage.current()
        if _cov is not None:
            _cov.save()
    except Exception:
        pass
    print("\n" + ("FAIL — " + ", ".join(failures) if failures else "PASS — discord-bridge-access-mutation-call-sites"))
    sys.exit(0 if (_r.result.wasSuccessful() and not failures) else 1)
