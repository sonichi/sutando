#!/usr/bin/env python3
"""Tests for the Discord reader scripts now that their REST calls route through
the shared 429-backoff helper (src/channels/discord/http.py request_json).

Covers:
  * src/discord-read.py         — _fetch wire-in, main() single-page + --until
                                  pagination/boundary/trim, token load, errors.
  * src/read_discord_channel.py — _api_get wire-in.

Both readers' request_json is stubbed so nothing hits the network. discord-read
is loaded via importlib because its filename isn't a valid module identifier.

Run: python3 tests/discord-readers-429.test.py
"""
from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))


def _load_hyphenated(mod_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(mod_name, SRC / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(cond, label):
    print(("  PASS: " if cond else "  FAIL: ") + label)
    return bool(cond)


def main() -> int:
    print("discord readers — request_json wire-in + reader logic")
    print("=" * 50)
    results = []
    dr = _load_hyphenated("discord_read_cli", "discord-read.py")

    # --- _fetch forwards to request_json ----------------------------------
    captured: dict = {}

    def fake_request_json(req, timeout=10):
        captured["req"] = req
        captured["timeout"] = timeout
        return [{"id": "42"}]

    dr.request_json = fake_request_json
    out = dr._fetch({"before": "100"}, "999", 10, {"Authorization": "Bot x"})
    req = captured.get("req")
    results.append(check(out == [{"id": "42"}], "_fetch returns helper result"))
    results.append(check(isinstance(req, urllib.request.Request), "_fetch hands a Request to request_json"))
    results.append(check(req is not None and "/channels/999/messages" in req.full_url and "limit=10" in req.full_url,
                         "_fetch builds the channel messages URL"))
    results.append(check(req is not None and "before=100" in req.full_url, "_fetch forwards paging cursor"))

    # --- boundary helpers -------------------------------------------------
    results.append(check(dr._at_or_before_boundary({"id": "50"}, "50") is True, "_at_or_before: id equal → True"))
    results.append(check(dr._at_or_before_boundary({"id": "60"}, "50") is False, "_at_or_before: id newer → False"))
    results.append(check(dr._at_or_before_boundary({}, "50") is False, "_at_or_before: missing id → False"))
    results.append(check(dr._at_or_before_boundary({"timestamp": "2026-06-24T00:00"}, "2026-06-25") is True,
                         "_at_or_before: ISO older → True"))
    results.append(check(dr._strictly_older_than_boundary({"id": "40"}, "50") is True, "_strictly_older: id older → True"))
    results.append(check(dr._strictly_older_than_boundary({"id": "50"}, "50") is False, "_strictly_older: id equal → False"))
    results.append(check(dr._strictly_older_than_boundary({}, "50") is False, "_strictly_older: missing id → False"))
    results.append(check(dr._strictly_older_than_boundary({"timestamp": "2026-06-23"}, "2026-06-25") is True,
                         "_strictly_older: ISO older → True"))

    # --- _load_token ------------------------------------------------------
    # The resolver's third tier is the Keychain vault, which is host state — on a
    # machine holding this key, "no file, no env" is not empty. Stub it to zero.
    import os as _os
    import channel_token as _ct
    _ct.token_from_vault = lambda var, vault_get=None: ""
    _os.environ.pop("DISCORD_BOT_TOKEN", None)
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
        f.write('# comment\nDISCORD_BOT_TOKEN=tok-123\n')
        env_path = Path(f.name)
    results.append(check(dr._load_token(env_path) == "tok-123", "_load_token reads token from env file"))
    _os.environ.pop("DISCORD_BOT_TOKEN", None)
    results.append(check(dr._load_token(Path("/nonexistent/x.env")) == "", "_load_token: no file, no env → empty"))

    # --- main() no token → returns 1 --------------------------------------
    dr._load_token = lambda env: ""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = dr.main(["999", "--operator"])
    results.append(check(rc == 1, "main: missing token → returns 1"))

    # --- main() single page (no --until) ----------------------------------
    dr._load_token = lambda env: "tok"
    dr.request_json = lambda req, timeout=10: [
        {"id": "2", "author": {"username": "bob"}, "content": "hi", "timestamp": "2026-07-01T10:00:00Z"},
        {"id": "1", "author": {"username": "amy"}, "content": "yo", "timestamp": "2026-07-01T09:00:00Z"},
    ]
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = dr.main(["999", "--operator"])
    printed = buf.getvalue()
    results.append(check(rc == 0 and "amy" in printed and "bob" in printed and printed.index("amy") < printed.index("bob"),
                         "main: single page prints oldest-first, returns 0"))

    # --- main() with --until: paginate, hit boundary, trim ----------------
    pages = [
        [{"id": "30", "author": {"username": "c"}, "content": "new", "timestamp": "t3"}],
        [{"id": "20", "author": {"username": "b"}, "content": "mid", "timestamp": "t2"},
         {"id": "10", "author": {"username": "a"}, "content": "old", "timestamp": "t1"}],
    ]
    seq = iter(pages)
    dr.request_json = lambda req, timeout=10: next(seq)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = dr.main(["999", "--until", "20", "--operator"])
    printed = buf.getvalue()
    # boundary=20 → id 10 is strictly older, trimmed; 20 and 30 kept.
    results.append(check(rc == 0 and "mid" in printed and "new" in printed and "old" not in printed,
                         "main: --until trims strictly-older, keeps boundary+newer"))

    # --- main() with --until: empty first page → loop breaks, no output ----
    dr.request_json = lambda req, timeout=10: []
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = dr.main(["999", "--until", "20", "--operator"])
    results.append(check(rc == 0 and buf.getvalue() == "", "main: --until empty page breaks cleanly, returns 0"))

    # --- main() fetch error → returns 1 -----------------------------------
    def boom(req, timeout=10):
        raise RuntimeError("429 storm")

    dr.request_json = boom
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = dr.main(["999", "--operator"])
    results.append(check(rc == 1, "main: fetch error → returns 1"))

    # --- read_discord_channel.py::_api_get --------------------------------
    import read_discord_channel as rdc  # noqa: E402
    seen: dict = {}

    def fake_rj(req, timeout=10):
        seen["req"] = req
        return {"guild_id": "7"}

    rdc.request_json = fake_rj
    got = rdc._api_get("/channels/5", "tok")
    api_req = seen.get("req")
    results.append(check(got == {"guild_id": "7"}, "_api_get returns helper result"))
    results.append(check(api_req is not None and api_req.full_url.endswith("/channels/5"),
                         "_api_get targets the requested path"))
    results.append(check(api_req is not None and api_req.get_header("Authorization") == "Bot tok",
                         "_api_get sends the bot auth header"))

    print("=" * 50)
    passed = sum(1 for x in results if x)
    print(f"{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
