#!/usr/bin/env python3
"""Behavioral: the proactive channel-redirect attachment loop reports every outcome.

A sibling structural test pins the wiring in source. This one drives the real
coroutine, because a source-shape assertion cannot tell whether the branch runs,
and the branch is the whole fix.

Covers BOTH copies of the loop: poll_proactive's channel-redirect and
poll_dm_fallback's. Reaching the latter took one more gate than expected --
its tier check reads access_tier off the ORIGINATING TASK FILE, not access.json,
so a result with no matching task defaults to "other" and is dropped.

The defect: a path that EXISTS but fails the allowlist matched neither
`_is_path_sendable` nor `not os.path.isfile`, so it was dropped with no send, no
log and no exception -- byte-identical to a successful attach from the outside.

Three stubs are needed to reach the loop, each standing in for a gate that
precedes it: `fetch_user` (the allowFrom walk requires a NON-BOT user, and the
discord stub's user is not one), a channel whose `send` records, and
`deliver_text` (the real provider refuses every chunk without a live token, and
the text leg runs BEFORE the attachments).
"""
import asyncio
import contextlib
import importlib.util
import io
import os
import pathlib
import shutil
import sys
import tempfile
import time
import types

REPO = pathlib.Path(__file__).resolve().parent.parent
prior = os.environ.get("CLAUDE_CONFIG_DIR")
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-attach-outcomes-")
_cfg = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / ".env").write_text("DISCORD_BOT_TOKEN=test-stub-token\n")
# The dm-fallback redirect refuses a target whose tier is not owner, so the
# target id needs an owner entry or that loop is never reached.
(_cfg / "access.json").write_text(
    '{"allowFrom": ["1022910063620390932"], "groups": {}, '
    '"tierMap": {"1022910063620390932": "owner", "1530802402603700415": "owner"}}\n')
tmp = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"])
BRIDGE = REPO / "src" / "discord-bridge.py"
TARGET = 1530802402603700415
fail = 0


def check(cond, label):
    global fail
    print(("PASS: " if cond else "FAIL: ") + label)
    if not cond:
        fail = 1


def load_bridge():
    sib = REPO / "tests" / "discord-bridge-collaborator-tier.test.py"
    spec = importlib.util.spec_from_loader("sib_stub", loader=None)
    m = importlib.util.module_from_spec(spec)
    m.__file__ = str(sib)
    exec(compile(sib.read_text(), str(sib), "exec"), m.__dict__)
    m._install_discord_stub()
    spec2 = importlib.util.spec_from_loader("bridge_ao", loader=None)
    b = importlib.util.module_from_spec(spec2)
    b.__file__ = str(BRIDGE)
    # Real path, so executed lines are attributed to src/discord-bridge.py.
    exec(compile(BRIDGE.read_text(), str(BRIDGE), "exec"), b.__dict__)
    return b


