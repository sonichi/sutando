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
it), and starts no bridge by itself: `launch` does, one bridge per plan entry.

This list is the install's instance directory. It never widens the Sutando Cloud bearer
allowlist or any other authorization: a row only tells startup which bridge to launch.

Writers go through one lock file beside the record, publish by temp file + `os.replace`,
and refuse a record over `MAX_BYTES` or off-schema on read. `status_for` joins a row
read-only with the bridge's own `state/gateway-status[.<instance>].json` and the launch
record `launch` leaves, into one `state`: pending, connected, disconnected or failed.

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

from file_lock import locked_file, try_lock_fd, unlock_fd  # noqa: E402

STATE_NAME = "connections.json"
LOCK_NAME = "connections.lock"
LAUNCH_DIR = "connections-launch"
SCHEMA_VERSION = 1
MAX_BYTES = 256 * 1024
MAX_ROWS = 64

PRIMARY_TOKEN_REF = "REMOTE_TASK_TOKEN"
INSTANCE_TOKEN_PREFIX = "AG2_REMOTE_TOKEN_"
ADDED_BY = ("login", "owner", "ui", "env")
STATES = ("pending", "connected", "disconnected", "failed")
SERVER_SOURCES = ("identity", "resolver", "gateway")
# The bridge's own GATEWAY_INSTANCE charset; a row that fails it could never launch.
INSTANCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_ENV_VAR_RE = re.compile(r"^AG2_REMOTE_TOKEN_([A-Za-z0-9_]+)$")
_MXID_RE = re.compile(r"^@[^:]+:([A-Za-z0-9.-]+)(?::\d+)?$")
_REQUIRED = ("server", "instance", "gateway_url", "token_ref", "primary", "added_by", "added_at", "verified",
             "server_source")
PROBE_TIMEOUT_S = 15
STOP_TIMEOUT_S = 8
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
    """The secret vault refused or could not be reached; nothing was written."""


class UnknownConnection(ConnectionsError):
    pass


class PrimaryProtected(ConnectionsError):
    pass


class InvalidToken(ConnectionsError):
    pass


class NoToken(ConnectionsError):
    """No secret under the row's token_ref, in the environment or the vault."""


class LaunchFailed(ConnectionsError):
    pass


class VaultKeyInUse(ConnectionsError):
    """The vault already holds a secret under this key and no row owns it."""


class TokenRejected(ConnectionsError):
    """The gateway refused the token on the preview; nothing to store."""


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
           "agent_mxid": None, "verified": False, "authenticated": None, "error": None}
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
    ids = [a.get("id") for a in agents if isinstance(a, dict) and isinstance(a.get("id"), str)] \
        if isinstance(agents, list) else []
    if registered and len(ids) == 1 and server_of_mxid(ids[0]) == registered:
        out.update(agent_mxid=ids[0], server_source="identity", verified=True)
    return out


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

    def set(self, key: str, value: str) -> None:
        self._m.set_vault_key(key, value)

    def delete(self, key: str) -> None:
        self._m.delete_vault_key(key)


def _vault(vault):
    return _VaultIntercept() if vault is None else vault


def _vault_store(vault, key: str, value: str) -> None:
    try:
        vault.set(key, value)
    except Exception as e:  # noqa: BLE001 - the vault's own error types vary; the cause is named
        raise VaultUnavailable(f"could not store {key} in the secret vault ({type(e).__name__})") from None


def _vault_get(vault, key: str) -> str:
    try:
        return str(vault.get(key) or "")
    except Exception:  # noqa: BLE001 - a miss and a failure both read as absent
        return ""


def _vault_delete(vault, key: str) -> None:
    try:
        vault.delete(key)
    except Exception as e:  # noqa: BLE001
        raise VaultUnavailable(f"could not remove {key} from the secret vault ({type(e).__name__})") from None


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
    if row.get("agent_mxid") is not None and not isinstance(row["agent_mxid"], str):
        raise SchemaError(f"connections.json: row {index} agent_mxid is not a string")
    if row["server_source"] not in SERVER_SOURCES:
        raise SchemaError(f"connections.json: row {index} server_source {row['server_source']!r} unknown")
    return row


REMOVED_KEY = "removed_env"


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
    return {"rows": rows, REMOVED_KEY: sorted(set(removed))}


