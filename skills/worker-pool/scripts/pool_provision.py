#!/usr/bin/env python3
"""A pool worker's own agent, created by the core instead of pasted by a person: the local half.

The server creates the worker's agent when the core calls the `agent.create` account Action, and
answers with a claim id, never a token. The local steps around that call live here:

- `begin` makes a one-time secret, keeps it only in the secret vault and hands back its SHA-256,
  which the Action takes as `claim_key_sha256`.
- `provision` makes the `agent.create` call as the core's own agent, asks again with the same
  operation id until the owner's approval lets it run, and redeems the claim at once: a claim lives
  five minutes from the moment its agent exists.
- `finish` redeems a claim as the core's own agent, with the core's lane credential and that secret,
  and saves the bearer the server answers with as the worker's connection, only when both the answer
  and the gateway name the agent the Action created.
- `retire` drops the worker's connection and its secret once the server has retired the agent.

The secret is 32 random bytes as 64 hex characters; its hash is the SHA-256 of those characters,
the text the redeem body carries. Nothing here returns, prints, logs or raises a secret or a token:
callers get ids and status.
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
# The server's rule for an agent's name; its id becomes @<owner>-<core>-<name>.agent.
NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?")
DISCOVERY_PATH = "/v1/mcp/discovery"
MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_AUTH_CODE = "AUTHENTICATION_FAILED"
# Each ask is one tool call against the agent's own per-minute budget on the server.
POLL_S = 10
APPROVAL_TIMEOUT_S = 900
DONE = ("succeeded", "success", "completed", "complete", "done", "ok", "executed")
ENDED = ("denied", "rejected", "failed", "expired", "cancelled", "canceled")
# Refusals that mean "ask again with the same operation id", not "no".
RETRY_REASONS = ("mint_failed", "soft_delete_incomplete")
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
    """The short reason an error answer carries: `details.reason` first (at the top or under `error`),
    then `code`, `error` or `error.code`; '' when it has none of that shape. Free text is never
    taken: it is the server's to word."""
    error = body.get("error")
    nested = error if isinstance(error, dict) else {}
    details = body.get("details") if isinstance(body.get("details"), dict) else nested.get("details")
    reason = details.get("reason") if isinstance(details, dict) else None
    for value in (reason, body.get("code"), error, nested.get("code")):
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


def _no_identity_yet(worker: str, core: dict, workspace: Optional[Path], done: str = "retire it first") -> None:
    """Refuse a second agent for `worker` on the server of the core lane it is provisioned through."""
    try:
        connections.require_no_worker_identity(worker, str(core.get("server") or ""), workspace)
    except connections.ConnectionsError as e:
        raise ProvisionError(f"{e}; {done}") from None


def suggested_name(role: str) -> str:
    """A name the server accepts for `role`: lowercase letters, digits and inner dashes, at most 32.
    The server puts the owner and the core's own label in front of it, so it names the role alone."""
    slug = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]", "-", role.lower())).strip("-")
    return slug[:32].strip("-")


def begin(worker: str, *, role: str, workspace: Optional[Path] = None, vault=None,
          lane: Optional[str] = None) -> dict:
    """Start provisioning `worker`'s agent: a fresh one-time secret goes into the vault, replacing any
    earlier pending one, and only its SHA-256 and a suggested name come back."""
    _no_identity_yet(worker, core_lane(workspace, lane), workspace)
    secret = secrets.token_hex(32)
    try:
        _vault(vault).set(claim_key(worker), secret)
    except Exception as e:  # noqa: BLE001 - the vault's error types vary; only the type is named
        raise ProvisionError(f"the claim secret could not be stored in the vault ({type(e).__name__})") from None
    return {"claim_key_sha256": claim_hash(secret), "name": suggested_name(role) or suggested_name(worker)}


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
        if status == 410:
            why += " (the claim is dead: retire the agent and create it again)"
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
    row = core_lane(workspace, lane)
    _no_identity_yet(worker, row, workspace, "nothing was redeemed")
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
    try:
        saved, created = connections.add_worker_identity(worker, token, expected_server=str(seen.get("server") or ""),
                                                         source="agent_link", preview=seen, workspace=workspace,
                                                         vault=vault)
    except connections.WorkerIdentityExists:
        saved, created = connections.worker_identity(worker, workspace, server=str(seen.get("server") or "")) or \
            {"instance": "another agent's, since removed"}, False
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


# ── the Action call, as the core's own agent ─────────────────────────────────

def find(value: object, key: str, depth: int = 4):
    """The first value under `key` in a nested answer, shallowest first; None when absent. An
    Action's result sits under `result`, its authority and audit beside it."""
    level = [value]
    for _ in range(depth):
        nxt = []
        for item in level:
            if isinstance(item, dict):
                if item.get(key) is not None:
                    return item[key]
                nxt += list(item.values())
        level = nxt
    return None


