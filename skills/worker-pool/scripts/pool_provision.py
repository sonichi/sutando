#!/usr/bin/env python3
"""A pool worker's own agent, created by the core instead of pasted by a person: the local half.

The core asks the server to create the worker's agent (the `agent.create` account Action) and is
answered with a claim id, never a token. The three local steps around that call live here:

- `begin` makes a one-time secret, keeps it only in the secret vault and hands back its SHA-256,
  which the Action takes as `claim_key_sha256`.
- `finish` redeems the claim as the core's own agent, with the core's lane credential and that
  secret, and saves the bearer the server answers with as the worker's connection, only when both
  the answer and the gateway name the agent the Action created.
- `retire` drops the worker's connection and its secret once the server has retired the agent.

The secret is 32 random bytes as 64 hex characters; its hash is the SHA-256 of those characters.
Nothing here returns, prints, logs or raises a secret or a token: callers get ids and status.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Tuple

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
sys.path[:0] = [p for p in (str(_HERE), str(_REPO / "src")) if p not in sys.path]

import connections
import gateway_serving

CLAIM_KEY_PREFIX = "AG2_PROVISION_CLAIM_"
REDEEM_PATH = "/api/v1/agents/provision-claims/{claim_id}/redeem"
# A claim id goes into a URL path: only characters that cannot change the route.
CLAIM_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
HTTP_TIMEOUT_S = 15
REPLY_MAX = 64 * 1024
REDEEM_ATTEMPTS = 3
REDACTED = "[redacted]"
_CODE_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_MXID_RE = re.compile(r"@[A-Za-z0-9._=/+-]{1,200}:[A-Za-z0-9.-]{1,200}(:[0-9]{1,5})?")
_LOOPBACK = ("localhost", "127.0.0.1", "::1")

Reply = Tuple[int, dict, dict]  # (status, headers by lowercased name, JSON object body)


class ProvisionError(Exception):
    """A provisioning step that did not complete. Its text never carries a secret."""


class TransportError(ProvisionError):
    """The server gave no HTTP answer."""


class NotRetired(ProvisionError):
    """The server has not been seen to retire the agent, so nothing local was removed."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib would copy Authorization onto the redirect target.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_json(method: str, url: str, *, bearer: str = "", payload: Optional[dict] = None,
              headers: Optional[Mapping[str, str]] = None, timeout: float = HTTP_TIMEOUT_S) -> Reply:
    """One JSON request and its answer, whatever the status. Only https, or plain http to this
    machine; a redirect is never followed. A body that is not a JSON object reads as {}."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if not (parts.scheme == "https" and host) and not (parts.scheme == "http" and host in _LOOPBACK):
        raise ProvisionError("refusing a request that is not https (or http to this machine)")
    send = {"Accept": "application/json", "User-Agent": "sutando-gateway-client/1.0", **(headers or {})}
    if bearer:
        send["Authorization"] = f"Bearer {bearer}"
    data = b"" if method == "POST" else None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        send["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=send)
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as resp:
            status, raw, got = resp.status, resp.read(REPLY_MAX), resp.headers
    except urllib.error.HTTPError as exc:
        status, raw, got = exc.code, exc.read(REPLY_MAX), exc.headers
        exc.close()
    except (urllib.error.URLError, OSError) as exc:
        raise TransportError(type(getattr(exc, "reason", exc)).__name__) from None
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = {}
    return status, {k.lower(): v for k, v in got.items()}, body if isinstance(body, dict) else {}


def scrub(text: object, known: Iterable[str]) -> str:
    """`text` with every value in `known` replaced, so a message built from a server's words or an
    error's cannot carry a secret this process holds."""
    out = str(text)
    for value in known:
        if value:
            out = out.replace(value, REDACTED)
    return out


def error_code(body: Mapping[str, object]) -> str:
    """The short error code an answer carries (`code`, `error`, or `error.code`); '' when it has none
    of that shape. Free text is never taken: it is the server's to word."""
    error = body.get("error")
    for value in (body.get("code"), error, error.get("code") if isinstance(error, dict) else None):
        if isinstance(value, str) and _CODE_RE.fullmatch(value):
            return value
    return ""


