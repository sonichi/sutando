"""One gateway proactive drain against a fake room endpoint, for the proactive outbox-lane tests.

Each test imports the bridge first, at module level, through hermetic_gateway's
isolate_then_import (env REMOTE_PROACTIVE_ROOM=""), then hands the module to a Lane. Each Lane is one results/ directory, so its
outbox is its own; restart() drops the built outbox and recovers orphan claims as a fresh
process would.
"""
from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OWNER_DM = "!OwnerDmRoomXyZ:ag2.space"
ROOM = "!TaskRoomAbCdEf:ag2.space"
AGENT = "@lane-agent:ag2.space"


def owner_of(agent: str) -> str:
    """The agent's owner, on the agent's own homeserver (a registry owner elsewhere is no owner)."""
    return "@lane-owner:" + agent.partition(":")[2]


def registry_reply(gb, method, path, payload, owner_dm: str):
    """What the gateway answers to the owner DM's member read (the agent and its owner) and to
    this agent's registry row; None for any other request."""
    agent, payload = gb._reenroll_identity(), payload or {}
    op = payload.get("op") if method == "POST" and path == "/v1/room" else None
    if op == "members" and payload.get("room_id") == owner_dm:
        return {"members": [{"user_id": u} for u in (agent, owner_of(agent))]}
    if method == "GET" and path == "/v1/agents":
        return {"agents": [{"id": agent, "owner": owner_of(agent), "owner_dm_room": owner_dm}]}
    return None


@contextlib.contextmanager
def known_owner(gb, agent: str = AGENT):
    """An agent identity (`agent` unless the test set one) and an owner read fresh from the registry."""
    saved = (os.environ.get("AGENT_MXID"), dict(gb._HITL_OWNER))
    if not gb._reenroll_identity():
        os.environ["AGENT_MXID"] = agent
    gb._HITL_OWNER.update(mxid="", at=0.0, bad_at=None)
    try:
        yield
    finally:
        if saved[0] is None:
            os.environ.pop("AGENT_MXID", None)
        else:
            os.environ["AGENT_MXID"] = saved[0]
        gb._HITL_OWNER.update(saved[1])


def scratch(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


class Lane:
    """A workspace (results/, tasks/, state/); `respond(payload)` answers each room POST or raises."""

    def __init__(self, gb, base: Path, respond=None, ws: "Path | None" = None):
        self.gb = gb
        self.ws = Path(ws) if ws else Path(tempfile.mkdtemp(dir=base, prefix="ws-"))
        self.results, self.tasks = self.ws / "results", self.ws / "tasks"
        for d in (self.results / "archive", self.tasks, self.ws / "state"):
            d.mkdir(parents=True, exist_ok=True)
        self.posts: list = []
        self.logs: list = []
        self.owner_dm = OWNER_DM
        self.respond = respond or (lambda payload: {"ok": True, "event_id": f"$evt{len(self.posts)}"})

    def _req(self, method, path, payload=None, timeout=None, **_kw):
        if method == "POST" and path == "/v1/room" and (payload or {}).get("op") == "message":
            self.posts.append(dict(payload))
            return self.respond(payload)
        return registry_reply(self.gb, method, path, payload, self.owner_dm) or {"ok": True}

    def write(self, name: str, body: str) -> Path:
        p = self.results / name
        p.write_text(body, encoding="utf-8")
        return p

    def drain(self, gate=None, route_gate=None, ack_gate=None) -> None:
        gb = self.gb
        saved = (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.UNDELIVERABLE_RESULTS_DIR, gb.TASKS_DIR,
                 gb.PROACTIVE_ROOM, gb._req, gb.PROACTIVE_CLAIM_GATE, gb._log,
                 getattr(gb, "PROACTIVE_DISPATCH_GATE", None), getattr(gb, "PROACTIVE_ROUTE_GATE", None),
                 getattr(gb, "PROACTIVE_ACK_GATE", None))
        gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR = self.results, self.results / "archive"
        gb.UNDELIVERABLE_RESULTS_DIR, gb.TASKS_DIR = self.results / "undelivered", self.tasks
        gb.PROACTIVE_ROOM, gb._req, gb.PROACTIVE_CLAIM_GATE = self.owner_dm, self._req, None
        gb._log, gb.PROACTIVE_DISPATCH_GATE = self.logs.append, gate
        gb.PROACTIVE_ROUTE_GATE, gb.PROACTIVE_ACK_GATE = route_gate, ack_gate
        gb._ROUTING.update(owner_dm=self.owner_dm, loaded=True, next=gb.time.time() + 3600)
        try:
            with known_owner(gb):
                gb._post_proactive()
        finally:
            (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.UNDELIVERABLE_RESULTS_DIR, gb.TASKS_DIR,
             gb.PROACTIVE_ROOM, gb._req, gb.PROACTIVE_CLAIM_GATE, gb._log,
             gb.PROACTIVE_DISPATCH_GATE, gb.PROACTIVE_ROUTE_GATE, gb.PROACTIVE_ACK_GATE) = saved
            gb._ROUTING.update(owner_dm="", loaded=False, next=0.0)

    def backend(self):
        """This lane's outbox backend, or None on a bridge that has no proactive outbox."""
        gb = self.gb
        if not hasattr(gb, "_PROACTIVE_CORE"):
            return None
        saved, gb.RESULTS_DIR = gb.RESULTS_DIR, self.results
        try:
            return gb._delivery_core("proactive").backend
        finally:
            gb.RESULTS_DIR = saved

    def restart(self) -> None:
        """What a new process does first: no outbox built yet, orphan claims recovered."""
        gb = self.gb
        if hasattr(gb, "_PROACTIVE_CORE"):
            gb._PROACTIVE_CORE = None
        saved = gb.RESULTS_DIR
        gb.RESULTS_DIR = self.results
        try:
            gb._recover_orphan_proactive()
        finally:
            gb.RESULTS_DIR = saved

    def items(self) -> list:
        from ag2_sparrow import outbox
        return outbox.list_items(self.results / ".outbox-ag2space-proactive")

    def where(self, stem: str) -> str:
        """'pending' | 'archived' | 'undelivered' | 'claimed' | 'gone' for one proactive file."""
        if (self.results / f"{stem}.txt").exists():
            return "pending"
        if any(self.results.glob(f"{stem}.sending*")):
            return "claimed"
        if any((self.results / "archive").glob(f"{stem}-*.txt")):
            return "archived"
        if any((self.results / "undelivered").glob(f"{stem}*")):
            return "undelivered"
        return "gone"


class Checks:
    def __init__(self):
        self.fails: list = []

    def __call__(self, cond: bool, msg: str) -> None:
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            self.fails.append(msg)

    def exit_code(self) -> int:
        print(f"\n{'FAIL' if self.fails else 'PASS'}: {len(self.fails)} failure(s)")
        return 1 if self.fails else 0