def envelope(result: Mapping[str, object]) -> dict:
    """A tool result's JSON: its structuredContent, else the last content text that is an object."""
    if isinstance(result.get("structuredContent"), dict):
        return dict(result["structuredContent"])
    for item in reversed(result.get("content") or []):
        try:
            value = json.loads(item.get("text") or "")
        except (ValueError, AttributeError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def approval_state(env: Mapping[str, object]) -> str:
    """How an Action call stands: `done`; `held` (an approval is open for it); `fail_closed`
    (`approval_required` and no approval was opened: the server cannot ask the owner yet); `retry`
    (a refusal that says to ask again); `denied`; or `refused` (any other error)."""
    status, reason = str(env.get("status") or "").lower(), error_code(env).lower()
    if env.get("is_error"):
        if "approval" in " ".join((status, reason, str(env.get("message") or "").lower())):
            return "held" if find(env, "approval_id") else "fail_closed"
        return "retry" if reason in RETRY_REASONS else "refused"
    if isinstance(env.get("result"), dict) or status in DONE:
        return "done"
    if status in ENDED:
        return "denied"
    if find(env, "approval_id") or "approval" in status or "pending" in status:
        return "held"
    return "done"


class Actions:
    """Account and room Actions as one agent: a delegation minted from its lane credential, then one
    tools/call per Action on the server's MCP endpoint. The lane credential goes to its own origin only."""

    def __init__(self, gateway_url: str, bearer: str, request: Callable[..., Reply], known: list):
        self.gateway, self.bearer, self.request, self.known = gateway_url, bearer, request, known
        self._mcp_url = self._access = ""

    def _connect(self) -> None:
        status, _, body = self.request("GET", self.gateway.rstrip("/") + DISCOVERY_PATH, bearer=self.bearer)
        mint_url, mcp_url = str(body.get("mint_url") or ""), str(body.get("mcp_url") or "")
        if status != 200 or origin_of(mint_url) != origin_of(self.gateway) or not origin_of(mcp_url):
            raise ProvisionError(f"MCP discovery answered http {status} with no mint endpoint on the gateway's "
                                 "own origin")
        status, _, body = self.request("POST", mint_url, bearer=self.bearer)
        token = body.get("access_token")
        if status != 200 or not isinstance(token, str) or not token:
            raise ProvisionError(f"the access-token exchange answered http {status} {error_code(body)}".rstrip())
        self.known.append(token)
        self._mcp_url, self._access = mcp_url, token

    def tool(self, name: str, arguments: dict) -> dict:
        """One tools/call; the tool's answer, with `is_error` set on a tool error. A rejected
        delegation is minted once more."""
        for attempt in (0, 1):
            if attempt or not self._access:
                self._connect()
            status, _, rpc = self.request(
                "POST", self._mcp_url, bearer=self._access, headers={"MCP-Protocol-Version": MCP_PROTOCOL_VERSION},
                payload={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": name, "arguments": arguments}})
            result = rpc.get("result") if status == 200 else None
            env = envelope(result) if isinstance(result, dict) else {}
            if attempt == 0 and (status == 401 or env.get("code") == MCP_AUTH_CODE):
                continue
            break
        if not isinstance(result, dict):
            raise ProvisionError(f"the MCP endpoint answered http {status} to {name} without a result")
        return {**env, "is_error": True} if result.get("isError") else env

    def execute(self, action: str, arguments: dict, *, operation_id: str = "", room_id: str = "") -> dict:
        payload = {"action": action, "arguments": arguments}
        payload.update({"room_id": room_id} if room_id else {"target_kind": "account"})
        if operation_id:
            payload["operation_id"] = operation_id
        return self.tool("room.action.execute", payload)

    def read(self, action: str, arguments: dict, room_id: str) -> dict:
        return self.tool("room.action.read", {"action": action, "room_id": room_id, "arguments": arguments})


def settle(call: Callable[[], dict], *, timeout: float, sleep, clock, first: Optional[dict] = None,
           waiting: Optional[Callable[[], None]] = None, poll: float = POLL_S) -> Tuple[str, dict]:
    """(state, answer) of an Action call once it stops waiting. Nothing tells an agent that its held
    call was approved, so `call` is made again (the same operation id and arguments return the same
    operation) until it is done or refused, or `timeout` when that does not happen in time."""
    env = call() if first is None else first
    state = approval_state(env)
    deadline = clock() + timeout
    if state in ("held", "retry") and waiting:
        waiting()
    while state in ("held", "retry"):
        if clock() >= deadline:
            return "timeout", env
        sleep(poll)
        env = call()
        state = approval_state(env)
    return state, env


def create_arguments(name: str, key_sha256: str, display_name: str = "", role: str = "") -> dict:
    """`agent.create`'s arguments. The owner and the kind are never arguments: the server takes them
    from the calling core."""
    if not NAME_RE.fullmatch(name or ""):
        raise ProvisionError("the name must be lowercase letters, digits and inner dashes, at most 32 characters")
    out = {"name": name, "claim_key_sha256": key_sha256}
    out.update({k: v for k, v in (("display_name", display_name), ("role", role)) if v})
    return out


def provision(worker: str, operation_id: str, name: str, *, display_name: str = "", role: str = "",
              timeout: float = APPROVAL_TIMEOUT_S, workspace: Optional[Path] = None, vault=None,
              lane: Optional[str] = None, request: Optional[Callable[..., Reply]] = None,
              preview: Optional[Callable[[str], dict]] = None, sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic, say: Optional[Callable[[str], None]] = None) -> dict:
    """Call `agent.create` for `worker` as the core's own agent, wait for the owner's approval by
    asking again with the same `operation_id`, and redeem the claim as soon as the agent exists.
    A wait that times out or is interrupted keeps the pending secret, because the request may still
    be approved; a refusal forgets it, because nothing was created. The core's lane is resolved
    once, before the call, and the redeem uses that same lane however long the wait was."""
    known: list = []
    pinned = lane
    try:
        secret = held(_vault(vault), claim_key(worker))
        if not secret:
            raise ProvisionError(f"worker {worker} has no pending claim secret: run provision-begin first")
        known.append(secret)
        arguments = create_arguments(name, claim_hash(secret), display_name, role)
        row = core_lane(workspace, lane)
        # The lane is chosen once: the claim can only be redeemed as the core that created it.
        pinned = str(row["instance"])
        gateway, token = connections.connection_credential(row, vault=vault)
        known.append(bearer_of(token))
        actions = Actions(gateway, bearer_of(token), request or http_json, known)
        state, env = settle(lambda: actions.execute("agent.create", arguments, operation_id=operation_id),
                            timeout=timeout, sleep=sleep, clock=clock,
                            waiting=lambda: say and say(f"waiting up to {int(timeout)}s for the owner to approve "
                                                        "agent.create"))
    except (ProvisionError, connections.ConnectionsError) as e:
        raise ProvisionError(scrub(_said(e), known)) from None
    if state == "timeout":
        raise ProvisionError(f"the owner has not approved agent.create within {int(timeout)}s. The pending secret "
                             f"is kept: run provision-wait again with the same operation id and arguments and "
                             f"--lane '{pinned}' while the request is open, or provision-abort to give up")
    agent_id, claim_id = find(env, "agent_id"), find(env, "claim_id")
    if state != "done" or not isinstance(agent_id, str) or not isinstance(claim_id, str):
        left = _forget(worker, vault, secret)
        why = {"fail_closed": "needs the owner's approval, which this server cannot ask for yet",
               "denied": "was not approved", "refused": "was refused",
               "done": "answered without an agent and a claim"}[state]
        raise ProvisionError("; ".join(x for x in (
            f"agent.create {why} ({scrub(error_code(env), known) or 'no reason given'}); nothing was created and "
            "the pending secret is forgotten", left) if x))
    return finish(worker, claim_id, agent_id, workspace=workspace, vault=vault, lane=pinned, request=request,
                  preview=preview, sleep=sleep)


def retire(worker: str, *, workspace: Optional[Path] = None, vault=None,
           preview: Optional[Callable[[str], dict]] = None, kill=None, local_anyway: bool = False) -> dict:
    """Drop every agent `worker` holds here (one per server) once each server has retired it: each
    gateway must reject its stored token. One token still accepted, or a gateway that gives no verdict,
    is `NotRetired` and leaves everything in place, unless `local_anyway` removes the lanes, rows and
    secrets regardless. `agent_id` names the agent when there is one; with several, `agent_ids`."""
    rows = connections.worker_identities(worker, workspace)
    if not rows:
        return {"agent_id": None, "status": "no_identity"}
    mine = _vault(vault)
    revoked_all = True
    for row in rows:
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
        revoked_all = revoked_all and revoked
        if not revoked and not local_anyway:
            verdict = "still accepts its token" if seen.get("authenticated") else \
                f"gave no verdict ({seen.get('error') or 'no token in the vault'})"
            raise NotRetired(f"the server {verdict} for {agent or 'this agent'}: retire it there first "
                             "(agent.retire); nothing removed")
    connections.retire_worker_identity(worker, workspace=workspace, vault=vault, kill=kill)
    left = [str(r["token_ref"]) for r in rows if held(mine, str(r["token_ref"]))]
    if left:
        raise ProvisionError(f"the vault still holds {', '.join(left)} after the removal")
    agents = [r.get("agent_mxid") for r in rows]
    out = {"agent_id": agents[0] if len(agents) == 1 else None, "status": "retired" if revoked_all else "removed_locally"}
    return out if len(agents) == 1 else {**out, "agent_ids": agents}