def origin_of(url: str) -> str:
    """`scheme://host[:port]` of a URL, lowercased with the default port dropped; '' when it has none."""
    try:
        parts = urllib.parse.urlsplit(url or "")
        port = parts.port
    except ValueError:
        return ""
    scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return ""
    host = f"[{host}]" if ":" in host else host
    return f"{scheme}://{host}" + (f":{port}" if port and port != {"http": 80, "https": 443}[scheme] else "")


def bearer_of(token: str) -> str:
    """The secret a stored `url|secret` token carries, or the token itself when it is bare."""
    return connections.secret_of(token) or (token or "").strip()


def claim_key(worker: str) -> str:
    """The vault key `worker`'s pending claim secret lives under: never a connection's token key."""
    return CLAIM_KEY_PREFIX + worker.upper().replace("-", "_")


def claim_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class _Keychain:
    """The secret vault's programmatic path (`vault_intercept`), bound at first use."""

    def __init__(self):
        import vault_intercept
        self._m = vault_intercept

    def lookup(self, key: str) -> Optional[str]:
        return self._m.lookup_vault_key(key)

    def set(self, key: str, value: str) -> None:
        self._m.set_vault_key(key, value)

    def delete(self, key: str) -> None:
        self._m.delete_vault_key(key)


def _vault(vault):
    """The vault this module reads and writes itself. `connections` is handed the caller's own `vault`
    (None for its default), since it asks more of one than `_Keychain` answers."""
    return _Keychain() if vault is None else vault


def held(vault, key: str) -> Optional[str]:
    """The secret under `key`, None when the vault holds none. A vault that cannot say raises."""
    lookup = getattr(vault, "lookup", None)
    try:
        value = lookup(key) if lookup is not None else vault.get(key)
    except KeyError:
        return None
    return str(value) if value else None


def core_lane(workspace: Optional[Path] = None, lane: Optional[str] = None, origin: str = "") -> dict:
    """The core's own connection a claim is created and redeemed as: the instance `lane` when named,
    else the one whose gateway is at `origin`, else the app's signed-in lane, the primary, or the only
    one. A worker's identity, a disconnected row and a row that names no agent are never it."""
    rows = [r for r in connections.read_rows(workspace)
            if not r.get("worker") and not r.get("disabled") and not r.get("parked")]
    if lane is not None:
        picked = [r for r in rows if r["instance"] == lane]
    elif origin:
        picked = [r for r in rows if origin_of(str(r["gateway_url"])) == origin_of(origin)]
    else:
        active = gateway_serving.active_lane()
        picked = ([r for r in rows if r["instance"] == active] if active else []) \
            or [r for r in rows if r["primary"]] or (rows if len(rows) == 1 else [])
    if len(picked) != 1:
        raise ProvisionError("no single core connection to act as (connected core lanes: "
                             f"{', '.join(sorted(str(r['instance']) or '<legacy>' for r in rows)) or 'none'}); "
                             "name one with --lane")
    row = dict(picked[0])
    if not row.get("agent_mxid") or not row.get("owner_mxid"):
        raise ProvisionError(f"the core connection {row['instance'] or '<legacy>'} has not learned its agent "
                             "and owner yet; nothing was done")
    return row


def _localpart(mxid: object) -> str:
    return str(mxid or "").lstrip("@").partition(":")[0]


def _slug(text: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]", "-", text.lower())).strip("-")


def suggested_label(core_row: Mapping[str, object], role: str) -> str:
    """`<core>-<role>`. The server names an agent `<owner>-<label>.agent`, so this label yields
    `<owner>-<core>-<role>`; the core's own label is its localpart without the owner and `.agent`."""
    owner, core = _localpart(core_row.get("owner_mxid")), _localpart(core_row.get("agent_mxid"))
    core = core[:-len(".agent")] if core.endswith(".agent") else core
    core = core[len(owner) + 1:] if owner and core.startswith(owner + "-") else core
    return _slug(f"{core}-{role}")


def begin(worker: str, *, role: str, workspace: Optional[Path] = None, vault=None,
          lane: Optional[str] = None) -> dict:
    """Start provisioning `worker`'s agent: a fresh one-time secret goes into the vault, replacing any
    earlier pending one, and only its SHA-256 and a suggested label come back."""
    if connections.worker_identity(worker, workspace) is not None:
        raise ProvisionError(f"worker {worker} already has an agent of its own; retire it first")
    row = core_lane(workspace, lane)
    secret = secrets.token_hex(32)
    try:
        _vault(vault).set(claim_key(worker), secret)
    except Exception as e:  # noqa: BLE001 - the vault's error types vary; only the type is named
        raise ProvisionError(f"the claim secret could not be stored in the vault ({type(e).__name__})") from None
    return {"claim_key_sha256": claim_hash(secret), "label": suggested_label(row, role or worker)}


