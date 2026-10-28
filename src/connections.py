"""Connections: the install's directory of homeserver gateways, one row per bridge instance.

`<workspace>/state/connections.json` is written here and nowhere else. A row names the
homeserver a gateway belongs to (`server`), the bridge instance that serves it
(`instance`; "" only for the legacy unsuffixed bridge imported from REMOTE_TASK_TOKEN),
the gateway URL exactly as the relay issued it, and the vault key the secret lives under
(`token_ref`). The secret itself never enters this file: `add_connection` stores it
through the secret vault and fails closed when the vault cannot take it.

Adding is two-step and source-agnostic (a pasted code, a browser flow, an env var all
arrive as a token): `preview_connection` asks the gateway embedded in the token who the
token is, and `add_connection` saves only when the caller's `expected_server` matches
what that preview says now. Adding is idempotent per server, never changes which row is
primary (the first row ever saved becomes primary; `set_primary` is the one way to move
it), and starts no bridge by itself: `launch` does, one bridge per plan entry, and `start` is
`launch` unless a bridge already holds the lease (the HTTP add starts the new row with it).

This list is the install's instance directory. It never widens the Sutando Cloud bearer
allowlist or any other authorization: a row only tells startup which bridge to launch.

Writers go through one lock file beside the record, publish by temp file + `os.replace`,
and refuse a record over `MAX_BYTES` or off-schema on read. `status_for` joins a row
read-only with the bridge's own `state/gateway-status[.<instance>].json` and the launch
record `launch` leaves, into one `state`: pending, connected, disconnected, failed or parked
(disabled by the owner), plus `running`: whether a bridge is proven to hold the row's lease.

Which process runs a bridge is read only from the bridge's own lease, through `workspace_lock`'s
reader (`bridge_pid`): a lease that proves no holder is a refusal, never pid 0, and only a pid
proven to be the lease's writer is ever signalled; a record written for another role, workspace or
schema proves nothing. `launch` keeps the record lock until its child is proven, by that same check,
to hold the lease or has exited; an exit is a success only when another holder is proven live.

`disconnect` stops a row's bridge and then marks it `disabled`, keeping the row, the vault secret
and the launch record: the plan skips a disabled row, so a restart leaves it down until
`reconnect` clears the flag. A bridge that will not stop leaves the row as it was
(`BridgeStillRunning`); the primary cannot be disconnected while it is primary, and a
disabled row cannot become primary. `update_token` replaces the secret under a row's existing
`token_ref` with a new code for the same server and agent (anything else is refused by code),
clears the flag and relaunches; it is refused while a higher-precedence source (the row's
environment variable) would shadow the vault. The legacy unsuffixed row is the environment's:
`disconnect` and `update_token` refuse it (`LegacyRowUnmanaged`); a fresh code for its server goes
through `add_connection`, which adopts it into a named row and retires the legacy one (`legacy_readopt`
in `CAPABILITIES`). Every such refusal is a
`Refusal` whose `CODE` each surface returns unchanged.

`promote` moves the primary in one operation while the old primary's bridge runs, the legacy one
included: it flips the primary, (re)launches the new row's bridge so it carries the proactive room,
waits for that bridge to report a gateway poll, then stops the old owner's through the same stop
path and marks the old row `parked` under one hold of the record lock. A parked row stays in the
record, the plan skips it and the legacy bridge stands down on it (`legacy_parked`), until
`unpark` (or a `promote` back) clears the flag.

A row's `server_source` says where its server came from: `identity` (the resolver's
registered DotSpace for the gateway host, confirmed by the gateway's own agent listing),
`resolver` (the registered DotSpace alone) or `gateway` (the host itself, provisional). Two
tokens for one registered homeserver's endpoints collapse onto one row; a provisional row
matches only its own gateway host, and a later identity re-keys it in place, slug kept.
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import contextlib
import hashlib
import http.client
import ipaddress
import socket
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import workspace_lock  # noqa: E402
from file_lock import locked_file  # noqa: E402

STATE_NAME = "connections.json"
LOCK_NAME = "connections.lock"
LAUNCH_DIR = "connections-launch"
SCHEMA_VERSION = 1
MAX_BYTES = 256 * 1024
MAX_ROWS = 64

PRIMARY_TOKEN_REF = "REMOTE_TASK_TOKEN"
INSTANCE_TOKEN_PREFIX = "AG2_REMOTE_TOKEN_"
ADDED_BY = ("login", "owner", "ui", "env")
STATES = ("pending", "connected", "disconnected", "failed", "parked")
SERVER_SOURCES = ("identity", "resolver", "gateway")
# The bridge's own GATEWAY_INSTANCE charset; a row that fails it could never launch.
INSTANCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
# The HTTP path segment that names the legacy row, whose instance is "" (an empty segment is dropped).
LEGACY_PATH_SEGMENT = "legacy"
# Fixed names on the HTTP resource: a row so named could never be addressed there.
RESERVED_INSTANCES = ("known-servers", "token", LEGACY_PATH_SEGMENT)
_ENV_VAR_RE = re.compile(r"^AG2_REMOTE_TOKEN_([A-Za-z0-9_]+)$")
_MXID_RE = re.compile(r"^@[^:]+:([A-Za-z0-9.-]+)(?::\d+)?$")
_REQUIRED = ("server", "instance", "gateway_url", "token_ref", "primary", "added_by", "added_at", "verified",
             "server_source")
# What the gateway's agent listing says about the one agent a verified preview names; never a secret.
IDENTITY_FIELDS = ("agent_mxid", "agent_display_name", "agent_avatar_url", "owner_mxid")
AVATAR_SCHEME = "mxc://"
PROBE_TIMEOUT_S = 15
STOP_TIMEOUT_S = 8
# How long `promote` waits for the new bridge to report a gateway poll; outlasts its first long poll.
CONNECT_TIMEOUT_S = 60
VAULT_PROBE_WAIT_S = 2  # outlasts a chat interception's brief write, never a hung holder
# How long `launch` holds the record lock waiting for its child to take the bridge lease.
LEASE_TIMEOUT_S = 20
# Names that never belong to a public homeserver; a probe there would scan the operator's own network.
RESERVED_SUFFIXES = (".localhost", ".local", ".internal", ".test", ".example", ".invalid", ".onion", ".home.arpa")
# The host's own resolver unless a test injects one.
DEFAULT_GETADDRINFO: Optional[Callable] = None


class ConnectionsError(Exception):
    """Base of every refusal this module raises; the message names the cause, never a secret."""


class SchemaError(ConnectionsError):
    """The record on disk is not a connections file this module wrote."""


class ConnectionMismatch(ConnectionsError):
    def __init__(self, derived: str, claimed: str):
        super().__init__(f"token belongs to server {derived!r}, not {claimed!r}")
        self.derived, self.claimed = derived, claimed


class VaultUnavailable(ConnectionsError):
    """The secret vault refused or could not be reached; nothing was written, unless the message
    names vault_keychain_state_unknown (the Keychain may hold the write)."""


class UnknownConnection(ConnectionsError):
    pass


class PrimaryProtected(ConnectionsError):
    pass


class Refusal(ConnectionsError):
    """Refused by the row's or the code's state, nothing written: `CODE` is the error code every
    surface returns as is (HTTP 409 `{"error": CODE}`, runtime-api CONFLICT, CLI exit 1)."""

    CODE = "refused"


class PrimaryCannotDisconnect(PrimaryProtected, Refusal):
    """The primary row stays up while it is primary; make another row primary first."""

    CODE = "primary_cannot_disconnect"

    def __init__(self, instance: str):
        super().__init__(f"{self.CODE}: {instance or '<primary>'} is the primary connection; "
                         "make another connection primary first")


class DisabledCannotBePrimary(Refusal):
    """A disabled row never launches, so as primary no bridge would carry the proactive room."""

    CODE = "disabled_cannot_be_primary"

    def __init__(self, instance: str):
        super().__init__(f"{self.CODE}: {instance} is disconnected; reconnect it before making it primary")


class ParkedCannotBePrimary(Refusal):
    """A parked row does not launch; `promote` brings it back as the primary, `unpark` as a secondary."""

    CODE = "parked_cannot_be_primary"

    def __init__(self, instance: str):
        super().__init__(f"{self.CODE}: {instance or 'the legacy row'} is parked; promote it, or unpark it first")


class LegacyBridgeNotRunning(Refusal):
    """The engine never launches the legacy bridge, so promoting its row needs it already up."""

    CODE = "legacy_bridge_not_running"

    def __init__(self):
        super().__init__(f"{self.CODE}: the legacy unsuffixed bridge is not running; unpark it, start it "
                         "(bash src/startup.sh), then promote it; nothing changed")


class NewPrimaryNotStarted(Refusal):
    """`promote` could not start the new row's bridge; the old primary was never touched."""

    CODE = "new_primary_not_started"

    def __init__(self, instance: str, reason: str):
        super().__init__(f"{self.CODE}: the bridge for {instance} did not start ({reason}); the primary did not move")
        self.reason = reason


class NewPrimaryNotConnected(Refusal):
    """The new row's bridge started but the gateway never accepted its token in time; rolled back."""

    CODE = "new_primary_not_connected"

    def __init__(self, instance: str, reason: str):
        super().__init__(f"{self.CODE}: the bridge for {instance} did not connect ({reason}); "
                         "the old primary kept running and the primary did not move")
        self.reason = reason


class PromoteConflict(Refusal):
    """The primary moved, or the row `promote` was to park changed, while it waited for the new bridge."""

    CODE = "promote_conflict"

    def __init__(self, instance: str):
        super().__init__(f"{self.CODE}: the primary moved while {instance} was connecting; the other change "
                         "stands, the bridge this call started is stopped and nothing was parked")


class LegacyRowUnmanaged(Refusal):
    """The unsuffixed bridge is launched by the environment, not by this directory."""

    CODE = "legacy_row_unmanaged"

    def __init__(self, verb: str):
        super().__init__(f"{self.CODE}: the legacy unsuffixed bridge is launched from REMOTE_TASK_TOKEN in the "
                         f"environment, so {verb} does not reach it; change that variable instead")


class BridgeStillRunning(Refusal):
    """The bridge kept its singleton lock past the stop deadline; the row is as it was."""

    CODE = "bridge_still_running"

    def __init__(self, instance: str, pid: int, detail: str = ""):
        super().__init__(f"{self.CODE}: the bridge for {instance or '<primary>'} (pid {pid}) is still running"
                         + (f" ({detail})" if detail else "") + "; nothing changed")
        self.pid = pid


class BridgeLeaseIndeterminate(Refusal):
    """The bridge's lease does not show which process holds it; nothing is signalled or written."""

    CODE = "bridge_lease_indeterminate"

    def __init__(self, instance: str, detail: str):
        super().__init__(f"{self.CODE}: the bridge lease of {instance or '<primary>'} does not prove its holder "
                         f"({detail}); restart that bridge (bash src/restart.sh) and retry; nothing changed")


class LegacyPrimaryRunning(Refusal):
    """The legacy bridge runs and may carry the proactive room; it decides that only when it starts."""

    CODE = "legacy_primary_running"

    def __init__(self, instance: str, pid: int):
        super().__init__(
            f"{self.CODE}: the legacy unsuffixed bridge (pid {pid}) is running and may carry the proactive room, "
            "which it decides only when it starts; stop it (bash src/restart.sh --stop-only), "
            f"make {instance} primary, then start again (bash src/startup.sh) so the legacy bridge comes up "
            f"without the room and {instance} with it; nothing changed")
        self.pid = pid


class LegacyPrimaryInUse(Refusal):
    """A fresh code for the legacy row's server is not adopted underneath its running primary bridge."""

    CODE = "legacy_primary_in_use"

    def __init__(self, pid: int):
        super().__init__(
            f"{self.CODE}: the legacy unsuffixed bridge (pid {pid}) is running as the primary; make another "
            "connection primary first (promote), then paste the code again; nothing changed")
        self.pid = pid


class VaultUnreadable(Refusal):
    """The vault could not say whether it holds the row's current secret; nothing was touched."""

    CODE = "vault_unreadable"


