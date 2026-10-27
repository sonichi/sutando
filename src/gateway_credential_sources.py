"""Which AG2 Space gateway credential wins when sources disagree: the one owner of that precedence.

Every source is read by the caller, at its own I/O edge (the environment, the channel .env files it
knows, a token file, a vault read), and handed in as plain values or a callable; this module reads
no credential itself. The one file it touches is a host supervisor's bridge declaration, the record
that names the token's variable: it checks, writes and removes it, so no adapter holds its bytes.
SOURCES states the precedence once and discover() walks it. discover() registers every materialized candidate
before it picks the winners, so a sink can withhold a discarded one, every assignment of a
credential key included. A credential file that exists but is refused is
named, never treated as absent in silence. Fail-closed covers the credential tiers only: a refused
AG2_DEVICE_ENV stops the file and vault token tiers, and a set REMOTE_TASK_TOKEN_FILE is the first token
source: set at all, it closes the file and vault tiers, and refused, every tier (Discovery.fatal either way),
while general config such as REMOTE_PROACTIVE_ROOM still falls through to the later files. A seat (SEAT_KEY,
the per-agent channel dir write_seat_credentials writes) reads its own .env and token file only.
Parsing and masking are the pure contract in gateway_credentials (generated from
shared/ag2_gateway_credentials.py). Stdlib only; vendored byte-identical into the ag2-sparrow
package by tools/sync_from_src.py.
"""
from __future__ import annotations

import contextlib
import json
import os
import os.path
import re
import secrets
import shlex
import tempfile
import urllib.parse
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


def token_env_name() -> str:
    """The token's environment variable by name, for a record that says where the token goes; another
    module may name it only through this call, never read the variable with it."""
    return TOKEN_KEYS[0]


_RECORD_TOKEN_ENV = "token_env"
# A declaration is a few hundred bytes; every reader refuses one past this without reading it.
MAX_DECLARATION_BYTES = 64 * 1024


def _bridge_declaration_text(*, written_by: str, host: str, name: str, module: str, env: Mapping[str, str],
                             token_vault_key: str) -> str:
    record = {"written_by": written_by, "schema": 1, "host": host, "name": name, "module": module, "env": env,
              "token_vault_key": token_vault_key, _RECORD_TOKEN_ENV: token_env_name()}
    return json.dumps(record, indent=1, sort_keys=False) + "\n"


