#!/usr/bin/env python3
"""The two Discord readers share one renderer and one contextNotFrom policy.

Pins the merge's claims: (1) discord-read's --serving mode runs the gate BEFORE
any fetch and exits 2 with nothing fetched on a block; (2) the gated reader now
renders forwards and reply context via the shared renderer (its private copy
printed blank lines for forwards); (3) both CLIs render a forward identically —
single implementation, not two agreeing copies; (4) the shared policy keeps the
fail-closed branch under an injected resolver.

Run: python3 tests/discord-reader-merge.test.py
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
sys.path.insert(0, str(SRC))

os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-reader-merge-")
_cfg_discord = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_cfg_discord.mkdir(parents=True, exist_ok=True)
(_cfg_discord / "access.json").write_text('{"allowFrom": []}')

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok: {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL: {name} {detail}", file=sys.stderr)


def load(mod_name, filename):
    spec = importlib.util.spec_from_file_location(mod_name, SRC / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


FORWARD_MSG = {
    "id": "123", "timestamp": "2026-08-18T00:00:00", "content": "",
    "author": {"username": "fwd-er"},
    "message_snapshots": [{"message": {
        "content": "the forwarded substance",
        "attachments": [{"filename": "log.txt"}], "embeds": [],
    }}],
}


def main() -> int:
    dr = load("dr_m", "discord-read.py")
    rdc = load("rdc_m", "read_discord_channel.py")

    # 1. --serving gate runs BEFORE any fetch; block -> exit 2, zero fetches.
    dr._load_token = lambda env: "tok"
    fetched = []
    dr._fetch = lambda *a, **k: fetched.append(1) or []

    class _BlockingPolicy:
        @staticmethod
        def gate(serving, target, token, **kw):
            return "blocked-by-test"
    dr.discord_context_policy = _BlockingPolicy()
    rc = dr.main(["999", "--serving", "111"])
    check("--serving block -> exit 2", rc == 2)
    check("--serving block -> NOTHING fetched", fetched == [])

    class _OpenPolicy:
        @staticmethod
        def gate(serving, target, token, **kw):
            return None
    dr.discord_context_policy = _OpenPolicy()
    rc = dr.main(["999", "--serving", "111"])
    check("--serving allow -> proceeds to fetch", rc == 0 and fetched == [1])

    # 1b. The boundary is NOT optional: a bare read refuses (exit 3, nothing
    #     fetched) even under an always-blocking policy that is never consulted.
    dr.discord_context_policy = _BlockingPolicy()
    fetched.clear()
    rc = dr.main(["999"])
    check("bare read -> exit 3 (serving-or-operator required)", rc == 3)
    check("bare read -> NOTHING fetched", fetched == [])
    rc = dr.main(["999", "--operator"])
    check("--operator: explicit privileged fetch, gate not consulted",
          rc == 0 and fetched == [1])

    # 1c. Production callers wired: the bridge instruction template and the
    #     skill doc are the DATA those surfaces emit — content pins, not spelling.
    bridge_src = (SRC / "discord-bridge.py").read_text()
    check("bridge instruction passes --serving",
          "src/discord-read.py {channel_id_str} --serving {channel_id_str}" in bridge_src)
    skill = (REPO / "skills" / "context-reconstruct" / "SKILL.md").read_text()
    check("context-reconstruct doc passes --serving",
          "--serving <task channel_id>" in skill)

    # 2. Gated reader renders forwards via the shared renderer (was blank).
    rdc._api_get = lambda path, token: [FORWARD_MSG]
    out = rdc.fetch_messages("999", 1, "tok")
    check("gated reader shows forwarded content",
          "[forwarded] the forwarded substance" in out and "log.txt" in out, out)

    # 3. Single implementation: both CLIs' _render IS the shared one — the same
    #    forward renders byte-identically through both module namespaces.
    check("one renderer behind both CLIs",
          dr._render(FORWARD_MSG) == sys.modules["channels.discord.reader"]._render(FORWARD_MSG)
          and "[forwarded]" in dr._render(FORWARD_MSG))

    # 4. Shared policy fail-closed: blacklist present + unresolvable guild -> block.
    policy = sys.modules["policy.context.discord"]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        json.dump({"groups": {"srv": {"contextNotFrom": ["private-guild"]}}}, tf)
        acc = Path(tf.name)
    reason = policy.gate("srv", "target-ch", "tok",
                         guild_resolver=lambda t, tok: None, access_file=acc)
    check("policy fail-closed on unresolvable guild",
          reason is not None and "fail-closed" in reason)
    reason = policy.gate("srv", "target-ch", "tok",
                         guild_resolver=lambda t, tok: "private-guild", access_file=acc)
    check("policy blocks guild-level entry", reason is not None and "guild" in reason)
    reason = policy.gate("srv", "target-ch", "tok",
                         guild_resolver=lambda t, tok: "other-guild", access_file=acc)
    check("policy allows unlisted guild", reason is None)

    # 5. Coverage of the remaining policy/reader surfaces (diff-cover bar).
    check("blacklist loader: unreadable file -> empty set",
          policy.load_channel_context_blacklist("srv", access_file=Path("/nonexistent/x.json")) == set())
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        json.dump({"groups": {"srv": {"contextNotFrom": ["g1"]}}}, tf)
        acc2 = Path(tf.name)
    check("blacklist loader: reads channel entries",
          policy.load_channel_context_blacklist("srv", access_file=acc2) == {"g1"})

    real_rj = policy.request_json
    try:
        policy.request_json = lambda req, timeout=10: {"guild_id": 42}
        check("resolve_guild returns str guild id", policy.resolve_guild("t", "tok") == "42")
        policy.request_json = lambda req, timeout=10: {}
        check("resolve_guild: DM channel (no guild) -> None", policy.resolve_guild("t", "tok") is None)
        def _boom(req, timeout=10):
            raise RuntimeError("net down")
        policy.request_json = _boom
        check("resolve_guild: transport failure -> None (fail-closed upstream)",
              policy.resolve_guild("t", "tok") is None)
    finally:
        policy.request_json = real_rj

    reader = sys.modules["channels.discord.reader"]
    line = reader.render_line({"id": "1", "timestamp": "2026-08-18T00:00:00.000",
                               "author": {"username": "u"}, "content": "x" * 300})
    check("render_line clips by default", "[2026-08-18T00:00:00] u: " in line and len(line) < 240)
    full_line = reader.render_line({"id": "1", "timestamp": "", "author": {}, "content": "x" * 300}, full=True)
    check("render_line --full does not clip", "x" * 300 in full_line)

    # rdc wrapper surfaces: loader wrapper + resolve_guild delegate + _api_get.
    rdc.ACCESS_FILE = acc2
    check("rdc loader wrapper binds module ACCESS_FILE",
          rdc.load_channel_context_blacklist("srv") == {"g1"})
    real_pol_rg = sys.modules["policy.context.discord"].resolve_guild
    try:
        sys.modules["policy.context.discord"].resolve_guild = lambda t, tok: "g9"
        check("rdc.resolve_guild delegates to policy", rdc.resolve_guild("t", "tok") == "g9")
    finally:
        sys.modules["policy.context.discord"].resolve_guild = real_pol_rg
    real_rdc_rj = rdc.request_json
    try:
        rdc.request_json = lambda req, timeout=10: [{"author": {"username": "a"}, "content": "hi"}]
        check("rdc._api_get drives request_json", rdc._api_get("/x", "tok") and True)
    finally:
        rdc.request_json = real_rdc_rj

    if FAILS:
        print(f"\nFAILED {len(FAILS)}: {FAILS}", file=sys.stderr)
        return 1
    print("\nPASS: readers merged — one renderer, one gate, gate-before-fetch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
