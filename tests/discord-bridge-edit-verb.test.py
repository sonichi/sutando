#!/usr/bin/env python3
"""`edit` replaces one sent message, and `send` hands back the id that addresses it.

Both halves exist because a delivered reply was previously unrevisable: the id
was discarded at send, so the only way to correct an over-long message was to
send a second one. The refusal case is the load-bearing one — an edit addresses
ONE message, so a body the chunker would split cannot be applied without
silently dropping the remainder.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-editverb-")
os.environ.pop("CLAUDE_HOME", None)
os.environ["SUTANDO_TEST_MODE"] = "1"
_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / ".env").write_text("DISCORD_BOT_TOKEN=test-stub-token\n", encoding="utf-8")
(_cfg / "access.json").write_text('{"allowFrom": []}', encoding="utf-8")

sys.path.insert(0, str(REPO / "src"))

try:
    import discord  # noqa: F401
except ImportError:
    print("SKIP: discord.py not installed in this runner")
    raise SystemExit(0)

_spec = importlib.util.spec_from_file_location("dbridge", REPO / "src" / "discord-bridge.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

FAILS = 0


def check(cond: bool, msg: str) -> None:
    global FAILS
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS += 1


from channels.discord.client import DiscordRestClient  # noqa: E402


def _bind_transport(calls, script=None, default=(200, {"id": "9999"})):
    """Bind a scripted transport through the PRODUCTION DiscordRestClient, so
    these cases exercise the real chokepoint rather than a copied recipe.
    `script` steps are (status, body) tuples or Exceptions; when exhausted the
    `default` step repeats."""
    steps = list(script or [])

    def transport(req, timeout):
        calls.append({"url": req.full_url, "method": req.get_method(),
                      "body": json.loads(req.data.decode()) if req.data else None})
        step = steps.pop(0) if steps else default
        if isinstance(step, Exception):
            raise step
        return step

    _mod._rest_client = lambda timeout=10: DiscordRestClient(
        "test-stub-token", transport=transport)


def main() -> int:
    real = _mod._rest_client
    check(isinstance(real(), DiscordRestClient),
          "_rest_client() builds the shared client (default timeout=10)")
    check(real()._timeout == 10, "_rest_client() keeps the pre-client 10s bound")
    try:
        # 1. edit issues a PATCH at the message-scoped URL
        calls = []
        _bind_transport(calls)
        _mod._edit_via_rest("111", "222", "corrected body")
        check(len(calls) == 1, f"edit issues exactly one request (got {len(calls)})")
        check(calls and calls[0]["method"] == "PATCH",
              f"method is PATCH (got {calls[0]['method'] if calls else None})")
        check(calls and calls[0]["url"].endswith("/channels/111/messages/222"),
              "URL addresses the specific message")
        check(calls and calls[0]["body"] == {"content": "corrected body"},
              "body carries the replacement content")

        # 2. THE LOAD-BEARING CASE: a body the chunker would split is refused,
        #    and refused BEFORE any network call — not truncated.
        calls = []
        _bind_transport(calls)
        try:
            _mod._edit_via_rest("111", "222", "x" * 5000)
            refused = False
        except SystemExit as e:
            refused = e.code != 0
        check(refused, "an over-long body exits non-zero instead of editing")
        check(len(calls) == 0, f"...and issues NO request (got {len(calls)})")

        # 3. empty body refused — an edit to nothing silently blanks a message
        calls = []
        _bind_transport(calls)
        try:
            _mod._edit_via_rest("111", "222", "   ")
            blanked = True
        except SystemExit:
            blanked = False
        check(not blanked and len(calls) == 0, "an empty body is refused, no request")

        # 4. send returns the id an edit needs — the gap that made this necessary
        calls = []
        _bind_transport(calls, default=(200, {"id": "4242"}))
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            _mod._send_via_rest("111", "hello")
        check("message_id 4242" in buf.getvalue(),
              f"send prints the created message_id (got {buf.getvalue()!r})")

        # 4b. The refusals above short-circuit before the request, so the
        #     failing-request path is otherwise never exercised.
        calls = []
        _bind_transport(calls, default=RuntimeError("403 Forbidden"))
        buf = io.StringIO()
        rc = None
        try:
            with redirect_stdout(buf):
                _mod._edit_via_rest("111", "222", "nope")
        except SystemExit as e:
            rc = e.code
        check(rc == 1, f"a failing PATCH exits 1 (got {rc})")
        check("Edit failed" in buf.getvalue(),
              f"...and names the failure (got {buf.getvalue()!r})")

        # 5. A COMMITTED send whose response body is unreadable is still a send.
        #    Reporting it as a failure invites the retry that duplicates it.
        calls = []
        _bind_transport(calls, default=(200, "<html>gateway</html>"))
        buf = io.StringIO()
        rc = None
        try:
            with redirect_stdout(buf):
                _mod._send_via_rest("111", "hello")
        except SystemExit as e:
            rc = e.code
        out = buf.getvalue()
        check(rc is None, f"malformed response body does NOT exit nonzero (got exit {rc})")
        check("Send failed" not in out, "...and is not reported as a send failure")
        check("unavailable" in out, f"...but the id is reported unavailable (got {out!r})")

        # 5b. Body dies mid-read after a 2xx -> (status, None) per
        #     _default_transport (gate test pins that); still committed here.
        calls = []
        _bind_transport(calls, default=(200, None))
        buf = io.StringIO()
        rc = None
        try:
            with redirect_stdout(buf):
                _mod._send_via_rest("111", "hello")
        except SystemExit as e:
            rc = e.code
        out = buf.getvalue()
        check(rc is None, f"a raising read() does NOT exit nonzero (got exit {rc})")
        check("Send failed" not in out, "...and is not reported as a send failure")
        check("unavailable" in out, f"...but the id is reported unavailable (got {out!r})")

        # 6. A later-chunk failure must not swallow the EARLIER chunk's id —
        #    that chunk is delivered and would otherwise be unrevisable.
        calls = []
        _bind_transport(calls, script=[(200, {"id": "7001"})],
                        default=RuntimeError("boom on chunk 2"))
        buf = io.StringIO()
        rc = None
        try:
            with redirect_stdout(buf):
                _mod._send_via_rest("111", "y" * 3000)  # forces >1 chunk
        except SystemExit as e:
            rc = e.code
        out = buf.getvalue()
        check(len(calls) >= 2, f"the body really did chunk (requests={len(calls)})")
        check("message_id 7001" in out,
              f"chunk 1's id is printed BEFORE chunk 2 fails (got {out!r})")
        check(rc == 1, f"and the overall send still fails (exit {rc})")
    finally:
        _mod._rest_client = real

    print(f"\n{'PASS' if FAILS == 0 else str(FAILS) + ' FAILURE(S)'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