def _declaration_owned(path: str, written_by: str) -> "tuple[bool, str]":
    """(present, '') when nothing is at `path` or it is a declaration `written_by` wrote, else (False, why).
    A file over MAX_DECLARATION_BYTES is not one: refused and left untouched, never read past the cap."""
    if os.path.isdir(path):
        return False, f"{path} is a directory, not a declaration; remove it yourself"
    if os.path.islink(path) and not os.path.exists(path):
        return False, f"{path} is a dangling symlink; remove it by hand (rm {shlex.quote(path)})"
    try:
        with open(path, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            data = fh.read(MAX_DECLARATION_BYTES + 1) if size <= MAX_DECLARATION_BYTES else b""
    except FileNotFoundError:
        return False, ""
    except OSError as e:
        return False, f"{path} unreadable ({e.strerror or e})"
    if size > MAX_DECLARATION_BYTES or len(data) > MAX_DECLARATION_BYTES:
        return False, (f"{path} is over the {MAX_DECLARATION_BYTES}-byte cap for a declaration; "
                       f"remove it by hand (rm {shlex.quote(path)}) if it is yours to remove")
    try:
        ours = json.loads(data.decode("utf-8", errors="replace")).get("written_by") == written_by
    except (ValueError, AttributeError):
        ours = False
    if not ours:
        return False, f"{path} was not written by install; remove it by hand (rm {path}) if it is yours to remove"
    return True, ""


def remove_bridge_declaration(path, *, written_by: str) -> "tuple[bool, str]":
    """Remove the bridge declaration at `path` if `written_by` wrote it: (removed, '') or (False, why);
    nothing there is (False, ''). Its bytes never leave this module."""
    path = os.fspath(path)
    present, problem = _declaration_owned(path, written_by)
    if present:
        os.unlink(path)
    return present, problem


def write_bridge_declaration(path, *, written_by: str, host: str, name: str, module: str, env: Mapping[str, str],
                             token_vault_key: str, mode: int = 0o644) -> str:
    """Write a host supervisor's bridge declaration at `path` (tmp + os.replace, so no reader sees a torn
    file): the worker, its environment, the vault key holding its token, and the token's variable by name.
    '' or why not; a file there that `written_by` did not write is left alone. The record and any
    existing file's bytes never leave this module, so no caller can read the variable's name out of them."""
    path = os.fspath(path)
    _present, problem = _declaration_owned(path, written_by)
    if problem:
        return problem
    text = _bridge_declaration_text(written_by=written_by, host=host, name=name, module=module, env=env,
                                    token_vault_key=token_vault_key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=f".{os.path.basename(path)}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return ""


def bridge_declaration_fields() -> "dict[str, str]":
    """The fields the owner fixes in every bridge declaration, for a supervisor that checks one it reads."""
    return {_RECORD_TOKEN_ENV: token_env_name()}


# Set in a declared bridge's environment by the supervisor that hands it its token: the credential
# file tiers are then closed, so the bridge never opens the host's channel .env (see env_files).
INJECTED_KEY = "GATEWAY_CREDENTIALS_INJECTED"
# The host config a supervisor resolves for a declared bridge and carries in as plain values, each
# audited here: none is a credential, and the bridge reads no file to find them.
BRIDGE_CARRIED_CONFIG = ("REMOTE_PROACTIVE_TRUST_OK",)


def bridge_token_environ(token: str, url: str = "") -> "dict[str, str]":
    """The variables a declared bridge's token is handed to it under, with the mark that it was, and the
    agent's own gateway `url` beside a token that embeds none (a URL inside the token wins). The supervisor
    reads `url` from where the owner stored it, never from the host's environment or the declaration."""
    out = {token_env_name(): token, INJECTED_KEY: "1"}
    if url and not token_embeds_url(token):
        out[URL_KEYS[0]] = url
    return out


def token_gateway(token: str) -> str:
    """The gateway URL a combined onboarding token carries, '' for any other token."""
    return contract.parse_onboarding_token(token or "")[0]


def token_embeds_url(token: str) -> bool:
    """Whether `token` is the combined onboarding form, carrying its gateway URL."""
    return bool(token_gateway(token))


def token_secret_empty(token: str) -> bool:
    """Whether `token` carries no secret, bare or after its url|secret separator (e.g. 'https://gw|')."""
    return not contract.parse_onboarding_token(token or "")[1].strip()


def token_url_shaped(token: str) -> bool:
    """Whether `token` starts with a URL scheme yet carries no url|secret separator. The contract reads it
    as an opaque bare secret; it may also be a combined token that lost its '|', which only a missing
    gateway makes worth saying. Never a reason to refuse a token whose gateway is known."""
    return (token or "").lower().startswith(("http://", "https://")) and not token_embeds_url(token)


def gateway_url_problem(url: str) -> str:
    """'' for a usable gateway URL (http or https with a parsed host and a valid port, no '|', no whitespace
    or control characters, no userinfo, query or fragment), else why not, without the value."""
    if not url or any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in url) or "|" in url:
        return "is not a gateway URL (empty, or holds whitespace, a control character or '|')"
    try:
        parts = urllib.parse.urlsplit(url)
        host, _port = parts.hostname, parts.port
    except ValueError:
        return "is not a URL with a valid host and port"
    if parts.scheme.lower() not in ("http", "https") or not host:
        return "is not an http(s) URL with a host"
    # Checked on the raw text too: urlsplit drops an empty '?' or '#', and a credential may ride either.
    if "@" in parts.netloc or "?" in url or "#" in url:
        return "carries userinfo, a query or a fragment; a gateway URL is scheme, host, port and path only"
    return ""


def credentials_injected(env: Mapping[str, str]) -> bool:
    """Whether `env` is a declared bridge's: its token was handed in, so no credential file is read."""
    return bool(env.get(INJECTED_KEY))


def bridge_carried_config(env: Mapping[str, str], read_text: "Callable[[str], object]") -> "dict[str, str]":
    """The BRIDGE_CARRIED_CONFIG values a host given `env` holds, from its environment first and else
    from its channel .env files through `read_text`, for a supervisor to hand a declared bridge. A key
    the host sets nowhere is absent. The files' credentials stay inside this call."""
    carried = {k: env[k] for k in BRIDGE_CARRIED_CONFIG if k in env}
    missing = [k for k in BRIDGE_CARRIED_CONFIG if k not in carried]
    if missing:
        files = env_files(env, read_text)
        carried.update({k: config_from_files(files, k) for k in missing if any(k in vals for _p, vals in files)})
    return carried


def wake_environ(child: Mapping[str, str], raw: str, url: str) -> "dict[str, str]":
    """The environment a bridge's wake command runs under, from `child` (the bridge's own minus every
    relay credential). In a declared bridge (the mark) the command acts as the agent: every gateway token
    and URL alias is dropped and the agent's own token as the bridge holds it (`raw`) and its gateway URL
    (`url`, which in a declared bridge only the agent's token can supply: bridge_strip_keys) are set, so a
    reader resolves exactly those and never the host's. Unmarked, `child` is unchanged."""
    if not credentials_injected(child):
        return dict(child)
    out = {k: v for k, v in child.items() if k not in bridge_strip_keys()}
    if raw:
        out[token_env_name()] = raw
        if url:
            out[URL_KEYS[0]] = url
    return out


def bridge_strip_keys() -> "tuple[str, ...]":
    """Every variable a host's own gateway token or endpoint may sit under, for a supervisor to drop from a
    declared bridge's environment: each token name, every alias the contract ranks (a child of the bridge
    resolving GATEWAY_TOKEN or RELAY_TOKEN first would act as the host), the media token, and each URL name
    and alias (discover() ranks an environment URL above the token's own, which would send the agent's
    bearer to the host's gateway). The bridge's endpoint is then the one inside the agent's token."""
    return tuple(dict.fromkeys(TOKEN_KEYS + tuple(contract.TOKEN_ALIAS_PRECEDENCE) + (MEDIA_TOKEN_KEY,)
                               + URL_KEYS + tuple(contract.URL_ALIAS_PRECEDENCE)))

# The precedence, stated once; the first source carrying a value wins (see discover() for each name:
# "token_file" is REMOTE_TASK_TOKEN_FILE, "file" the first listed .env file carrying a token, "token" the URL
# inside the winning one).
SOURCES = (
    ("token", ("token_file", "env", "file", "vault")),
    ("url", ("env", "token", "file")),
    ("token_file", ("env", "file")),
    ("media_token", ("env",)),
)

EnvFile = "tuple[str, Mapping[str, str]]"


unquote = contract.parse_env_value


def parse_env_pairs(text: str) -> "list[tuple[str, str]]":
    """A .env file's KEY=VALUE lines, unquoted, in order, a repeated key every time it appears."""
    pairs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        pairs.append((key.strip(), unquote(val)))
    return pairs


def parse_env_text(text: str) -> "dict[str, str]":
    """A .env file's KEY=VALUE lines, unquoted (comments and blank lines skipped); the last
    assignment of a repeated key wins."""
    return dict(parse_env_pairs(text))


DEFAULT_CHANNEL_DIR = "ag2space"


CHANNEL_DIR_KEY = "REMOTE_TASK_CHANNEL_DIR"


def channel_dir(env: Mapping[str, str]) -> str:
    """Which channels/<dir>/ a Sparrow reads: REMOTE_TASK_CHANNEL_DIR, else the default."""
    return env.get(CHANNEL_DIR_KEY) or DEFAULT_CHANNEL_DIR


# A seat: one agent's own channel dir, <seat>/channels/ag2space/, holding its .env and its token file.
SEAT_KEY = "REMOTE_TASK_SEAT"
SEAT_TOKEN_NAME = "token"
SEAT_TIERS = ("owner", "team", "guest")  # highest first; the bridge's LOCAL_TIER vocabulary
SEAT_INSTANCE_RE = re.compile(r"[0-9a-f]{32}")
# What a seat bridge must not inherit: a token, URL or identity in the environment would win over the seat's own.
SEAT_CLEARED = ("AGENT_ID", "AG2SPACE_USER_ID", "AG2_REMOTE_TIER", "GATEWAY_CREDENTIALS_INJECTED")


def named_token_file(env: Mapping[str, str]) -> str:
    """The token file REMOTE_TASK_TOKEN_FILE names in `env`, '' when none. A seat's holds its credential alone
    and is opened private (read_private); a host's may be its channel .env, read as any other."""
    return env.get(TOKEN_FILE_KEY) or ""


def is_seat(env: Mapping[str, str]) -> bool:
    """Whether `env` is a seat bridge's: SEAT_KEY, or a GATEWAY_INSTANCE in the seat's 32-hex form (a named
    instance such as `dev` is none). Its token and URL come from its token file alone, its channel .env is
    AG2_DEVICE_ENV alone, never the host's channel dir, environment token or vault."""
    return bool(env.get(SEAT_KEY)) or bool(SEAT_INSTANCE_RE.fullmatch(env.get("GATEWAY_INSTANCE") or ""))


def seat_channel_dir(seat) -> str:
    """<seat>/channels/ag2space: where a seat's .env and token file live."""
    return os.path.join(os.fspath(seat), "channels", DEFAULT_CHANNEL_DIR)


def mint_gateway_instance() -> str:
    """A fresh 32-hex GATEWAY_INSTANCE, for a seat that has none."""
    return secrets.token_hex(16)


def _seat_values_problem(values: Mapping[str, str]) -> str:
    if not (values.get("AGENT_MXID") or "").strip():
        return "AGENT_MXID is missing"
    if not SEAT_INSTANCE_RE.fullmatch(values.get("GATEWAY_INSTANCE") or ""):
        return "GATEWAY_INSTANCE is not 32 lowercase hex"
    if (values.get("REMOTE_TASK_TIER") or "") not in SEAT_TIERS:
        return f"REMOTE_TASK_TIER is not one of {', '.join(SEAT_TIERS)}"
    if not values.get(TOKEN_FILE_KEY):
        return f"{TOKEN_FILE_KEY} is missing"
    return ""


def _replace_file(path: str, text: str, mode: int) -> None:
    """`text` at `path` through a temp file and os.replace, created at `mode`, so no reader sees a torn
    or briefly wider file."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=f".{os.path.basename(path)}.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def seat_credential_files(seat, *, agent_id: str, instance: str, tier: str, relay_url: str,
                          bearer: str) -> "tuple[dict, str]":
    """({name in the seat's channel dir: its text}, '') or ({}, why not, never the bearer): the `token`
    (one line, <relay_url>|<bearer>) and `.env` (AGENT_MXID, GATEWAY_INSTANCE, REMOTE_TASK_TOKEN_FILE,
    REMOTE_TASK_TIER) write_seat_credentials writes, for a writer that holds the channel dir open itself."""
    token_path = os.path.join(seat_channel_dir(seat), SEAT_TOKEN_NAME)
    values = {"AGENT_MXID": (agent_id or "").strip(), "GATEWAY_INSTANCE": instance or "",
              TOKEN_FILE_KEY: token_path, "REMOTE_TASK_TIER": tier or ""}
    problem = _seat_values_problem(values)
    if problem:
        return {}, f"the seat's {problem}"
    for v in (values["AGENT_MXID"], token_path):
        if "\n" in v or "\r" in v or v != v.strip() or v[:1] in "\"'" or v[-1:] in "\"'":
            return {}, "the agent id or the seat path cannot be carried on a .env line as it is"
    why = gateway_url_problem(relay_url or "")
    if why:
        return {}, f"the relay URL {why}"
    if not bearer or any(c.isspace() or c == "|" or ord(c) < 0x20 for c in bearer):
        return {}, "the bearer is empty or holds whitespace, a control character or '|'"
    return {SEAT_TOKEN_NAME: f"{relay_url}|{bearer}\n", ".env": "".join(f"{k}={v}\n" for k, v in values.items())}, ""


def write_seat_credentials(seat, *, agent_id: str, instance: str, tier: str, relay_url: str, bearer: str) -> str:
    """Write a seat's channel dir (seat_credential_files): each file 0600 through temp + rename, the dir
    0700. A rewrite replaces the token in place, which is a rotation. '' or why not, never the bearer."""
    files, problem = seat_credential_files(seat, agent_id=agent_id, instance=instance, tier=tier,
                                           relay_url=relay_url, bearer=bearer)
    if problem:
        return problem
    chan = seat_channel_dir(seat)
    os.makedirs(chan, mode=0o700, exist_ok=True)
    os.chmod(chan, 0o700)
    for name, text in files.items():
        _replace_file(os.path.join(chan, name), text, 0o600)
    return ""


def _clamp_tier(tier: str, cap: str) -> str:
    return SEAT_TIERS[max(SEAT_TIERS.index(tier), SEAT_TIERS.index(cap))] if cap in SEAT_TIERS else tier


def seat_bridge_environ(seat_env_path: str, read_text: "Callable[[str], object]",
                        core_tier: str = "") -> "tuple[dict, str]":
    """The environment a seat's bridge runs with, from its .env read through the caller's `read_text` (see
    _read_result): the seat's keys exported (the bridge reads them from its environment only), SEAT_KEY set,
    every inherited token, URL and identity variable cleared. `core_tier`, on a host with a core, caps the
    tier. ({}, '') for a file that is no seat; its values never leave this call."""
    text, reason = _read_result(read_text(seat_env_path))
    if reason:
        return {}, refusal_note(seat_env_path, reason)
    values = parse_env_text(text or "")
    if TOKEN_FILE_KEY not in values:
        return {}, ""
    problem = _seat_values_problem(values)
    if problem:
        return {}, f"the seat .env {seat_env_path}: {problem}"
    out = {k: "" for k in bridge_strip_keys() + SEAT_CLEARED}
    out.update({"AG2_DEVICE_ENV": seat_env_path, SEAT_KEY: "1", "AGENT_MXID": values["AGENT_MXID"].strip(),
                "GATEWAY_INSTANCE": values["GATEWAY_INSTANCE"], TOKEN_FILE_KEY: values[TOKEN_FILE_KEY],
                "REMOTE_TASK_TIER": _clamp_tier(values["REMOTE_TASK_TIER"], core_tier)})
    return out, ""


def _read_result(got) -> "tuple[Optional[str], str]":
    """A caller's reader returns text, None (absent), or bounded_file_read.read_regular's
    (text, reason), whose non-empty reason means the file exists but was refused."""
    if isinstance(got, tuple):
        return got[0], got[1] or ""
    return got, ""


def refusal_note(path: str, reason: str) -> str:
    """The one line an adapter prints for a refused credential file: path and reason, no content."""
    return f"credential file {path} was refused ({reason}); nothing in it was read"


class EnvFiles(list):
    """env_files' result: the readable files as (path, values), plus every credential-key
    (key, value) each one assigns (a repeated key every time), the refused (path, reason) pairs, and `blocked`,
    the message that stops the token tiers when the explicitly named AG2_DEVICE_ENV was refused."""
    credential_pairs: "tuple[tuple[str, str], ...]" = ()
    refused: "tuple[tuple[str, str], ...]" = ()
    blocked: str = ""


def env_files(env: Mapping[str, str], read_text: "Callable[[str], object]") -> EnvFiles:
    """The channel .env files a Sparrow given `env` reads, in order, through the caller's
    `read_text` (see _read_result). An absent file is skipped; a refused one is recorded and
    skipped, a refused AG2_DEVICE_ENV also setting `blocked`, which discover() applies to the
    credential tiers only: later files still supply config. The bridge calls this. A declared bridge
    (credentials_injected) reads none: its token was handed in, and the host's files are the host's."""
    out, values, refused = EnvFiles(), [], []
    if credentials_injected(env):
        return out
    device = env.get("AG2_DEVICE_ENV") or ""
    for path in channel_env_paths(env, channel_dir(env)):
        text, reason = _read_result(read_text(path))
        if text is not None:
            out.append((path, parse_env_text(text)))
            values += [(k, v) for k, v in parse_env_pairs(text) if k in CREDENTIAL_KEYS]
            values += _assignment_pairs(text, CREDENTIAL_KEYS)  # `export KEY=` too
        elif reason:
            refused.append((path, reason))
            if path == device:
                out.blocked = (f"AG2_DEVICE_ENV names {path}, which was refused ({reason}); not falling "
                               "back to another credential source. Fix that file or unset AG2_DEVICE_ENV")
    out.credential_pairs, out.refused = tuple(values), tuple(refused)
    return out


def channel_env_paths(env: Mapping[str, str], channel_dir: str = DEFAULT_CHANNEL_DIR) -> "list[str]":
    """The channel .env files a Sparrow reads, in precedence order: AG2_DEVICE_ENV, then
    $CLAUDE_CONFIG_DIR/channels/<dir>/.env. Never a bare-home guess, which could pick up an
    unrelated install's token. A seat reads its own .env alone. Paths only: the caller reads them."""
    paths = [env.get("AG2_DEVICE_ENV") or ""]
    if env.get("CLAUDE_CONFIG_DIR") and not is_seat(env):
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


def _assignment_pairs(text: str, keys) -> "list[tuple[str, str]]":
    """Every (key, value) assignment of `keys` in `text`, in order, duplicates included."""
    found = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        for key in keys:
            if line.startswith(key + "="):
                found.append((key, unquote(line[len(key) + 1:])))
    return found


def _assignments(text: str, keys) -> "dict[str, str]":
    return dict(_assignment_pairs(text, keys))


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


def token_file_pairs(text: str) -> "list[tuple[str, str]]":
    """Every (key, value) in a token file's text that is or could become its token or URL, not
    only the winners: each assignment (a losing alias, an overwritten duplicate), and the raw line
    token_file_token reads once no assignment is left, keyed as a token. Beside an assignment that
    line is only a candidate, so it counts when at least BARE_SECRET_MIN long."""
    pairs = _assignment_pairs(text, TOKEN_KEYS + URL_KEYS)
    raw = token_file_token("\n".join(line for line in text.splitlines() if "=" not in line))
    if raw and (not _assignments(text, TOKEN_KEYS) or len(raw) >= contract.BARE_SECRET_MIN):
        pairs.append((TOKEN_KEYS[0], raw))
    return pairs


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


def register_pairs(registry: "contract.SecretRegistry", pairs) -> None:
    """Register (key, value) candidates: a URL key's plain URL by its credential parts only, so its
    redacted form stays renderable; every other value as register() does."""
    for key, value in pairs:
        if (key in URL_KEYS and value.lower().startswith(("http://", "https://"))
                and not contract.parse_onboarding_token(value)[0]):
            registry.register_gateway_url(value)
        else:
            register(registry, [value])


@dataclass(frozen=True)
class Discovery:
    """The winners by SOURCES, and what they came from: `token_source` is "env", "file", "vault"
    or "none"; `token_key` the key the token was read under; `url_key` the env key of an
    env-supplied URL; `source_file` (and its values) the file that supplied the token, if one did;
    `media_token` the homeserver media token, from the environment only; `deprecated` the
    (legacy key, current name) pairs set in the environment without their current name; `notes`
    one line per refused credential file; `fatal` why the adapter must not start or install."""
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
    notes: "tuple[str, ...]" = ()
    fatal: str = ""


def discover(env: Mapping[str, str], files: "Sequence[EnvFile]" = (), *,
             read_token_file: "Optional[Callable[[str], str]]" = None,
             vault_lookup: "Optional[Callable[[str], str]]" = None,
             registry: "Optional[contract.SecretRegistry]" = None) -> Discovery:
    """Register every materialized candidate, then pick the winners by walking SOURCES in order.
    `files` are the channel .env files the caller reads, in its order, as (path, values); `read_token_file(path)` returns a
    token file's text, or a read_regular result (see _read_result); `vault_lookup(key)` is the
    caller's vault read. `files` from env_files carry every assignment and any refusal."""
    token_file_env = env.get(TOKEN_FILE_KEY) or ""
    token_file_text, token_file_reason = (_read_result(read_token_file(token_file_env))
                                          if (token_file_env and read_token_file) else ("", ""))
    token_file_missing = bool(token_file_env and read_token_file) and token_file_text is None and not token_file_reason
    token_file_text = token_file_text or ""
    notes = [refusal_note(p, r) for p, r in getattr(files, "refused", ())]
    blocked = getattr(files, "blocked", "")
    fatal = [blocked] if blocked else []
    if token_file_reason:
        notes.append(refusal_note(token_file_env, token_file_reason))
        fatal.append(f"REMOTE_TASK_TOKEN_FILE names {token_file_env}, which was refused "
                     f"({token_file_reason}); no other credential source stands in for it. Fix that file or unset "
                     "REMOTE_TASK_TOKEN_FILE")
    seat = is_seat(env)
    if seat and token_file_missing:
        fatal.append(f"REMOTE_TASK_TOKEN_FILE names {token_file_env}, which does not exist; a seat has no other "
                     "credential source. Reconnect the agent")
    if seat and not token_file_env:
        fatal.append("this bridge is a seat but REMOTE_TASK_TOKEN_FILE is unset; a seat reads its token from its "
                     "own token file only")
    if seat and not env.get("AG2_DEVICE_ENV"):
        fatal.append("this bridge is a seat but AG2_DEVICE_ENV does not name its .env")
    # A named token file decides alone: refused, no tier stands in; blank or not yet written, only the env may.
    # Named means authoritative: a channel .env or the vault would connect as a credential nobody named.
    seat_without_file = seat and (not token_file_env or token_file_missing)
    closed = (("token_file", "env", "file", "vault") if token_file_reason or seat_without_file
              else ("env", "file", "vault") if seat else ("file", "vault") if token_file_env else ())
    if registry is not None:
        register_pairs(registry, [(key, unquote(env.get(key) or "")) for key in CREDENTIAL_KEYS])
        register_pairs(registry, [(key, values.get(key) or "") for _p, values in files for key in CREDENTIAL_KEYS])
        register_pairs(registry, getattr(files, "credential_pairs", ()))
        register_pairs(registry, token_file_pairs(token_file_text))
        # A channel file's token file is the rotation file whenever that file wins: its values are candidates.
        for _path, values in files:
            named = values.get(TOKEN_FILE_KEY) or ""
            if named and named != token_file_env and read_token_file:
                register_pairs(registry, token_file_pairs(_read_result(read_token_file(named))[0] or ""))

    found = {"raw": "", "token_key": "", "source": "none", "source_file": "", "source_vals": {},
             "file_url": ""}

    def token_from_token_file():
        raw = token_file_token(token_file_text) if token_file_env else ""
        return raw and dict(raw=raw, token_key=TOKEN_FILE_KEY, file_url=token_file_url(token_file_text))

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
        if name in closed or (blocked and name != "env"):
            continue  # a refused explicit device file: no later tier may stand in for it
        got = {"token_file": token_from_token_file, "env": token_env, "file": token_file, "vault": token_vault}[name]()
        if got:
            found.update(got, source=name)
            break
    raw = found["raw"]
    url_from_token, token = contract.parse_onboarding_token(raw)
    env_url, url_key = ("", "") if seat else first_env(env, URL_KEYS)  # a seat's URL is its token file's
    file_ok = token_file_env and not (token_file_reason or token_file_missing)
    if file_ok and not raw:
        fatal.append(f"REMOTE_TASK_TOKEN_FILE names {token_file_env}, which holds no token"
                     + ("; a seat has no other credential source. Reconnect the agent" if seat else
                        " and no REMOTE_TASK_TOKEN is set. Reconnect the agent or fix that file"))
    elif seat and raw and not (url_from_token or found["file_url"]):
        fatal.append(f"REMOTE_TASK_TOKEN_FILE names {token_file_env}, whose token carries no gateway URL; a seat's "
                     "token file is one line, <relay_url>|<bearer>. Reconnect the agent")
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
                                      if env.get(old) and not env.get(new)),
                     notes=tuple(notes), fatal="; ".join(fatal))