class TokenRestoreFailed(Refusal):
    """A write after the vault took the new code failed, and so did putting the previous secret back:
    read back, the vault holds the NEW code while the row is unchanged."""

    CODE = "token_restore_failed"


class TokenStateUnknown(Refusal):
    """An update failed after the vault write began and reading the key back could not establish
    whether it holds the previous secret or the new code; the bridge is not relaunched on a guess."""

    CODE = "token_state_unknown"


class TokenSourceShadowed(Refusal):
    """A source the bridge reads before the vault holds this row's token; a vault write would be ignored."""

    CODE = "token_source_shadowed"

    def __init__(self, instance: str, variable: str):
        super().__init__(f"{self.CODE}: {variable} is set in the environment and outranks the vault; unset it "
                         f"(or let import-env own it) before updating the token of {instance}")
        self.variable = variable


class TokenMismatch(Refusal):
    """The new code is not this row's: `update_token` refuses it, naming the code in CODE."""

    CODE = "token_mismatch"

    def __init__(self, instance: str, detail: str):
        super().__init__(f"{self.CODE}: {detail}; connection {instance or '<primary>'} is unchanged")


class TokenServerMismatch(TokenMismatch):
    """The previewed server is not the row's; a code for another server is a new connection."""

    CODE = "token_server_mismatch"


class TokenAgentMismatch(TokenMismatch):
    """Both the row and the preview name an agent and they differ; add that agent as its own row."""

    CODE = "token_agent_mismatch"


class TokenUnverified(TokenMismatch):
    """The row names an agent but the preview could not confirm one (gateway unreachable, an error,
    or a listing not naming exactly one agent): the code is not shown to be this connection's."""

    CODE = "token_unverified"


class InvalidToken(ConnectionsError):
    pass


class NoToken(ConnectionsError):
    """No secret under the row's token_ref, in the environment or the vault."""


class LaunchFailed(ConnectionsError):
    pass


class VaultKeyInUse(Refusal):
    """The vault already holds a secret under this key and no row owns it."""

    CODE = "vault_key_in_use"


class TokenRejected(Refusal):
    """The gateway refused the token on the preview; nothing to store."""

    CODE = "token_rejected"

    def __init__(self):
        super().__init__(f"{self.CODE}: the gateway rejected this token; nothing stored")


# ── token → server ───────────────────────────────────────────────────────────

def gateway_url_of(token: str) -> str:
    """The gateway URL a `url|secret` token embeds, exactly as issued; '' for any other shape."""
    token = (token or "").strip()
    url, sep, secret = token.partition("|")
    if not sep or not secret.strip():
        return ""
    if not url.lower().startswith(("http://", "https://")):
        return ""
    return url


def secret_of(token: str) -> str:
    return (token or "").strip().partition("|")[2].strip()


def server_of_token(token: str) -> str:
    """The lowercased host of the token's embedded gateway URL: the server when nothing better is known."""
    url = gateway_url_of(token)
    if not url:
        return ""
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host.lower()


def server_of_mxid(mxid: str) -> str:
    m = _MXID_RE.match((mxid or "").strip())
    return m.group(1).lower() if m else ""


def instance_for(server: str) -> str:
    """The default instance slug for a server: its name with dots as dashes; a name too long
    for the slug keeps a prefix plus a digest, so two long servers never share a slug."""
    slug = re.sub(r"[^a-z0-9_-]", "-", server.lower().replace(".", "-")).strip("-")
    if len(slug) <= 32:
        return slug
    return slug[:24].rstrip("-") + "-" + hashlib.sha256(server.lower().encode()).hexdigest()[:7]


def host_of_url(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def token_ref_for(instance: str) -> str:
    return PRIMARY_TOKEN_REF if not instance else INSTANCE_TOKEN_PREFIX + instance.upper().replace("-", "_")


def _inst_var(instance: str) -> str:
    return instance.upper().replace("-", "_")


def channel_dir_for(instance: str, environ: Optional[Mapping[str, str]] = None) -> str:
    """`<instance>-ag2space`, unless `REMOTE_TASK_CHANNEL_DIR_<INST>` names another directory."""
    environ = os.environ if environ is None else environ
    return environ.get("REMOTE_TASK_CHANNEL_DIR_" + _inst_var(instance), "") or f"{instance}-ag2space"


# ── preview: who is this token, according to its gateway ─────────────────────

def _public_address(ip: str) -> bool:
    """Whether `ip` may be a homeserver's: not loopback, private, link-local, multicast, reserved
    or unspecified, in its own form or as the IPv4 an IPv6 address maps."""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    # is_global refuses shared address space (100.64/10, a tailnet range); the IPv4 embedded in
    # a 6to4 or Teredo address is checked on every interpreter (3.9 passes 2002::/16 as global).
    embedded = [addr, getattr(addr, "ipv4_mapped", None), getattr(addr, "sixtofour", None)]
    embedded.extend(getattr(addr, "teredo", None) or ())
    for a in embedded:
        if a is not None and (not a.is_global or a.is_loopback or a.is_private or a.is_link_local
                              or a.is_multicast or a.is_reserved or a.is_unspecified
                              or getattr(a, "is_site_local", False)):
            return False
    return True


def _gateway_port(url: str) -> Optional[int]:
    """The gateway URL's port (443 when absent); None when the URL carries userinfo, a port
    outside 1..65535, or a port that is not a number, all of which refuse the token."""
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            return None
        port = parts.port
    except ValueError:
        return None
    if port is None:
        return 443
    return port if 1 <= port <= 65535 else None


def vetted_address(host: str, port: int, getaddrinfo=None) -> Optional[str]:
    """Resolve `host` once; the first address when every answer is public, '' when any is not,
    None when the name does not resolve. The caller pins the connection to the address it got."""
    getaddrinfo = getaddrinfo or DEFAULT_GETADDRINFO or socket.getaddrinfo
    try:
        infos = getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError):
        return None
    addresses = [str(info[4][0]) for info in infos if info and info[4]]
    if not addresses:
        return None
    if not all(_public_address(ip) for ip in addresses):
        return ""
    return addresses[0]


def _connect(address: str, port: int, host: str, timeout: float):
    """TLS to the vetted address with the name for SNI and the certificate check."""
    sock = socket.create_connection((address, port), timeout=timeout)
    try:
        return ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    except Exception:
        sock.close()
        raise


def _fetch_agents(gateway_url: str, token: str, address: str, *, connect=None) -> dict:
    """The gateway's own `GET /v1/agents` under the token's bearer, over a connection pinned to
    the vetted `address` with the URL's host as SNI and Host. Anything but 200 raises HTTPError
    with that status: a redirect is never followed."""
    parts = urllib.parse.urlsplit(gateway_url)
    host = parts.hostname or ""
    port = parts.port or 443
    sock = (connect or _connect)(address, port, host, PROBE_TIMEOUT_S)
    conn = http.client.HTTPConnection(host, port, timeout=PROBE_TIMEOUT_S)
    conn.sock = sock  # the transport is the pinned one; connect() is never reached
    try:
        conn.request("GET", parts.path.rstrip("/") + "/v1/agents", headers={
            "Host": f"{parts.hostname}:{port}" if parts.port else parts.hostname,
            "Authorization": f"Bearer {secret_of(token)}",
            "Accept": "application/json", "User-Agent": "sutando-gateway-client/1.0"})
        resp = conn.getresponse()
        body = resp.read()
    finally:
        conn.close()
    if resp.status != 200:
        raise urllib.error.HTTPError(gateway_url, resp.status, resp.reason, resp.headers, None)
    return json.loads(body.decode("utf-8") or "{}")


# The resolver used when a caller passes none: `dotspace.dotspace_for` once that module imports.
DEFAULT_RESOLVER: Optional[Callable[[str], object]] = None


def _default_resolver():
    global DEFAULT_RESOLVER
    if DEFAULT_RESOLVER is None:
        try:
            import dotspace  # type: ignore
            DEFAULT_RESOLVER = dotspace.dotspace_for
        except Exception:  # noqa: BLE001 - the resolver is optional
            return None
    return DEFAULT_RESOLVER


def resolve_server(host: str, resolver=None) -> str:
    """The homeserver the resolver knows `host` to serve, '' when none is present or it only
    echoes the host. The resolver is `dotspace.dotspace_for`; injectable for tests."""
    resolver = _default_resolver() if resolver is None else resolver
    if resolver is None or not host:
        return ""
    try:
        found = resolver(host)
    except Exception:  # noqa: BLE001 - a resolver failure leaves the host provisional
        return ""
    server = str(getattr(found, "server", "") or "").lower()
    if not server or getattr(found, "source", "none") == "none":
        return ""
    return server


def gateway_host_ok(host: str) -> bool:
    """Whether a gateway host may be probed at all: a multi-label server name under no reserved
    suffix, never an IP literal, by the resolver's own rules; the addresses are vetted after."""
    if not host or "." not in host or host.endswith(RESERVED_SUFFIXES):
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    last = host.rsplit(".", 1)[-1]
    if last.isdigit() or (last.startswith("0x") and len(last) > 2):
        return False
    try:
        import dotspace  # type: ignore
        return bool(dotspace.is_server_name(host)) and not dotspace._never_fetched(host)
    except Exception:  # noqa: BLE001 - without the resolver the local rules above decide
        return re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+", host) is not None


def preview_connection(token: str, *, fetch: Optional[Callable] = None, resolver=None,
                       getaddrinfo=None) -> dict:
    """What saving this token would record, without saving: the gateway URL as issued, the
    server and where it came from (`server_source`), and whether the token authenticated
    (`authenticated` None when the gateway gave no verdict).

    The gateway's agent listing can CONFIRM an identity, never establish one: `verified` is
    true only when the listing names one agent whose server is the DotSpace the resolver
    registers for the gateway host. An unregistered host stays the server itself, whatever the
    listing claims. The gateway URL must be https and its host a public server name: an http
    URL, an address, a reserved or single-label name, or a name resolving to any non-public
    address is refused as invalid_gateway before any probe; the probe is pinned to the vetted
    address."""
    url = gateway_url_of(token)
    if not url:
        raise InvalidToken("token carries no gateway URL (expected 'https://<gateway>|<secret>')")
    host = server_of_token(token)
    port = _gateway_port(url)
    if not url.lower().startswith("https://") or port is None or not gateway_host_ok(host):
        raise InvalidToken("invalid_gateway")
    registered = resolve_server(host, resolver)
    out = {"gateway_url": url, "gateway_host": host, "server": registered or host,
           "server_source": "resolver" if registered else "gateway",
           "verified": False, "authenticated": None, "error": None}
    out.update({k: None for k in IDENTITY_FIELDS})
    address = vetted_address(host, port, getaddrinfo)
    if address == "":
        raise InvalidToken("invalid_gateway")
    if address is None:
        out["error"] = "unreachable (resolution)"
        return out
    fetch = _fetch_agents if fetch is None else fetch
    try:
        answer = fetch(url, token, address)
    except urllib.error.HTTPError as e:
        out["authenticated"] = False if e.code in (401, 403) else None
        out["error"] = {401: "auth_rejected", 403: "auth_rejected", 404: "not_a_gateway"}.get(
            e.code, f"http_{e.code}")
        return out
    except Exception as e:  # noqa: BLE001 - the probe is best-effort; the reason is named, the URL is not
        out["error"] = f"unreachable ({type(e).__name__})"
        return out
    out["authenticated"] = True
    agents = answer.get("agents") if isinstance(answer, dict) else None
    named = [a for a in agents if isinstance(a, dict) and isinstance(a.get("id"), str)] \
        if isinstance(agents, list) else []
    if registered and len(named) == 1 and server_of_mxid(named[0]["id"]) == registered:
        out.update(_identity_of(named[0]), server_source="identity", verified=True)
    return out