def _read_record(path: Path) -> dict:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return {"rows": [], REMOVED_KEY: []}
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
         agent_mxid: Optional[str] = None) -> dict:
    row = {"server": server, "instance": instance, "gateway_url": gateway_url,
           "token_ref": token_ref, "primary": primary, "added_by": added_by, "added_at": _now(),
           "verified": verified, "server_source": server_source}
    if agent_mxid:
        row["agent_mxid"] = agent_mxid
    return row


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
    fails validation raises as before."""
    path = record_path(workspace)
    rows = _read(path)
    if rows or path.exists():
        return rows
    try:
        if import_from_env(workspace=workspace, vault=vault):
            rows = _read(path)
    except Exception as e:  # noqa: BLE001 - the list answers even when nothing could be imported
        logging.getLogger(__name__).warning("connections: import on an empty record failed (%s)", type(e).__name__)
    return rows


def removed_env_refs(workspace: Optional[Path] = None) -> list:
    """Env token names whose rows were removed; import_from_env leaves them alone."""
    return _read_record(record_path(workspace))[REMOVED_KEY]


def add_connection(token: str, *, added_by: str, expected_server: str, instance: Optional[str] = None,
                   preview: Optional[dict] = None, fetch: Optional[Callable] = None,
                   resolver=None, getaddrinfo=None, workspace: Optional[Path] = None,
                   vault=None) -> "tuple[dict, bool]":
    """Save the token as a row once its gateway still answers to `expected_server`.

    Returns (row, created). The same connection (see `_same_connection`) returns its row,
    re-keyed to the identity's server when the row was provisional and this preview is
    verified, and touches the vault never. The row is primary only when the install has no
    primary yet (the first row ever saved); later rows never are. The secret goes to the vault
    under the row's token_ref before the row exists; a vault key no row owns is never overwritten.
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
        raise TokenRejected("the gateway rejected this token; nothing stored")
    inst = instance if instance is not None else instance_for(server)
    if not INSTANCE_RE.match(inst):
        raise ConnectionsError(f"instance {inst!r} is not a slug of [A-Za-z0-9_-]{{1,32}}")
    ref = token_ref_for(inst)
    vault = _vault(vault)
    stored = False
    try:
        with _transaction(workspace) as rec:
            rows = rec["rows"]
            existing = _same_connection(rows, seen)
            if existing is not None:
                _rekey(existing, seen)
                return dict(existing), False
            if any(r["instance"] == inst or r["token_ref"] == ref for r in rows):
                raise ConnectionsError(f"instance {inst!r} is taken by another server; pass another instance")
            if _vault_get(vault, ref):
                raise VaultKeyInUse(f"vault key {ref} already holds a secret that no connection owns")
            _vault_store(vault, ref, token.strip())
            stored = True
            row = _row(server=server, instance=inst, gateway_url=str(seen["gateway_url"]), token_ref=ref,
                       primary=not any(r["primary"] for r in rows), added_by=added_by,
                       verified=bool(seen.get("verified")),
                       server_source=str(seen.get("server_source") or "gateway"),
                       agent_mxid=seen.get("agent_mxid"))
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
    """A verified identity replaces a provisional server in place; the instance slug stays."""
    if row["server_source"] == "identity" or not seen.get("verified"):
        return False
    row.update(server=str(seen["server"]), server_source="identity", verified=True)
    if seen.get("agent_mxid"):
        row["agent_mxid"] = seen["agent_mxid"]
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


def set_primary(instance: str, *, workspace: Optional[Path] = None) -> dict:
    """Make `instance` the primary; proactive and owner-DM defaults follow it at the next launch."""
    with _transaction(workspace) as rec:
        row = _find(rec["rows"], instance)
        for r in rec["rows"]:
            r["primary"] = r is row
    return dict(row)


def remove_connection(instance: str, *, force: bool = False, workspace: Optional[Path] = None,
                      vault=None, kill=None) -> dict:
    """Stop the bridge, then drop the row, its vault key and its launch record; the primary only
    under `force`. Other rows and their bridges are untouched; no row inherits the primary flag.
    An env-imported row is remembered so import_from_env does not bring it back."""
    vault = _vault(vault)
    with _transaction(workspace) as rec:
        rows = rec["rows"]
        row = _find(rows, instance)
        if row["primary"] and not force:
            raise PrimaryProtected("refusing to remove the primary connection without force")
        stopped = stop_bridge(instance, workspace=workspace, kill=kill) if instance else 0
        _vault_delete(vault, row["token_ref"])
        rows.remove(row)
        if row["added_by"] == "env":
            rec[REMOVED_KEY] = sorted(set(rec[REMOVED_KEY]) | {row["token_ref"]})
    try:
        launch_record_path(instance, workspace).unlink()
    except FileNotFoundError:
        pass
    out = dict(row)
    out["stopped_pid"] = stopped
    return out


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
            if any(r["instance"] == inst or r["token_ref"] == ref for r in rows) \
                    or _same_connection(rows, seen) is not None:
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


def derive_state(sidecar: Mapping[str, object], launch: Mapping[str, object]) -> "tuple[str, Optional[str]]":
    """(state, reason) from the bridge's sidecar and the last launch record."""
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


