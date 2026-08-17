#!/usr/bin/env python3
"""
Test the [reply: <message_id>] directive in discord-bridge poll_results.

Purpose: catch the class of bug where the MessageReference construction
references an out-of-scope variable (e.g. `int(channel_id)` when the
function scope only has `channel`). The original v1 of PR #620 had this
exact bug — the ternary short-circuit meant tests with no directive
passed green, but the moment a result file contained `[reply: <id>]`
the bridge would NameError.

Run: python3 tests/discord-bridge-reply-directive.test.py
"""

from __future__ import annotations
import asyncio
import contextlib
import importlib.util
import hashlib
import shutil
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests" / "_helpers"))
from discord_env import temp_config_root  # noqa: E402

# Stub minimal discord module — same pattern as other bridge tests
_discord_stub = types.ModuleType("discord")


class _Intents:
    @classmethod
    def default(cls):
        i = cls()
        i.message_content = False
        i.members = False
        return i


class _Client:
    def __init__(self, *a, **kw):
        self.user = None
        self.loop = types.SimpleNamespace(create_task=lambda *a, **kw: None)
    def event(self, fn): return fn
    def get_channel(self, _): return None


class _AllowedMentions:
    def __init__(self, *a, **kw): pass


class _MessageReference:
    """Captures the constructor args so tests can assert on them."""
    def __init__(self, message_id=None, channel_id=None, fail_if_not_exists=True):
        self.message_id = message_id
        self.channel_id = channel_id
        self.fail_if_not_exists = fail_if_not_exists


class _DMChannel: pass


_discord_stub.Intents = _Intents
_discord_stub.Client = _Client
_discord_stub.AllowedMentions = _AllowedMentions
class _Thread:
    """The stub omitted Thread, so the thread branch was unreachable under
    test and went uncovered."""


_discord_stub.Thread = _Thread
_discord_stub.MessageReference = _MessageReference
_discord_stub.MessageType = types.SimpleNamespace(default=0, reply=1)
_discord_stub.File = lambda *a, **kw: None
_discord_stub.DMChannel = _DMChannel

sys.modules["discord"] = _discord_stub


def load_bridge():
    src = (REPO / "src" / "discord-bridge.py").read_text()
    # Give this fixture its OWN config root. Seeding the AMBIENT root fixes which
    # root the bridge reads but not WHOSE: with a real CLAUDE_CONFIG_DIR set, the
    # fixture fabricates a Discord install in the caller's config dir and leaves a
    # stub credential behind — the PR's own reported production symptom, relocated
    # rather than removed (john-the-dev, #2357 review 2026-07-31T07:36).
    # The bridge resolves its token at exec time, so the temp root only has to be
    # live across the exec; the caller's environment is restored on the way out.
    with temp_config_root():
        spec = importlib.util.spec_from_loader("bridge", loader=None)
        bridge = importlib.util.module_from_spec(spec)
        bridge.__file__ = str(REPO / "src" / "discord-bridge.py")
        exec(compile(src, bridge.__file__, "exec"), bridge.__dict__)
    return bridge


bridge = load_bridge()

# Set by main() before any case runs; read by the live-workspace regression.
_LIVE_BASELINE = {}


