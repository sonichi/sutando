"""Shared token-resolution policy for the channel bridges.

Each bridge reads its own `channels/<name>/.env` — that is provider-edge
mechanics and stays where it is. What this module owns is the *policy* every
bridge needs to agree on:

  1. **A present-but-empty value does not count.** `startup.sh` gates each
     bridge with `grep -q "<VAR>=" "$env"`, which is prefix-only: a file
     containing `TELEGRAM_BOT_TOKEN=` with nothing after the `=` passes the gate
     and starts a bridge that cannot authenticate. Every layer here requires a
     non-empty value before it counts as an answer.

  2. **The vault is a real source, not just a backup.** Before this, no bridge
     read the Keychain vault at all (`get_vault_key` references in
     telegram/discord/slack bridges: 0, 0, 0). So `vault set TELEGRAM_BOT_TOKEN`
     stored the value correctly and changed nothing — the operator spent the
     secret and saw no effect.

     That is not hypothetical. On 2026-06-08 one operation rewrote all three
     channel `.env` files on a peer host; telegram's came out **1 byte**. The
     bridge kept working for eight weeks because the running process had
     inherited the token from its launcher's environment. macOS does not let you
     read another process's environment, so when that process was killed the
     token was gone with no copy anywhere — and `vault set`, the obvious
     recovery, could not help because nothing read it.

**Adoption is per-channel, decided by whether this module's order IS that
channel's native order.** The DISCORD consumers (`discord-bridge.py`,
`discord-read.py`, `read_discord_channel.py`, `dm-result.py`) resolve through
`resolve_channel_token()` directly: their native precedence was already
env -> `.env` -> vault, so adopting the shared resolver changes nothing but
who owns the quoting/emptiness rules (five private parsers had already
drifted on both). TELEGRAM does not adopt it and must not:
`telegram-bridge.py:91` documents that its config file must WIN over a stale
shell env (#416 — `setdefault` once let a prior session's token silently
override a freshly-rotated one), the opposite of this module's order — it
keeps its own order and appends `token_from_vault()` as the last tier only.
Declaring one policy while a bridge follows another would make this docstring
a lie about the code beside it. (Original distinction caught by @Sutando-Pro.)

`resolve_channel_token()` also serves the GATE, which asks a question
precedence cannot affect: *does a usable token exist at all?* If more than
one layer holds a value, existence is true regardless of which is preferred.

The value is never printed, logged, or returned in an error message.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _clean(value: object) -> str:
    """Strip whitespace and one layer of matching quotes; '' if not usable.

    `.env` conventions allow `VAR="abc"`, and the literal quotes reaching an API
    URL is a real bug this repo has already hit (telegram 404s on
    `.../bot"abc"/getUpdates`).
    """
    if not isinstance(value, str):
        return ""
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        v = v[1:-1].strip()
    return v


def token_from_env_file(var: str, env_file: Path) -> str:
    """Read `var` from a `KEY=VALUE` file. '' when absent, empty, or unreadable."""
    try:
        # Decode leniently: one stray byte ELSEWHERE must not hide a clean token
        # line. The value itself is checked below, so nothing lossy is returned.
        text = env_file.read_text(errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == var:
            # A value that lost bytes to replacement is unreadable, not a token:
            # returning it hands the caller mojibake every caller treats as real.
            got = _clean(value)
            return "" if "\ufffd" in got else got
    return ""


def token_from_vault(var: str, vault_get=None) -> str:
    """Read `var` from the Keychain vault. '' on ANY failure.

    Deliberately total: a missing key, an unavailable keychain, a locked
    keyring, or an import error must degrade to "no answer" rather than crash a
    bridge at startup. The caller decides what an absent token means; this
    function never decides for it, and never surfaces the value in an exception.
    """
    if vault_get is None:
        try:
            from vault_intercept import get_vault_key as vault_get  # type: ignore
        except Exception:
            return ""
    try:
        return _clean(vault_get(var))
    except Exception:
        return ""


def resolve_channel_token(var: str, env_file: Path | None = None,
                          environ=None, vault_get=None) -> str:
    """Resolve one channel token: process env -> `.env` file -> vault.

    Returns '' when no layer holds a non-empty value. An exported value wins so
    an already-working host is unaffected; the vault is consulted only when the
    conventional sources have nothing usable.
    """
    environ = os.environ if environ is None else environ
    found = _clean(environ.get(var, ""))
    if found:
        return found
    if env_file is not None:
        found = token_from_env_file(var, env_file)
        if found:
            return found
    return token_from_vault(var, vault_get=vault_get)


#: Both spellings of the ag2.space gateway token, newest first.
GATEWAY_TOKEN_VARS = ("REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN")


def gateway_token(env_file: Path | None = None, environ=None,
                  vault_get=None) -> str:
    """The ag2.space gateway token from env -> `.env` -> vault, or ''.

    ONE resolver for every lifecycle gate. Launch, health and recovery each had
    their own env+file copy, so a vault-only host was configured for the bridge
    and invisible to the gates that install, watch and restart it.

    SOURCE-first across both aliases, which is what the bridge does: it reads
    both spellings from env, then both from the file, then both from the vault.
    Looping aliases outermost instead made a canonical value in a LATER source
    beat a legacy value in an EARLIER one, so this gate could hand recovery a
    different bearer than the bridge would have chosen for itself.
    """
    environ = os.environ if environ is None else environ
    for var in GATEWAY_TOKEN_VARS:
        found = _clean(environ.get(var, ""))
        if found:
            return found
    if env_file is not None:
        for var in GATEWAY_TOKEN_VARS:
            found = token_from_env_file(var, env_file)
            if found:
                return found
    for var in GATEWAY_TOKEN_VARS:
        found = token_from_vault(var, vault_get=vault_get)
        if found:
            return found
    return ""


def main(argv: list[str] | None = None) -> int:
    """`--has VAR [--env-file PATH]` -> 0 = usable token, 3 = definitively absent.

    **3, not 1, and the choice is load-bearing.** Python exits 1 for a syntax
    error and for any uncaught exception (measured), so a shell caller cannot
    distinguish "I checked and there is no token" from "this script is broken".
    `startup.sh` branches on exactly that difference: a definitive NO must refuse
    to start the bridge, while an unrunnable resolver must degrade to the old
    grep rather than take every bridge on the host down over a code bug. 3 is a
    value the interpreter will not produce on its own. (@Sutando-Pro on #2638.)

    For `startup.sh`, whose per-bridge gate is `grep -q "<VAR>=" "$env"`. That
    grep is prefix-only and blind to the vault, so it both starts bridges with an
    empty token and skips bridges whose token is safely stored. Nothing is
    printed on success — the exit code is the whole answer, so the value cannot
    leak into a log.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] != "--has" or len(argv) < 2:
        print("usage: channel_token.py --has VAR [--env-file PATH]", file=sys.stderr)
        return 2
    var = argv[1]
    env_file = None
    if "--env-file" in argv:
        i = argv.index("--env-file")
        if i + 1 < len(argv):
            env_file = Path(argv[i + 1])
    return 0 if resolve_channel_token(var, env_file=env_file) else 3


if __name__ == "__main__":
    raise SystemExit(main())
