"""An in-process stand-in for the hosted backend's agent-provisioning contract.

It answers the same `(method, url, bearer, payload, headers)` call `pool_provision.http_json` makes,
so the client code under test runs unchanged against it:

- the gateway's `GET /v1/agents`, `GET /v1/whoami` and `GET /v1/mcp/discovery`, under the relay's
  path prefix or without one;
- the access-token exchange and one MCP endpoint carrying `room.action.execute` and
  `room.action.read` (a room's messages and members);
- the account Actions `agent.create` and `agent.retire`: refused with `approval_required`
  (`mode="none"`, the server before it can ask the owner), held until approved
  (`mode="approval"`), or run at once (`mode="grant"`); idempotent on `operation_id`;
- `POST /api/v1/agents/provision-claims/{claim_id}/redeem` on the gateway's origin: creator-bound,
  secret-bound, single use, a 5-minute lifetime, a 120-second re-collect window and a burn on the
  fifth wrong secret. Refusals carry `details.reason`.

The contract fixes the Actions' arguments, results and refusal reasons and the redeem route. It does
not say what a held call answers; here that is `status: approval_required` with an `approval_id`
(`bugs={"held_as_error"}` answers it as an error instead). `bugs` also switches one rule off at a
time, so a test can show that the check for it fails. `serve` puts the same object behind a real
loopback HTTP server. Time is the fake's own: `sleep` advances it.
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import hmac
import http.server
import json
import re
import secrets
import threading
import urllib.parse

MINT_PATH = "/api/v1/mcp/agent-access-tokens"
REDEEM_RE = re.compile(r"/api/v1/agents/provision-claims/([^/]+)/redeem")
NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?")
SECRET_RE = re.compile(r"[A-Za-z0-9_-]{43,128}")
NO_STORE = {"cache-control": "no-store"}


def _iso(when):
    return datetime.datetime.fromtimestamp(when, datetime.timezone.utc).isoformat()


class FakeBackend:
    CLAIM_TTL_S = 300
    RECOLLECT_S = 120
    WRONG_SECRET_LIMIT = 5
    OWNER_CAP = 20
    CREATES_PER_HOUR = 5

    def __init__(self, *, origin="https://chat.alpha-fixture.net", server="alpha-fixture.net",
                 owner="@owner:alpha-fixture.net", mode="approval"):
        self.origin, self.server, self.owner, self.mode = origin, server, owner, mode
        self.now = 1_800_000_000.0
        self.agents, self.claims, self.operations, self.approvals, self.access = {}, {}, {}, {}, {}
        self.rooms, self.members, self.responders, self.bugs = {}, {}, set(), set()
        self.auto_approve = False
        self.paths, self.creates = [], []

    # ── the test's own controls ──────────────────────────────────────────────
    def add_agent(self, mxid, bearer, *, owner=None, kind="sutando", created_by=None):
        self.agents[mxid] = {"id": mxid, "bearer": bearer, "owner": owner or self.owner, "kind": kind,
                             "created_by": created_by, "deleted": False}
        return f"{self.origin}/relay|{bearer}"

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.auto_approve:
            self.approve()

    def approve(self, decision="approved"):
        for approval in [a for a in self.approvals.values() if a["status"] == "pending"]:
            approval["status"] = decision
            op = self.operations[approval["operation_id"]]
            if decision == "approved":
                self._run(op, "approval", approval["approval_id"])
            else:
                op.update(status="denied")

    def bearer_of(self, mxid):
        return self.agents[mxid]["bearer"]

    # ── transport ────────────────────────────────────────────────────────────
    def request(self, method, url, *, bearer="", payload=None, headers=None, timeout=None):
        parts = urllib.parse.urlsplit(url)
        path = parts.path[len("/relay"):] if parts.path.startswith("/relay/v1/") else parts.path
        self.paths.append(f"{method} {path}")
        if f"{parts.scheme}://{parts.netloc}" != self.origin:
            return 404, {}, {"code": "NOT_FOUND"}
        if path == "/v1/mcp/discovery" and method == "GET":
            return 200, {}, {"mint_url": self.origin + MINT_PATH, "mcp_url": self.origin + "/mcp"}
        if path == "/mcp" and method == "POST":
            return self._mcp(self.access.get(bearer), payload or {})
        match = REDEEM_RE.fullmatch(path)
        if match and method == "POST":
            return self._redeem(match.group(1), self._by_bearer(bearer.rpartition("|")[2]), payload or {})
        agent = self._by_bearer(bearer)
        if agent is None:
            return 401, {}, {"code": "AGENT_CREDENTIAL_UNKNOWN"}
        if path == "/v1/whoami" and method == "GET":
            return 200, {}, {"agent_mxid": agent["id"]}
        if path == "/v1/agents" and method == "GET":
            listed = [{"id": a["id"], "owner": a["owner"]} for a in self.agents.values()
                      if a["owner"] == agent["owner"] and not a["deleted"]
                      and not ("unlisted" in self.bugs and a.get("created_via"))]
            return 200, {}, {"agents": listed}
        if path == MINT_PATH and method == "POST":
            token = "access-" + secrets.token_hex(16)
            self.access[token] = agent["id"]
            return 200, {}, {"access_token": token, "expires_in": 900}
        return 404, {}, {"code": "NOT_FOUND"}

    def _by_bearer(self, bearer):
        for agent in self.agents.values():
            live = not agent["deleted"] or "bearer_survives_retire" in self.bugs
            if bearer and agent["bearer"] and live and hmac.compare_digest(agent["bearer"], bearer):
                return agent
        return None

    @staticmethod
    def _refused(status, reason, **details):
        return status, dict(NO_STORE), {"code": reason.upper(), "message": "refused",
                                        "details": {"reason": reason, **details}}

    # ── the redeem route ─────────────────────────────────────────────────────
    def _redeem(self, claim_id, caller, payload):
        no_store = {} if "no_store_missing" in self.bugs else dict(NO_STORE)
        claim = self.claims.get(claim_id)
        if caller is None:
            return self._refused(401, "invalid_bearer")
        if claim is None or (caller["id"] != claim["creator"] and "accept_other_caller" not in self.bugs):
            return self._refused(404, "claim_not_found")
        secret = payload.get("secret") if isinstance(payload.get("secret"), str) else ""
        if not SECRET_RE.fullmatch(secret):
            return self._refused(400, "invalid_secret")
        if claim["burned"]:
            return self._refused(410, "claim_burned")
        matches = hmac.compare_digest(hashlib.sha256(secret.encode()).hexdigest(), claim["key_sha256"])
        agent = self.agents[claim["agent_id"]]
        answer = {"relay_url": self.origin + "/relay", "agent_id": agent["id"]}
        if claim["redeemed_at"] is not None:
            if matches and self.now <= claim["delivered_until"]:
                if "second_bearer" in self.bugs:
                    agent["bearer"] = secrets.token_hex(24)
                return 200, no_store, {**answer, "bearer": agent["bearer"]}
            return self._refused(409, "claim_redeemed")
        if self.now > claim["expires_at"] and "accept_expired" not in self.bugs:
            return self._refused(410, "claim_expired")
        if not matches and "accept_wrong_secret" not in self.bugs:
            claim["wrong"] += 1
            claim["burned"] = claim["wrong"] >= self.WRONG_SECRET_LIMIT
            return self._refused(403, "wrong_secret", attempts_left=self.WRONG_SECRET_LIMIT - claim["wrong"],
                                 burned=claim["burned"])
        agent["bearer"] = secrets.token_hex(24)
        claim.update(redeemed_at=self.now, delivered_until=self.now + self.RECOLLECT_S)
        return 200, no_store, {**answer, "bearer": agent["bearer"]}

    # ── MCP and the Actions ──────────────────────────────────────────────────
    def _mcp(self, actor, rpc):
        if actor is None or self.agents[actor]["deleted"]:
            return 401, {}, {"code": "AUTHENTICATION_FAILED"}
        params = rpc.get("params") or {}
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "room.action.execute":
            env = self._execute(actor, args)
        elif name == "room.action.read" and args.get("action") == "room.context.read":
            env = {"messages": list(reversed(self.rooms.get(args.get("room_id"), [])))}
        elif name == "room.action.read" and args.get("action") == "room.members.list" \
                and "no_members_action" not in self.bugs:
            env = {"members": [{"user_id": m} for m in sorted(self.members.get(args.get("room_id"), ()))]}
        else:
            env = self._error(404, "TOOL_NOT_FOUND")
        result = {"content": [{"type": "text", "text": json.dumps(env)}], "isError": bool(env.get("_error"))}
        if "text_only" not in self.bugs:
            result["structuredContent"] = {k: v for k, v in env.items() if k != "_error"}
        return 200, {}, {"jsonrpc": "2.0", "id": rpc.get("id"), "result": result}

    @staticmethod
    def _error(status, reason, **details):
        return {"_error": True, "code": {400: "TOOL_INPUT_INVALID", 403: "FORBIDDEN", 409: "CONFLICT",
                                         429: "RATE_LIMITED", 503: "UNAVAILABLE"}.get(status, reason),
                "http_status": status, "message": "refused", "details": {"reason": reason, **details}}

    def _op_view(self, op):
        if op["status"] == "approval_required":
            if "held_as_error" in self.bugs:
                return self._error(403, "approval_required", approval_id=op["approval_id"])
            return {"operation_id": op["operation_id"], "status": "approval_required",
                    "approval_id": op["approval_id"]}
        if op["status"] == "failed":
            return self._error(op["http"], op["reason"], operation_id=op["operation_id"])
        if op["status"] == "denied":
            return {"operation_id": op["operation_id"], "status": "denied"}
        view = {"operation_id": op["operation_id"], "result": op["result"], "authority": op["authority"],
                "audit": {"id": "aa_" + op["operation_id"][-6:]}}
        if "no_authority" in self.bugs:
            del view["authority"]
        return view

    def _execute(self, actor, args):
        action, arguments = args.get("action"), args.get("arguments") or {}
        if action == "room.message.send":
            return self._send(actor, args.get("room_id"), arguments)
        if action not in ("agent.create", "agent.retire") or args.get("target_kind") != "account":
            return self._error(404, "ACTION_NOT_FOUND")
        operation_id = args.get("operation_id")
        if not operation_id:
            return self._error(400, "operation_id_required")
        if operation_id in self.operations:
            op = self.operations[operation_id]
            if (op["action"], op["arguments"]) != (action, arguments):
                return self._error(409, "operation_claimed")
            if op["status"] == "failed" and op["reason"] == "soft_delete_incomplete":
                self._run(op, op["authority"]["source"], op["authority"].get("approval_id"))
            return self._op_view(op)
        caller = self.agents[actor]
        if caller["kind"] != "sutando":
            return self._error(403, "not_a_core")
        if caller["created_by"]:
            return self._error(403, "worker_cannot_create")
        if action == "agent.create":
            if set(arguments) - {"name", "claim_key_sha256", "display_name", "role"}:
                return self._error(400, "invalid_arguments")
            if not NAME_RE.fullmatch(str(arguments.get("name") or "")):
                return self._error(400, "invalid_name")
            if not re.fullmatch(r"[0-9a-f]{64}", str(arguments.get("claim_key_sha256") or "")):
                return self._error(400, "invalid_arguments")
            recent = [t for t in self.creates if t > self.now - 3600]
            if len(recent) >= self.CREATES_PER_HOUR:
                return self._error(429, "rate_limited")
            self.creates = recent + [self.now]
        elif (self.agents.get(arguments.get("agent_id")) or {}).get("created_by") != actor:
            return self._error(403, "not_created_by_actor")
        if self.mode == "none":
            return self._error(403, "approval_required", approval_reason=action.replace(".", "_"))
        op = {"operation_id": operation_id, "actor": actor, "action": action, "arguments": arguments,
              "status": "approval_required", "approval_id": None, "result": None, "authority": None}
        self.operations[operation_id] = op
        if self.mode == "grant":
            self._run(op, "delegation", "dg_fixture")
        else:
            op["approval_id"] = "ap_" + secrets.token_hex(6)
            self.approvals[op["approval_id"]] = {"approval_id": op["approval_id"], "status": "pending",
                                                 "operation_id": operation_id}
        return self._op_view(op)

    def _core_label(self, actor, owner):
        local, who = actor.lstrip("@").partition(":")[0], owner.lstrip("@").partition(":")[0]
        if local.startswith(who + "-") and local.endswith(".agent"):
            return local[len(who) + 1:-len(".agent")]
        return "sutando"

    def _run(self, op, source, under):
        actor, arguments = op["actor"], op["arguments"]
        owner = self.agents[actor]["owner"]
        op["authority"] = {"source": "own" if "odd_authority" in self.bugs else source, "on_behalf_of": owner,
                           "approval_id" if source == "approval" else "grant_id": under}
        if op["action"] == "agent.retire":
            agent = self.agents[arguments["agent_id"]]
            agent["deleted"] = True
            if "soft_delete_incomplete_once" in self.bugs and not agent.get("stalled"):
                agent["stalled"] = True
                op.update(status="failed", http=503, reason="soft_delete_incomplete")
                return
            op.update(status="succeeded", result={
                "agent_id": agent["id"], "state": "soft_deleted", "deleted_at": _iso(self.now),
                "hard_delete_after": _iso(self.now + 180 * 86400), "bearer_revoked": True,
                "released": ["relay bearer: revoked"], "owner_notified": True})
            return
        who, label = owner.lstrip("@").partition(":")[0], self._core_label(actor, owner)
        if sum(1 for a in self.agents.values() if a["owner"] == owner) >= self.OWNER_CAP:
            op.update(status="failed", http=403, reason="agent_cap_reached")
            return
        names = [arguments["name"]] + [f"{arguments['name']}-{n}" for n in range(2, 10)]
        ids = [f"@{name}.agent:{self.server}" if "flat_name" in self.bugs else
               f"@{who}-{label}-{name}.agent:{self.server}" for name in names]
        mxid = next((i for i in ids if i not in self.agents), None)
        if mxid is None:
            op.update(status="failed", http=409, reason="agent_name_taken")
            return
        recorded = "@someone-else.agent:" + self.server if "wrong_creator" in self.bugs else actor
        self.agents[mxid] = {"id": mxid, "bearer": None, "owner": owner, "kind": "sutando", "created_by": actor,
                             "created_on_behalf_of": owner, "created_via": "agent.create", "created_under": under,
                             "deleted": False}
        claim_id = "apc_" + secrets.token_hex(13).upper()
        expires = self.now + self.CLAIM_TTL_S
        self.claims[claim_id] = {"agent_id": mxid, "creator": actor, "key_sha256": arguments["claim_key_sha256"],
                                 "expires_at": expires, "redeemed_at": None, "delivered_until": 0.0,
                                 "wrong": 0, "burned": False}
        result = {"agent_id": mxid, "claim_id": claim_id, "expires_at": _iso(expires),
                  "redeem_path": f"/api/v1/agents/provision-claims/{claim_id}/redeem",
                  "owner": "@mallory:" + self.server if "wrong_owner" in self.bugs else owner,
                  "created_by": recorded, "owner_notified": True}
        for bug, key in (("no_expires_at", "expires_at"), ("hide_provenance", "created_by")):
            if bug in self.bugs:
                del result[key]
        op.update(status="succeeded", result=result)

    def _send(self, actor, room_id, arguments):
        if room_id not in self.rooms:
            return self._error(404, "ROOM_NOT_FOUND")
        room = self.rooms[room_id]
        event_id = f"$e{len(room)}"
        room.append({"event_id": event_id, "sender": actor, "body": arguments.get("body")})
        for mxid in arguments.get("mentions") or []:
            joined = mxid in self.members.get(room_id, ()) or "no_members_action" in self.bugs
            if mxid in self.responders and joined and mxid in self.agents and self.agents[mxid]["bearer"]:
                room.append({"event_id": f"$e{len(room)}", "sender": mxid, "body": "here", "reply_to": event_id})
        return {"result": {"event_id": event_id}}


@contextlib.contextmanager
def serve(backend):
    """`backend` behind a real HTTP server on this machine; yields its `http://127.0.0.1:<port>`.
    `GET /redirect` answers 302 to the whoami route, and `GET /text` a body that is not JSON."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def _answer(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/v1/whoami")
                self.end_headers()
                return
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            bearer = (self.headers.get("Authorization") or "").partition("Bearer ")[2]
            status, headers, body = backend.request(self.command, backend.origin + self.path, bearer=bearer,
                                                    payload=json.loads(raw) if raw else None)
            data = b"not json" if self.path == "/text" else json.dumps(body).encode()
            self.send_response(200 if self.path == "/text" else status)
            for key, value in {"Content-Type": "application/json", "Content-Length": str(len(data)),
                               **headers}.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _answer

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def preview_through(backend, connections):
    """A `preview_connection` whose gateway probes are answered by `backend`."""
    import urllib.error
    real = connections.preview_connection

    def probe(path):
        def fetch(url, token, address=None):
            status, _, body = backend.request("GET", url.rstrip("/") + path, bearer=connections.secret_of(token))
            if status != 200:
                raise urllib.error.HTTPError(url, status, "refused", {}, None)
            return body
        return fetch
    return lambda token: real(token, fetch=probe("/v1/agents"), whoami=probe("/v1/whoami"))


def created(backend, actor, name, key_sha256):
    """`agent.create` as `actor`, approved: (agent id, claim id)."""
    operation_id = "op-" + secrets.token_hex(4)
    backend._execute(actor, {"action": "agent.create", "target_kind": "account", "operation_id": operation_id,
                             "arguments": {"name": name, "claim_key_sha256": key_sha256}})
    backend.approve()
    result = backend.operations[operation_id]["result"]
    return result["agent_id"], result["claim_id"]


def recording(vault_class):
    """`vault_class` that also remembers every value it was ever given, with a `lookup` like the Keychain's."""
    class Recording(vault_class):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.ever = []

        def set(self, key, value):
            super().set(key, value)
            self.ever.append(value)

        def lookup(self, key):
            return self.store.get(key)
    return Recording


class KeychainShaped:
    """All the real vault adapter answers (lookup, set, delete), over another vault's store."""

    def __init__(self, vault):
        self.lookup, self.set, self.delete = vault.lookup, vault.set, vault.delete
