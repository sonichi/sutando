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
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# Isolate the channel config BEFORE any bridge is imported. `channel_access_path()`
# falls back to the LEGACY real-home `~/.claude/channels/<ch>/access.json` when the
# canonical path is missing, so clearing the token env var alone is NOT isolation —
# the bridge still reads the operator's real allowlist. Caught here by
# `scripts/lint-hermetic-bridge-tests.py`: an earlier revision of this file set
# CLAUDE_CONFIG_DIR inside the loader function, which is invisible to the lint AND
# left the legacy fallback reachable (the run printed the [util_paths] DEPRECATION
# banner naming the operator's real path — the hole announcing itself).
# Seeded with LITERAL channel names, not a loop: the lint resolves path segments
# statically, and a loop variable is not a literal it can prove. Writing the file
# matters as much as setting the var — an EMPTY temp config dir still sends
# `channel_access_path()` down the legacy fallback.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-vault-fallback-")
_cfg_discord = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_cfg_discord.mkdir(parents=True, exist_ok=True)
(_cfg_discord / "access.json").write_text('{"allowFrom": []}')
_cfg_slack = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "slack"
_cfg_slack.mkdir(parents=True, exist_ok=True)
(_cfg_slack / "access.json").write_text('{"allowFrom": []}')
_cfg_telegram = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "telegram"
_cfg_telegram.mkdir(parents=True, exist_ok=True)
(_cfg_telegram / "access.json").write_text('{"allowFrom": []}')

ct = importlib.import_module("channel_token")

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name} {detail}")


