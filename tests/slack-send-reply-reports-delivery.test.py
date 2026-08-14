#!/usr/bin/env python3
"""`_send_reply` must REPORT delivery — the real helper, not a stub.

Stubs only the Slack SDK surface, so a mutation of the helper's return value
is caught here and cannot be caught by a caller-side stub test.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_tmp_home = tempfile.mkdtemp(prefix="slack-delivery-cfg-")
os.environ["CLAUDE_CONFIG_DIR"] = _tmp_home
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-not-real")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-not-real")
_cfg = Path(_tmp_home) / "channels" / "slack"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / "access.json").write_text(json.dumps({"allowFrom": ["UOWNER"]}))

for name in ("slack_bolt", "slack_bolt.adapter", "slack_bolt.adapter.socket_mode",
             "slack_sdk", "slack_sdk.errors"):
    if name not in sys.modules:
        m = types.ModuleType(name)
        if name == "slack_bolt":
            m.App = type("App", (), {"__init__": lambda self, **kw: None,
                                     "event": lambda self, *a, **k: (lambda fn: fn),
                                     "client": types.SimpleNamespace()})
        if name == "slack_bolt.adapter.socket_mode":
            m.SocketModeHandler = type("SocketModeHandler", (),
                                       {"__init__": lambda self, *a, **kw: None})
        if name == "slack_sdk.errors":
            m.SlackApiError = type("SlackApiError", (Exception,), {})
        sys.modules[name] = m


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sb = _load("slack_bridge_delivery", REPO / "src" / "slack-bridge.py")
_LIVE_RESULTS = Path(sb.RESULTS_DIR)
_live_before = sorted(p.name for p in _LIVE_RESULTS.iterdir()) if _LIVE_RESULTS.exists() else None

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name} {detail}")


def _with_slack(post_behaviour, file_ok: bool = True):
    """Install a fake Slack client; return (calls, restore)."""
    calls: list[dict] = []

    def _post(**kwargs):
        calls.append(kwargs)
        return post_behaviour()

    orig_app = sb.app
    orig_send_file = sb._send_file
    sb.app = types.SimpleNamespace(client=types.SimpleNamespace(chat_postMessage=_post))
    sb._send_file = lambda *a, **kw: file_ok

    def restore():
        sb.app = orig_app
        sb._send_file = orig_send_file

    return calls, restore


def _call(text: str, post_behaviour, file_ok: bool = True):
    calls, restore = _with_slack(post_behaviour, file_ok)
    try:
        return sb._send_reply("D1", None, text, access_tier="owner"), calls
    finally:
        restore()


def main() -> int:
    print("slack _send_reply — does it report delivery?")

    def _ok():
        return {"ok": True}

    def _boom():
        raise RuntimeError("chat_postMessage refused")

    # `return delivered_ok` -> `return True` passes every stubbed test; a
    # refused text post has to come back False here.
    got, calls = _call("hello owner", _boom)
    check("text refused -> returns False (the mutation @john-the-dev found)",
          got is False, f"returned {got!r}; an unconditional `return True` looks identical here")
    check("text refused: the send WAS attempted", len(calls) == 1, f"calls={len(calls)}")

    # --- POSITIVE CONTROL -------------------------------------------------
    # Without this, a helper hard-wired to `return False` passes the case above.
    got, calls = _call("hello owner", _ok)
    check("POSITIVE CONTROL — text delivered -> returns True", got is True, f"returned {got!r}")
    check("delivered: exactly one post", len(calls) == 1, f"calls={len(calls)}")

    # A body of just `[file: ...]` posts no text chunk, so a swallowed warning
    # means nothing reached the user at all.
    with tempfile.TemporaryDirectory() as td:
        missing = str(Path(td) / "no-such-file.png")

        got, calls = _call(f"[file: {missing}]", _boom)
        check("attachment-only + not-found notice REFUSED -> False",
              got is False, f"returned {got!r}; the user saw nothing at all")
        check("not-found: the notice was attempted", len(calls) == 1, f"calls={len(calls)}")

        got, _ = _call(f"[file: {missing}]", _ok)
        check("POSITIVE CONTROL — not-found notice DELIVERED -> True",
              got is True, f"returned {got!r}; the user was told the file is missing")

        # Blocked-but-present path: a real file that is not allowlisted.
        blocked = Path(td) / "blocked.bin"
        blocked.write_text("x")
        if not sb._is_path_sendable(str(blocked)):
            got, calls = _call(f"[file: {blocked}]", _boom)
            check("attachment-only + deny notice REFUSED -> False",
                  got is False, f"returned {got!r}")
            got, _ = _call(f"[file: {blocked}]", _ok)
            check("POSITIVE CONTROL — deny notice DELIVERED -> True", got is True, f"returned {got!r}")
        else:
            # Don't silently skip: an allowlist that accepts a tmpdir means this
            # branch is untested, and saying so is better than a quiet pass.
            check("deny branch is reachable (tmpdir is not allowlisted)", False,
                  f"_is_path_sendable({blocked}) is True — deny branch NOT exercised")

    # The allowlist predicate is stubbed rather than worked around: a non-
    # allowlisted path takes the DENY branch, which is a different property.
    with tempfile.TemporaryDirectory() as td:
        real = Path(td) / "payload.png"
        real.write_text("x")
        _orig_sendable = sb._is_path_sendable
        sb._is_path_sendable = lambda p: p == str(real)
        try:
            got, calls = _call(f"here you go [file: {real}]", _ok, file_ok=False)
            check("upload failed -> returns False", got is False, f"returned {got!r}")
            check("upload-failed: the text body still posted",
                  len(calls) == 1, f"calls={len(calls)} — should be the text, not a deny notice")
            got, _ = _call(f"here you go [file: {real}]", _ok, file_ok=True)
            check("POSITIVE CONTROL — upload succeeded -> returns True",
                  got is True, f"returned {got!r}")
        finally:
            sb._is_path_sendable = _orig_sendable

    # --- hermetic ----------------------------------------------------------
    after = sorted(p.name for p in _LIVE_RESULTS.iterdir()) if _LIVE_RESULTS.exists() else None
    check("HERMETIC — the operator's real results/ is untouched", after == _live_before,
          f"before={_live_before if _live_before is None else len(_live_before)} "
          f"after={after if after is None else len(after)}")

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("slack _send_reply delivery reporting: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