@contextlib.contextmanager
def _bridge_writes_redirected():
    """Point every filesystem write this fixture triggers at a throwaway tree.

    SIX escapes, all to the OWNER's live workspace, and the old comment at
    `_run_one_poll_iteration` ("RESULTS_DIR is shared with prod") signed off on
    the first as acceptable rather than treating it as the defect it is:

      results/…                      staged result files
      results/archive/<ym>/…         archive_file() moving them
      state/outbox.log               poll_results -> outbox_log.append
      state/result-audit.log         result_audit
      state/discord-bridge.heartbeat the bridge's own heartbeat
      logs/events-<date>.jsonl       event_log

    `state/outbox.log` is the one that matters most — the delivery audit log the
    dashboard's Outbox card shows the owner. Measured on this host before the
    fix, fixtures had been landing in the real log since 2026-05-28:
    `discord_channel` rows whose `recipient` is `111222333` / `444555666` /
    `777888999` rather than a Discord snowflake.

    I found three of the six by fixing and re-measuring, not by reading — each
    round the whole-tree regression named what the previous fix had missed.
    That is why the redirect is now structural (patch the resolver everywhere it
    is BOUND, plus the import-time constants) rather than an enumerated list of
    the paths I happened to know about.

    Restores in a `finally` so a mid-run assertion cannot leave the globals
    patched for whatever runs next in the same interpreter (#2614).
    """
    import workspace_default

    tmp = Path(tempfile.mkdtemp(prefix="reply-directive-test-"))
    for sub in ("results", "state", "tasks", "logs"):
        (tmp / sub).mkdir(parents=True, exist_ok=True)

    # ONE patch covers every helper that resolves at CALL time — outbox_log,
    # result_audit and event_log all call `resolve_workspace()` per write, so
    # patching the resolver redirects all three and any future sibling. Chasing
    # them individually is what let two of them slip: my first pass redirected
    # RESULTS_DIR + outbox only, and `state/result-audit.log`,
    # `state/discord-bridge.heartbeat` and `logs/events-*.jsonl` still escaped.
    orig_resolve = workspace_default.resolve_workspace
    # Accept and IGNORE any args: `observability.config.load_observability_config()`
    # and `observability.sink.JsonlFileSink.write()` call
    # `resolve_workspace(migrate=False)`. A zero-arg stub raises TypeError there,
    # and the best-effort observability facade SWALLOWS it — so the redirect
    # silently DISABLED the write path instead of redirecting it, and the suite
    # stayed green while `_emit_channel` was never exercised (qingyun-wu, #2619).
    # Preserving the resolver's signature is what keeps this a redirect.
    _fake = lambda *a, **kw: tmp

    # Patch the name in EVERY module that holds it, not just where it is defined.
    # `from workspace_default import resolve_workspace` binds the function into
    # the IMPORTER's namespace, so replacing `workspace_default.resolve_workspace`
    # alone leaves every already-imported consumer pointing at the original.
    # Measured: with only the source module patched, `state/result-audit.log` and
    # `logs/events-*.jsonl` still escaped, because `result_audit` and `event_log`
    # each hold their own binding. The whole-tree regression caught it; the
    # enumerated one had not.
    _patched = []
    for _mod in list(sys.modules.values()):
        if getattr(_mod, "resolve_workspace", None) is orig_resolve:
            _patched.append(_mod)
            _mod.resolve_workspace = _fake
    workspace_default.resolve_workspace = _fake

    # The bridge's own constants are computed AT IMPORT from `REPO`
    # (discord-bridge.py:244-248, 3991, 4099, 4214), so patching the resolver
    # cannot reach them — they must be rebound explicitly. This is the same
    # import-time-vs-call-time split as #2615 vs #2618, in one file.
    _consts = ("REPO", "TASKS_DIR", "RESULTS_DIR", "STATE_DIR",
               "ARCHIVE_TASKS_DIR", "ARCHIVE_RESULTS_DIR",
               "DM_CHECKPOINT_FILE", "DELIVERED_DIR", "PENDING_REPLIES_FILE")
    saved = {}
    for name in _consts:
        if hasattr(bridge, name):
            saved[name] = getattr(bridge, name)
            rel = Path(str(saved[name])).relative_to(orig_resolve()) \
                if str(saved[name]).startswith(str(orig_resolve())) else None
            setattr(bridge, name, tmp / rel if rel is not None else tmp)
    try:
        yield tmp, tmp / "state" / "outbox.log"
    finally:
        workspace_default.resolve_workspace = orig_resolve
        for _mod in _patched:
            _mod.resolve_workspace = orig_resolve
        # ALSO restore anything that bound `_fake` DURING the context. `_patched`
        # is built before the body runs, so a module imported lazily inside it
        # (outbox_log, result_audit, event_log all are) picks up `_fake` and was
        # never in the list — leaving it pointed at a deleted temp dir for the
        # rest of the interpreter. qingyun-wu found this on the sibling #2620;
        # it is the same code shape here, so it is fixed in both.
        for _m in list(sys.modules.values()):
            if getattr(_m, "resolve_workspace", None) is _fake:
                _m.resolve_workspace = orig_resolve
        for name, val in saved.items():
            setattr(bridge, name, val)
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Mock channel that captures send() calls
# ---------------------------------------------------------------------------

class _MockChannel:
    def __init__(self, channel_id=987654321):
        self.id = channel_id
        self.sent = []  # list of {content, reference, file}
    async def send(self, content=None, *, reference=None, file=None, allowed_mentions=None):
        self.sent.append({
            "content": content,
            "reference": reference,
            "file": file,
            "allowed_mentions": allowed_mentions,
        })
        return types.SimpleNamespace(id=12345)


class _MockThreadChannel(_MockChannel, _Thread):
    """A channel that is a Discord thread."""


# ---------------------------------------------------------------------------
# Drive one iteration of poll_results
# ---------------------------------------------------------------------------