def status_for(row: Mapping[str, object], workspace: Optional[Path] = None) -> dict:
    """The row plus `status` (connected, last_ok_ts from gateway-status) and the derived `state`."""
    inst = str(row.get("instance") or "")
    sidecar = _sidecar(inst, workspace)
    launch = _read_json(launch_record_path(inst, workspace))
    state, reason = derive_state(sidecar, launch)
    out = dict(row)
    out["status"] = {"connected": bool(sidecar["connected"]) if "connected" in sidecar else None,
                     "last_ok_ts": sidecar.get("last_ok_ts")}
    out["state"] = state
    out["reason"] = reason
    out["launched_at"] = launch.get("ts")
    return out


# ── launching ────────────────────────────────────────────────────────────────

def launch_plan(environ: Optional[Mapping[str, str]] = None, *,
                workspace: Optional[Path] = None) -> list:
    """One entry per bridge the plan launches: every row with an instance (the legacy
    unsuffixed bridge is the primary launch path's), then any AG2_REMOTE_TOKEN_<INST>
    still without a row (an import the vault refused) unless its row was removed.
    An unreadable record raises, so a caller never mistakes it for an empty plan."""
    environ = os.environ if environ is None else environ
    record = _read_record(record_path(workspace))
    rows = record["rows"]
    plan = []
    seen_inst = {r["instance"] for r in rows}
    seen_ref = {r["token_ref"] for r in rows} | set(record[REMOVED_KEY])
    for r in rows:
        if not r["instance"]:
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
    (the fallback when the record cannot be read), never primary."""
    environ = os.environ if environ is None else environ
    if env_only:
        for inst, var in env_token_vars(environ):
            if inst == instance:
                return {"instance": inst, "token_ref": var, "channel_dir": channel_dir_for(inst, environ),
                        "primary": False}
        raise UnknownConnection(f"no AG2_REMOTE_TOKEN variable names {instance!r}")
    for entry in launch_plan(environ, workspace=workspace):
        if entry["instance"] == instance:
            return entry
    raise UnknownConnection(f"no connection {instance!r} to launch")


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


def _spawn(argv: list, env: dict, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
    return proc.pid


def launch(instance: str, *, repo: Path, logs_dir: Path, environ: Optional[Mapping[str, str]] = None,
           workspace: Optional[Path] = None, vault=None, spawn=None, python: Optional[str] = None,
           env_only: bool = False) -> dict:
    """Start the bridge for one plan entry and leave a launch record; the bridge's own
    singleton lock turns a duplicate into a clean stand-down."""
    environ = os.environ if environ is None else environ
    entry = plan_entry(instance, environ, workspace=workspace, env_only=env_only)
    try:
        env = launch_env(entry, environ, vault=vault)
    except NoToken as e:
        _record_launch(instance, workspace, ok=False, reason="no_token")
        raise LaunchFailed(str(e)) from None
    argv = [python or sys.executable, str(Path(repo) / "src" / "remote-gateway-bridge.py")]
    log_path = Path(logs_dir) / f"remote-gateway-bridge.{instance}.log"
    try:
        pid = (spawn or _spawn)(argv, env, log_path)
    except Exception as e:  # noqa: BLE001 - the reason is recorded for status_for, then re-raised by name
        _record_launch(instance, workspace, ok=False, reason="launch_failed")
        raise LaunchFailed(f"could not start the bridge for {instance} ({type(e).__name__})") from None
    return _record_launch(instance, workspace, ok=True, pid=pid)


def bridge_pid(instance: str, workspace: Optional[Path] = None) -> int:
    """The pid in the bridge's held singleton lock, 0 when no process holds it."""
    path = _workspace(workspace) / "state" / "locks" / f"gateway-bridge.{instance}.lock"
    try:
        fd = os.open(str(path), os.O_RDWR)
    except OSError:
        return 0
    try:
        if try_lock_fd(fd):
            unlock_fd(fd)
            return 0
        return int((os.read(fd, 32).decode() or "0").strip() or 0)
    except (OSError, ValueError):
        return 0
    finally:
        os.close(fd)


def stop_bridge(instance: str, *, workspace: Optional[Path] = None, kill=None,
                timeout_s: float = STOP_TIMEOUT_S) -> int:
    """SIGTERM the bridge holding `instance`'s lock and wait for the lock to free; the pid, or 0."""
    pid = bridge_pid(instance, workspace)
    if not pid:
        return 0
    (kill or os.kill)(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout_s
    while bridge_pid(instance, workspace) == pid and time.monotonic() < deadline:
        time.sleep(0.1)
    return pid


def reconnect(instance: str, *, repo: Path, logs_dir: Path, environ: Optional[Mapping[str, str]] = None,
              workspace: Optional[Path] = None, vault=None, spawn=None, kill=None,
              python: Optional[str] = None, fetch=None, getaddrinfo=None) -> dict:
    """Relaunch exactly this one bridge: stop the running one, learn the identity its token
    answers to now (a provisional row is re-keyed), then `launch`."""
    stopped = stop_bridge(instance, workspace=workspace, kill=kill)
    environ = os.environ if environ is None else environ
    rows = {r["instance"]: r for r in list_connections(workspace, vault=vault)}
    if instance in rows:
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