def abort(worker: str, *, vault=None) -> None:
    """Forget `worker`'s pending claim secret; a claim made with it can no longer be redeemed."""
    try:
        _vault(vault).delete(claim_key(worker))
    except Exception as e:  # noqa: BLE001
        raise ProvisionError(f"the claim secret could not be deleted from the vault ({type(e).__name__})") from None


def redeem(gateway_url: str, bearer: str, claim_id: str, secret: str, *,
           request: Optional[Callable[..., Reply]] = None, sleep: Callable[[float], None] = time.sleep) -> dict:
    """POST the claim's redeem route as the bearer's agent and return the 200 body. No answer or a 5xx
    is asked again (the server re-delivers to the same caller and secret); any other status is final."""
    if not CLAIM_ID_RE.fullmatch(claim_id or ""):
        raise ProvisionError("the claim id is not one a server issues (letters, digits, _ and - only)")
    url = origin_of(gateway_url) + REDEEM_PATH.format(claim_id=claim_id)
    why = "no answer"
    for attempt in range(REDEEM_ATTEMPTS):
        if attempt:
            sleep(2 ** attempt)
        try:
            status, _, body = (request or http_json)("POST", url, bearer=bearer, payload={"secret": secret})
        except TransportError as e:
            why = f"no answer ({e})"
            continue
        if status == 200:
            return body
        code = error_code(body)
        why = f"http {status}" + (f" {code}" if code else "")
        if status < 500:
            break
    raise ProvisionError(f"the claim was not redeemed: {why}")


def _said(e: Exception) -> str:
    """An error's text, led by its refusal code when it is one of the connection rules' refusals."""
    return f"refused ({e.CODE}): {e}" if isinstance(e, connections.Refusal) else str(e)


def _redeem_and_save(worker: str, claim_id: str, expected: str, known: list, *, workspace, vault, lane, request,
                     preview, sleep) -> dict:
    secret = held(_vault(vault), claim_key(worker))
    if not secret:
        raise ProvisionError(f"worker {worker} has no pending claim secret: run provision-begin first")
    known.append(secret)
    if not _MXID_RE.fullmatch(expected or ""):
        raise ProvisionError("the agent id must be the one agent.create returned (an @name:server id)")
    if connections.worker_identity(worker, workspace) is not None:
        raise ProvisionError(f"worker {worker} already has an agent of its own; nothing was redeemed")
    row = core_lane(workspace, lane)
    gateway, token = connections.connection_credential(row, vault=vault)
    known.append(bearer_of(token))
    body = redeem(gateway, bearer_of(token), claim_id, secret, request=request, sleep=sleep)
    relay_url, bearer, agent_id = (body.get(k) for k in ("relay_url", "bearer", "agent_id"))
    if not all(isinstance(v, str) and v for v in (relay_url, bearer, agent_id)):
        raise ProvisionError("the redeem answer lacks relay_url, bearer or agent_id; nothing stored")
    known.append(bearer)
    try:
        if agent_id != expected:
            raise ProvisionError(f"the redeem answered for another agent than {expected}; nothing stored")
        return _save(worker, f"{relay_url}|{bearer}", expected, gateway, workspace=workspace, vault=vault,
                     preview=preview)
    except (ProvisionError, connections.ConnectionsError) as e:
        raise ProvisionError(f"{_said(e)}. The claim's agent now has a token nobody holds: retire {expected} "
                             "and provision again") from None


