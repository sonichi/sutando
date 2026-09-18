"""Trusted-side room-membership verdicts for room-bound voice sessions.

The voice client only NAMES the room it is docked in; proving that the agent
and its owner are joined there takes the gateway's credentials, which live in
the gateway bridge. So the task bridge drops `<key>.request.json` into
`state/voice-room-checks/`, the verifier thread answers with
`<key>.verdict.json` (one gateway read per room per TTL), and the proactive
claim gate re-asks the same verifier before a voice result may post into a
room. Anything the gateway cannot confirm reads as NOT verified.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

CHECK_DIR_NAME = "voice-room-checks"
REQUEST_SUFFIX = ".request.json"
VERDICT_SUFFIX = ".verdict.json"
# Verdict lifetime, shared with the task bridge's reader: a member who leaves
# is re-read within this window, and navigation never waits on the gateway twice.
VERDICT_TTL_S = 60
_MATRIX_ROOM_RE = re.compile(r"^![^\s:]+:\S+$")
_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")

MembersFn = Callable[[str], Optional[List[str]]]


def membership_verdict(room_id: str, members: Optional[List[str]],
                       agent: str, owner: str, now: Optional[float] = None) -> dict:
    """Pure verdict: verified only when BOTH the agent and its owner are joined.
    A missing identity or an unreadable member list is a refusal, never a pass."""
    now = time.time() if now is None else now
    out = {"room_id": room_id, "verified": False, "agent_joined": False,
           "owner_joined": False, "reason": "", "checked_at": now, "ttl_s": VERDICT_TTL_S}
    if not _MATRIX_ROOM_RE.match(room_id or ""):
        out["reason"] = "not a matrix room id"
        return out
    if not agent:
        out["reason"] = "agent identity unknown"
        return out
    if not owner:
        out["reason"] = "owner identity unknown"
        return out
    if members is None:
        out["reason"] = "members unreadable"
        return out
    joined = set(members)
    out["agent_joined"] = agent in joined
    out["owner_joined"] = owner in joined
    if out["agent_joined"] and out["owner_joined"]:
        out["verified"] = True
        out["reason"] = "agent and owner joined"
    elif not out["agent_joined"]:
        out["reason"] = "agent not joined"
    else:
        out["reason"] = "owner not joined"
    return out


def write_verdict(path: Path, verdict: dict) -> None:
    """Atomic publish: the reader must never see a half-written verdict."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(verdict), encoding="utf-8")
    os.replace(tmp, path)


class RoomMembershipVerifier:
    """One verifier per gateway lane; `members`, `agent_mxid` and `owner_mxid`
    are the gateway bridge's own readers, so the verdict is the gateway's word."""

    def __init__(self, check_dir: Path, members: MembersFn,
                 agent_mxid: Callable[[], str], owner_mxid: Callable[[], str],
                 log: Callable[[str], None] = print, ttl_s: float = VERDICT_TTL_S,
                 clock: Callable[[], float] = time.time) -> None:
        self.check_dir = Path(check_dir)
        self._members = members
        self._agent_mxid = agent_mxid
        self._owner_mxid = owner_mxid
        self._log = log
        self._ttl_s = ttl_s
        self._clock = clock
        self._cache: Dict[str, dict] = {}
        self._lock = threading.Lock()

    def _identity(self, reader: Callable[[], str]) -> str:
        try:
            value = reader()
        except Exception as e:  # noqa: BLE001 — an unreadable identity refuses, never raises
            self._log(f"voice-room: identity read failed: {e}")
            return ""
        return value.strip() if isinstance(value, str) else ""

    def _read_members(self, room_id: str) -> Optional[List[str]]:
        try:
            members = self._members(room_id)
        except Exception as e:  # noqa: BLE001 — the gateway's failure is a refusal
            self._log(f"voice-room: members read failed for {room_id}: {e}")
            return None
        if not isinstance(members, list):
            return None
        return [m for m in members if isinstance(m, str)]

    def verdict(self, room_id: str) -> dict:
        """The cached verdict while fresh, else one new gateway read."""
        now = self._clock()
        with self._lock:
            cached = self._cache.get(room_id)
            if cached and now - cached["checked_at"] < self._ttl_s:
                return dict(cached)
        agent = self._identity(self._agent_mxid)
        owner = self._identity(self._owner_mxid) if agent else ""
        members = self._read_members(room_id) if agent and owner else None
        out = membership_verdict(room_id, members, agent, owner, now=now)
        with self._lock:
            self._cache[room_id] = dict(out)
        return out

    def verified(self, room_id: str) -> bool:
        return bool(self.verdict(room_id).get("verified"))

    def service_once(self) -> int:
        """Answer every pending request file; returns how many were answered."""
        try:
            requests = sorted(self.check_dir.glob("*" + REQUEST_SUFFIX))
        except OSError:
            return 0
        answered = 0
        for req in requests:
            key = req.name[:-len(REQUEST_SUFFIX)]
            room_id = self._request_room(req)
            if not _KEY_RE.match(key) or room_id is None:
                # An unreadable request is dropped: nothing sound can answer it.
                req.unlink(missing_ok=True)
                continue
            out = self.verdict(room_id)
            try:
                write_verdict(self.check_dir / (key + VERDICT_SUFFIX), out)
            except OSError as e:
                self._log(f"voice-room: could not write verdict for {room_id}: {e}")
                continue
            req.unlink(missing_ok=True)
            answered += 1
            self._log(f"voice-room: {room_id} {'verified' if out['verified'] else 'REFUSED'} ({out['reason']})")
        return answered

    @staticmethod
    def _request_room(req: Path) -> Optional[str]:
        try:
            body = json.loads(req.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        room = body.get("room_id") if isinstance(body, dict) else None
        return room if isinstance(room, str) and _MATRIX_ROOM_RE.match(room) else None

    def run(self, stop: threading.Event, poll_s: float = 0.25) -> None:
        """Daemon loop: a request is answered within one poll interval."""
        while not stop.is_set():
            try:
                self.check_dir.mkdir(parents=True, exist_ok=True)
                self.service_once()
            except Exception as e:  # noqa: BLE001 — one bad pass never stops the verifier
                self._log(f"voice-room: verifier pass failed: {e}")
            stop.wait(poll_s)

    def start(self, stop: Optional[threading.Event] = None,
              poll_s: float = 0.25) -> threading.Thread:
        stop = stop or threading.Event()
        t = threading.Thread(target=self.run, args=(stop, poll_s),
                             name="voice-room-verifier", daemon=True)
        t.start()
        return t


def members_from_room_op(answer: object) -> Optional[List[str]]:
    """The joined mxids from a `/v1/room` `{"op": "members"}` answer; None for
    an error envelope or any shape that is not a member list."""
    if not isinstance(answer, dict) or answer.get("error") or not isinstance(answer.get("members"), list):
        return None
    out = []
    for row in answer["members"]:
        uid = row.get("user_id") if isinstance(row, dict) else None
        if isinstance(uid, str) and uid:
            out.append(uid)
    return out
