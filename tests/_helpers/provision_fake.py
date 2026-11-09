"""An in-process stand-in for the hosted backend's agent-provisioning contract.

It answers the same `(method, url, bearer, payload, headers)` call `pool_provision.http_json` makes,
so the client code under test runs unchanged against it:

- the gateway's `GET /v1/agents`, `GET /v1/whoami` and `GET /v1/mcp/discovery`;
- the access-token exchange and one MCP endpoint carrying `room.action.execute`, `room.action.read`
  (a room's messages and members), `operation.inspect` and `approval.inspect`;
- the account Actions `agent.create` and `agent.retire`, gated on an approval (`mode="approval"`),
  failing closed (`mode="none"`) or running under a grant (`mode="grant"`);
- `POST /api/v1/agents/provision-claims/{claim_id}/redeem`: creator-bound, secret-bound, single use,
  a 5-minute lifetime, a 120-second re-collect window and a burn after five wrong secrets.

`bugs` switches one rule off at a time, so a test can show that the check for it fails. `serve`
puts the same object behind a real loopback HTTP server. Time is the fake's own: `sleep` advances it.
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
LABEL_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


class FakeBackend:
    CLAIM_TTL_S = 300
    RECOLLECT_S = 120
    WRONG_SECRET_LIMIT = 5
    OWNER_CAP = 20

    def __init__(self, *, origin="https://chat.alpha-fixture.net", server="alpha-fixture.net",
                 owner="@owner:alpha-fixture.net", mode="approval"):
        self.origin, self.server, self.owner, self.mode = origin, server, owner, mode
        self.now = 1_800_000_000.0
        self.agents, self.claims, self.operations, self.approvals, self.access = {}, {}, {}, {}, {}
        self.rooms, self.members, self.responders, self.bugs = {}, {}, set(), set()
        self.auto_approve = False
        self.paths = []

    # ── the test's own controls ──────────────────────────────────────────────
    def add_agent(self, mxid, bearer, *, owner=None, kind="sutando", created_by=None):
        self.agents[mxid] = {"id": mxid, "bearer": bearer, "owner": owner or self.owner, "kind": kind,
                             "created_by": created_by, "deleted": False}
        return f"{self.origin}|{bearer}"

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
        path = parts.path
        self.paths.append(f"{method} {path}")
        if f"{parts.scheme}://{parts.netloc}" != self.origin:
            return 404, {}, {"code": "NOT_FOUND"}
        if path == "/v1/mcp/discovery" and method == "GET":
            return 200, {}, {"mint_url": self.origin + MINT_PATH, "mcp_url": self.origin + "/mcp"}
        if path == "/mcp" and method == "POST":
            return self._mcp(self.access.get(bearer), payload or {})
        match = REDEEM_RE.fullmatch(path)
        if match and method == "POST":
            return self._redeem(match.group(1), self._by_bearer(bearer), payload or {})
        agent = self._by_bearer(bearer)
        if agent is None:
            return 401, {}, {"code": "AGENT_CREDENTIAL_UNKNOWN"}
        if path == "/v1/whoami" and method == "GET":
            return 200, {}, {"agent_mxid": agent["id"]}
        if path == "/v1/agents" and method == "GET":
            keep = ("id", "owner", "created_by", "created_via", "created_on_behalf_of")
            if "hide_provenance" in self.bugs:
                keep = ("id", "owner")
            listed = [{k: a.get(k) for k in keep if a.get(k) is not None} for a in self.agents.values()
                      if a["owner"] == agent["owner"] and not a["deleted"]
                      and not ("unlisted" in self.bugs and a.get("created_via"))]
            if "wrong_owner" in self.bugs:
                listed = [{**a, "owner": "@mallory:" + self.server} if a.get("created_via") else a for a in listed]
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

    # ── the redeem route ─────────────────────────────────────────────────────
    def _redeem(self, claim_id, caller, payload):
        no_store = {} if "no_store_missing" in self.bugs else {"cache-control": "no-store"}
        claim = self.claims.get(claim_id)
        if caller is None:
            return 401, {}, {"code": "AGENT_CREDENTIAL_UNKNOWN"}
        if claim is None:
            return 404, {}, {"code": "claim_not_found"}
        if caller["id"] != claim["creator"] and "accept_other_caller" not in self.bugs:
            return 403, {}, {"code": "claim_not_yours"}
        if claim["burned"]:
            return 410, {}, {"code": "claim_burned"}
        secret = payload.get("secret") if isinstance(payload.get("secret"), str) else ""
        matches = hmac.compare_digest(hashlib.sha256(secret.encode()).hexdigest(), claim["key_sha256"])
        agent = self.agents[claim["agent_id"]]
        answer = {"relay_url": self.origin, "agent_id": agent["id"]}
        if claim["redeemed_at"] is not None:
            if matches and self.now <= claim["delivered_until"]:
                if "second_bearer" in self.bugs:
                    agent["bearer"] = secrets.token_hex(24)
                return 200, no_store, {**answer, "bearer": agent["bearer"]}
            return 409, {}, {"code": "claim_redeemed"}
        if self.now > claim["expires_at"] and "accept_expired" not in self.bugs:
            return 410, {}, {"code": "claim_expired"}
        if not matches and "accept_wrong_secret" not in self.bugs:
            claim["wrong"] += 1
            claim["burned"] = claim["wrong"] >= self.WRONG_SECRET_LIMIT
            return 403, {}, {"code": "claim_secret_mismatch"}
        agent["bearer"] = secrets.token_hex(24)
        claim.update(redeemed_at=self.now, delivered_until=self.now + self.RECOLLECT_S)
        return 200, no_store, {**answer, "bearer": agent["bearer"]}

    # ── MCP and the Actions ──────────────────────────────────────────────────
    def _mcp(self, actor, rpc):
        if actor is None or self.agents[actor]["deleted"]:
            return 401, {}, {"code": "AUTHENTICATION_FAILED"}
        params = rpc.get("params") or {}
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "operation.inspect":
            env = self._op_view(self.operations.get(args.get("operation_id")))
        elif name == "approval.inspect":
            env = dict(self.approvals.get(args.get("approval_id")) or self._error("NOT_FOUND"))
        elif name == "room.action.execute":
            env = self._execute(actor, args)
        elif name == "room.action.read" and args.get("action") == "room.context.read":
            env = {"messages": list(reversed(self.rooms.get(args.get("room_id"), [])))}
        elif name == "room.action.read" and args.get("action") == "room.members.list" and "no_members_action" not in self.bugs:
            env = {"members": [{"user_id": m} for m in sorted(self.members.get(args.get("room_id"), ()))]}
        else:
            env = self._error("TOOL_NOT_FOUND")
        result = {"content": [{"type": "text", "text": json.dumps(env)}], "isError": bool(env.get("_error"))}
        if "text_only" not in self.bugs:
            result["structuredContent"] = {k: v for k, v in env.items() if k != "_error"}
        return 200, {}, {"jsonrpc": "2.0", "id": rpc.get("id"), "result": result}

    @staticmethod
    def _error(code, **more):
        return {"_error": True, "code": code, **more}

    def _op_view(self, op):
        if op is None:
            return self._error("NOT_FOUND")
        view = {"operation_id": op["operation_id"], "status": op["status"]}
        if op["status"] == "approval_required":
            view["approval_id"] = op["approval_id"]
        if op["status"] == "succeeded":
            view["result"] = op["result"]
        if op["status"] == "failed":
            return self._error(op["code"], operation_id=op["operation_id"])
        return view

    def _execute(self, actor, args):
        action, arguments = args.get("action"), args.get("arguments") or {}
        if action == "room.message.send":
            return self._send(actor, args.get("room_id"), arguments)
        if action not in ("agent.create", "agent.retire") or args.get("target_kind") != "account":
            return self._error("ACTION_NOT_FOUND")
        operation_id = args.get("operation_id")
        if not operation_id:
            return self._error("TOOL_INPUT_INVALID", message="operation_id is required")
        if operation_id in self.operations:
            return self._op_view(self.operations[operation_id])
        caller = self.agents[actor]
        if caller["kind"] != "sutando" or caller["created_by"]:
            return self._error("FORBIDDEN", message="only an owner-created sutando agent may do this")
        if action == "agent.create" and not (LABEL_RE.fullmatch(str(arguments.get("label") or ""))
                                             and re.fullmatch(r"[0-9a-f]{64}", str(arguments.get("claim_key_sha256") or ""))):
            return self._error("TOOL_INPUT_INVALID")
        if action == "agent.retire" and (self.agents.get(arguments.get("agent_id")) or {}).get("created_by") != actor:
            return self._error("FORBIDDEN", message="only the agent that created it may retire it")
        if self.mode == "none":
            return self._error("approval_required", message="Action approval coordination is not available")
        op = {"operation_id": operation_id, "actor": actor, "action": action, "arguments": arguments,
              "status": "approval_required", "approval_id": None, "result": None, "code": None}
        self.operations[operation_id] = op
        if self.mode == "grant":
            self._run(op, "delegation", "dg_fixture")
            if "thin_execute" in self.bugs:
                return {"operation_id": operation_id, "status": op["status"]}
        else:
            op["approval_id"] = "ap_" + secrets.token_hex(6)
            self.approvals[op["approval_id"]] = {"approval_id": op["approval_id"], "status": "pending",
                                                 "operation_id": operation_id}
        return self._op_view(op)

    def _run(self, op, source, under):
        actor, arguments = op["actor"], op["arguments"]
        authority = {"source": "own" if "odd_authority" in self.bugs else source,
                     "approval_id" if source == "approval" else "grant_id": under}
        if op["action"] == "agent.retire":
            self.agents[arguments["agent_id"]]["deleted"] = True
            op.update(status="succeeded", result={"agent_id": arguments["agent_id"], "authority": authority})
            return
        owner = self.agents[actor]["owner"]
        mxid = f"@{owner.lstrip('@').partition(':')[0]}-{arguments['label']}.agent:{self.server}"
        if "flat_name" in self.bugs:
            mxid = f"@{arguments['label']}.agent:{self.server}"
        if mxid in self.agents:
            op.update(status="failed", code="agent_label_taken")
            return
        if sum(1 for a in self.agents.values() if a["owner"] == owner) >= self.OWNER_CAP:
            op.update(status="failed", code="agent_cap_reached")
            return
        recorded = "@someone-else.agent:" + self.server if "wrong_creator" in self.bugs else actor
        self.agents[mxid] = {"id": mxid, "bearer": None, "owner": owner, "kind": "sutando", "created_by": recorded,
                             "created_on_behalf_of": owner, "created_via": "action", "created_under": under,
                             "deleted": False}
        claim_id = "apc_" + secrets.token_hex(13).upper()
        expires = self.now + self.CLAIM_TTL_S
        self.claims[claim_id] = {"agent_id": mxid, "creator": actor, "key_sha256": arguments["claim_key_sha256"],
                                 "expires_at": expires, "redeemed_at": None, "delivered_until": 0.0,
                                 "wrong": 0, "burned": False}
        when = datetime.datetime.fromtimestamp(expires, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        result = {"agent_id": mxid, "claim_id": claim_id, "expires_at": when, "authority": authority,
                  "audit": {"id": "aa_" + secrets.token_hex(4)}}
        if "no_expires_at" in self.bugs:
            del result["expires_at"]
        if "no_authority" in self.bugs:
            del result["authority"]
        op.update(status="succeeded", result=result)

    def _send(self, actor, room_id, arguments):
        if room_id not in self.rooms:
            return self._error("ROOM_NOT_FOUND")
        room = self.rooms[room_id]
        event_id = f"$e{len(room)}"
        room.append({"event_id": event_id, "sender": actor, "body": arguments.get("body")})
        for mxid in arguments.get("mentions") or []:
            joined = mxid in self.members.get(room_id, ()) or "no_members_action" in self.bugs
            if mxid in self.responders and joined and mxid in self.agents and self.agents[mxid]["bearer"]:
                room.append({"event_id": f"$e{len(room)}", "sender": mxid, "body": "here", "reply_to": event_id})
        return {"status": "succeeded", "result": {"event_id": event_id}}


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


def created(backend, actor, label, key_sha256):
    """`agent.create` as `actor`, approved: (agent id, claim id)."""
    operation_id = "op-" + secrets.token_hex(4)
    backend._execute(actor, {"action": "agent.create", "target_kind": "account", "operation_id": operation_id,
                             "arguments": {"label": label, "claim_key_sha256": key_sha256}})
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