try:
    bridge = load_bridge()
    results, state = tmp / "results", tmp / "state"
    tasks = tmp / "tasks"
    for d in (results, state, tasks, results / "undelivered"):
        d.mkdir(parents=True, exist_ok=True)
    bridge.RESULTS_DIR, bridge.STATE_DIR = results, state
    if hasattr(bridge, "TASKS_DIR"):
        bridge.TASKS_DIR = tasks

    sends = []

    class Ch:
        id = TARGET
        name = "target"

        async def send(self, *a, **k):
            sends.append((a, k))
            return types.SimpleNamespace(id=1)

    class U:
        bot = False

        async def create_dm(self):
            return Ch()

    async def fetch_user(_uid):
        return U()

    async def fetch_channel(_cid):
        return Ch()

    bridge.client.fetch_user = fetch_user
    bridge.client.fetch_channel = fetch_channel
    if hasattr(bridge.client, "get_channel"):
        bridge.client.get_channel = lambda _cid: Ch()
    bridge.discord_proactive_send.deliver_text = lambda *a, **k: None
    bridge._PROACTIVE_PROVIDER = object()

    async def one_pass():
        with contextlib.suppress(Exception):
            await asyncio.wait_for(bridge.poll_proactive(), timeout=3.0)

    def run(body: str) -> str:
        sends.clear()
        f = results / "proactive-case.txt"
        f.write_text(body)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            asyncio.run(one_pass())
        return buf.getvalue()

    # ---- REFUSED: a real file that is NOT allowlisted ----------------------
    refused = tmp / "not-allowlisted.txt"
    refused.write_text("payload")
    assert refused.is_file() and not bridge._is_path_sendable(str(refused)), (
        "fixture invalid: must EXIST and be REFUSED, else the test cannot tell "
        "the defect from a missing file")
    out = run(f"[channel: {TARGET}]\nbody\n[file: {refused}]\n")
    check("REJECTED file" in out and "not in allowlist" in out,
          "an allowlist-refused attachment is LOGGED")
    check(any("file not allowed" in str(a) for a, _ in sends),
          "and the refusal is SURFACED to the target, not swallowed")

    # ---- EMPTY: a CALLER must ACT on it, not merely receive it -------------
    from result_markers import parse_markers as _pm
    assert [a.value for a in _pm("[file: ]\n").actions
            if a.kind == "attach"] == [""], "fixture invalid: blank marker must parse to attach('')"
    oute = run(f"[channel: {TARGET}]\nbody\n[file: ]\n")
    check("EMPTY path" in oute,
          "an EMPTY attachment path is LOGGED, not silently dropped")
    check(any("had no path" in str(a) for a, _ in sends),
          "and the malformed marker is SURFACED to the target")

    # ---- SEND: an allowlisted file ----------------------------------------
    ok_dir = pathlib.Path(bridge.SEND_ALLOWED_ROOTS[0])
    sendable = None
    if ok_dir.is_dir():
        fd, p = tempfile.mkstemp(suffix=".txt", dir=str(ok_dir))
        os.close(fd)
        sendable = pathlib.Path(p)
    if sendable and bridge._is_path_sendable(str(sendable)):
        out2 = run(f"[channel: {TARGET}]\nbody\n[file: {sendable}]\n")
        check("sent file:" in out2, "a permitted attachment LOGS its success")
        check(any("file" in k for _, k in sends),
              "and is actually attached")
        sendable.unlink(missing_ok=True)
    else:
        print("SKIP: no writable allowed root on this host for the SEND case")

    # dm-fallback: tier is read off the ORIGINATING TASK FILE (the
    # missing-task case is driven below).
    (tasks / "question-attach.txt").write_text(
        "id: question-attach\naccess_tier: owner\ntask: probe\n")
    fb = results / "question-attach.txt"
    fb.write_text(f"[channel: {TARGET}]\nbody\n[file: {refused}]\n")
    aged = time.time() - 400          # past the 90s grace window
    os.utime(fb, (aged, aged))
    sends.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        with contextlib.suppress(Exception):
            asyncio.run(asyncio.wait_for(bridge.poll_dm_fallback(), timeout=3.0))
    fbout = buf.getvalue()
    check("REJECTED file" in fbout and "dm-fallback" in fbout,
          "dm-fallback: an allowlist-refused attachment is LOGGED")
    check(any("file not allowed" in str(a) for a, _ in sends),
          "dm-fallback: and the refusal is SURFACED to the target")

    # dm-fallback EMPTY: the other call site, driven the same way.
    (tasks / "question-empty.txt").write_text(
        "id: question-empty\naccess_tier: owner\ntask: probe\n")
    fe = results / "question-empty.txt"
    fe.write_text(f"[channel: {TARGET}]\nbody\n[file: ]\n")
    os.utime(fe, (aged, aged))
    sends.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        with contextlib.suppress(Exception):
            asyncio.run(asyncio.wait_for(bridge.poll_dm_fallback(), timeout=3.0))
    feout = buf.getvalue()
    check("EMPTY path" in feout and "dm-fallback" in feout,
          "dm-fallback: an EMPTY attachment path is LOGGED")
    check(any("had no path" in str(a) for a, _ in sends),
          "dm-fallback: and the malformed marker is SURFACED")

    # dm-fallback MISSING TASK: no originating task file anywhere, so the tier
    # read raises and falls to "guest"; the redirect must be dropped, not sent.
    fm = results / "question-missing.txt"
    fm.write_text(f"[channel: {TARGET}]\nbody\n")
    os.utime(fm, (aged, aged))
    sends.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        with contextlib.suppress(Exception):
            asyncio.run(asyncio.wait_for(bridge.poll_dm_fallback(), timeout=3.0))
    fmout = buf.getvalue()
    check("[dm-fallback channel-redirect] dropped — tier 'guest'" in fmout,
          "dm-fallback: a result with NO task file falls to guest and is dropped")
    check(not any(str(TARGET) in str(k) for _, k in sends) and "sent question-missing.txt" not in fmout,
          "dm-fallback: and nothing reached the redirect target")

    # Control: the refused-branch probe must be able to score zero.
    check("REJECTED file" not in "", "control: the probe matches nothing on empty output")
    check("EMPTY path" not in "", "control: the EMPTY probe also scores zero on empty output")
finally:
    if prior is None:
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
    else:
        os.environ["CLAUDE_CONFIG_DIR"] = prior
    shutil.rmtree(tmp, ignore_errors=True)

if fail:
    print("FAIL: proactive redirect attachment outcomes")
    sys.exit(1)
print("PASS: the redirect attachment loop reports every outcome.")