def _identity_of(agent: Mapping[str, object]) -> dict:
    """The row fields a listing entry carries for its agent: the mxid, and when the gateway
    sends them, `display_name`, an `mxc://` `avatar_url` and the `owner` mxid; else None."""
    owner = agent.get("owner")
    avatar = agent.get("avatar_url")
    name = agent.get("display_name")
    return {"agent_mxid": agent["id"],
            "agent_display_name": name.strip() if isinstance(name, str) and name.strip() else None,
            "agent_avatar_url": avatar if isinstance(avatar, str) and avatar.startswith(AVATAR_SCHEME) else None,
            "owner_mxid": owner if isinstance(owner, str) and server_of_mxid(owner) else None}


# ── vault ────────────────────────────────────────────────────────────────────

class _VaultIntercept:
    """The secret-vault skill's programmatic path (`vault_intercept`), bound lazily."""

    def __init__(self):
        try:
            import vault_intercept  # type: ignore
        except Exception as e:  # noqa: BLE001 - any import failure means no vault
            raise VaultUnavailable(f"secret vault not importable ({type(e).__name__})") from None
        self._m = vault_intercept

    def get(self, key: str) -> str:
        return self._m.get_vault_key(key)

    def lookup(self, key: str) -> Optional[str]:
        return self._m.lookup_vault_key(key)

    def set(self, key: str, value: str) -> None:
        self._m.set_vault_key(key, value)

    def delete(self, key: str) -> None:
        self._m.delete_vault_key(key)

    def writer_lock(self, wait_s=None):
        return self._m.writer_lock(wait_s)


def _vault(vault):
    return _VaultIntercept() if vault is None else vault


@contextlib.contextmanager
def _vault_writer(vault, wait_s=None):
    """The vault's own writer transaction when it has one (vault_intercept.writer_lock), else nothing.
    Taken only while connections.lock is held, never the reverse; one not taken is VaultUnavailable."""
    lock = getattr(vault, "writer_lock", None)
    with contextlib.ExitStack() as stack:
        try:
            if lock is not None:
                stack.enter_context(lock() if wait_s is None else lock(wait_s=wait_s))
        except Exception as e:  # noqa: BLE001 - a held or broken lock both mean nothing may be written
            raise VaultUnavailable(f"the secret vault's writer lock was not taken ({_vault_cause(e)}); "
                                   "nothing was written") from None
        yield


_VAULT_CODE_RE = re.compile(r"^(vault_[a-z0-9_]{1,60}):")


def _vault_cause(e: BaseException) -> str:
    """The error's type and, when it leads with a `vault_*` code, that code: never its free text,
    which may quote Keychain output."""
    m = _VAULT_CODE_RE.match(str(getattr(e, "CODE", "") or e) + ":")
    return f"{type(e).__name__}: {m.group(1)}" if m else type(e).__name__


def _vault_store(vault, key: str, value: str) -> None:
    try:
        vault.set(key, value)
    except Exception as e:  # noqa: BLE001 - the vault's own error types vary; the cause is named
        raise VaultUnavailable(f"could not store {key} in the secret vault ({_vault_cause(e)})") from None


def _vault_get(vault, key: str) -> str:
    try:
        return str(vault.get(key) or "")
    except Exception:  # noqa: BLE001 - a miss and a failure both read as absent
        return ""


def _vault_snapshot(vault, key: str) -> Optional[str]:
    """The secret under `key`, None when the vault says it holds none; VaultUnreadable when it
    cannot tell, so a caller never mistakes a failed read for an absent secret."""
    lookup = getattr(vault, "lookup", None)
    try:
        if lookup is not None:
            value = lookup(key)
        else:
            try:
                value = vault.get(key)
            except KeyError:
                return None
    except Exception as e:  # noqa: BLE001 - the vault's own error types vary; the cause is named
        raise VaultUnreadable(f"{VaultUnreadable.CODE}: could not read {key} from the secret vault "
                              f"({type(e).__name__}); nothing changed") from None
    return str(value) if value else None


def _vault_delete(vault, key: str) -> None:
    try:
        vault.delete(key)
    except Exception as e:  # noqa: BLE001
        raise VaultUnavailable(f"could not remove {key} from the secret vault ({_vault_cause(e)})") from None


# ── record I/O ───────────────────────────────────────────────────────────────

def _workspace(workspace: Optional[Path]) -> Path:
    if workspace is not None:
        return Path(workspace)
    from workspace_default import resolve_workspace
    return resolve_workspace()


def record_path(workspace: Optional[Path] = None) -> Path:
    return _workspace(workspace) / "state" / STATE_NAME


def _lock_path(workspace: Optional[Path]) -> Path:
    return _workspace(workspace) / "state" / LOCK_NAME


def launch_record_path(instance: str, workspace: Optional[Path] = None) -> Path:
    return _workspace(workspace) / "state" / LAUNCH_DIR / f"{instance or 'primary'}.json"


def _validate_row(row: object, index: int) -> dict:
    if not isinstance(row, dict):
        raise SchemaError(f"connections.json: row {index} is not an object")
    missing = [k for k in _REQUIRED if k not in row]
    if missing:
        raise SchemaError(f"connections.json: row {index} lacks {', '.join(missing)}")
    for key in ("server", "instance", "gateway_url", "token_ref", "added_by", "added_at"):
        if not isinstance(row[key], str):
            raise SchemaError(f"connections.json: row {index} field {key} is not a string")
    for key in ("primary", "verified"):
        if not isinstance(row[key], bool):
            raise SchemaError(f"connections.json: row {index} field {key} is not a boolean")
    if row["instance"] and not INSTANCE_RE.match(row["instance"]):
        raise SchemaError(f"connections.json: row {index} instance {row['instance']!r} is not a slug")
    if row["added_by"] not in ADDED_BY:
        raise SchemaError(f"connections.json: row {index} added_by {row['added_by']!r} unknown")
    for key in IDENTITY_FIELDS:
        if row.get(key) is not None and not isinstance(row[key], str):
            raise SchemaError(f"connections.json: row {index} {key} is not a string")
    if row.get("agent_avatar_url") and not row["agent_avatar_url"].startswith(AVATAR_SCHEME):
        raise SchemaError(f"connections.json: row {index} agent_avatar_url is not an {AVATAR_SCHEME} URI")
    if row["server_source"] not in SERVER_SOURCES:
        raise SchemaError(f"connections.json: row {index} server_source {row['server_source']!r} unknown")
    # Absent on rows written before the flag existed: those load as enabled.
    for key in ("disabled", "parked"):
        if key in row and not isinstance(row[key], bool):
            raise SchemaError(f"connections.json: row {index} field {key} is not a boolean")
    keys = row.get("launch_keys", [])
    if not isinstance(keys, list) or not all(isinstance(k, str) and _ENV_VAR_RE.match(k) for k in keys):
        raise SchemaError(f"connections.json: row {index} launch_keys is not a list of AG2_REMOTE_TOKEN_<INST> names")
    return row


REMOVED_KEY = "removed_env"
# Record-level park of a legacy bridge that has no row of its own (a named row covers its server).
LEGACY_PARKED_KEY = "legacy_parked"
# What this engine's connections surfaces support, listed beside the rows so a client can gate on it.
CAPABILITIES = ("legacy_readopt",)


def _validate(payload: object) -> dict:
    """{"rows": [...], "removed_env": [...]} from a record, or SchemaError naming the defect."""
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise SchemaError(f"connections.json: expected schema_version {SCHEMA_VERSION}")
    rows = payload.get("connections")
    if not isinstance(rows, list):
        raise SchemaError("connections.json: 'connections' is not a list")
    if len(rows) > MAX_ROWS:
        raise SchemaError(f"connections.json: more than {MAX_ROWS} rows")
    rows = [_validate_row(r, i) for i, r in enumerate(rows)]
    for key in ("instance", "token_ref", "server"):
        values = [r[key] for r in rows]
        if len(set(values)) != len(values):
            raise SchemaError(f"connections.json: duplicate {key}")
    if sum(1 for r in rows if r["primary"]) > 1:
        raise SchemaError("connections.json: more than one primary row")
    removed = payload.get(REMOVED_KEY, [])
    if not isinstance(removed, list) or not all(isinstance(x, str) for x in removed):
        raise SchemaError(f"connections.json: '{REMOVED_KEY}' is not a list of names")
    parked = payload.get(LEGACY_PARKED_KEY, False)
    if not isinstance(parked, bool):
        raise SchemaError(f"connections.json: '{LEGACY_PARKED_KEY}' is not a boolean")
    return {"rows": rows, REMOVED_KEY: sorted(set(removed)), LEGACY_PARKED_KEY: parked}


def _read_record(path: Path) -> dict:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return {"rows": [], REMOVED_KEY: [], LEGACY_PARKED_KEY: False}
    if size > MAX_BYTES:
        raise SchemaError(f"connections.json: {size} bytes exceeds the {MAX_BYTES}-byte bound")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise SchemaError(f"connections.json: not valid JSON ({e})") from None
    return _validate(payload)


def _read(path: Path) -> list:
    return _read_record(path)["rows"]


