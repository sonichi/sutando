#!/usr/bin/env python3
"""Every Discord consumer resolves its bot token through channel_token.

Five private resolvers had drifted: quote-stripping differed (the bridge kept
quotes a `.env` value carried; the readers stripped them), vault reachability
differed (discord-read/read_discord_channel/dm-result never consulted it), and
dm-result checked a repo-root .env no other path knew about. This pins the
unification: the three importable consumers are exercised BEHAVIORALLY against
a temp .env + stubbed vault; the bridge (import-time resolution, heavy SDK
imports) is pinned structurally — its token block must call
resolve_channel_token and carry no private line-scan.

Run: python3 tests/discord-token-delegation.test.py
"""
import importlib.util
import os
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
sys.path.insert(0, str(SRC))

# Isolate channel config BEFORE any bridge-file load: dm-result resolves
# channel paths at import, and an unset CLAUDE_CONFIG_DIR reads the real one.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-token-delegation-")
_cfg_discord = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_cfg_discord.mkdir(parents=True, exist_ok=True)
(_cfg_discord / "access.json").write_text('{"allowFrom": []}')

import channel_token  # noqa: E402

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


def main() -> int:
    env_backup = os.environ.pop("DISCORD_BOT_TOKEN", None)
    try:
        with tempfile.TemporaryDirectory() as td:
            envf = Path(td) / ".env"

            dread = load("dread_t", "discord-read.py")
            rdc = load("rdc_t", "read_discord_channel.py")

            # 1. Quoted .env values are stripped for every consumer (shared _clean).
            envf.write_text('DISCORD_BOT_TOKEN="tok-quoted"\n')
            check("discord-read strips quotes",
                  dread._load_token(envf) == "tok-quoted")
            rdc.ENV_FILE = envf
            check("read_discord_channel strips quotes",
                  rdc._bot_token() == "tok-quoted")

            # 1b. Drift population: the shapes where the five old parsers
            #     disagreed — pin the unified one-matching-layer semantics.
            envf.write_text("DISCORD_BOT_TOKEN=\"abc'\n")
            check("mismatched quotes kept verbatim (discord-read)",
                  dread._load_token(envf) == "\"abc'")
            check("mismatched quotes kept verbatim (read_discord_channel)",
                  rdc._bot_token() == "\"abc'")
            envf.write_text('DISCORD_BOT_TOKEN=""abc""\n')
            check("doubled quotes: exactly one layer stripped (discord-read)",
                  dread._load_token(envf) == '"abc"')
            check("doubled quotes: exactly one layer stripped (read_discord_channel)",
                  rdc._bot_token() == '"abc"')
            envf.write_text('DISCORD_BOT_TOKEN="tok-quoted"\n')

            # 2. Process env wins over the file for both.
            os.environ["DISCORD_BOT_TOKEN"] = "tok-env"
            check("discord-read: env wins", dread._load_token(envf) == "tok-env")
            check("read_discord_channel: env wins", rdc._bot_token() == "tok-env")
            del os.environ["DISCORD_BOT_TOKEN"]

            # 3. Vault is reached when env + file are empty — previously these
            #    consumers never consulted it (positive control: stub returns).
            envf.write_text("DISCORD_BOT_TOKEN=\n")
            real_vault = channel_token.token_from_vault
            channel_token.token_from_vault = lambda var, vault_get=None: "tok-vault"
            try:
                check("discord-read reaches vault", dread._load_token(envf) == "tok-vault")
                check("read_discord_channel reaches vault", rdc._bot_token() == "tok-vault")
            finally:
                channel_token.token_from_vault = real_vault

            # 4. Empty everywhere -> falsy, and read_discord_channel keeps its
            #    None-on-absent API (callers do `if not token`/`is None` checks).
            check("read_discord_channel absent -> None", rdc._bot_token() is None)
            check("discord-read absent -> ''", dread._load_token(envf) == "")

            # 5. dm-result: shared resolution first, repo/.env only as a
            #    legacy tier (redirected claude_home_path; no real config).
            dmr = load("dmr_t", "dm-result.py")
            # Pin the PRODUCTION source before injecting paths: REPO is
            # resolve_workspace() — the workspace .env, NOT the repo root.
            import workspace_default
            check("dm-result legacy tier reads the WORKSPACE .env",
                  dmr.REPO == workspace_default.resolve_workspace())
            chan_env = Path(td) / "chan.env"
            repo_env = Path(td) / "repo.env"
            chan_env.write_text("DISCORD_BOT_TOKEN=tok-chan\n")
            repo_env.write_text("DISCORD_BOT_TOKEN=tok-repo\n")
            dmr.claude_home_path = lambda *a: chan_env
            dmr.REPO = Path(td)
            real_repo_env = Path(td) / ".env"
            real_repo_env.write_text("DISCORD_BOT_TOKEN=tok-repo\n")
            check("dm-result: channel .env wins over workspace .env",
                  dmr._load_token() == "tok-chan")
            chan_env.write_text("DISCORD_BOT_TOKEN=\n")
            check("dm-result: workspace .env legacy tier still reachable",
                  dmr._load_token() == "tok-repo")
            # divergence: resolved wins AND the flip is logged (values never).
            chan_env.write_text("DISCORD_BOT_TOKEN=tok-chan\n")
            import contextlib
            import io
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                got = dmr._load_token()
            check("dm-result divergence: resolved source wins", got == "tok-chan")
            check("dm-result divergence is logged, values are not",
                  "differs from the resolved source" in buf.getvalue()
                  and "tok-chan" not in buf.getvalue()
                  and "tok-repo" not in buf.getvalue())

        # 6. Bridge (structural): token block delegates, no private scan left.
        bridge_src = (SRC / "discord-bridge.py").read_text()
        check("bridge calls resolve_channel_token",
              'resolve_channel_token("DISCORD_BOT_TOKEN"' in bridge_src)
        check("bridge has no private DISCORD_BOT_TOKEN= line-scan",
              not re.search(r'startswith\(["\']DISCORD_BOT_TOKEN=', bridge_src))

        # 7. No consumer keeps a private KEY= parser for this token.
        for fname in ("discord-read.py", "read_discord_channel.py", "dm-result.py"):
            body = (SRC / fname).read_text()
            check(f"{fname} has no private DISCORD_BOT_TOKEN= parser",
                  not re.search(r'startswith\(["\']DISCORD_BOT_TOKEN=', body)
                  and "partition(\"=\")" not in body.split("def _load_token")[-1][:400])
    finally:
        if env_backup is not None:
            os.environ["DISCORD_BOT_TOKEN"] = env_backup

    if FAILS:
        print(f"\nFAILED: {len(FAILS)}: {FAILS}", file=sys.stderr)
        return 1
    print("\nPASS: all Discord consumers resolve DISCORD_BOT_TOKEN via channel_token")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
