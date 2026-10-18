"""Which AG2 Space gateway credential wins when sources disagree: the one owner of that precedence.

Every source is read by the caller, at its own I/O edge (the environment, the channel .env files it
knows, a token file, a vault read), and handed in as plain values or a callable; this module opens
nothing itself. SOURCES states the precedence once and discover() walks it. discover() registers
every materialized candidate before it picks the winners, so a sink can withhold a discarded one.
Parsing and masking are the pure contract in gateway_credentials (generated from
shared/ag2_gateway_credentials.py). Stdlib only; vendored byte-identical into the ag2-sparrow
package by tools/sync_from_src.py.
"""
from __future__ import annotations

import os.path
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

try:
    from . import gateway_credentials as contract  # the package's generated copy
except ImportError:
    import ag2_gateway_credentials as contract  # the canonical shared/ module, when on the path

TOKEN_KEYS = ("REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN")
URL_KEYS = ("REMOTE_TASK_URL", "AG2_REMOTE_URL")
TOKEN_FILE_KEY = "REMOTE_TASK_TOKEN_FILE"
MEDIA_TOKEN_KEY = "REMOTE_MEDIA_HS_TOKEN"
CREDENTIAL_KEYS = TOKEN_KEYS + URL_KEYS + (MEDIA_TOKEN_KEY,)
LEGACY_ALIASES = {"AG2_REMOTE_TOKEN": "REMOTE_TASK_TOKEN", "AG2_REMOTE_URL": "REMOTE_TASK_URL"}

# The precedence, stated once; the first source carrying a value wins (see discover() for each name:
# "file" is the first listed .env file carrying a token, "token" the URL inside the winning one).
SOURCES = (
    ("token", ("env", "file", "vault")),
    ("url", ("env", "token", "file")),
    ("token_file", ("env", "file")),
    ("media_token", ("env",)),
)

EnvFile = "tuple[str, Mapping[str, str]]"


unquote = contract.parse_env_value


def parse_env_text(text: str) -> "dict[str, str]":
    """A .env file's KEY=VALUE lines, unquoted (comments and blank lines skipped)."""
    vals = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        vals[key.strip()] = unquote(val)
    return vals


DEFAULT_CHANNEL_DIR = "ag2space"


CHANNEL_DIR_KEY = "REMOTE_TASK_CHANNEL_DIR"


def channel_dir(env: Mapping[str, str]) -> str:
    """Which channels/<dir>/ a Sparrow reads: REMOTE_TASK_CHANNEL_DIR, else the default."""
    return env.get(CHANNEL_DIR_KEY) or DEFAULT_CHANNEL_DIR


def env_files(env: Mapping[str, str], read_text: "Callable[[str], Optional[str]]") -> "list[EnvFile]":
    """The channel .env files a Sparrow given `env` reads, in order, as (path, values), through the
    caller's `read_text` (None when unreadable, which skips that file). One candidate set for every
    adapter: the bridge and the Hatch adapter both call this."""
    out = []
    for path in channel_env_paths(env, channel_dir(env)):
        text = read_text(path)
        if text is not None:
            out.append((path, parse_env_text(text)))
    return out


def channel_env_paths(env: Mapping[str, str], channel_dir: str = DEFAULT_CHANNEL_DIR) -> "list[str]":
    """The channel .env files a Sparrow reads, in precedence order: AG2_DEVICE_ENV, then
    $CLAUDE_CONFIG_DIR/channels/<dir>/.env. Never a bare-home guess, which could pick up an
    unrelated install's token. Paths only: the caller reads them."""
    paths = [env.get("AG2_DEVICE_ENV") or ""]
    if env.get("CLAUDE_CONFIG_DIR"):
        paths.append(os.path.join(env["CLAUDE_CONFIG_DIR"], "channels", channel_dir, ".env"))
    return [p for p in paths if p]


def first(values: Mapping[str, str], keys) -> "tuple[str, str]":
    """(value, key) of the first key with a non-empty value; ('', '') when none. The values are
    already parsed (parse_env_text, _assignments): unquoting them again would change a padded token."""
    for key in keys:
        value = values.get(key) or ""
        if value:
            return value, key
    return "", ""


def first_env(env: Mapping[str, str], keys) -> "tuple[str, str]":
    """The environment tier: the first key SET non-empty wins, then is unquoted (a value of only
    quotes is a blank that still wins, as it always has); ('', '') when none is set."""
    for key in keys:
        if env.get(key):
            return unquote(env[key]), key
    return "", ""


def config_from_files(files: "Sequence[EnvFile]", key: str) -> str:
    """The first file CARRYING `key`, else "". Presence decides: an explicit blank is a decision
    here exactly as in the environment."""
    for _path, vals in files:
        if key in vals:
            return vals[key]
    return ""


def _assignments(text: str, keys) -> "dict[str, str]":
    found = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        for key in keys:
            if line.startswith(key + "="):
                found[key] = unquote(line[len(key) + 1:])
    return found


def token_file_token(text: str) -> str:
    """The onboarding string in a token file's text, or "": a REMOTE_TASK_TOKEN= line (the legacy
    AG2_REMOTE_TOKEN= honoured whole-file, after it; optional `export `; quotes stripped), else the
    raw string alone on the first non-comment, '='-free line."""
    value, _key = first(_assignments(text, TOKEN_KEYS), TOKEN_KEYS)
    if value:
        return value
    for line in text.splitlines():
        line = line.strip()
        if line and "=" not in line and not line.startswith("#"):
            return line
    return ""