def _atomic_json(path: Path, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if len(body.encode("utf-8")) > MAX_BYTES:
        raise SchemaError(f"{path.name}: record would exceed the {MAX_BYTES}-byte bound")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _write(path: Path, record: dict) -> None:
    payload = {"schema_version": SCHEMA_VERSION, "connections": record["rows"],
               REMOVED_KEY: sorted(set(record[REMOVED_KEY]))}
    if record.get(LEGACY_PARKED_KEY):
        payload[LEGACY_PARKED_KEY] = True
    _validate(payload)
    _atomic_json(path, payload)


@contextmanager
def _transaction(workspace: Optional[Path]) -> Iterator[dict]:
    """Read-mutate-publish under the record's lock; the yielded record is what gets written."""
    path = record_path(workspace)
    with locked_file(_lock_path(workspace), create_mode=0o600):
        record = _read_record(path)
        yield record
        _write(path, record)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _row(*, server: str, instance: str, gateway_url: str, token_ref: str, primary: bool,
         added_by: str, verified: bool, server_source: str = "gateway",
         identity: Optional[Mapping[str, object]] = None) -> dict:
    row = {"server": server, "instance": instance, "gateway_url": gateway_url,
           "token_ref": token_ref, "primary": primary, "added_by": added_by, "added_at": _now(),
           "verified": verified, "server_source": server_source, "disabled": False}
    _learn_fields(row, identity or {})
    return row


def _learn_fields(row: dict, seen: Mapping[str, object]) -> None:
    """Copy the identity fields a preview carries onto the row; an absent or empty one is left as is."""
    for key in IDENTITY_FIELDS:
        value = seen.get(key)
        if isinstance(value, str) and value:
            row[key] = value


def _find(rows: list, instance: str) -> dict:
    match = [r for r in rows if r["instance"] == instance]
    if not match:
        raise UnknownConnection(f"no connection {instance or '<primary>'}")
    return match[0]


# ── API ──────────────────────────────────────────────────────────────────────

def list_connections(workspace: Optional[Path] = None, *, vault=None) -> list:
    """Every row, validated; [] when no record exists.

    An uninitialized record (no file yet) first takes what `import_from_env` finds, so a token the
    connect flow wrote after the engine started shows up on the next list. A record that exists
    but is empty is left alone: its rows were removed on purpose, and a lingering token must not
    bring them back. An import that fails is logged and the list still answers; a record that
    fails validation raises as before. A record that exists still adopts the launch keys a host
    wrote after it (`channel_launch_keys`), as `add_connection` does; a removed key stays removed."""
    path = record_path(workspace)
    rows = _read(path)
    if rows or path.exists():
        return _read(path) if _adopt_launch_keys(rows, workspace, vault) else rows
    try:
        if import_from_env(workspace=workspace, vault=vault):
            rows = _read(path)
    except Exception as e:  # noqa: BLE001 - the list answers even when nothing could be imported
        logging.getLogger(__name__).warning("connections: import on an empty record failed (%s)", type(e).__name__)
    return rows


def _adopt_launch_keys(rows: list, workspace: Optional[Path], vault) -> bool:
    """Import the launch keys no row owns yet; True when the import ran. Never raises."""
    try:
        known = {r["token_ref"] for r in rows} | {k for r in rows for k in r.get("launch_keys") or []}
        known |= set(removed_env_refs(workspace))
        taken = {r["instance"] for r in rows}
        # A key whose instance another row holds never becomes a row; skipping it keeps a list read-only.
        new = {v: t for v, t in channel_launch_keys().items()
               if v not in known and v[len(INSTANCE_TOKEN_PREFIX):].lower() not in taken}
        if not new:
            return False
        import_from_env(new, workspace=workspace, vault=vault)
        return True
    except Exception as e:  # noqa: BLE001 - the list answers even when nothing could be adopted
        logging.getLogger(__name__).warning("connections: adopting launch keys failed (%s)", type(e).__name__)
        return False


def removed_env_refs(workspace: Optional[Path] = None) -> list:
    """Env token names whose rows were removed; import_from_env leaves them alone."""
    return _read_record(record_path(workspace))[REMOVED_KEY]


def known_servers(workspace: Optional[Path] = None, *, instances=None) -> list:
    """Every DotSpace the resolver registers (`dotspace.registered_instances`: the configured
    list, else the built-in default), one entry per server in registry order, joined with the
    rows: `connected` is whether a row exists for it, `instance` that row's slug or None, and
    `primary` / `disabled` that row's flags. Nothing is fetched; a server no row names is
    the picker's offer, a server with a row is already in the directory."""
    if instances is None:
        try:
            import dotspace  # type: ignore
            instances = dotspace.registered_instances()
        except Exception:  # noqa: BLE001 - without the resolver there is no registry to offer
            instances = []
    by_server = {r["server"]: r for r in list_connections(workspace)}
    out, seen = [], set()
    for d in instances:
        server = str(getattr(d, "server", "") or "").lower()
        if not server or server in seen:
            continue
        seen.add(server)
        row = by_server.get(server)
        out.append({"server": server, "connected": row is not None,
                    "instance": row["instance"] if row else None,
                    "primary": bool(row and row["primary"]), "disabled": bool(row and row.get("disabled"))})
    return out


def channel_launch_keys(environ: Optional[Mapping[str, str]] = None) -> dict:
    """{var: token} for every `AG2_REMOTE_TOKEN_<INST>` the AG2 Space channel env file sets, then the
    environment's (which win): the launch keys a desktop host writes after the engine started."""
    environ = os.environ if environ is None else environ
    found = {}
    try:
        from channel_env_resolve import resolve_channel_env
        from channel_token import token_from_env_file
        from util_paths import claude_home_path
        path = resolve_channel_env(claude_home_path("channels"), "ag2space")
        names = re.findall(r"^\s*(AG2_REMOTE_TOKEN_[A-Za-z0-9_]+)=", path.read_text(), re.M) if path else []
        found = {v: token_from_env_file(v, path) for v in names}
    except Exception:  # noqa: BLE001 - no channel home or an unreadable file is simply no keys
        found = {}
    found.update({v: environ[v] for v in environ if _ENV_VAR_RE.match(v)})
    return {v: t.strip() for v, t in found.items() if t and t.strip()}


def add_connection(token: str, *, added_by: str, expected_server: str, instance: Optional[str] = None,
                   preview: Optional[dict] = None, fetch: Optional[Callable] = None,
                   resolver=None, getaddrinfo=None, workspace: Optional[Path] = None,
                   vault=None, environ: Optional[Mapping[str, str]] = None, kill=None) -> "tuple[dict, bool]":
    """Save the token as a row once its gateway still answers to `expected_server`.

    Launch keys not yet in the record (`channel_launch_keys`) are imported first, under the instance
    their launcher already runs them as, so a token for one of their servers adopts that row instead
    of starting a second bridge on another slug. Returns (row, created). The same connection (see `_same_connection`) returns its row,
    re-keyed to the identity's server when the row was provisional and this preview is
    verified, and touches the vault never. The row is primary only when the install has no
    primary yet (the first row ever saved); later rows never are. The secret goes to the vault
    under the row's token_ref before the row exists; a vault key no row owns is never overwritten.

    A verified code for the legacy row's server that is not the secret that row runs on re-adopts
    the server (`_readopts_legacy`): getting it rotated the environment's credential, so the code
    becomes a new named, app-managed row and the legacy row is retired the way `promote` leaves a
    legacy bridge a named row covers. Its row is dropped, its bridge stopped, the record marked
    `legacy_parked` and REMOTE_TASK_TOKEN remembered as removed, so no launcher starts it on the
    dead token. Refused for another agent than the one the legacy row learned (`TokenAgentMismatch`,
    as `update_token`) and while that bridge runs as the primary (`LegacyPrimaryInUse`).
    """
    if added_by not in ADDED_BY:
        raise ConnectionsError(f"added_by must be one of {', '.join(ADDED_BY)}")
    if instance is not None and not isinstance(instance, str):
        raise ConnectionsError("instance must be a string")
    seen = preview if preview is not None else preview_connection(token, fetch=fetch, resolver=resolver,
                                                                  getaddrinfo=getaddrinfo)
    server = str(seen.get("server") or "")
    if not server:
        raise InvalidToken("token carries no gateway URL (expected 'https://<gateway>|<secret>')")
    if not isinstance(expected_server, str) or expected_server.strip().lower() != server:
        raise ConnectionMismatch(server, expected_server if isinstance(expected_server, str) else "")
    if seen.get("authenticated") is False:
        raise TokenRejected()
    inst = instance if instance is not None else instance_for(server)
    if not INSTANCE_RE.match(inst):
        raise ConnectionsError(f"instance {inst!r} is not a slug of [A-Za-z0-9_-]{{1,32}}")
    if inst in RESERVED_INSTANCES:
        raise ConnectionsError(f"instance {inst!r} is a reserved name ({', '.join(RESERVED_INSTANCES)}); pass another")
    ref = token_ref_for(inst)
    vault = _vault(vault)
    keys = channel_launch_keys(environ)
    if keys:
        import_from_env(keys, workspace=workspace, vault=vault, resolver=resolver)
    stored = False
    try:
        with _transaction(workspace) as rec:
            rows = rec["rows"]
            existing = _same_connection(rows, seen)
            retired = None
            if existing is not None and _readopts_legacy(existing, seen, token, vault, environ):
                mine, theirs = existing.get("agent_mxid"), seen.get("agent_mxid")
                if mine and theirs != mine:
                    raise TokenAgentMismatch("", f"the code is for agent {theirs}, not {mine}")
                pid = bridge_pid("", workspace)
                if pid and (existing["primary"] or not any(r["primary"] for r in rows)):
                    raise LegacyPrimaryInUse(pid)
                rows.remove(existing)
                retired, existing = existing, None
            if existing is not None:
                _rekey(existing, seen)
                return dict(existing), False
            if any(r["instance"] == inst or r["token_ref"] == ref for r in rows):
                raise ConnectionsError(f"instance {inst!r} is taken by another server; pass another instance")
            if _vault_get(vault, ref):
                raise VaultKeyInUse(f"vault key {ref} already holds a secret that no connection owns")
            stored = True  # a failed store may still have landed (vault_keychain_state_unknown)
            _vault_store(vault, ref, token.strip())
            if retired is not None:
                stop_bridge("", workspace=workspace, kill=kill)
                rec[LEGACY_PARKED_KEY] = True
                rec[REMOVED_KEY] = sorted(set(rec[REMOVED_KEY]) | {retired["token_ref"]})
            row = _row(server=server, instance=inst, gateway_url=str(seen["gateway_url"]), token_ref=ref,
                       primary=not any(r["primary"] for r in rows), added_by=added_by,
                       verified=bool(seen.get("verified")),
                       server_source=str(seen.get("server_source") or "gateway"),
                       identity=seen)
            rows.append(row)
            rec[REMOVED_KEY] = [x for x in rec[REMOVED_KEY] if x != ref]
    except Exception:
        if stored:
            # A secret without a row would be unreachable; take it back out.
            try:
                vault.delete(ref)
            except Exception:  # noqa: BLE001
                pass
        raise
    return dict(row), True


def _readopts_legacy(row: Mapping[str, object], seen: Mapping[str, object], token: str, vault,
                     environ: Optional[Mapping[str, str]]) -> bool:
    """Whether a code matched to `row` re-adopts its server: the row is the legacy one, the preview
    is verified, and the code is not the secret the row's vault key or environment holds."""
    if row["instance"] or not seen.get("verified"):
        return False
    environ = os.environ if environ is None else environ
    held = (environ.get(PRIMARY_TOKEN_REF) or "").strip() or primary_token_from_channel_file(environ)
    return token.strip() not in (held, _vault_snapshot(vault, str(row["token_ref"])))


def _same_connection(rows: list, seen: Mapping[str, object]) -> Optional[dict]:
    """The row this preview belongs to. A row whose server the registry or an identity vouches
    for matches on server; a provisional (gateway-sourced) row matches only on its own gateway
    host, so it can never stand in for a real connection elsewhere."""
    server = str(seen.get("server") or "")
    host = str(seen.get("gateway_host") or "")
    for r in rows:
        if r["server_source"] != "gateway" and r["server"] == server:
            return r
    for r in rows:
        if host and host_of_url(r["gateway_url"]) == host:
            return r
    return None


def _rekey(row: dict, seen: Mapping[str, object]) -> bool:
    """A verified identity replaces a provisional server in place; the instance slug stays. A row
    already keyed by identity only refreshes its identity fields from a verified preview of the
    same server AND the same agent (or one it has not learned yet), never its key: another agent
    of that server is another connection, whatever becomes of its code."""
    if not seen.get("verified"):
        return False
    if row["server_source"] == "identity":
        if str(seen.get("server") or "") == row["server"] and row.get("agent_mxid") in (None, seen.get("agent_mxid")):
            _learn_fields(row, seen)
        return False
    row.update(server=str(seen["server"]), server_source="identity", verified=True)
    _learn_fields(row, seen)
    return True


def learn_identity(instance: str, preview: Mapping[str, object], *, workspace: Optional[Path] = None) -> dict:
    """Re-key `instance` from a verified preview of its own token (see `_rekey`); the row either way."""
    with _transaction(workspace) as rec:
        row = _find(rec["rows"], instance)
        _rekey(row, preview)
    return dict(row)


def primary_instance(workspace: Optional[Path] = None) -> Optional[str]:
    """The instance slug of the primary row ("" for the legacy unsuffixed bridge); None when no
    row is primary. The legacy launch reads it to drop the proactive room from its environment
    once a named row holds the primary, so only one bridge posts nudges."""
    for row in list_connections(workspace):
        if row.get("primary"):
            return str(row.get("instance") or "")
    return None


def legacy_keeps_proactive_room(workspace: Optional[Path] = None) -> bool:
    """Asked by the legacy bridge right after it takes its lease: whether it may keep the proactive
    room. Under the record lock, so a `set_primary` either committed first (False here) or runs
    after and sees this bridge's lease (refused); an unreadable record keeps it (env-only fail-open)."""
    try:
        with locked_file(_lock_path(workspace), create_mode=0o600):
            rows = _read(record_path(workspace))
    except (SchemaError, OSError):
        return True
    return not any(r["primary"] and r["instance"] for r in rows)


def legacy_parked(workspace: Optional[Path] = None) -> bool:
    """Whether a primary move parked the legacy row, or the legacy bridge that has no row: asked by
    its launchers before they spawn it and by the bridge right after it takes its lease. Under the
    record lock, so a `promote` that is stopping it commits first; an unreadable record parks nothing
    (the env-only fail-open)."""
    try:
        with locked_file(_lock_path(workspace), create_mode=0o600):
            rec = _read_record(record_path(workspace))
    except (SchemaError, OSError):
        return False
    return rec[LEGACY_PARKED_KEY] or any(not r["instance"] and r.get("parked") for r in rec["rows"])


def owns_unaddressed(instance: str, workspace: Optional[Path] = None) -> bool:
    """Whether the bridge running as `instance` owns unaddressed proactive messages and the owner's
    cards: the primary row's bridge, under its slug or a launch-key alias. With no named primary, or
    an unreadable record, the legacy unsuffixed bridge owns them, as before any row existed."""
    try:
        primary = next((r for r in _read(record_path(workspace)) if r["primary"]), None)
    except (SchemaError, OSError):
        primary = None
    if primary is None or not primary["instance"]:
        return not instance
    return bool(instance) and instance in [primary["instance"]] + alias_instances(primary)


def set_primary(instance: str, *, workspace: Optional[Path] = None) -> dict:
    """Make `instance` the primary; proactive and owner-DM defaults follow it at the next launch.
    Refused before anything is written: a disabled or parked row (`DisabledCannotBePrimary`,
    `ParkedCannotBePrimary`: neither launches); any named promotion while the legacy bridge runs,
    whatever the record's primary (`LegacyPrimaryRunning`: it settles the room once, at start, so it
    may hold it); a lease that proves no holder (`BridgeLeaseIndeterminate`). The returned row carries
    `relaunch_needed`: true when the new primary's bridge is running, since it keeps its launch
    environment until `reconnect`."""
    with _transaction(workspace) as rec:
        row = _find(rec["rows"], instance)
        if row.get("disabled"):
            raise DisabledCannotBePrimary(instance)
        if row.get("parked"):
            raise ParkedCannotBePrimary(instance)
        promoted = not row["primary"]
        if promoted and instance:
            legacy = bridge_pid("", workspace)
            if legacy:
                raise LegacyPrimaryRunning(instance, legacy)
        relaunch = bool(promoted and instance and bridge_pid(instance, workspace))
        for r in rec["rows"]:
            r["primary"] = r is row
    out = dict(row)
    out["relaunch_needed"] = relaunch
    return out


def _owner_row(rows: list, workspace: Optional[Path]) -> Optional[dict]:
    """The row whose bridge owns unaddressed messages now (`owns_unaddressed`): the primary, else
    the legacy row while its bridge runs."""
    primary = next((r for r in rows if r["primary"]), None)
    if primary is not None:
        return primary
    legacy = next((r for r in rows if not r["instance"]), None)
    return legacy if legacy is not None and bridge_pid("", workspace) else None


def promote(instance: str, *, repo: Path, logs_dir: Path, environ: Optional[Mapping[str, str]] = None,
            workspace: Optional[Path] = None, vault=None, spawn=None, kill=None,
            python: Optional[str] = None, connect_timeout_s: Optional[float] = None) -> dict:
    """Make `instance` the primary while the old primary's bridge may still run, the legacy one included.

    In order: under the record lock, clear the row's `parked` flag and flip the primary to it, so
    `launch_env` gives its bridge the proactive room; stop the row's bridge if one runs (it was
    launched as a secondary, without the room) and `start` it; wait up to `CONNECT_TIMEOUT_S` for
    that bridge's own status to show the gateway accepted its token; then, under one hold of the
    record lock, stop the old owner's bridge (`_owner_row` before the flip, by the stop path
    `disconnect` uses) and mark that row `parked`. A legacy bridge with no row of its own (a named
    row covers its server) is stopped and parked as well (`legacy_parked`). Ownership moves at the
    flip (`owns_unaddressed` reads the record per pass): until the new bridge polls nobody owns
    unaddressed messages, and they wait. Refused before anything changes: a disabled row. A start
    that fails is `NewPrimaryNotStarted`, one that never connects `NewPrimaryNotConnected`; those,
    a refused stop (`BridgeStillRunning`, `BridgeLeaseIndeterminate`) and `PromoteConflict` (the
    primary moved, or the row to park was removed or parked, while it waited) re-raise after the
    rollback: the primary flag (never over a newer move) and a cleared `parked` flag are restored,
    a bridge this call started is stopped and one it stopped is started again, and the old bridge was never touched
    (a rowless legacy bridge already stopped stays parked). The legacy row is `_promote_legacy`.
    Idempotent: on the current primary it only starts the bridge if it is down. The row carries
    `previous` (the parked instance, None when nothing was parked), `stopped_pid`,
    `legacy_stopped_pid`, `launch`, `changed` and `relaunch_needed` (the new primary's bridge
    runs without the room: one that was up under another launch and could not be relaunched)."""
    if not instance:
        return _promote_legacy(workspace, kill)
    with _transaction(workspace) as rec:
        row = _find(rec["rows"], instance)
        if row.get("disabled"):
            raise DisabledCannotBePrimary(instance)
        before = next((r["instance"] for r in rec["rows"] if r["primary"]), None)
        old = _owner_row(rec["rows"], workspace)
        old_inst = None if old is None or old is row else old["instance"]
        was_parked, was_primary = bool(row.pop("parked", False)), row["primary"]
        for r in rec["rows"]:
            r["primary"] = r is row
        snapshot = dict(row)
    undo = {"instance": instance, "before": before, "was_parked": was_parked, "workspace": workspace,
            "kill": kill, "started": 0, "relaunched": False}
    start_kw = dict(repo=repo, logs_dir=logs_dir, environ=environ, workspace=workspace, vault=vault,
                    spawn=spawn, python=python)
    try:
        if not was_primary:
            undo["relaunched"] = bool(_stop_row(snapshot, workspace, kill))
        launch = start(instance, **start_kw)
        if not launch.get("ok"):
            raise NewPrimaryNotStarted(instance, str(launch.get("reason") or "launch_failed"))
        undo["started"] = int(launch.get("pid") or 0) if launch.get("lease") == "held" else 0
        _await_connected(instance, int(launch.get("holder_pid") or launch.get("pid") or 0), workspace,
                         CONNECT_TIMEOUT_S if connect_timeout_s is None else connect_timeout_s)
    except ConnectionsError:
        with locked_file(_lock_path(workspace), create_mode=0o600):
            _undo_promote(_read_record(record_path(workspace)), **undo)
        _restart_secondary(undo, start_kw)
        raise
    path = record_path(workspace)
    failure = None
    with locked_file(_lock_path(workspace), create_mode=0o600):
        rec = _read_record(path)
        row = next((r for r in rec["rows"] if r["instance"] == instance), None)
        old = next((r for r in rec["rows"] if old_inst is not None and r["instance"] == old_inst), None)
        stopped = legacy_stopped = 0
        try:
            # Another writer may have moved the primary while the new bridge connected.
            if row is None or not row["primary"] or (old_inst is not None and (old is None or old.get("parked"))):
                raise PromoteConflict(instance)
            if row.get("disabled"):
                raise DisabledCannotBePrimary(instance)
            if not any(not r["instance"] for r in rec["rows"]):
                legacy_stopped = stop_bridge("", workspace=workspace, kill=kill)
                rec[LEGACY_PARKED_KEY] = rec[LEGACY_PARKED_KEY] or bool(legacy_stopped)
            stopped = _stop_row(old, workspace, kill) if old is not None else 0
        except ConnectionsError as e:
            _undo_promote(rec, **undo, restore_primary=not isinstance(e, PromoteConflict))
            failure = e
        else:
            if old is not None:
                old["parked"] = True
            row.pop("parked", None)
            for r in rec["rows"]:
                r["primary"] = r is row
            _write(path, rec)
    if failure is not None:
        _restart_secondary(undo, start_kw)
        raise failure
    out = dict(row)
    out.update(previous=None if old is None else old["instance"], stopped_pid=stopped,
               legacy_stopped_pid=legacy_stopped, launch=launch,
               changed=old is not None or bool(legacy_stopped) or was_parked or not was_primary,
               relaunch_needed=launch.get("lease") in ("already_running", "alias_running") and not was_primary)
    return out


def _promote_legacy(workspace: Optional[Path], kill) -> dict:
    """`promote('')`: the engine never launches the legacy bridge, so it must already run
    (`LegacyBridgeNotRunning`); under one hold of the record lock stop the old owner's bridge, park
    its row and flip. That bridge settled the proactive room when it started, so with a named
    primary then it runs without the room and the row carries `relaunch_needed`: its launchers
    restart it (`bash src/restart.sh`)."""
    path = record_path(workspace)
    with locked_file(_lock_path(workspace), create_mode=0o600):
        rec = _read_record(path)
        row = _find(rec["rows"], "")
        if row.get("disabled"):
            raise DisabledCannotBePrimary("")
        if not bridge_pid("", workspace):
            raise LegacyBridgeNotRunning()
        was_parked, was_primary = bool(row.get("parked")), row["primary"]
        old = _owner_row(rec["rows"], workspace)
        old = None if old is row else old
        stopped = _stop_row(old, workspace, kill) if old is not None else 0
        if old is not None:
            old["parked"] = True
        row.pop("parked", None)
        rec[LEGACY_PARKED_KEY] = False
        for r in rec["rows"]:
            r["primary"] = r is row
        _write(path, rec)
    out = dict(row)
    out.update(previous=None if old is None else old["instance"], stopped_pid=stopped, legacy_stopped_pid=0,
               launch=None, changed=old is not None or was_parked or not was_primary,
               relaunch_needed=bool(old is not None and old["instance"]))
    return out


def _await_connected(instance: str, pid: int, workspace: Optional[Path], timeout_s: float) -> None:
    """Until the deadline: return once `pid`'s own status file says the gateway answered its poll;
    `NewPrimaryNotConnected` when that status shows the token rejected, the bridge exits, or time runs out."""
    deadline = time.monotonic() + timeout_s
    while True:
        sidecar = _sidecar(instance, workspace)
        if pid and sidecar.get("pid") == pid:
            if sidecar.get("connected"):
                return
            if derive_state(sidecar, {})[1] == "auth_rejected":
                raise NewPrimaryNotConnected(instance, "auth_rejected")
        if not pid or _exited(pid):
            raise NewPrimaryNotConnected(instance, "exited")
        if time.monotonic() >= deadline:
            raise NewPrimaryNotConnected(instance, "timeout")
        time.sleep(0.2)


def _undo_promote(rec: dict, *, instance: str, before: Optional[str], was_parked: bool,
                  workspace: Optional[Path], kill, started: int, relaunched: bool,
                  restore_primary: bool = True) -> None:
    """Under the record lock: stop the bridge `promote` started, put the primary back on `before`
    (unless another writer moved it since) and restore the row's `parked` flag; a bridge that will
    not stop keeps the row unparked."""
    down = True
    if started:
        try:
            stop_bridge(instance, workspace=workspace, kill=kill)
        except ConnectionsError:
            down = False
    for r in rec["rows"]:
        if restore_primary:
            r["primary"] = r["instance"] == before
        if r["instance"] == instance and was_parked and down:
            r["parked"] = True
    _write(record_path(workspace), rec)


def _restart_secondary(undo: dict, start_kw: dict) -> None:
    """After a rollback, outside the record lock (`launch` takes it): start again, as a secondary,
    the bridge `promote` stopped to relaunch it."""
    if undo["relaunched"]:
        start(undo["instance"], **start_kw)


def unpark(instance: str, *, repo: Path, logs_dir: Path, environ: Optional[Mapping[str, str]] = None,
           workspace: Optional[Path] = None, vault=None, spawn=None, python: Optional[str] = None) -> dict:
    """Clear the row's `parked` flag and, for a named row, `start` its bridge as a secondary (`launch`
    in the row; None for the legacy row, whose launchers resume it, and for a disabled one).
    Un-parking a row that is not parked only starts it. `''` also clears the parked legacy bridge
    that has no row of its own (a bare `{"instance": ""}` comes back)."""
    with _transaction(workspace) as rec:
        if not instance:
            rec[LEGACY_PARKED_KEY] = False
        row = next((r for r in rec["rows"] if r["instance"] == instance), None)
        if row is None and instance:
            row = _find(rec["rows"], instance)
        row = {"instance": "", "primary": False} if row is None else row
        row.pop("parked", None)
    out = dict(row)
    out["launch"] = start_added(row, repo=repo, logs_dir=logs_dir, environ=environ, workspace=workspace,
                                vault=vault, spawn=spawn, python=python)
    return out


def remove_connection(instance: str, *, force: bool = False, workspace: Optional[Path] = None,
                      vault=None, kill=None) -> dict:
    """Stop the bridge, then drop the row, its vault key, its launch record and (a named row) its
    status sidecar, all under the record lock; the primary only under `force`. Other rows and their
    bridges are untouched; no row inherits the primary flag.
    An env-imported row is remembered so import_from_env does not bring it back."""
    vault = _vault(vault)
    path = record_path(workspace)
    # Cleanup stays under the record lock: a same-name re-add must not have its new files deleted.
    with locked_file(_lock_path(workspace), create_mode=0o600):
        rec = _read_record(path)
        rows = rec["rows"]
        row = _find(rows, instance)
        if row["primary"] and not force:
            raise PrimaryProtected("refusing to remove the primary connection without force")
        stopped = _stop_row(row, workspace, kill) if instance else 0
        _vault_delete(vault, row["token_ref"])
        rows.remove(row)
        if row["added_by"] == "env":
            rec[REMOVED_KEY] = sorted(set(rec[REMOVED_KEY]) | {row["token_ref"]})
        _write(path, rec)
        stale = [launch_record_path(instance, workspace)]
        if instance:  # stop_bridge waited for the process to exit, so no later status write lands
            from workspace_default import status_path
            name = f"gateway-status.{instance}.json"
            stale += [status_path(name, _workspace(workspace)), _workspace(workspace) / name]
        for stale_path in stale:
            try:
                stale_path.unlink()
            except FileNotFoundError:
                pass
    out = dict(row)
    out["stopped_pid"] = stopped
    return out


def disconnect(instance: str, *, workspace: Optional[Path] = None, kill=None) -> dict:
    """Stop the bridge, then mark the row disabled; the row, its vault key and its launch record
    stay, so `reconnect` resumes it without a new token. The primary is refused while it is
    primary (`PrimaryCannotDisconnect`), the legacy unsuffixed row always (`LegacyRowUnmanaged`),
    and a bridge that keeps its lock past the stop deadline leaves the row enabled
    (`BridgeStillRunning`): `disabled` is only ever written over a stopped bridge. Disconnecting
    a disabled row is a no-op."""
    with _transaction(workspace) as rec:
        row = _find(rec["rows"], instance)
        if not instance:
            raise LegacyRowUnmanaged("disconnect")
        if row["primary"]:
            raise PrimaryCannotDisconnect(instance)
        stopped = _stop_row(row, workspace, kill)
        row["disabled"] = True
    out = dict(row)
    out["stopped_pid"] = stopped
    return out


def _enable(instance: str, workspace: Optional[Path]) -> None:
    with _transaction(workspace) as rec:
        row = _find(rec["rows"], instance)
        row["disabled"] = False
        row.pop("parked", None)


def env_token_vars(environ: Mapping[str, str]) -> list:
    """(instance, var) for every `AG2_REMOTE_TOKEN_<INST>` with a non-empty value, instance lowercased."""
    out = []
    for var in sorted(environ):
        m = _ENV_VAR_RE.match(var)
        if m and environ[var].strip():
            out.append((m.group(1).lower(), var))
    return out


def primary_token_from_channel_file(environ: Mapping[str, str]) -> str:
    """The relay token (`url|secret`) the AG2 Space channel env file holds under either relay
    spelling, read through the resolver `scripts/channel-env.sh` uses; '' when the environment
    names one itself, when no file resolves, or when the file holds none."""
    try:
        from channel_credentials import channel_credentials
        from channel_token import RELAY_TOKEN_VARS
        found = channel_credentials(RELAY_TOKEN_VARS, environ)
    except Exception:  # noqa: BLE001 - no channel home or an unreadable file is simply no token
        return ""
    return next((found[var].strip() for var in RELAY_TOKEN_VARS if found.get(var)), "")


def import_from_env(environ: Optional[Mapping[str, str]] = None, *,
                    workspace: Optional[Path] = None, vault=None, resolver=None) -> list:
    """Create a row once for REMOTE_TASK_TOKEN and each AG2_REMOTE_TOKEN_<INST>; existing rows stay.

    The REMOTE_TASK_TOKEN row comes from the environment, else from the channel env file the
    connect flow writes (`primary_token_from_channel_file`): a desktop install signs in after
    the engine started, so the token reaches the file but never this process's environment.
    Env rows are unverified: the resolver's server when it knows the host, else the host. Only
    the REMOTE_TASK_TOKEN row (the legacy unsuffixed bridge) can become primary, and only when
    the install has none; a named env token never does, so no secondary ever inherits the
    proactive room. A name whose row was removed is skipped until the variable is unset. The
    secret is copied into the vault under the row's token_ref unless the vault already holds
    one. Returns the rows created.
    """
    environ = os.environ if environ is None else environ
    candidates = []
    primary_token = environ.get(PRIMARY_TOKEN_REF, "").strip() or primary_token_from_channel_file(environ)
    if gateway_url_of(primary_token):
        candidates.append(("", PRIMARY_TOKEN_REF, primary_token))
    for inst, var in env_token_vars(environ):
        if gateway_url_of(environ[var]):
            candidates.append((inst, var, environ[var].strip()))
    if not candidates:
        return []
    vault = _vault(vault)
    created = []
    with _transaction(workspace) as rec:
        rows = rec["rows"]
        for inst, ref, token in candidates:
            if ref in rec[REMOVED_KEY]:
                continue
            host = server_of_token(token)
            resolved = resolve_server(host, resolver)
            seen = {"server": resolved or host, "gateway_host": host}
            if any(r["instance"] == inst or r["token_ref"] == ref for r in rows):
                continue
            same = _same_connection(rows, seen)
            if same is not None:
                if inst and ref not in same.get("launch_keys", []):
                    same["launch_keys"] = sorted(set(same.get("launch_keys", [])) | {ref})
                continue
            if not _vault_get(vault, ref):
                _vault_store(vault, ref, token)
            row = _row(server=seen["server"], instance=inst, gateway_url=gateway_url_of(token), token_ref=ref,
                       primary=(inst == "" and not any(r["primary"] for r in rows)), added_by="env",
                       verified=False, server_source="resolver" if resolved else "gateway")
            rows.append(row)
            created.append(dict(row))
    return created


# ── status ───────────────────────────────────────────────────────────────────

def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _sidecar(instance: str, workspace: Optional[Path]) -> dict:
    from workspace_default import status_read_path
    name = f"gateway-status{'.' + instance if instance else ''}.json"
    return _read_json(status_read_path(name, _workspace(workspace)))


def derive_state(sidecar: Mapping[str, object], launch: Mapping[str, object],
                 disabled: bool = False, parked: bool = False) -> "tuple[str, Optional[str]]":
    """(state, reason) from the bridge's sidecar and the last launch record; a disabled row is
    `parked` with reason `disabled`, a row a primary move parked with reason `parked`, whatever a
    stale sidecar still says."""
    if disabled:
        return "parked", "disabled"
    if parked:
        return "parked", "parked"
    if launch and launch.get("ok") is False:
        return "failed", str(launch.get("reason") or "launch_failed")
    if not sidecar:
        return "pending", None
    if isinstance(sidecar.get("reenroll"), dict) and sidecar["reenroll"].get("pending"):
        return "failed", "auth_rejected"
    if sidecar.get("connected"):
        return "connected", None
    error = str(sidecar.get("error") or "")
    if re.search(r"\b(401|403)\b|auth", error, re.IGNORECASE):
        return "failed", "auth_rejected"
    return "disconnected", None


def _writer_gone(sidecar: Mapping[str, object]) -> bool:
    """Pid liveness of the sidecar's writer: gone when its pid is free or now names a process that
    started at another time. A hung writer is alive; anything unprovable is not gone."""
    pid = sidecar.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    started = sidecar.get("proc_start")
    if not isinstance(started, str) or not started:
        return False
    now = workspace_lock.proc_start(pid)
    return now is not None and now != started


def status_for(row: Mapping[str, object], workspace: Optional[Path] = None) -> dict:
    """The row plus `status` (connected, last_ok_ts from gateway-status), the derived `state`,
    `running` (a bridge proven to hold the lease; None when the lease proves nothing), `legacy` (the
    environment's unsuffixed row, `legacy` on the HTTP path) and every optional field spelled out
    (`disabled` and `parked` false, identity fields None when unknown)."""
    inst = str(row.get("instance") or "")
    sidecar = _sidecar(inst, workspace)
    if sidecar.get("connected") and _writer_gone(sidecar):
        sidecar = {**sidecar, "connected": False}  # a bridge that died says nothing about now
    launch = _read_json(launch_record_path(inst, workspace))
    out = dict(row)
    out["disabled"] = bool(row.get("disabled"))
    out["parked"] = bool(row.get("parked"))
    out["legacy"] = not inst
    for key in IDENTITY_FIELDS:
        out.setdefault(key, None)
    state, reason = derive_state(sidecar, launch, out["disabled"], out["parked"])
    out["status"] = {"connected": bool(sidecar["connected"]) if "connected" in sidecar else None,
                     "last_ok_ts": sidecar.get("last_ok_ts")}
    out["state"] = state
    out["reason"] = reason
    out["launched_at"] = launch.get("ts")
    try:
        out["running"] = any(bridge_pid(name, workspace) for name in [inst] + alias_instances(row))
    except BridgeLeaseIndeterminate:
        out["running"] = None
    return out


# ── launching ────────────────────────────────────────────────────────────────

def alias_instances(row: Mapping[str, object]) -> list:
    """The instances a host's launcher runs this row's `launch_keys` as (each key's own suffix):
    other names one connection's bridge may already hold a lease under."""
    return [str(k)[len(INSTANCE_TOKEN_PREFIX):].lower() for k in row.get("launch_keys") or []]


def _row_named(instance: str, workspace: Optional[Path]) -> Optional[dict]:
    try:
        return next((r for r in _read(record_path(workspace)) if r["instance"] == instance), None)
    except (SchemaError, OSError):
        return None


def _stop_row(row: Mapping[str, object], workspace: Optional[Path], kill) -> int:
    """Stop the row's bridge and any held under one of its alias names; the first pid stopped."""
    stopped = 0
    for inst in [str(row["instance"])] + alias_instances(row):
        pid = stop_bridge(inst, workspace=workspace, kill=kill)
        stopped = stopped or pid
    return stopped


def launch_plan(environ: Optional[Mapping[str, str]] = None, *,
                workspace: Optional[Path] = None) -> list:
    """One entry per bridge the plan launches: every enabled row with an instance (the legacy
    unsuffixed bridge is the primary launch path's; a disabled or parked row stays down), then any
    AG2_REMOTE_TOKEN_<INST> still without a row (an import the vault refused) unless its row
    was removed. An unreadable record raises, so a caller never mistakes it for an empty plan."""
    environ = os.environ if environ is None else environ
    record = _read_record(record_path(workspace))
    rows = record["rows"]
    plan = []
    seen_inst = {r["instance"] for r in rows}
    seen_ref = {r["token_ref"] for r in rows} | set(record[REMOVED_KEY]) | {k for r in rows for k in r.get("launch_keys") or []}
    for r in rows:
        if not r["instance"] or r.get("disabled") or r.get("parked"):
            continue
        plan.append({"instance": r["instance"], "token_ref": r["token_ref"],
                     "channel_dir": channel_dir_for(r["instance"], environ), "primary": r["primary"]})
    for inst, var in env_token_vars(environ):
        if inst in seen_inst or var in seen_ref:
            continue
        plan.append({"instance": inst, "token_ref": var,
                     "channel_dir": channel_dir_for(inst, environ), "primary": False})
    return plan


def plan_entry(instance: str, environ: Optional[Mapping[str, str]] = None, *,
               workspace: Optional[Path] = None, env_only: bool = False) -> dict:
    """The plan entry for `instance`; with `env_only`, built from AG2_REMOTE_TOKEN_<INST> alone
    (the fallback when the record cannot be read), never primary. A readable record still
    refuses a row it marks disabled or parked, in either mode."""
    environ = os.environ if environ is None else environ
    if env_only:
        try:
            rows = _read(record_path(workspace))
        except (SchemaError, OSError):
            rows = []
        var = next((v for inst, v in env_token_vars(environ) if inst == instance), "")
        for r in rows:
            if r["instance"] == instance or (var and r["token_ref"] == var):
                _refuse_down(r)
        if not var:
            raise UnknownConnection(f"no AG2_REMOTE_TOKEN variable names {instance!r}")
        return {"instance": instance, "token_ref": var, "channel_dir": channel_dir_for(instance, environ),
                "primary": False}
    for entry in launch_plan(environ, workspace=workspace):
        if entry["instance"] == instance:
            return entry
    # The record as read, never list_connections: launch calls this under the record lock.
    for r in _read(record_path(workspace)):
        if r["instance"] == instance:
            _refuse_down(r)
    raise UnknownConnection(f"no connection {instance!r} to launch")


def _refuse_down(row: Mapping[str, object]) -> None:
    if row.get("disabled"):
        raise ConnectionsError(f"connection {row['instance']!r} is disconnected; reconnect it to launch")
    if row.get("parked"):
        raise ConnectionsError(f"connection {row['instance']!r} is parked; unpark or promote it to launch")


def _token_file(entry: Mapping[str, str], environ: Mapping[str, str]) -> str:
    override = environ.get("REMOTE_TASK_TOKEN_FILE_" + _inst_var(entry["instance"]), "")
    if override:
        return override if os.path.isfile(override) else ""
    try:
        from util_paths import claude_home_path
        path = claude_home_path("channels", entry["channel_dir"], ".env")
    except Exception:  # noqa: BLE001 - no channel home means no durable token file
        return ""
    return str(path) if path.is_file() else ""


def launch_env(entry: Mapping[str, object], environ: Optional[Mapping[str, str]] = None, *,
               vault=None) -> dict:
    """The bridge's environment for one plan entry: the token from the env var of its token_ref,
    else the vault; no gateway URL beside the token; the proactive room only for the primary."""
    environ = os.environ if environ is None else environ
    ref = str(entry["token_ref"])
    token = (environ.get(ref) or "").strip() or _vault_get(_vault(vault), ref)
    if not token:
        raise NoToken(f"no token under {ref}")
    env = dict(environ)
    env.update({
        "SUTANDO_SUPERVISED": "1",
        "GATEWAY_INSTANCE": str(entry["instance"]),
        "REMOTE_TASK_TOKEN": token,
        "REMOTE_TASK_URL": "",
        "REMOTE_TASK_TOKEN_FILE": _token_file(entry, environ),
        "REMOTE_TASK_CHANNEL_DIR": str(entry["channel_dir"]),
        "REMOTE_PROACTIVE_ROOM": environ.get("REMOTE_PROACTIVE_ROOM", "") if entry.get("primary") else "",
    })
    return env


def _record_launch(instance: str, workspace: Optional[Path], **fields) -> dict:
    record = {"instance": instance, "ts": int(time.time()), **fields}
    _atomic_json(launch_record_path(instance, workspace), record)
    return record


# Children this process spawned, kept so their exit is observed and reaped, never guessed from a pid.
_CHILDREN: dict = {}


def _spawn(argv: list, env: dict, log_path: Path) -> "subprocess.Popen":
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
    return proc


def _exited(pid: int) -> bool:
    """Whether `pid` has ended: our own child by its exit status, any other by the kernel."""
    child = _CHILDREN.get(pid)
    if child is not None:
        if child.poll() is None:
            return False
        _CHILDREN.pop(pid, None)
        return True
    waitid = getattr(os, "waitid", None)
    if waitid is not None:
        try:  # an exited, unreaped child of this process: seen without reaping it
            if waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                return True
        except OSError:
            pass
    elif os.name != "nt" and _zombie(pid):
        return True
    if os.name == "nt":
        from outbox import OwnerState, process_identity
        return process_identity(pid).state == OwnerState.DEAD
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _zombie(pid: int) -> bool:
    """Whether `pid` has exited but is unreaped, where waitid is missing: our child is reaped, any other read from ps."""
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return True
    except ChildProcessError:
        pass
    except OSError:
        return False
    else:
        return False
    ps = "/bin/ps" if os.path.exists("/bin/ps") else "ps"
    try:
        out = subprocess.run([ps, "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return out.strip().startswith("Z")


def _bridge_role(instance: str) -> str:
    """The bridge's own lease role (`_LOCK_ROLE` in remote_gateway_bridge)."""
    return "gateway-bridge" + (f".{instance}" if instance else "")


def _proven_holder(instance: str, workspace: Optional[Path]) -> int:
    """The pid proven, by the same check the stop path uses, to hold a live lease bound to
    `instance`'s role and this workspace; 0 when no such holder is proven."""
    try:
        holder = workspace_lock.read_lease(_bridge_role(instance), _workspace(workspace))
        pid = workspace_lock.lease_process(holder) if holder else 0
    except workspace_lock.LeaseIndeterminate:
        return 0
    return pid if pid and workspace_lock.lease_is_live(holder) else 0


def _retained_wait(instance: str, workspace: Optional[Path]) -> Optional[float]:
    """Seconds until a retained lease whose writer has exited goes stale; None for any other lease."""
    try:
        holder = workspace_lock.read_lease(_bridge_role(instance), _workspace(workspace))
        if not holder or not holder.get("retained") or workspace_lock.lease_process(holder):
            return None
    except workspace_lock.LeaseIndeterminate:
        return None
    return workspace_lock.lease_stale_in(holder)


def _await_lease(instance: str, pid: int, workspace: Optional[Path], timeout_s: float) -> "tuple[str, int]":
    """('held', pid) once `pid` is proven to hold a live lease for `instance`; ('exited', holder)
    when it ended first, holder the other process proven to hold it or 0; ('timeout', 0)."""
    deadline = time.monotonic() + timeout_s
    while True:
        if _exited(pid):
            holder = _proven_holder(instance, workspace)
            return "exited", holder if holder != pid else 0
        # An exited, unreaped child still proves its lease: held only if it is still running after.
        if _proven_holder(instance, workspace) == pid and not _exited(pid):
            return "held", pid
        if time.monotonic() >= deadline:
            return "timeout", 0
        time.sleep(0.05)


def _exit_code(child) -> Optional[int]:
    poll = getattr(child, "poll", None)
    return poll() if poll is not None else None


def launch(instance: str, *, repo: Path, logs_dir: Path, environ: Optional[Mapping[str, str]] = None,
           workspace: Optional[Path] = None, vault=None, spawn=None, python: Optional[str] = None,
           env_only: bool = False) -> dict:
    """Start the bridge for one plan entry and leave a launch record; the bridge's own
    singleton lock turns a duplicate into a clean stand-down. The record lock is held from the
    plan read until the child is proven to hold its lease or has exited (`lease` in the record),
    so a `disconnect` or `remove` always finds the bridge it must stop. A child that exits is a
    success only when another process is proven to hold the lease (`holder_pid`: it stood down to
    a live duplicate); one that stood down to a retained lease whose writer is gone is spawned
    again once that lease goes stale, if that fits in `LEASE_TIMEOUT_S` (else `retained_lease`);
    any other exit fails (`exited`, with the child's rc). A child still without a lease by the
    deadline is killed when it is ours and the launch fails (`lease_timeout`)."""
    environ = os.environ if environ is None else environ
    # Holding the lock to the lease is safe: the bridge's startup never reads this record.
    with locked_file(_lock_path(workspace), create_mode=0o600):
        entry = plan_entry(instance, environ, workspace=workspace, env_only=env_only)
        for alias in alias_instances(_row_named(instance, workspace) or {}):
            holder = _proven_holder(alias, workspace)
            if holder:  # the same connection, already up under the name its host launched it as
                return _record_launch(instance, workspace, ok=True, pid=holder, lease="alias_running", alias=alias)
        try:
            env = launch_env(entry, environ, vault=vault)
        except NoToken as e:
            _record_launch(instance, workspace, ok=False, reason="no_token")
            raise LaunchFailed(str(e)) from None
        argv = [python or sys.executable, str(Path(repo) / "src" / "remote-gateway-bridge.py")]
        log_path = Path(logs_dir) / f"remote-gateway-bridge.{instance}.log"
        deadline = time.monotonic() + LEASE_TIMEOUT_S
        for attempt in (1, 2):
            try:
                child = (spawn or _spawn)(argv, env, log_path)
            except Exception as e:  # noqa: BLE001 - the reason is recorded for status_for, then re-raised by name
                _record_launch(instance, workspace, ok=False, reason="launch_failed")
                raise LaunchFailed(f"could not start the bridge for {instance} ({type(e).__name__})") from None
            pid = int(getattr(child, "pid", child))
            if hasattr(child, "poll"):
                _CHILDREN[pid] = child
            if env.get("SUTANDO_BRIDGE_LOCK", "1") == "0":
                return _record_launch(instance, workspace, ok=True, pid=pid, lease="disabled")
            lease, holder = _await_lease(instance, pid, workspace, max(0.0, deadline - time.monotonic()))
            if lease == "held":
                return _record_launch(instance, workspace, ok=True, pid=pid, lease=lease)
            if lease == "timeout":
                mine = _CHILDREN.pop(pid, None)
                if mine is not None:
                    mine.kill()
                    mine.wait()
                _record_launch(instance, workspace, ok=False, reason="lease_timeout", pid=pid)
                raise LaunchFailed(f"the bridge for {instance} (pid {pid}) took no lease within {LEASE_TIMEOUT_S}s"
                                   + ("; it was stopped" if mine is not None else ""))
            if holder:
                return _record_launch(instance, workspace, ok=True, pid=pid, lease=lease, holder_pid=holder)
            rc = _exit_code(child)
            wait = _retained_wait(instance, workspace) if rc in (None, 75) else None
            if wait is not None and attempt == 1 and time.monotonic() + wait + 0.1 < deadline:
                time.sleep(wait + 0.1)
                continue
            break
        reason = "retained_lease" if wait is not None else "exited"
        _record_launch(instance, workspace, ok=False, reason=reason, pid=pid, rc=rc)
        detail = (f"it stood down to a retained lease that goes stale in {wait:.0f}s; retry then" if wait is not None
                  else "no other process is proven to hold its lease")
        raise LaunchFailed(f"the bridge for {instance} (pid {pid}) exited (rc {rc}): {detail}")


def start(instance: str, *, repo: Path, logs_dir: Path, environ: Optional[Mapping[str, str]] = None,
          workspace: Optional[Path] = None, vault=None, spawn=None, python: Optional[str] = None) -> dict:
    """`launch`, unless a process is already proven to hold the lease (`lease: already_running`,
    nothing spawned). A start that fails returns its record (`ok` false, `reason`; any other
    refusal is recorded under its class, e.g. `vault_unavailable`), never raises."""
    try:
        # Advisory, outside the record lock: it only saves a spawn. The bridge's lease is what keeps
        # one holder; a racing start's child stands down to it and `launch` records that.
        row = _row_named(instance, workspace) or {}
        for name in [instance] + alias_instances(row):
            holder = _proven_holder(name, workspace)
            if holder:
                return {"instance": instance, "ok": True, "pid": holder, "lease": "already_running", "as": name}
        return launch(instance, repo=repo, logs_dir=logs_dir, environ=environ, workspace=workspace,
                      vault=vault, spawn=spawn, python=python)
    except LaunchFailed:
        return _read_json(launch_record_path(instance, workspace))
    except ConnectionsError as e:
        return _record_launch(instance, workspace, ok=False, reason=_error_reason(e))


def _error_reason(e: Exception) -> str:
    """`VaultUnavailable` → `vault_unavailable`."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", type(e).__name__).lower()


def start_added(row: Mapping[str, object], **launch_kw) -> Optional[dict]:
    """After an add: `start` the row's bridge, so a saved connection is never left waiting for the
    next restart. None for the legacy row (its launcher owns it), a disabled one (reconnect) and a
    parked one (unpark)."""
    if not row.get("instance") or row.get("disabled") or row.get("parked"):
        return None
    return start(str(row["instance"]), **launch_kw)


def bridge_pid(instance: str, workspace: Optional[Path] = None) -> int:
    """The pid of the process proven to hold `instance`'s bridge lease (workspace_lock's own record
    and identity check), 0 when none runs; `BridgeLeaseIndeterminate` when the lease proves neither."""
    try:
        holder = workspace_lock.read_lease(_bridge_role(instance), _workspace(workspace))
        return workspace_lock.lease_process(holder) if holder else 0
    except workspace_lock.LeaseIndeterminate as e:
        raise BridgeLeaseIndeterminate(instance, str(e)) from None


def stop_bridge(instance: str, *, workspace: Optional[Path] = None, kill=None,
                timeout_s: Optional[float] = None) -> int:
    """SIGTERM the process proven to hold `instance`'s lease and wait until no process holds it and
    that process has exited; the pid, or 0 when none held it. A holder still running at the deadline, or a signal the process
    refuses, is `BridgeStillRunning`; a lease that proves no holder is `BridgeLeaseIndeterminate`
    and nothing is signalled. Either way the caller writes nothing over a bridge that may be up."""
    timeout_s = STOP_TIMEOUT_S if timeout_s is None else timeout_s
    pid = bridge_pid(instance, workspace)
    if not pid:
        return 0
    try:
        (kill or os.kill)(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # gone between the check and the signal
    except OSError as e:
        raise BridgeStillRunning(instance, pid, f"signal refused: {type(e).__name__}") from None
    deadline = time.monotonic() + timeout_s
    while True:
        _exited(pid)  # reaps our own child, so it stops reading as alive
        holder = bridge_pid(instance, workspace)
        if not holder and _exited(pid):
            return pid
        if not holder and time.monotonic() >= deadline:
            raise BridgeStillRunning(instance, pid, "process still running after it released its lease")
        if time.monotonic() >= deadline:
            raise BridgeStillRunning(instance, holder, "lease still held after the stop deadline")
        time.sleep(0.1)


def reconnect(instance: str, *, repo: Path, logs_dir: Path, environ: Optional[Mapping[str, str]] = None,
              workspace: Optional[Path] = None, vault=None, spawn=None, kill=None,
              python: Optional[str] = None, fetch=None, getaddrinfo=None) -> dict:
    """Relaunch exactly this one bridge: stop the running one, learn the identity its token
    answers to now (a provisional row is re-keyed), then `launch`."""
    known = _row_named(instance, workspace)
    stopped = _stop_row(known, workspace, kill) if known else stop_bridge(instance, workspace=workspace, kill=kill)
    environ = os.environ if environ is None else environ
    rows = {r["instance"]: r for r in list_connections(workspace, vault=vault)}
    if instance in rows:
        if rows[instance].get("disabled"):
            _enable(instance, workspace)
        ref = rows[instance]["token_ref"]
        token = (environ.get(ref) or "").strip() or _vault_get(_vault(vault), ref)
        if token:
            try:
                learn_identity(instance, preview_connection(token, fetch=fetch, getaddrinfo=getaddrinfo),
                               workspace=workspace)
            except ConnectionsError:
                pass
    record = launch(instance, repo=repo, logs_dir=logs_dir, environ=environ, workspace=workspace,
                    vault=vault, spawn=spawn, python=python)
    record["stopped_pid"] = stopped
    return record


def update_token(instance: str, token: str, *, repo: Path, logs_dir: Path,
                 environ: Optional[Mapping[str, str]] = None, workspace: Optional[Path] = None,
                 vault=None, spawn=None, kill=None, python: Optional[str] = None, fetch=None,
                 resolver=None, getaddrinfo=None) -> dict:
    """Replace the secret under `instance`'s token_ref with a new code for the same connection
    (the homeserver's Get new token or Revoke, an agent moved away and back), then relaunch the
    bridge the way `reconnect` does. The row keeps its instance, primary flag and added_at;
    `gateway_url` and the identity fields follow the preview (a row without an agent identity
    learns one, see `_rekey`) and `disabled` is cleared.

    Refused with nothing touched, in this order: the legacy unsuffixed row (`LegacyRowUnmanaged`:
    its token is the environment's); a row whose token_ref is set in `environ`
    (`TokenSourceShadowed`: the bridge reads that variable before the vault, so the new secret
    would never be used); a code the gateway rejected (`TokenRejected`); a code whose preview is
    not this connection's, or whose verified server is already another row's
    (`TokenServerMismatch`); a row naming an agent whose preview did not confirm that same agent:
    unconfirmed (`TokenUnverified`) or another agent (`TokenAgentMismatch`); a bridge that will
    not stop (`BridgeStillRunning`); a vault that cannot say whether it holds the current secret
    (`VaultUnreadable`). The row, its token_ref and the previous secret are read inside the record
    transaction, right before the stop and the vault write, so a concurrent update or a remove and
    re-add is never undone by a stale snapshot. Once the vault write has begun, a failure is
    reconciled under that same hold of the record lock by reading the key back (`_reconcile_vault`): the previous
    secret is put back only while the key holds exactly the new code, all inside the vault's own writer
    transaction (`_vault_writer`), so no other vault writer lands between the compare and the restore; read back as the previous secret, the stopped bridge is relaunched on it and the
    error says whether that worked; read back as the new code, `TokenRestoreFailed`; anything else,
    or a read that fails, `TokenStateUnknown`. Neither of those relaunches.
    Returns the row with `stopped_pid` and the launch record under `launch`; a launch that fails
    is that record (and the row's state), not an exception."""
    environ = os.environ if environ is None else environ
    current = _find(list_connections(workspace, vault=vault), instance)  # an unknown instance probes nothing
    if not instance:
        raise LegacyRowUnmanaged("update_token")
    if (environ.get(current["token_ref"]) or "").strip():
        raise TokenSourceShadowed(instance, current["token_ref"])
    seen = preview_connection(token, fetch=fetch, resolver=resolver, getaddrinfo=getaddrinfo)
    if seen.get("authenticated") is False:
        raise TokenRejected()
    vault = _vault(vault)
    new = token.strip()
    ref, previous, mutating, stopped, failure, found, restore = "", None, False, 0, None, "", ""
    path = record_path(workspace)
    # Snapshot, effects, publication and any reconcile share one hold of the record lock.
    with locked_file(_lock_path(workspace), create_mode=0o600):
        rec = _read_record(path)
        try:
            row = _find(rec["rows"], instance)
            ref = row["token_ref"]
            if (environ.get(ref) or "").strip():
                raise TokenSourceShadowed(instance, ref)
            candidate = _updated_row(rec, row, seen, instance)
            _vault_snapshot(vault, ref)  # an unreadable vault refuses before the stop
            with _vault_writer(vault, wait_s=VAULT_PROBE_WAIT_S):  # a busy one too; not held across the stop,
                pass                               # which may wait on a bridge's own vault write
            stopped = _stop_row(row, workspace, kill)
            # No other vault writer interleaves from the snapshot through the restore.
            with _vault_writer(vault):
                try:
                    previous = _vault_snapshot(vault, ref)
                    mutating = True
                    _vault_store(vault, ref, new)
                    row.clear()
                    row.update(candidate)
                    _write(path, rec)
                except Exception:
                    if mutating:
                        found, restore = _reconcile_vault(vault, ref, previous, new)
                    raise
        except Exception as e:  # noqa: BLE001 - settled below, after the locks are released
            failure = e
    if failure is not None:
        e = failure
        if mutating:
            cause = f"{type(e).__name__}: {e}"
            if found == "new":
                raise TokenRestoreFailed(
                    f"{TokenRestoreFailed.CODE}: the update of {instance} failed after the vault write began "
                    f"({cause}) and restoring the previous secret failed ({restore}); read back, {ref} holds the NEW code "
                    "while the row is unchanged" + ("; the bridge was left stopped" if stopped else "")) from None
            if found != "previous":
                raise TokenStateUnknown(
                    f"{TokenStateUnknown.CODE}: the update of {instance} failed after the vault write began ({cause}) "
                    f"and reading {ref} back {found}, so it is not known which secret it holds; the row is unchanged "
                    "and the bridge was not relaunched; run update-token again with the code you want") from None
        if isinstance(e, BridgeStillRunning):
            raise _with_note(e, f"the bridge was not confirmed stopped and may still be running on the old code; {ref} was not changed") from None
        if not stopped:
            raise e
        try:
            relaunched = launch(instance, repo=repo, logs_dir=logs_dir, environ=environ, workspace=workspace,
                                vault=vault, spawn=spawn, python=python)
            note = f"the bridge was relaunched on the unchanged code (pid {relaunched['pid']})"
        except Exception as again:  # noqa: BLE001 - reported in the error below
            note = f"the bridge stays stopped: relaunching it failed ({type(again).__name__})"
        if isinstance(e, ConnectionsError):
            raise _with_note(e, note) from None
        raise ConnectionsError(f"could not update the token of {instance} ({type(e).__name__}); {note}") from e
    out = dict(row)
    out["stopped_pid"] = stopped
    try:
        out["launch"] = launch(instance, repo=repo, logs_dir=logs_dir, environ=environ, workspace=workspace,
                               vault=vault, spawn=spawn, python=python)
    except LaunchFailed:
        out["launch"] = _read_json(launch_record_path(instance, workspace))
    return out


def _digest(value: Optional[str]) -> Optional[str]:
    return None if value is None else hashlib.sha256(value.encode()).hexdigest()


def _reconcile_vault(vault, ref: str, previous: Optional[str], new: str) -> "tuple[str, str]":
    """After a failed write under `ref`, a compare-and-swap on the stored value: the previous secret
    goes back only while a read shows exactly the new code this update wrote. ('previous', ...) once a
    read shows the previous secret, ('new', ...) when the restore failed, else why it could not tell
    (a read that failed, or another writer's value, which is left as it is); the second item names the
    restore's own error. Values are compared by digest and never logged."""
    def read_back() -> "tuple[bool, Optional[str]]":
        try:
            return True, _digest(_vault_snapshot(vault, ref))
        except VaultUnreadable:
            return False, None

    want, fresh = _digest(previous), _digest(new)
    ok, got = read_back()
    if not ok:
        return "failed", "none"
    if got == want:
        return "previous", "none"
    if got != fresh:
        return "showed another writer's value, left in place", "none"
    restore = "none"
    try:
        vault.set(ref, previous) if previous is not None else vault.delete(ref)
    except Exception as e:  # noqa: BLE001 - the read-back below decides what the vault holds
        restore = type(e).__name__
    ok, got = read_back()
    if not ok:
        return "failed after the restore", restore
    if got == want:
        return "previous", restore
    return ("new" if got == fresh else "showed another writer's value, left in place"), restore


def _with_note(error: ConnectionsError, note: str) -> ConnectionsError:
    """`error` with `note` appended to its message, same class and attributes."""
    error.args = (f"{error.args[0] if error.args else error}; {note}",) + tuple(error.args[1:])
    return error


def _updated_row(rec: dict, row: dict, seen: Mapping[str, object], instance: str) -> dict:
    """The row `update_token` would write for `seen`, checked as `add_connection` checks a new row
    (same connection, same agent, unique server) and against the record schema; a refusal here
    precedes every external effect."""
    if _same_connection([row], seen) is None:
        raise TokenServerMismatch(instance, f"the code is for server {seen['server']!r}, not {row['server']!r}")
    mine, theirs = row.get("agent_mxid"), seen.get("agent_mxid")
    if mine and not theirs:
        raise TokenUnverified(instance, f"the gateway did not confirm agent {mine} for this code"
                              + (f" ({seen['error']})" if seen.get("error") else ""))
    if mine and theirs != mine:
        raise TokenAgentMismatch(instance, f"the code is for agent {theirs}, not {mine}")
    candidate = dict(row)
    candidate["gateway_url"] = str(seen["gateway_url"])
    _rekey(candidate, seen)
    candidate["disabled"] = False
    taken = [r["instance"] for r in rec["rows"] if r is not row and r["server"] == candidate["server"]]
    if taken:
        raise TokenServerMismatch(instance, f"the code's server {candidate['server']!r} is connection {taken[0]!r}")
    trial = [candidate if r is row else r for r in rec["rows"]]
    _validate({"schema_version": SCHEMA_VERSION, "connections": trial, REMOVED_KEY: rec[REMOVED_KEY]})
    return candidate
