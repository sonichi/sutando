#!/usr/bin/env python3
"""Runtime-API request-domain dispatch, separated from socket transport.

`server.py` owns the daemon: Unix-socket ownership and permissions, NDJSON
framing, connection lifecycle and supervision. Everything here is the request
domain — method dispatch, approval/elicitation validation, human-action
mirroring, governed-capability authorization, idempotency, durable state
transitions, crash recovery and the resolver loop.

The split exists so the security policy is directly testable without a socket,
and so a future transport cannot reimplement (or quietly diverge from)
approval and capability behavior.

The policies in here encode prior review findings and are security contracts —
notably: governed actions fail closed without an approved, unconsumed approval;
an approval is bound to an exact action+resource+input fingerprint and
authorizes exactly one execution; approval consumption and capability creation
are one transaction; and recovery never claims a side effect did not run.
Do not simplify or reword them.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Callable, Mapping, Optional

_HERE = Path(__file__).resolve().parent
import sys  # noqa: E402
sys.path.insert(0, str(_HERE))

from protocol import ELICITATION_TYPES, ProtocolError  # noqa: E402
from request_store import RequestStore, TERMINAL  # noqa: E402
from ha_adapter import HumanActionAdapter, ha_action_id  # noqa: E402
from capability_registry import EphemeralCapabilityRegistry  # noqa: E402


def _log(msg: str) -> None:
    print(f"[runtime-api] {msg}", flush=True)


def _exec_message_send(params: dict) -> dict:
    """Governed room send through the gateway. Fails closed: only a response
    carrying the posted message's event_id counts as delivered (the #2324
    client / #207 broker contract) — a swallowed-send 200 is a failure.
    Credentials are daemon-resolved from the environment (REMOTE_TASK_URL /
    REMOTE_TASK_TOKEN, same names the gateway bridge documents); the CLI can
    never supply them."""
    import urllib.request
    url = (os.environ.get("REMOTE_TASK_URL") or "").rstrip("/")
    token = os.environ.get("REMOTE_TASK_TOKEN") or ""
    if not url or not token:
        raise RuntimeError("gateway not configured (REMOTE_TASK_URL/REMOTE_TASK_TOKEN)")
    resource = params.get("resource") or {}
    room = resource.get("roomId")
    body = (params.get("input") or {}).get("body")
    if not room or not body:
        raise RuntimeError("message.send needs resource.roomId and input.body")
    req = urllib.request.Request(
        url + "/v1/room",
        data=json.dumps({"op": "message", "room_id": room,
                         "body": str(body)}).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "Accept": "application/json",
                 "User-Agent": "sutando-gateway-client/1.0"},
        method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        reply = json.loads(resp.read().decode("utf-8") or "{}")
    event_id = reply.get("event_id") or (reply.get("result") or {}).get("event_id")
    if not event_id:
        raise RuntimeError(f"send not confirmed (no event_id in {str(reply)[:120]!r})")
    return {"executed": True, "eventId": event_id, "roomId": room}


EXECUTORS = {
    "message.send": _exec_message_send,
}

# Governed actions REQUIRE an approved, unconsumed approval — the daemon holds
# real credentials, so an ungated socket client must never reach the gateway
# (review P1: omitting approvalRequestId silently skipped the gate).
GOVERNED_ACTIONS = frozenset({"message.send"})


def _fingerprint(params: dict) -> str:
    """Canonical identity of an execution: action + resource + input. An
    idempotency key may only replay a request with the SAME fingerprint —
    a reused key with different content must be rejected, never report a
    different side effect as complete (review P1)."""
    import hashlib
    canon = json.dumps({"action": params.get("action"),
                        "resource": params.get("resource"),
                        "input": params.get("input")},
                       sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


RESOLVER_POLL_S = float(os.environ.get("SUTANDO_RUNTIME_RESOLVE_POLL", "2"))
WAIT_POLL_S = 0.5
DEFAULT_WAIT_TIMEOUT_S = 30.0



class RuntimeDispatcher:
    """Request-domain orchestration. Dependencies are ordinary constructor
    arguments — the server composes the real store/adapter/actor/executors and
    delegates parsed requests here.

    `actor_id` is resolved DAEMON-SIDE by the caller and passed in explicitly;
    a client parameter can never override it."""

    def __init__(self, store: RequestStore, human_actions: HumanActionAdapter,
                 actor_id: str,
                 executors: Mapping[str, Callable[[dict], dict]] = EXECUTORS,
                 agents_view=None, identity_view=None, tasks_view=None,
                 runtime_view=None, schedules_view=None,
                 capability_registry: Optional[EphemeralCapabilityRegistry] = None,
                 granted_methods: frozenset = frozenset()):
        self.store = store
        self.ha = human_actions
        self.actor_id = actor_id
        # Caller grants, resolved DAEMON-SIDE per transport (a paired device's
        # granted_methods). The plain Unix socket grants nothing: default empty.
        self.granted_methods = frozenset(granted_methods)
        self.executors = executors
        # Discovery/identity/task views — injected like executors so tests
        # compose tmp-dir views; None = those methods unavailable.
        self.agents = agents_view
        self.identity = identity_view
        self.tasks = tasks_view
        self.runtime = runtime_view
        self.schedules = schedules_view
        self.capability_registry = (
            capability_registry if capability_registry is not None
            else EphemeralCapabilityRegistry())
        # request_id → ha action_id, rebuilt at boot for crash recovery.
        self._ha_of: dict = {}

    # ── boot ────────────────────────────────────────────────────────────────
    def recover(self) -> None:
        """Post-restart recovery. Approvals/elicitations re-link to their ha
        actions (the card lifecycle continues). A PENDING capability row is
        different: create_consuming() commits it (and spends the approval)
        BEFORE the executor runs, so a crash in that window leaves a row
        nothing will ever execute — permanently pending with a one-time
        approval consumed (review P1). The outcome is genuinely unknown (the
        crash may have been before OR mid-send), so the safe recovery is an
        honest terminal state, never a silent replay that could double-send:
        mark it failed with the ambiguity recorded; retrying requires a fresh
        approval by design."""
        n = relinked = 0
        for rec in self.store.pending():
            if rec["requestType"] in ("approval", "elicitation", "human_action"):
                self._ha_of[rec["requestId"]] = ha_action_id(rec["requestId"])
                relinked += 1
            elif rec["requestType"] == "capability":
                # Deliberately NO `executed` boolean: the crash may have
                # landed after the gateway accepted the send but before the
                # terminal transition, so asserting executed:false would
                # invite a confident duplicate retry (review P1). `outcome:
                # unknown` forces the caller to verify the side effect before
                # spending a fresh approval.
                self.store.transition(
                    rec["requestId"], "failed",
                    result={"outcome": "unknown",
                            "error": "interrupted by daemon restart — the "
                                     "execution may or may not have completed; "
                                     "VERIFY the side effect before retrying. "
                                     "Any consumed approval is spent (a retry "
                                     "needs a new approval)"},
                    resolved_by="daemon-recovery")
                n += 1
        if relinked or n:
            _log(f"recovered {relinked} pending request(s)"
                 + (f", failed {n} interrupted capability execution(s)" if n else ""))

    # ── dispatch ───────────────────────────────────────────────────────────
    async def handle(self, method: str, params: dict) -> dict:
        if method == "approval.request":
            return self._issue("approval", method, params,
                               required=("action",))
        if method == "elicitation.request":
            etype = params.get("type", "single_select")
            if etype not in ELICITATION_TYPES:
                raise ProtocolError(-32602, f"type must be one of {ELICITATION_TYPES}")
            # v0 transport = the human-action card lifecycle, whose answer
            # grammar is numeric-options only. free_text has no answerable
            # shape there (zero options → every answer rejected → the request
            # would strand until expiry — review P1 dead path). Reject loudly
            # instead of stranding; lands with a richer transport.
            if etype == "free_text":
                raise ProtocolError(-32602,
                                    "free_text elicitation is not supported in v0 "
                                    "(card transport is options-based) — use "
                                    "single_select/multi_select/confirmation")
            if etype in ("single_select", "multi_select") and not params.get("options"):
                raise ProtocolError(-32602, f"{etype} requires options")
            return self._issue("elicitation", method, params,
                               required=("question",))
        if method == "human_action.request":
            return self._issue("human_action", method, params,
                               required=("action",))
        if method in ("human_action.complete", "human_action.decline"):
            # Settling a human-only action is the same authority claim as
            # approval.respond: the requester must never settle its own request.
            if method not in self.granted_methods:
                raise ProtocolError(
                    -32601, f"{method} requires an authorized device grant — "
                            "not callable on this transport (the human settles "
                            "the action on its card)")
            return self._human_action_settle(method, params)
        if method == "approval.respond":
            # Fails closed: only a transport whose caller carries this grant
            # may resolve approvals — a requester must never approve itself.
            if method not in self.granted_methods:
                raise ProtocolError(
                    -32601, "approval.respond requires an authorized device "
                            "grant — not callable on this transport")
            return self._approval_respond(params)
        if method == "human_action.status":
            return self._get(params)
        if method == "capability.list":
            return self.capability_registry.list(params)
        if method == "capability.read":
            return await self.capability_registry.read(params)
        if method == "capability.execute":
            return await self._capability(params)
        if method == "request.list":
            # Pending human-collaboration requests, newest first — what a
            # fresh client renders as "waiting on a human" after install.
            out = []
            for rec in self.store.pending():
                p = rec.get("params") or {}
                out.append({"requestId": rec["requestId"],
                            "requestType": rec["requestType"],
                            **({"taskId": rec["taskId"]} if rec.get("taskId") else {}),
                            **({"question": p["question"]} if p.get("question") else {}),
                            **({"action": p["action"]} if p.get("action") else {}),
                            **({"createdAt": rec["createdAt"]}
                               if rec.get("createdAt") else {})})
            return {"requests": out}
        if method == "request.get":
            return self._get(params)
        if method == "request.wait":
            return await self._wait(params)
        if method == "request.cancel":
            return self._cancel(params)
        if method == "agent.list":
            return self._agents().list_agents()
        if method == "agent.status":
            agent_id = params.get("agentId")
            if not agent_id:
                raise ProtocolError(-32602, "missing required param: agentId")
            entry = self._agents().agent_status(agent_id)
            if entry is None:
                raise ProtocolError(-32602, f"unknown agent: {agent_id!r}")
            return entry
        if method.startswith("sutando."):
            if self.identity is None:
                raise ProtocolError(-32601,
                                    "identity surface is not configured on this daemon")
            if method == "sutando.stand":
                return self.identity.stand_card(bool(params.get("details")))
            if method == "sutando.resolve":
                return self.identity.resolve(str(params.get("provider") or ""),
                                             str(params.get("subject") or ""))
            fn = {"sutando.info": self.identity.info,
                  "sutando.channels": self.identity.entrances,
                  "sutando.entrances": self.identity.entrances,
                  "sutando.status": self.identity.status,
                  "sutando.owner": self.identity.owner,
                  "sutando.allowlist": self.identity.allowlist}.get(method)
            if fn is not None:
                return fn()
        if method.startswith("runtime."):
            if self.runtime is None:
                raise ProtocolError(-32601,
                                    "runtime surface is not configured on this daemon")
            fn = {"runtime.health": self.runtime.health,
                  "runtime.details": self.runtime.details}.get(method)
            if fn is not None:
                return fn()
        if method == "schedule.list":
            if self.schedules is None:
                raise ProtocolError(-32601,
                                    "schedule surface is not configured on this daemon")
            return self.schedules.list_schedules()
        if method.startswith("task."):
            return self._task(method, params)
        raise ProtocolError(-32601, f"unknown method {method}")

    def _task(self, method: str, params: dict) -> dict:
        if self.tasks is None:
            raise ProtocolError(-32601,
                                "task pipeline is not configured on this daemon")
        try:
            if method == "task.submit":
                if not params.get("task"):
                    raise ProtocolError(-32602, "missing required param: task")
                return self.tasks.submit(params["task"],
                                         priority=params.get("priority") or "normal")
            if method == "task.list":
                return self.tasks.list_tasks()
            if method == "task.list_results":
                return self.tasks.list_results()
            if method == "task.get_result":
                # taskId is OPTIONAL here — absent means "the newest result".
                tid = params.get("taskId")
                out = self.tasks.get_result(tid)
                if out is None:
                    raise ProtocolError(
                        -32602,
                        f"no result for task: {tid!r}" if tid
                        else "no results found yet")
                return out
            tid = params.get("taskId")
            if not tid:
                raise ProtocolError(-32602, "missing required param: taskId")
            if method == "task.status":
                return self.tasks.status(tid)
            if method == "task.details":
                out = self.tasks.details(tid)
                if out is None:
                    raise ProtocolError(-32602, f"unknown task: {tid!r}")
                return out
            if method == "task.cancel":
                return self.tasks.cancel(tid)
        except ValueError as e:
            raise ProtocolError(-32602, str(e)) from e
        raise ProtocolError(-32601, f"unknown method {method}")

    def _agents(self):
        if self.agents is None:
            raise ProtocolError(-32601,
                                "agent discovery is not configured on this daemon")
        return self.agents

    def _issue(self, rtype: str, method: str, params: dict, required=()) -> dict:
        for k in required:
            if not params.get(k):
                raise ProtocolError(-32602, f"missing required param: {k}")
        rec = self.store.create(rtype, method, self.actor_id, params,
                                task_id=params.get("taskId"),
                                expires_in_s=params.get("expiresInS"))
        opener = {"approval": self.ha.open_approval,
                  "human_action": self.ha.open_human_action}.get(
                      rtype, self.ha.open_elicitation)
        try:
            self._ha_of[rec["requestId"]] = opener(rec)
        except Exception as e:  # noqa: BLE001 — mirror failure = failed request
            # The durable row must never outlive a failed card mirror as
            # "pending" — nothing would ever answer it (review blocker). Mark
            # it failed durably, THEN surface the error to the caller.
            self.store.transition(rec["requestId"], "failed",
                                  result={"error": f"human-action mirror failed: {e}"},
                                  resolved_by="daemon")
            raise ProtocolError(-32603,
                                f"could not open the human-action card: {e}") from e
        return {"requestId": rec["requestId"], "status": "pending"}

    def _human_action_settle(self, method: str, params: dict) -> dict:
        """API-side completion: the human confirmed out-of-band (e.g. via CLI)
        instead of answering the card. Resolves the request AND closes the
        card so it does not dangle for CardPoster."""
        rid = params.get("requestId")
        if not rid:
            raise ProtocolError(-32602, "missing required param: requestId")
        rec = self.store.get(rid)
        if rec is None:
            raise ProtocolError(-32602, f"unknown requestId: {rid!r}")
        if rec["requestType"] != "human_action":
            raise ProtocolError(-32602,
                                f"{rid!r} is a {rec['requestType']} request — "
                                "human_action.complete/decline apply only to "
                                "human_action requests")
        if rec["status"] != "pending":
            return {"requestId": rid, "status": rec["status"],
                    "alreadyTerminal": True}
        status = ("completed" if method == "human_action.complete"
                  else "declined")
        note = params.get("note")
        self.store.transition(rid, status,
                              result={"note": note} if note else None,
                              resolved_by=self.actor_id)
        aid = self._ha_of.get(rid) or ha_action_id(rid)
        self.ha.close(aid, self.actor_id, note=note)
        return {"requestId": rid, "status": status}

    def _approval_respond(self, params: dict) -> dict:
        """Resolve an approval from an authorized client (e.g. a paired wearable
        granted approval.respond). Mirrors the owner's card answer: approve →
        approved, reject → denied. Same CAS terminal-immutability as every other
        resolution path, so a late/duplicate respond — or a concurrent card
        answer — cannot overwrite a resolved request; the mirrored card is closed
        so it does not dangle. Reachable only when the composed granted_methods
        carries approval.respond (see handle()); off by default."""
        rid = params.get("requestId")
        if not rid:
            raise ProtocolError(-32602, "missing required param: requestId")
        decision = params.get("decision")
        if decision not in ("approve", "reject"):
            raise ProtocolError(-32602, "decision must be 'approve' or 'reject'")
        rec = self.store.get(rid)
        if rec is None:
            raise ProtocolError(-32602, f"unknown requestId: {rid!r}")
        if rec["requestType"] != "approval":
            raise ProtocolError(
                -32602, f"{rid!r} is a {rec['requestType']} request — "
                "approval.respond applies only to approval requests")
        if rec["status"] != "pending":
            return {"requestId": rid, "status": rec["status"],
                    "alreadyTerminal": True}
        status = "approved" if decision == "approve" else "denied"
        self.store.transition(rid, status, resolved_by=self.actor_id)
        aid = self._ha_of.get(rid) or ha_action_id(rid)
        self.ha.close(aid, self.actor_id)
        return {"requestId": rid, "status": status}

    async def _capability(self, params: dict) -> dict:
        action = params.get("action")
        if not action:
            raise ProtocolError(-32602, "missing required param: action")
        # Idempotent retry: a key that already created a request returns THAT
        # request's current state — no re-execution, no double approval
        # consumption (review P1: a retry after a lost response duplicated the
        # send, or spuriously failed on the already-consumed approval). The
        # key is persisted BEFORE approval consumption and gateway contact.
        idem_key = params.get("idempotencyKey")
        fp = _fingerprint(params)

        def _replay(existing: dict) -> dict:
            # A key replays ONLY the identical execution — a different
            # fingerprint under the same key is a caller bug and must not
            # report the earlier side effect as this call's result.
            if existing.get("fingerprint") != fp:
                raise ProtocolError(-32602,
                                    f"idempotencyKey {idem_key!r} was used for a "
                                    "different action/resource/input — keys are "
                                    "per-execution, pick a new one")
            return {"requestId": existing["requestId"],
                    "status": existing["status"],
                    **({"result": existing["result"]}
                       if existing.get("result") is not None else {}),
                    "idempotentReplay": True}

        if idem_key:
            existing = self.store.by_idempotency_key(idem_key)
            if existing is not None:
                return _replay(existing)
        # Governed actions REQUIRE approval — validated and consumed atomically
        # BEFORE the executor can touch daemon-held credentials. One approval
        # authorizes exactly one execution (design: "approving 'merge PR X' must not be replayable").
        approval_id = params.get("approvalRequestId")
        if action in GOVERNED_ACTIONS and not approval_id:
            raise ProtocolError(-32602,
                                f"action {action!r} is governed — an approved "
                                "approvalRequestId is required (issue one with "
                                "approval.request and wait for it to resolve)")
        if approval_id:
            appr = self.store.get(approval_id)
            if appr is None or appr["requestType"] != "approval":
                raise ProtocolError(-32602, f"unknown approval: {approval_id!r}")
            if appr["status"] != "approved":
                raise ProtocolError(-32602,
                                    f"approval {approval_id} is {appr['status']}, not approved")
            # BINDING: an approval authorizes exactly the EFFECT the owner saw
            # on the card — action, resource AND governed input (review P1:
            # binding only action+resource let a caller obtain approval for a
            # benign message body and substitute any other body afterwards —
            # the payload IS the effect for message.send). Compared via the
            # same canonical fingerprint idempotency uses, BEFORE consumption,
            # so a mismatch costs nothing.
            ap = appr.get("params") or {}
            if ap.get("action") != action:
                raise ProtocolError(-32602,
                                    f"approval {approval_id} authorizes action "
                                    f"{ap.get('action')!r}, not {action!r}")
            if _fingerprint(ap) != fp:
                raise ProtocolError(-32602,
                                    f"approval {approval_id} is bound to a different "
                                    "resource/input than this execution — the owner "
                                    "approves the exact effect, payload included")
        # Record creation + approval consumption are ONE durable transaction
        # (review P1: consume-then-create left a window where a crash between
        # the steps spent the approval with no replayable record, and a
        # same-key/different-approval race could consume an extra approval
        # before the unique index rejected the insert). create_consuming
        # commits both or neither; a duplicate-key loss rolls back untouched.
        try:
            if approval_id:
                rec = self.store.create_consuming(
                    approval_id, "capability", "capability.execute",
                    self.actor_id, params, task_id=params.get("taskId"),
                    idempotency_key=idem_key, fingerprint=fp)
                if rec is None:
                    raise ProtocolError(-32602,
                                        f"approval {approval_id} already consumed "
                                        "(one-time use)")
            else:
                rec = self.store.create("capability", "capability.execute",
                                        self.actor_id, params,
                                        task_id=params.get("taskId"),
                                        idempotency_key=idem_key,
                                        fingerprint=fp)
        except sqlite3.IntegrityError:
            # Lost a same-key race after the replay check above: the winner's
            # row is durable and OUR approval consume rolled back with the
            # insert — replay the winner instead of double-executing.
            existing = self.store.by_idempotency_key(idem_key) if idem_key else None
            if existing is None:
                raise
            return _replay(existing)
        # Read the INJECTED map, not the module global: a test (or a future
        # caller) passing `executors=` must actually take effect. Reading the
        # global here would silently ignore the constructor argument.
        executor = self.executors.get(action)
        if executor is None:
            result = {"executed": False,
                      "error": f"no executor for action {action!r}"}
            self.store.transition(rec["requestId"], "failed", result=result,
                                  resolved_by="executor")
            return {"requestId": rec["requestId"], "status": "failed",
                    "result": result}
        try:
            # Blocking executors (urlopen) must not stall the event loop — one
            # slow send would freeze every client AND the resolver (review P1,
            # measured: a 1.5s executor delayed asyncio.sleep(0) by 1.5s).
            result = await asyncio.to_thread(executor, params)
            status = "completed"
        except Exception as e:  # noqa: BLE001 — executor failure = failed request
            result = {"executed": False, "error": str(e)}
            status = "failed"
        self.store.transition(rec["requestId"], status, result=result,
                              resolved_by="executor")
        return {"requestId": rec["requestId"], "status": status,
                "result": result}

    def _get(self, params: dict) -> dict:
        rec = self._require(params)
        return self._public(rec)

    async def _wait(self, params: dict) -> dict:
        rec = self._require(params)
        timeout = float(params.get("timeoutS") or DEFAULT_WAIT_TIMEOUT_S)
        deadline = time.monotonic() + timeout
        while True:
            self._settle(rec["requestId"])
            rec = self.store.get(rec["requestId"])
            if rec["status"] in TERMINAL:
                return self._public(rec)
            if time.monotonic() >= deadline:
                return {**self._public(rec), "timedOut": True}
            await asyncio.sleep(WAIT_POLL_S)

    def _cancel(self, params: dict) -> dict:
        rec = self._require(params)
        self.store.transition(rec["requestId"], "cancelled",
                              resolved_by=self.actor_id)
        return self._public(self.store.get(rec["requestId"]))

    def _require(self, params: dict) -> dict:
        rid = params.get("requestId")
        rec = self.store.get(rid) if rid else None
        if rec is None:
            raise ProtocolError(-32602, f"unknown requestId: {rid!r}")
        return rec

    @staticmethod
    def _public(rec: dict) -> dict:
        out = {"requestId": rec["requestId"], "status": rec["status"]}
        if rec.get("result") is not None:
            out["result"] = rec["result"]
        if rec.get("resolvedBy"):
            out["resolvedBy"] = rec["resolvedBy"]
        return out

    # ── resolution mapping (ha decision → runtime terminal state) ──────────
    def _settle(self, request_id: str) -> None:
        rec = self.store.get(request_id)
        if not rec or rec["status"] != "pending":
            return
        aid = self._ha_of.get(request_id)
        if not aid:
            return
        res = self.ha.poll_resolution(aid)
        if res is None:
            return
        status, answers, resolved_by = res
        if status == "expired":
            self.store.transition(request_id, "expired", resolved_by=resolved_by)
            return
        if rec["requestType"] == "human_action":
            chosen = self.ha.first_answer(answers or {},
                                          [{"label": "Done"}, {"label": "Decline"}])
            labels = chosen if isinstance(chosen, list) else [chosen]
            done = any(str(c).strip().lower() == "done" for c in labels if c)
            self.store.transition(request_id,
                                  "completed" if done else "declined",
                                  resolved_by=resolved_by)
            return
        if rec["requestType"] == "approval":
            chosen = self.ha.first_answer(answers or {},
                                          [{"label": "Approve"}, {"label": "Deny"}])
            labels = chosen if isinstance(chosen, list) else [chosen]
            approved = any(str(c).strip().lower() == "approve" for c in labels if c)
            self.store.transition(request_id,
                                  "approved" if approved else "denied",
                                  resolved_by=resolved_by)
        else:
            p = rec["params"]
            options = [{"label": str(o)} for o in (p.get("options") or [])]
            if p.get("type") == "confirmation" and not options:
                options = [{"label": "Yes"}, {"label": "No"}]
            answer = self.ha.first_answer(answers or {}, options)
            if isinstance(answer, list) and p.get("type") == "single_select":
                answer = answer[0] if answer else None
            self.store.transition(request_id, "resolved",
                                  result={"answer": answer},
                                  resolved_by=resolved_by)

    async def resolver_loop(self) -> None:
        while True:
            try:
                for rec in self.store.pending():
                    self._settle(rec["requestId"])
            except Exception as e:  # noqa: BLE001 — resolver must never die
                _log(f"resolver error (isolated): {e}")
            await asyncio.sleep(RESOLVER_POLL_S)