def _load_bridge_starved(filename: str, mod_name: str, wants: list[str], stubs: tuple,
                         vault_value=None):
    """Load a bridge with NO token in env and NO `.env` on disk — vault only.

    HERMETIC by construction: `CLAUDE_CONFIG_DIR` points at an empty temp dir so
    the real `channels/*/.env` is unreachable, and `channel_token` is replaced in
    `sys.modules` so the bridge's `from channel_token import token_from_vault`
    binds to a stub. The operator's Keychain is never consulted — same rule the
    rest of this suite holds itself to.

    Returns (module, [vars the vault was asked for]) or None if the module could
    not be loaded in this environment (missing third-party dep, etc.).
    """
    import importlib.util
    import types

    asked: list[str] = []
    fake_ct = types.ModuleType("channel_token")
    fake_ct.token_from_vault = lambda var, vault_get=None: (
        asked.append(var) or (f"vault-{var}" if vault_value is None else vault_value))
    # discord-bridge resolves via resolve_channel_token now; mirror its order
    # over the SAME stub vault so `asked` still records the consult.
    fake_ct.resolve_channel_token = lambda var, env_file=None, environ=None, vault_get=None: (
        (environ or os.environ).get(var, "").strip()
        or fake_ct.token_from_vault(var))

    saved_mods = {k: sys.modules.get(k) for k in ("channel_token", *stubs)}
    saved_env = {k: os.environ.get(k) for k in (*wants, "SUTANDO_TEST_MODE")}
    # CLAUDE_CONFIG_DIR is isolated at MODULE level (see the top of this file) —
    # deliberately not re-pointed per call: the lint only recognises module-level
    # isolation, and one temp root that is seeded once is easier to reason about.
    try:
        sys.modules["channel_token"] = fake_ct
        for s in stubs:
            if s == "discord":
                d = types.ModuleType("discord")
                d.Intents = type("Intents", (), {"default": staticmethod(lambda: types.SimpleNamespace(
                    message_content=True, members=True, guilds=True))})
                d.Client = type("Client", (), {
                    "__init__": lambda self, **k: None,
                    # discord.py registers handlers via `@client.event`; without
                    # it the module dies AFTER the token block, which would still
                    # pass the assertion below but on a half-executed module.
                    "event": lambda self, fn=None: (fn if fn else (lambda f: f)),
                    "run": lambda self, *a, **k: None,
                    "get_channel": lambda self, *a, **k: None,
                })
                d.File = type("File", (), {"__init__": lambda self, *a, **k: None})
                d.Object = type("Object", (), {"__init__": lambda self, *a, **k: None})
                d.errors = types.SimpleNamespace(HTTPException=Exception, Forbidden=Exception)
                sys.modules["discord"] = d
            elif s == "slack_bolt":
                b = types.ModuleType("slack_bolt")
                b.App = type("App", (), {"__init__": lambda self, **k: None,
                                         "event": lambda self, *a, **k: (lambda fn: fn),
                                         "message": lambda self, *a, **k: (lambda fn: fn),
                                         "command": lambda self, *a, **k: (lambda fn: fn)})
                sys.modules["slack_bolt"] = b
                ad = types.ModuleType("slack_bolt.adapter")
                sm = types.ModuleType("slack_bolt.adapter.socket_mode")
                sm.SocketModeHandler = type("H", (), {"__init__": lambda self, *a, **k: None})
                sys.modules["slack_bolt.adapter"] = ad
                sys.modules["slack_bolt.adapter.socket_mode"] = sm
        for v in wants:
            os.environ.pop(v, None)
        os.environ["SUTANDO_TEST_MODE"] = "1"
        spec = importlib.util.spec_from_file_location(mod_name, REPO / "src" / filename)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except SystemExit as exc:
            # A bridge with no token anywhere prints the `vault set` hint and
            # exits 1. That is the intended refusal, not a test failure.
            return "EXIT" if (exc.code or 0) != 0 else "EXIT0"
        except BaseException as exc:
            print(f"    (note: {filename} raised {type(exc).__name__}: {str(exc)[:90]})")
            return (mod, asked) if asked else None
        return mod, asked
    finally:
        for k, v in saved_mods.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


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
        check("--has exits 3 for `VAR=` with no value "
              "(the exact state the grep gate passes)", rc == 3, f"rc={rc}")
        check("--has with no VAR is a usage error (2 = cannot answer), not a false 0",
              ct.main(["--has"]) == 2)

    # A DEFINITIVE no must be distinguishable from a BROKEN resolver, or
    # startup.sh cannot decide whether to refuse the bridge or fall back to the
    # old grep. Python exits 1 for a syntax error AND for any uncaught exception,
    # so 1 can never mean "no". Measured, not assumed:
    #     syntax error -> 1     missing file -> 2     answered no -> 3
    # Falling back on a definitive no is what reinstates the empty-value pass
    # this gate exists to close (@Sutando-Pro reproduced exactly that on #2638).
    # Stub the vault tier: `main()` takes no injection hook, so calling it with a
    # real name would consult the OPERATOR'S keychain — the very hermeticity this
    # suite asserts two blocks down. Reading is milder than Pro's write, but a
    # suite that exempts itself from its own rule is not hermetic.
    _real_tfv = ct.token_from_vault
    ct.token_from_vault = lambda var, vault_get=None: ""
    try:
        with tempfile.TemporaryDirectory() as td:
            empty = Path(td) / "empty.env"
            empty.write_text("UNRELATED=x\n")
            rc_no = ct.main(["--has", "NOT_A_REAL_TOKEN_XYZ", "--env-file", str(empty)])
        check("a definitive NO is 3, a code Python will not emit on its own", rc_no == 3,
              f"rc={rc_no}")
        check("...and it is NOT 1, which a syntax error or uncaught raise produces",
              rc_no != 1)
    finally:
        ct.token_from_vault = _real_tfv

    # Both sides of the contract, or it only half exists: startup.sh must branch
    # on 3 (refuse) and must NOT treat 1 (broken resolver) as an answer.
    _startup = (REPO / "src" / "startup.sh").read_text()
    check("all 3 startup gates branch on the definitive-no code",
          _startup.count('_tok_rc" -eq 3') == 3, f"found {_startup.count(chr(95)+chr(116)+chr(111)+chr(107)+chr(95)+chr(114)+chr(99)+chr(34)+' -eq 3')}")
    check("no startup gate treats 1 as a definitive no",
          '_tok_rc" -eq 1' not in _startup)

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
    # The helper existing is not the fix; the bridges USING it is. This used to
    # assert `"token_from_vault" in src` — a substring check that passes on a
    # commented-out call, a call in dead code, or the wrong variable name, and
    # that executes none of the added lines. `diff coverage` caught it honestly:
    # 25% on all three bridges. Counting a reference is not exercising it.
    #
    # So each bridge is LOADED with no token anywhere except the vault, and the
    # assertion is that its module-level TOKEN ends up holding the vault value.
    for bridge, mod_name, wants, stubs in (
        ("telegram-bridge.py", "tg_vault", ["TELEGRAM_BOT_TOKEN"], ()),
        ("discord-bridge.py", "dc_vault", ["DISCORD_BOT_TOKEN"], ("discord",)),
        ("slack-bridge.py", "sk_vault", ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"], ("slack_bolt",)),
    ):
        got = _load_bridge_starved(bridge, mod_name, wants, stubs)
        if got is None:
            check(f"{bridge} consults the vault for {wants[0]}", False, "module failed to load")
            continue
        mod, asked = got
        token = getattr(mod, "TOKEN", None) or getattr(mod, "BOT_TOKEN", None)
        check(f"{bridge} takes its token FROM THE VAULT when nothing else has it",
              token == f"vault-{wants[0]}" and wants[0] in asked,
              f"TOKEN={token!r} vault asked for {asked}")
        if len(wants) > 1:
            check(f"{bridge} resolves {wants[1]} from the vault independently",
                  getattr(mod, "APP_TOKEN", None) == f"vault-{wants[1]}" and wants[1] in asked,
                  f"APP_TOKEN={getattr(mod, 'APP_TOKEN', None)!r} asked={asked}")

    # --- and the OTHER side: nothing anywhere must refuse to start -----------
    # The vault answering "" is the state an operator lands in after `vault set`
    # of the wrong name. The bridge must exit non-zero rather than run tokenless.
    for bridge, mod_name, wants, stubs in (
        ("telegram-bridge.py", "tg_none", ["TELEGRAM_BOT_TOKEN"], ()),
        ("discord-bridge.py", "dc_none", ["DISCORD_BOT_TOKEN"], ("discord",)),
    ):
        rc = _load_bridge_starved(bridge, mod_name, wants, stubs, vault_value="")
        check(f"{bridge} REFUSES to start when even the vault is empty",
              rc == "EXIT", f"got {rc!r} instead of a non-zero exit")

    # --- the two remaining branches of the helper itself ---------------------
    check("_clean rejects a non-string (a vault backend may return None)",
          ct._clean(None) == "" and ct._clean(b"bytes") == "")
    _saved = sys.modules.get("vault_intercept")
    sys.modules["vault_intercept"] = None      # makes `from vault_intercept ...` raise
    try:
        check("an unimportable vault_intercept degrades to '' (never crashes a bridge)",
              ct.token_from_vault("ANY") == "")
    finally:
        if _saved is None:
            sys.modules.pop("vault_intercept", None)
        else:
            sys.modules["vault_intercept"] = _saved

    # gateway_token must be SOURCE-first across both aliases: the bridge reads
    # both spellings per tier, and an alias-outer loop inverted that.
    def _envfile(**pairs):
        f = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        for k, v in pairs.items():
            f.write(f"{k}={v}\n")
        f.close()
        return Path(f.name)

    LEGACY, CANON = "AG2_REMOTE_TOKEN", "REMOTE_TASK_TOKEN"

    # The two mixed-source cases the review measured as inverted.
    ef = _envfile(**{CANON: "from-file"})
    check("legacy in ENV beats canonical in FILE (env tier wins)",
          ct.gateway_token(env_file=ef, environ={LEGACY: "from-env"},
                           vault_get=lambda v: "") == "from-env")

    ef2 = _envfile(**{LEGACY: "legacy-file"})
    check("legacy in FILE beats canonical in VAULT (file tier wins)",
          ct.gateway_token(env_file=ef2, environ={},
                           vault_get=lambda v: "vault-canon"
                           if v == CANON else "") == "legacy-file")

    # Within one source the canonical spelling still wins — the alias order is
    # preserved, only the tier order changed.
    check("canonical beats legacy INSIDE the same source",
          ct.gateway_token(environ={CANON: "canon-env", LEGACY: "legacy-env"},
                           vault_get=lambda v: "") == "canon-env")

    ef3 = _envfile(**{CANON: "canon-file", LEGACY: "legacy-file"})
    check("canonical beats legacy inside the FILE too",
          ct.gateway_token(env_file=ef3, environ={},
                           vault_get=lambda v: "") == "canon-file")

    # Controls: the vault tier still answers when nothing earlier does, and a
    # host with nothing anywhere still yields '' rather than a stray value.
    check("vault still answers when env and file are empty",
          ct.gateway_token(environ={}, vault_get=lambda v: "vault-tok"
                           if v == CANON else "") == "vault-tok")
    check("no token in any source yields ''",
          ct.gateway_token(environ={}, vault_get=lambda v: "") == "")

    # An undecodable .env is ABSENCE, never a mojibake bearer: errors="replace"
    # turns unreadable bytes into a string that every caller reads as a token.
    bf = tempfile.NamedTemporaryFile("wb", suffix=".env", delete=False)
    bf.write(b"REMOTE_TASK_TOKEN=\xff\xfe\x00binary\n")
    bf.close()
    check("undecodable file reads as absence, not a garbage token",
          ct.token_from_env_file(CANON, Path(bf.name)) == "")
    # Positive control: the '' above is the FILE tier declining, not every tier
    # failing — the vault still answers behind it.
    check("gateway_token falls through an undecodable file to the vault",
          ct.gateway_token(env_file=Path(bf.name), environ={},
                           vault_get=lambda v: "vault-tok"
                           if v == CANON else "") == "vault-tok")

    # The lane lives INSIDE the resolver: AG2_DEVICE_ENV, then channels/<REMOTE_
    # TASK_CHANNEL_DIR or ag2space>/.env, so no gate judges dev from prod's file.
    cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])
    prod = cfg / "channels" / "ag2space" / ".env"
    dev = cfg / "channels" / "dev" / ".env"
    check("default lane is channels/ag2space/.env under the Claude home",
          ct.gateway_env_file(environ={}) == prod, str(ct.gateway_env_file(environ={})))
    check("REMOTE_TASK_CHANNEL_DIR=dev resolves channels/dev/.env",
          ct.gateway_env_file(environ={"REMOTE_TASK_CHANNEL_DIR": "dev"}) == dev)
    device = _envfile(**{CANON: "device-tok"})
    check("AG2_DEVICE_ENV wins over the lane when it names an existing file",
          ct.gateway_env_file(environ={"AG2_DEVICE_ENV": str(device),
                                       "REMOTE_TASK_CHANNEL_DIR": "dev"}) == device)
    check("a missing AG2_DEVICE_ENV falls through to the lane file, as the bridge does",
          ct.gateway_env_file(environ={"AG2_DEVICE_ENV": str(cfg / "nope.env"),
                                       "REMOTE_TASK_CHANNEL_DIR": "dev"}) == dev)
    # The detector-shaped call: no env_file argument, so the resolver picks it.
    dev.parent.mkdir(parents=True, exist_ok=True)
    dev.write_text(f"{CANON}=dev-lane-tok\n")
    try:
        check("a dev-lane host with prod's file absent resolves the dev token",
              not prod.exists()
              and ct.gateway_token(environ={"REMOTE_TASK_CHANNEL_DIR": "dev"},
                                   vault_get=lambda v: "") == "dev-lane-tok")
        check("...and the same host with the lane unset still reads prod (absent -> '')",
              ct.gateway_token(environ={}, vault_get=lambda v: "") == "")
        check("AG2_DEVICE_ENV alone configures the gateway with no lane file at all",
              ct.gateway_token(environ={"AG2_DEVICE_ENV": str(device)},
                               vault_get=lambda v: "") == "device-tok")
    finally:
        dev.unlink()

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