async def _run_one_poll_iteration(task_id, channel, result_text):
    """Set up state, write the result file, drive poll_results until first
    sleep, then cancel. Returns the captured channel.sent list."""
    # Stage state in the bridge module
    bridge.pending_replies.clear()
    bridge.pending_replies[task_id] = channel
    # Write the result file under bridge's RESULTS_DIR
    result_file = bridge.RESULTS_DIR / f"{task_id}.txt"
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_text(result_text)
    # Stub client.is_ready so heartbeat block is skipped cleanly
    bridge.client.is_ready = lambda: True
    # Stub save_pending_replies so we don't write to disk during tests
    bridge.save_pending_replies = lambda: None
    # archive_file uses real fs; RESULTS_DIR is redirected to a throwaway tree by
    # `_bridge_writes_redirected`, so this no longer touches the owner's results/.
    # but the file we wrote is fully synthetic and gets archived

    # Run poll_results with a tight timeout — we only need 1 iteration
    try:
        await asyncio.wait_for(bridge.poll_results(), timeout=0.5)
    except asyncio.TimeoutError:
        pass
    return channel.sent


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

def case_a_directive_present_constructs_reference():
    """When result text contains `[reply: <id>]`, channel.send must be called
    with reference=MessageReference(message_id=<id>, channel_id=channel.id)."""
    fails = []
    ch = _MockChannel(channel_id=111222333)
    parent_msg_id = "1500000000000000001"
    result_text = f"[reply: {parent_msg_id}] hello world"
    sent = asyncio.run(_run_one_poll_iteration("test-task-a", ch, result_text))
    if not sent:
        fails.append("a) no channel.send call observed")
        return fails
    first = sent[0]
    if first["reference"] is None:
        fails.append("a) first send should have reference set (got None)")
        return fails
    ref = first["reference"]
    if not hasattr(ref, "message_id") or str(ref.message_id) != parent_msg_id:
        fails.append(f"a) reference.message_id should be {parent_msg_id} (got {getattr(ref, 'message_id', None)})")
    if not hasattr(ref, "channel_id") or ref.channel_id != ch.id:
        fails.append(f"a) reference.channel_id should be channel.id={ch.id} (got {getattr(ref, 'channel_id', None)})")
    if not hasattr(ref, "fail_if_not_exists") or ref.fail_if_not_exists is not False:
        fails.append(f"a) reference.fail_if_not_exists should be False (got {getattr(ref, 'fail_if_not_exists', None)})")
    # Directive should be stripped from the sent content
    if "[reply:" in (first["content"] or ""):
        fails.append(f"a) [reply:] directive should be stripped from sent content (got {first['content']!r})")
    return fails


def case_b_no_directive_no_reference():
    """Backwards-compat: a result text without `[reply: <id>]` must NOT have
    a reference attached. This is what made the v1 bug invisible."""
    fails = []
    ch = _MockChannel(channel_id=444555666)
    sent = asyncio.run(_run_one_poll_iteration("test-task-b", ch, "plain text reply"))
    if not sent:
        fails.append("b) no channel.send call observed")
        return fails
    if sent[0]["reference"] is not None:
        fails.append(f"b) without directive, reference should be None (got {sent[0]['reference']})")
    return fails


def case_c_directive_only_first_chunk():
    """For multi-chunk text, only the first chunk should carry the reference
    (Discord allows one reply-anchor per send)."""
    fails = []
    ch = _MockChannel(channel_id=777888999)
    parent = "1500000000000000002"
    # Force multi-chunk by exceeding the chunker's max_len (1900 chars)
    body = "x" * 2200
    result_text = f"[reply: {parent}] {body}"
    sent = asyncio.run(_run_one_poll_iteration("test-task-c", ch, result_text))
    if len(sent) < 2:
        fails.append(f"c) expected ≥2 chunks for 2200-char body (got {len(sent)})")
        return fails
    if sent[0]["reference"] is None:
        fails.append("c) first chunk should have reference set")
    if sent[1]["reference"] is not None:
        fails.append(f"c) second chunk should NOT have reference (got {sent[1]['reference']})")
    return fails


def case_d_oversized_result_has_a_delivery_budget():
    """Oversized results use three previews plus a truncation notice."""
    fails = []
    ch = _MockChannel(channel_id=888999000)
    body = "oversized-review-output\n" * 1200
    sent = asyncio.run(_run_one_poll_iteration("test-task-d", ch, body))
    if len(sent) != 4:
        fails.append(f"d) oversized result should use exactly 4 sends (got {len(sent)})")
        return fails
    if not (sent[0]["content"] or "").startswith("oversized-review-output"):
        fails.append("d) first send should retain the beginning of the result")
    notice = sent[-1]["content"] or ""
    if "truncated" not in notice.lower() or "suppressed" not in notice.lower():
        fails.append(f"d) final send should explain truncation (got {notice!r})")
    return fails


