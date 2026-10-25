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

A row's `server_source` says where its server came from: `identity` (the gateway named
one agent), `resolver` (`dotspace.dotspace_for` on the gateway host, when that module is
present) or `gateway` (the host itself, provisional). Two tokens for one homeserver's
endpoints collapse onto one row: adding matches on server or gateway host, and a later
identity re-keys a provisional row in place, instance slug kept.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import hashlib
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

def _fetch_agents(gateway_url: str, token: str) -> dict:
    """The gateway's own `GET /v1/agents` under the token's bearer; the bridge's auth probe."""
    req = urllib.request.Request(gateway_url.rstrip("/") + "/v1/agents", method="GET")
    req.add_header("Authorization", f"Bearer {secret_of(token)}")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "sutando-gateway-client/1.0")
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(req, timeout=PROBE_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """An answer is attributed to the host asked, never to one it redirects to."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


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


def preview_connection(token: str, *, fetch: Optional[Callable[[str, str], dict]] = None,
                       resolver=None) -> dict:
    """What saving this token would record, without saving: the gateway URL as issued, the
    server and where it came from (`server_source`), the agent mxid when the gateway names
    exactly one agent (then `verified`), and whether the token authenticated (`authenticated`
    None when the gateway gave no verdict)."""
    url = gateway_url_of(token)
    if not url:
        raise InvalidToken("token carries no gateway URL (expected 'https://<gateway>|<secret>')")
    host = server_of_token(token)
    out = {"gateway_url": url, "gateway_host": host, "server": host, "server_source": "gateway",
           "agent_mxid": None, "verified": False, "authenticated": None, "error": None}
    fetch = _fetch_agents if fetch is None else fetch
    try:
        answer = fetch(url, token)
    except urllib.error.HTTPError as e:
        out["authenticated"] = False if e.code in (401, 403) else None
        out["error"] = {401: "auth_rejected", 403: "auth_rejected", 404: "not_a_gateway"}.get(
            e.code, f"http_{e.code}")
        answer = None
    except Exception as e:  # noqa: BLE001 - the probe is best-effort; the reason is named, the URL is not
        out["error"] = f"unreachable ({type(e).__name__})"
        answer = None
    if answer is not None:
        out["authenticated"] = True
        agents = answer.get("agents") if isinstance(answer, dict) else None
        ids = [a.get("id") for a in agents if isinstance(a, dict) and isinstance(a.get("id"), str)] \
            if isinstance(agents, list) else []
        if len(ids) == 1 and server_of_mxid(ids[0]):
            out.update(agent_mxid=ids[0], server=server_of_mxid(ids[0]), server_source="identity", verified=True)
            return out
    resolved = resolve_server(host, resolver)
    if resolved:
        out.update(server=resolved, server_source="resolver")
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


def _validate(payload: object) -> list:
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
    return rows


def _read(path: Path) -> list:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return []
    if size > MAX_BYTES:
        raise SchemaError(f"connections.json: {size} bytes exceeds the {MAX_BYTES}-byte bound")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise SchemaError(f"connections.json: not valid JSON ({e})") from None
    return _validate(payload)


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


def _write(path: Path, rows: list) -> None:
    _validate({"schema_version": SCHEMA_VERSION, "connections": rows})
    _atomic_json(path, {"schema_version": SCHEMA_VERSION, "connections": rows})


@contextmanager
def _transaction(workspace: Optional[Path]) -> Iterator[list]:
    """Read-mutate-publish under the record's lock; the yielded list is what gets written."""
    path = record_path(workspace)
    with locked_file(_lock_path(workspace), create_mode=0o600):
        rows = _read(path)
        yield rows
        _write(path, rows)


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

def list_connections(workspace: Optional[Path] = None) -> list:
    """Every row, validated; [] when no record exists."""
    return _read(record_path(workspace))


def add_connection(token: str, *, added_by: str, expected_server: str, instance: Optional[str] = None,
                   preview: Optional[dict] = None, fetch: Optional[Callable[[str, str], dict]] = None,
                   resolver=None, workspace: Optional[Path] = None, vault=None) -> "tuple[dict, bool]":
    """Save the token as a row once its gateway still answers to `expected_server`.

    Returns (row, created). A server or gateway host that already has a row returns that row,
    re-keyed to the identity's server when the row was provisional and this preview is
    verified, and touches the vault never. The row is primary only when the install has no
    primary yet (the first row ever saved); later rows never are. The secret goes to the vault
    under the row's token_ref before the row exists.
    """
    if added_by not in ADDED_BY:
        raise ConnectionsError(f"added_by must be one of {', '.join(ADDED_BY)}")
    seen = preview if preview is not None else preview_connection(token, fetch=fetch, resolver=resolver)
    server = str(seen.get("server") or "")
    if not server:
        raise InvalidToken("token carries no gateway URL (expected 'https://<gateway>|<secret>')")
    if (expected_server or "").strip().lower() != server:
        raise ConnectionMismatch(server, expected_server or "")
    inst = instance if instance is not None else instance_for(server)
    if not INSTANCE_RE.match(inst):
        raise ConnectionsError(f"instance {inst!r} is not a slug of [A-Za-z0-9_-]{{1,32}}")
    ref = token_ref_for(inst)
    vault = _vault(vault)
    stored = False
    try:
        with _transaction(workspace) as rows:
            existing = _same_connection(rows, server, str(seen.get("gateway_host") or ""))
            if existing is not None:
                _rekey(existing, seen)
                return dict(existing), False
            if any(r["instance"] == inst or r["token_ref"] == ref for r in rows):
                raise ConnectionsError(f"instance {inst!r} is taken by another server; pass another instance")
            _vault_store(vault, ref, token.strip())
            stored = True
            row = _row(server=server, instance=inst, gateway_url=str(seen["gateway_url"]), token_ref=ref,
                       primary=not any(r["primary"] for r in rows), added_by=added_by,
                       verified=bool(seen.get("verified")),
                       server_source=str(seen.get("server_source") or "gateway"),
                       agent_mxid=seen.get("agent_mxid"))
            rows.append(row)
    except Exception:
        if stored:
            # A secret without a row would be unreachable; take it back out.
            try:
                vault.delete(ref)
            except Exception:  # noqa: BLE001
                pass
        raise
    return dict(row), True


def _same_connection(rows: list, server: str, gateway_host: str) -> Optional[dict]:
    """The row for this server, else the row whose gateway shares the host: one homeserver's
    endpoints are one connection."""
    for r in rows:
        if r["server"] == server:
            return r
    for r in rows:
        if gateway_host and host_of_url(r["gateway_url"]) == gateway_host:
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
    with _transaction(workspace) as rows:
        row = _find(rows, instance)
        _rekey(row, preview)
    return dict(row)


def set_primary(instance: str, *, workspace: Optional[Path] = None) -> dict:
    """Make `instance` the primary; proactive and owner-DM defaults follow it at the next launch."""
    with _transaction(workspace) as rows:
        row = _find(rows, instance)
        for r in rows:
            r["primary"] = r is row
    return dict(row)


def remove_connection(instance: str, *, force: bool = False, workspace: Optional[Path] = None,
                      vault=None) -> dict:
    """Drop the row, its vault key and its launch record; the primary only under `force`.
    Other rows and their bridges are untouched; no row inherits the primary flag."""
    vault = _vault(vault)
    with _transaction(workspace) as rows:
        row = _find(rows, instance)
        if row["primary"] and not force:
            raise PrimaryProtected("refusing to remove the primary connection without force")
        _vault_delete(vault, row["token_ref"])
        rows.remove(row)
    try:
        launch_record_path(instance, workspace).unlink()
    except FileNotFoundError:
        pass
    return dict(row)


def env_token_vars(environ: Mapping[str, str]) -> list:
    """(instance, var) for every `AG2_REMOTE_TOKEN_<INST>` with a non-empty value, instance lowercased."""
    out = []
    for var in sorted(environ):
        m = _ENV_VAR_RE.match(var)
        if m and environ[var].strip():
            out.append((m.group(1).lower(), var))
    return out


def import_from_env(environ: Optional[Mapping[str, str]] = None, *,
                    workspace: Optional[Path] = None, vault=None, resolver=None) -> list:
    """Create a row once for REMOTE_TASK_TOKEN and each AG2_REMOTE_TOKEN_<INST>; existing rows stay.

    Env rows are unverified: the resolver's server when it knows the host, else the host. Only
    the REMOTE_TASK_TOKEN row (the legacy unsuffixed bridge) can become primary, and only when
    the install has none; a named env token never does, so no secondary ever inherits the
    proactive room. The secret is copied into the vault under the row's token_ref unless the
    vault already holds one. Returns the rows created.
    """
    environ = os.environ if environ is None else environ
    candidates = []
    primary_token = environ.get(PRIMARY_TOKEN_REF, "").strip()
    if gateway_url_of(primary_token):
        candidates.append(("", PRIMARY_TOKEN_REF, primary_token))
    for inst, var in env_token_vars(environ):
        if gateway_url_of(environ[var]):
            candidates.append((inst, var, environ[var].strip()))
    if not candidates:
        return []
    vault = _vault(vault)
    created = []
    with _transaction(workspace) as rows:
        for inst, ref, token in candidates:
            host = server_of_token(token)
            resolved = resolve_server(host, resolver)
            server = resolved or host
            if any(r["instance"] == inst or r["token_ref"] == ref for r in rows) \
                    or _same_connection(rows, server, host) is not None:
                continue
            if not _vault_get(vault, ref):
                _vault_store(vault, ref, token)
            row = _row(server=server, instance=inst, gateway_url=gateway_url_of(token), token_ref=ref,
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
    still without a row (an import the vault refused)."""
    environ = os.environ if environ is None else environ
    rows = list_connections(workspace)
    plan = []
    seen_inst = {r["instance"] for r in rows}
    seen_ref = {r["token_ref"] for r in rows}
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
               workspace: Optional[Path] = None) -> dict:
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
           workspace: Optional[Path] = None, vault=None, spawn=None, python: Optional[str] = None) -> dict:
    """Start the bridge for one plan entry and leave a launch record; the bridge's own
    singleton lock turns a duplicate into a clean stand-down."""
    environ = os.environ if environ is None else environ
    entry = plan_entry(instance, environ, workspace=workspace)
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
              python: Optional[str] = None, fetch=None) -> dict:
    """Relaunch exactly this one bridge: stop the running one, learn the identity its token
    answers to now (a provisional row is re-keyed), then `launch`."""
    stopped = stop_bridge(instance, workspace=workspace, kill=kill)
    environ = os.environ if environ is None else environ
    rows = {r["instance"]: r for r in list_connections(workspace)}
    if instance in rows:
        ref = rows[instance]["token_ref"]
        token = (environ.get(ref) or "").strip() or _vault_get(_vault(vault), ref)
        if token:
            try:
                learn_identity(instance, preview_connection(token, fetch=fetch), workspace=workspace)
            except ConnectionsError:
                pass
    record = launch(instance, repo=repo, logs_dir=logs_dir, environ=environ, workspace=workspace,
                    vault=vault, spawn=spawn, python=python)
    record["stopped_pid"] = stopped
    return record
