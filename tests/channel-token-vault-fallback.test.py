#!/usr/bin/env python3
"""A bridge must be able to get its token from the vault — and must never leak it.

Before this, `get_vault_key` references in telegram/discord/slack bridges were
0, 0, 0. `vault set TELEGRAM_BOT_TOKEN` stored the value correctly and changed
nothing, so the obvious recovery from a lost token did not work.

**This suite never touches the real Keychain**, and that is not incidental.
@Sutando-Pro tested the vault classifier on 2026-08-04 by calling the real
`intercept_vault_commands()`; it wrote a fake token into the owner's live
Keychain, added a manifest entry, and flipped their own credential probe from
GUTTED to ok — a false green on the instrument built to catch that exact
outage. Every vault read here is injected, and `test_hermetic_no_real_vault`
asserts the real reader is never reached.

Run: python3 tests/channel-token-vault-fallback.test.py
"""
from __future__ import annotations

import io
import contextlib
import importlib
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

ct = importlib.import_module("channel_token")

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name} {detail}")


def main() -> int:
    print("channel token — vault fallback:")

    # --- the prefix-only grep bug: `VAR=` with no value must NOT count --------
    # startup.sh gates on `grep -q "<VAR>="`, which a valueless line satisfies,
    # so a bridge starts unable to authenticate. Every layer must require a value.
    with tempfile.TemporaryDirectory() as td:
        env = Path(td) / ".env"
        env.write_text("TELEGRAM_BOT_TOKEN=\nOTHER=x\n")
        check("an empty `VAR=` line in .env does not count",
              ct.token_from_env_file("TELEGRAM_BOT_TOKEN", env) == "")
        env.write_text('TELEGRAM_BOT_TOKEN="abc123"\n')
        check("quotes are stripped (a quoted token 404s the API verbatim)",
              ct.token_from_env_file("TELEGRAM_BOT_TOKEN", env) == "abc123")
        env.write_text("# TELEGRAM_BOT_TOKEN=commented\n")
        check("a commented-out line does not count",
              ct.token_from_env_file("TELEGRAM_BOT_TOKEN", env) == "")
        check("a missing file is '' not a crash",
              ct.token_from_env_file("X", Path(td) / "nope.env") == "")

    # --- the vault tier ------------------------------------------------------
    check("vault supplies the token when nothing else has it",
          ct.token_from_vault("K", vault_get=lambda k: "from-vault") == "from-vault")
    check("an empty vault value does not count",
          ct.token_from_vault("K", vault_get=lambda k: "") == "")
    check("a vault KeyError degrades to '' (never crashes a bridge at startup)",
          ct.token_from_vault("K", vault_get=lambda k: (_ for _ in ()).throw(KeyError("K"))) == "")
    check("an unavailable keychain degrades to ''",
          ct.token_from_vault("K", vault_get=lambda k: (_ for _ in ()).throw(OSError("no keychain"))) == "")

    # --- resolution order, and that the vault is LAST ------------------------
    with tempfile.TemporaryDirectory() as td:
        env = Path(td) / ".env"
        env.write_text("K=from-file\n")
        called = {"vault": 0}

        def _spy(k):
            called["vault"] += 1
            return "from-vault"

        check("an exported env value wins",
              ct.resolve_channel_token("K", env_file=env, environ={"K": "from-env"},
                                       vault_get=_spy) == "from-env")
        check("...and the vault was not consulted at all", called["vault"] == 0,
              f"vault called {called['vault']}x")
        check("the .env is used when env is empty",
              ct.resolve_channel_token("K", env_file=env, environ={"K": ""},
                                       vault_get=_spy) == "from-file")
        check("...vault still not consulted", called["vault"] == 0)
        env.write_text("K=\n")
        check("the vault answers when env AND .env are empty",
              ct.resolve_channel_token("K", env_file=env, environ={}, vault_get=_spy) == "from-vault")
        check("...and only then was it consulted", called["vault"] == 1,
              f"vault called {called['vault']}x")

    # --- the CLI gate for startup.sh ----------------------------------------
    with tempfile.TemporaryDirectory() as td:
        env = Path(td) / ".env"
        env.write_text("GATEVAR=real-token\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ct.main(["--has", "GATEVAR", "--env-file", str(env)])
        check("--has exits 0 when a usable token exists", rc == 0, f"rc={rc}")
        check("--has prints NOTHING on success (the value cannot reach a log)",
              buf.getvalue() == "", repr(buf.getvalue()))

        env.write_text("GATEVAR=\n")
        # No vault entry for this name -> the empty file value must not satisfy it.
        rc = ct.main(["--has", "GATEVAR", "--env-file", str(env)])
        check("--has exits nonzero for `VAR=` with no value "
              "(the exact state the grep gate passes)", rc != 0, f"rc={rc}")
        check("--has with no VAR is a usage error, not a false 0",
              ct.main(["--has"]) == 2)

    # --- HERMETIC ------------------------------------------------------------
    # If `token_from_vault` ever reaches the real reader, a test run could write
    # to or read from the operator's live Keychain. Prove the injected path is
    # the one taken by making the real import fail loudly if used.
    real_used = {"n": 0}
    orig = sys.modules.get("vault_intercept")
    class _Tripwire:
        def __getattr__(self, name):
            real_used["n"] += 1
            raise AssertionError("the REAL vault reader was reached from a test")
    sys.modules["vault_intercept"] = _Tripwire()
    try:
        got = ct.token_from_vault("ANY", vault_get=lambda k: "injected")
    finally:
        if orig is not None:
            sys.modules["vault_intercept"] = orig
        else:
            sys.modules.pop("vault_intercept", None)
    check("HERMETIC — an injected vault_get bypasses the real reader entirely",
          got == "injected" and real_used["n"] == 0,
          f"got={got!r} real_reader_touches={real_used['n']}")

    # --- every bridge actually calls the tier -------------------------------
    # The helper existing is not the fix; the bridges using it is.
    for bridge, var in (("discord-bridge.py", "DISCORD_BOT_TOKEN"),
                        ("telegram-bridge.py", "TELEGRAM_BOT_TOKEN"),
                        ("slack-bridge.py", "SLACK_BOT_TOKEN")):
        src = (REPO / "src" / bridge).read_text()
        check(f"{bridge} consults the vault for {var}",
              "token_from_vault" in src and var in src)

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("channel token vault fallback: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