def test_resolver_bindings_restored_after_the_context() -> int:
    """Protect the late-import restore sweep with an activated assertion.

    Raised on the sibling #2620 by john-the-dev and qingyun-wu: the sweep
    works, but deleting it leaves the suite green while the lazily-imported
    consumers stay bound to the throwaway stub and resolve into a REMOVED
    temp tree. Same code shape here (`result_audit` and `event_log` are both
    lazily imported), so the same assertion belongs here — added without
    waiting to be told twice.
    """
    import workspace_default
    bad = []
    for name in ("outbox_log", "result_audit", "event_log"):
        mod = sys.modules.get(name)
        if mod is None:
            continue
        if getattr(mod, "resolve_workspace", None) is not workspace_default.resolve_workspace:
            bad.append(name)
    if bad:
        print(f"  ✗ resolver binding NOT restored in: {', '.join(bad)} — later writes "
              f"in this interpreter would land in a deleted throwaway tree", file=sys.stderr)
        return 1
    print("  ✓ resolver bindings restored after the context "
          f"({len([n for n in ('outbox_log','result_audit','event_log') if n in sys.modules])} lazy consumers checked)")
    return 0


def _workspace_fingerprint(ws) -> dict:
    """(size, mtime) for EVERY file under the workspace.

    Deliberately whole-tree rather than an enumerated list of the directories
    this fixture is known to touch. The first version of this regression
    checked only `results/` and `state/outbox.log`, PASSED, and three writes
    escaped anyway — `state/result-audit.log`, `state/discord-bridge.heartbeat`
    and `logs/events-*.jsonl`. An assertion only catches what it looks at, so
    it must look at everything.
    """
    out = {}
    if not ws.is_dir():
        return out
    for f in ws.rglob("*"):
        if not f.is_file():
            continue
        try:
            # CONTENT hash under results/ — that subtree holds the owner's
            # archived task evidence, and the fixture task ids are FIXED
            # (test-task-a/b/c/d), so a destructive OVERWRITE of an existing file
            # is the realistic collision (john-the-dev, #2619). A same-size
            # rewrite is exactly what a name-only or size-only check misses.
            # (size, mtime) elsewhere keeps a whole-workspace scan cheap.
            if "results" in f.parts:
                out[str(f)] = ("sha", hashlib.sha256(f.read_bytes()).hexdigest())
            else:
                st = f.stat()
                out[str(f)] = (st.st_size, st.st_mtime)
        except OSError:
            pass
    return out


def test_no_writes_reach_the_live_workspace(live_ws) -> int:
    """THE regression: a run must not touch the owner's results/ or outbox log.

    Baseline is taken by `main()` BEFORE any case runs, not here — taking it
    here would only cover this function's own activity, so removing the
    redirect from the cases would still pass.

    Asserts on the RESOLVED live workspace rather than a fixture path, because
    the defect was precisely that the fixture escaped to wherever that resolves.
    """
    now = _workspace_fingerprint(live_ws)
    before = _LIVE_BASELINE

    # Compare the UNION of keys. Iterating `now` alone cannot see a DELETION:
    # a path in `before` and absent from `now` never comes up, so the loop
    # finds nothing and the guard certifies "untouched".
    #
    # john-the-dev reproduced exactly that on #2619 \u2014 seed
    # results/test-task-{a,b,c}.txt, drop RESULTS_DIR from the rebind, run:
    # exit 0 and "live workspace untouched (0 paths compared)" while all three
    # files were GONE. The `0` was the tell; it counted `now`, which was empty
    # precisely BECAUSE everything had been deleted.
    #
    # Deletion of the owner's staged results is the worst case this guard
    # exists for, so it is reported as its own category rather than folded in.
    deleted = sorted(k for k in before if k not in now)
    added = sorted(k for k in now if k not in before)
    modified = sorted(k for k in (set(before) & set(now)) if before[k] != now[k])

    if deleted or added or modified:
        print(f"  \u2717 live workspace was written to \u2014 {len(deleted)} deleted, "
              f"{len(modified)} modified, {len(added)} added:", file=sys.stderr)
        for label, group in (("DELETED", deleted), ("MODIFIED", modified), ("ADDED", added)):
            for c in group[:5]:
                print(f"      {label}: {c}", file=sys.stderr)
        return 1
    print(f"  \u2713 live workspace untouched "
          f"({len(set(before) | set(now))} paths compared, union of before+after)")
    return 0