def _save(worker: str, token: str, expected: str, gateway: str, *, workspace, vault, preview) -> dict:
    if origin_of(connections.gateway_url_of(token)) != origin_of(gateway):
        raise ProvisionError("the redeem answer names a gateway that is not the core's; nothing stored")
    seen = (preview or connections.preview_connection)(token)
    mxid = str(seen.get("agent_mxid") or "") if seen.get("verified") else ""
    if mxid != expected:
        raise ProvisionError(f"the gateway does not confirm the redeemed token is agent {expected}'s "
                             f"({seen.get('error') or 'it names ' + (mxid or 'no agent')}); nothing stored")
    saved, created = connections.add_worker_identity(worker, token, expected_server=str(seen.get("server") or ""),
                                                     source="agent_link", preview=seen, workspace=workspace,
                                                     vault=vault)
    if not created:
        raise ProvisionError(f"worker {worker} already had a connection ({saved['instance']}); the redeemed "
                             "token was not stored")
    return {"agent_id": mxid, "status": "ok", "row": saved}


def _forget(worker: str, vault, read: Optional[str]) -> str:
    """Delete `worker`'s pending secret, unless a later `begin` replaced the one this call read.
    '' when done, else what failed; it never raises, so it can run while an interrupt unwinds."""
    try:
        mine = _vault(vault)
        try:
            replaced = read is not None and held(mine, claim_key(worker)) not in (None, read)
        except Exception:  # noqa: BLE001 - a vault that cannot say is asked to delete all the same
            replaced = False
        if not replaced:
            mine.delete(claim_key(worker))
        return ""
    except Exception as e:  # noqa: BLE001
        return f"the claim secret could not be deleted from the vault ({type(e).__name__})"


def finish(worker: str, claim_id: str, agent_id: str, *, workspace: Optional[Path] = None, vault=None,
           lane: Optional[str] = None, request: Optional[Callable[..., Reply]] = None,
           preview: Optional[Callable[[str], dict]] = None,
           sleep: Callable[[float], None] = time.sleep) -> dict:
    """Redeem `claim_id` with `worker`'s pending secret and the core's own lane credential, and save
    the answered bearer as the worker's connection once the answer and the gateway both name
    `agent_id`, the agent `agent.create` returned. The pending secret is deleted however this ends,
    an interrupt included; a failure says what failed and echoes no secret."""
    known: list = []
    out, error, left = None, "", ""
    try:
        try:
            out = _redeem_and_save(worker, claim_id, agent_id, known, workspace=workspace, vault=vault, lane=lane,
                                   request=request, preview=preview, sleep=sleep)
        except (ProvisionError, connections.ConnectionsError) as e:
            error = scrub(_said(e), known)
        except Exception as e:  # noqa: BLE001 - an unexpected error's text is not ours to vouch for
            error = f"unexpected {type(e).__name__}"
    finally:
        left = _forget(worker, vault, known[0] if known else None)
    if left:
        error = f"{error}; {left}" if error else f"the worker's connection was saved, but {left}: run provision-abort"
    if error:
        raise ProvisionError(error)
    return out


def retire(worker: str, *, workspace: Optional[Path] = None, vault=None,
           preview: Optional[Callable[[str], dict]] = None, kill=None, local_anyway: bool = False) -> dict:
    """Drop `worker`'s own agent here once its server has retired it: the gateway must reject the
    stored token. A token still accepted, or a gateway that gives no verdict, is `NotRetired` and
    leaves everything in place, unless `local_anyway` removes the lane, row and secret regardless."""
    row = connections.worker_identity(worker, workspace)
    if row is None:
        return {"agent_id": None, "status": "no_identity"}
    mine = _vault(vault)
    ref, agent = str(row["token_ref"]), row.get("agent_mxid")
    try:
        token = held(mine, ref)
    except Exception as e:  # noqa: BLE001
        raise ProvisionError(f"the vault could not be read ({type(e).__name__}); nothing removed") from None
    seen: dict = {}
    if token:
        try:
            seen = (preview or connections.preview_connection)(token)
        except connections.ConnectionsError as e:
            seen = {"error": type(e).__name__}
    revoked = seen.get("authenticated") is False
    if not revoked and not local_anyway:
        verdict = "still accepts its token" if seen.get("authenticated") else \
            f"gave no verdict ({seen.get('error') or 'no token in the vault'})"
        raise NotRetired(f"the server {verdict} for {agent or 'this agent'}: retire it there first "
                         "(agent.retire); nothing removed")
    connections.retire_worker_identity(worker, workspace=workspace, vault=vault, kill=kill)
    if held(mine, ref):
        raise ProvisionError(f"the vault still holds {ref} after the removal")
    return {"agent_id": agent, "status": "retired" if revoked else "removed_locally"}