def token_file_url(text: str) -> str:
    """The REMOTE_TASK_URL (legacy AG2_REMOTE_URL) of a split-layout token file's text, or ""."""
    return first(_assignments(text, URL_KEYS), URL_KEYS)[0]


def token_from_files(files: "Sequence[EnvFile]"):
    """The file tier: (token, url, path, values) of the first file carrying a token, with the URL
    from that same file; ('', '', '', {}) when none does. A blank secret is absence."""
    for path, vals in files:
        token, _key = first(vals, TOKEN_KEYS)
        if token:
            return token, first(vals, URL_KEYS)[0], path, vals
    return "", "", "", {}


def token_from_vault(vault_lookup: "Callable[[str], str]") -> "tuple[str, str]":
    """The vault tier: (token, key) from the caller's vault read, each key in order."""
    for key in TOKEN_KEYS:
        token = vault_lookup(key) or ""
        if token:
            return token, key
    return "", ""


def register(registry: "contract.SecretRegistry", values) -> None:
    """Register credential values as secrets: each value whole, and also an onboarding string's
    secret and a URL's credential parts."""
    for value in values:
        if not value:
            continue
        registry.register(value)  # whole, whatever its shape: a bearer may look like a URL
        url, secret = contract.parse_onboarding_token(value)
        if url:
            registry.register_gateway_url(url)
            registry.register(secret)
        elif value.lower().startswith(("http://", "https://")):
            registry.register_gateway_url(value)


@dataclass(frozen=True)
class Discovery:
    """The winners by SOURCES, and what they came from: `token_source` is "env", "file", "vault"
    or "none"; `token_key` the key the token was read under; `url_key` the env key of an
    env-supplied URL; `source_file` (and its values) the file that supplied the token, if one did;
    `media_token` the homeserver media token, from the environment only; `deprecated` the
    (legacy key, current name) pairs set in the environment without their current name."""
    raw: str = ""
    token: str = ""
    url: str = ""
    token_file: str = ""
    token_source: str = "none"
    token_key: str = ""
    url_key: str = ""
    source_file: str = ""
    source_file_values: Mapping[str, str] = field(default_factory=dict)
    media_token: str = ""
    deprecated: "tuple[tuple[str, str], ...]" = ()


def discover(env: Mapping[str, str], files: "Sequence[EnvFile]" = (), *,
             read_token_file: "Optional[Callable[[str], str]]" = None,
             vault_lookup: "Optional[Callable[[str], str]]" = None,
             registry: "Optional[contract.SecretRegistry]" = None) -> Discovery:
    """Register every materialized candidate, then pick the winners by walking SOURCES in order.
    `files` are the channel .env files the caller reads, in its order, as (path, values); `read_token_file(path)` returns a
    token file's text ('' when unreadable); `vault_lookup(key)` is the caller's vault read."""
    token_file_env = env.get(TOKEN_FILE_KEY) or ""
    token_file_text = read_token_file(token_file_env) if (token_file_env and read_token_file) else ""
    if registry is not None:
        register(registry, [unquote(env.get(key) or "") for key in CREDENTIAL_KEYS])
        register(registry, [values.get(key) or "" for _p, values in files for key in CREDENTIAL_KEYS])
        register(registry, [token_file_token(token_file_text), token_file_url(token_file_text)])

    found = {"raw": "", "token_key": "", "source": "none", "source_file": "", "source_vals": {},
             "file_url": ""}

    def token_env():
        raw, key = first_env(env, TOKEN_KEYS)
        return raw and dict(raw=raw, token_key=key)

    def token_file():
        raw, url, path, vals = token_from_files(files)
        return raw and dict(raw=raw, token_key=first(vals, TOKEN_KEYS)[1], source_file=path,
                            source_vals=vals, file_url=url)

    def token_vault():
        raw, key = token_from_vault(vault_lookup) if vault_lookup is not None else ("", "")
        if raw and registry is not None:
            register(registry, [raw])
        return raw and dict(raw=raw, token_key=key)

    tiers = dict(SOURCES)
    for name in tiers["token"]:
        got = {"env": token_env, "file": token_file, "vault": token_vault}[name]()
        if got:
            found.update(got, source=name)
            break
    raw = found["raw"]
    url_from_token, token = contract.parse_onboarding_token(raw)
    env_url, url_key = first_env(env, URL_KEYS)
    url = next((v for v in ({"env": env_url, "token": url_from_token, "file": found["file_url"]}[n]
                            for n in tiers["url"]) if v), "")
    token_file_path = next((v for v in ({"env": token_file_env, "file": found["source_file"]}[n]
                                        for n in tiers["token_file"]) if v), "")
    media = next((v for v in ({"env": env.get(MEDIA_TOKEN_KEY) or ""}[n] for n in tiers["media_token"]) if v), "")
    source, token_key, source_file, source_vals = (found["source"], found["token_key"], found["source_file"],
                                                   found["source_vals"])
    return Discovery(raw=raw, token=token, url=url.rstrip("/"), token_file=token_file_path,
                     token_source=source, token_key=token_key, url_key=url_key if url == env_url and url else "",
                     source_file=source_file, source_file_values=dict(source_vals), media_token=media,
                     deprecated=tuple((old, new) for old, new in LEGACY_ALIASES.items()
                                      if env.get(old) and not env.get(new)))