def _auto_thread_reference(task_id, channel):
    """Drive one poll with an anchor registered and NO explicit directive."""
    bridge.pending_reply_anchors.clear()
    bridge.pending_reply_anchors[task_id] = 1500000000000000009
    sent = asyncio.run(_run_one_poll_iteration(task_id, channel, "plain text reply"))
    return sent


def case_e_auto_thread_quotes_in_a_plain_channel():
    """Anchor registered, no directive: the reply quotes what triggered it."""
    fails = []
    sent = _auto_thread_reference("test-task-e", _MockChannel(channel_id=111222333))
    if not sent:
        return ["e) no channel.send call observed"]
    ref = sent[0]["reference"]
    if ref is None or getattr(ref, "message_id", None) != 1500000000000000009:
        fails.append(f"e) plain channel should quote the anchor, got {ref}")
    return fails


def case_f_auto_thread_quotes_inside_a_thread_too():
    """A Discord THREAD gets the same quote: interleaved exchanges make
    position stop identifying what a message answers."""
    fails = []
    sent = _auto_thread_reference("test-task-f", _MockThreadChannel(channel_id=444000111))
    if not sent:
        return ["f) no channel.send call observed"]
    ref = sent[0]["reference"]
    if ref is None:
        fails.append("f) a thread reply carries NO reference — the skip is still in place")
    elif getattr(ref, "message_id", None) != 1500000000000000009:
        fails.append(f"f) thread reply quoted the wrong message: {ref}")
    return fails


def case_g_the_stub_can_actually_see_a_thread():
    """Guard: an absent Thread class reads exactly like a non-thread channel,
    so without this case f would pass for the wrong reason."""
    fails = []
    if getattr(bridge.discord, "Thread", None) is None:
        fails.append("g) the stub exposes no Thread class, so case f proves nothing")
    if not isinstance(_MockThreadChannel(), getattr(bridge.discord, "Thread", ())):
        fails.append("g) _MockThreadChannel is not an instance of the stub's Thread")
    return fails


def main():
    # Baseline BEFORE any case runs, so the final check covers every write this
    # file triggers — not just the ones the regression itself makes.
    from workspace_default import resolve_workspace

    global _LIVE_BASELINE
    live_ws = resolve_workspace()
    _LIVE_BASELINE = _workspace_fingerprint(live_ws)

    cases = [
        ("a-directive-present-constructs-reference", case_a_directive_present_constructs_reference),
        ("b-no-directive-no-reference", case_b_no_directive_no_reference),
        ("c-directive-only-first-chunk", case_c_directive_only_first_chunk),
        ("d-oversized-result-has-a-delivery-budget", case_d_oversized_result_has_a_delivery_budget),
        ("e-auto-thread-quotes-in-a-plain-channel", case_e_auto_thread_quotes_in_a_plain_channel),
        ("f-auto-thread-quotes-inside-a-thread-too", case_f_auto_thread_quotes_inside_a_thread_too),
        ("g-the-stub-can-actually-see-a-thread", case_g_the_stub_can_actually_see_a_thread),
    ]
    failures = []
    with _bridge_writes_redirected() as (_tmp, _redirected):
        for label, fn in cases:
            fs = fn()
            if fs:
                failures.extend([(label, msg) for msg in fs])
                print(f"  ✗ case {label}")
                for f in fs:
                    print(f"      {f}")
            else:
                print(f"  ✓ case {label}")

        # POSITIVE CONTROL for the redirect itself: an outbound observability
        # event must actually LAND inside the throwaway tree. Without this, a
        # redirect that DISABLES the write path (the zero-arg-lambda TypeError
        # qingyun-wu found) looks identical to one that redirects it — the
        # facade swallows the error and every other assertion still passes.
        _events = sorted((_tmp / "logs").glob("events-*.jsonl")) if (_tmp / "logs").is_dir() else []
        if not _events or not any(f.stat().st_size for f in _events):
            print("  ✗ no observability event landed in the throwaway tree — the "
                  "redirect is disabling the write path, not redirecting it", file=sys.stderr)
            failures.append(("observability-redirected", "no events-*.jsonl in tmp/logs"))
        else:
            print(f"  ✓ observability event redirected into the throwaway tree "
                  f"({_events[0].name})")

    if test_resolver_bindings_restored_after_the_context():
        failures.append(("resolver-restored", "lazy consumer left bound to the stub"))
    if test_no_writes_reach_the_live_workspace(live_ws):
        failures.append(("live-workspace-untouched", "fixture escaped to the live workspace"))

    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nAll [reply: <id>] directive invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
