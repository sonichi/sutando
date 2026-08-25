#!/usr/bin/env python3
"""Durable logical session profiles for the lead-follower pool.

A profile is the long-lived identity of a context: which rooms it covers,
which provider session currently continues it, and which core is seated on
it. Cores and provider sessions are both replaceable underneath it — the
profile is what survives.

Two fencing rules make that safe. The lead is the only writer of seating
state, and every seat carries a monotonic epoch; a core may advance a
profile's session lineage only while holding the current epoch, so a core
that died, was re-seated elsewhere and then briefly revived cannot write
into a profile it no longer owns.

This module owns profiles.json alone: schema, bounds, atomicity and failure
semantics. It holds no task logic — task ownership remains entirely with the
pool's atomic-rename claim. Injected path/clock; stdlib only.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

SCHEMA_VERSION = 1

# A rotation reason is recorded on the generation it closes, so an audit can
# tell a deliberate size rotation from a resume that failed.
ROTATE_REASONS = ("rotated_for_size", "rotated_for_age", "resume_failed",
                  "crashed", "auth_death", "runtime_switch", "manual")

ROOM_WRITE_MODES = ("scoped", "none")
SHARING_MODES = ("explicit",)
PROVENANCE_MODES = ("preserve_room",)

DEFAULT_CONTEXT_POLICY = {"sharing": "explicit", "provenance": "preserve_room"}
DEFAULT_POLICY = {"max_age_s": 7 * 24 * 3600, "max_bytes": 64 * 1024 * 1024,
                  "crash_budget": 3}


class ProfileStoreCorrupt(Exception):
    """profiles.json exists but is unreadable or does not match the schema."""


class SeatFenced(Exception):
    """The caller is not the current seat, or holds a stale epoch."""


class PolicyViolation(Exception):
    """A room or context policy value is missing or not an enumerated one."""


class UnknownProfile(Exception):
    """No profile with that id."""


class NotTheWriter(Exception):
    """Seating and membership are the lead's to write; this caller is not it."""


def _validate_rooms(rooms) -> dict:
    if not isinstance(rooms, dict) or not rooms:
        raise PolicyViolation("rooms must be a non-empty mapping")
    out = {}
    for room_id, spec in rooms.items():
        if not isinstance(room_id, str) or not room_id:
            raise PolicyViolation(f"bad room id: {room_id!r}")
        if not isinstance(spec, dict):
            raise PolicyViolation(f"room {room_id}: spec must be a mapping")
        if set(spec) - {"read", "write"}:
            raise PolicyViolation(f"room {room_id}: unknown keys "
                                  f"{sorted(set(spec) - {'read', 'write'})}")
        read = spec.get("read")
        write = spec.get("write")
        if not isinstance(read, bool):
            raise PolicyViolation(f"room {room_id}: read must be a bool")
        if write not in ROOM_WRITE_MODES:
            raise PolicyViolation(f"room {room_id}: write must be one of "
                                  f"{ROOM_WRITE_MODES}, got {write!r}")
        out[room_id] = {"read": read, "write": write}
    return out


def _validate_context_policy(cp) -> dict:
    if cp is None:
        cp = dict(DEFAULT_CONTEXT_POLICY)
    if not isinstance(cp, dict):
        raise PolicyViolation("context_policy must be a mapping")
    if set(cp) != {"sharing", "provenance"}:
        raise PolicyViolation(f"context_policy keys must be "
                              f"{{'sharing', 'provenance'}}, got {sorted(cp)}")
    if cp["sharing"] not in SHARING_MODES:
        raise PolicyViolation(f"sharing must be one of {SHARING_MODES}")
    if cp["provenance"] not in PROVENANCE_MODES:
        raise PolicyViolation(f"provenance must be one of {PROVENANCE_MODES}")
    return dict(cp)


def _validate_policy(p) -> dict:
    if p is None:
        return dict(DEFAULT_POLICY)
    if not isinstance(p, dict) or set(p) - set(DEFAULT_POLICY):
        raise PolicyViolation(f"policy keys must be a subset of "
                              f"{sorted(DEFAULT_POLICY)}")
    merged = dict(DEFAULT_POLICY)
    for k, v in p.items():
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise PolicyViolation(f"policy.{k} must be a non-negative int")
        merged[k] = v
    return merged


def _validate_profile(profile_id: str, prof) -> None:
    if not isinstance(prof, dict):
        raise ProfileStoreCorrupt(f"{profile_id}: not a mapping")
    required = {"rooms", "runtime", "lineage", "seat", "policy",
                "context_policy"}
    missing = required - set(prof)
    if missing:
        raise ProfileStoreCorrupt(f"{profile_id}: missing {sorted(missing)}")
    lin, seat = prof["lineage"], prof["seat"]
    if not isinstance(lin, dict) or set(lin) != {
            "generation", "active_session_id", "previous_session_ids"}:
        raise ProfileStoreCorrupt(f"{profile_id}: bad lineage")
    if not isinstance(lin["generation"], int) or lin["generation"] < 1:
        raise ProfileStoreCorrupt(f"{profile_id}: bad generation")
    if not isinstance(lin["previous_session_ids"], list):
        raise ProfileStoreCorrupt(f"{profile_id}: bad previous_session_ids")
    if not isinstance(seat, dict) or set(seat) != {"core_id", "epoch"}:
        raise ProfileStoreCorrupt(f"{profile_id}: bad seat")
    if not isinstance(seat["epoch"], int) or seat["epoch"] < 0:
        raise ProfileStoreCorrupt(f"{profile_id}: bad seat epoch")


class ProfileStore:
    """Read-modify-write access to profiles.json under an exclusive lock."""

    def __init__(self, path, lead_label: str = "pool-lead",
                 now_fn=time.time) -> None:
        self.path = Path(path)
        self.lead_label = lead_label
        self.now = now_fn

    # ── storage ──────────────────────────────────────────────────────────
    def _lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    def load(self) -> dict:
        """A missing store is empty; an unparseable one is an error. Never
        substitute an empty store for a corrupt one — that silently unseats."""
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            return {"version": SCHEMA_VERSION, "profiles": {}}
        except OSError as e:
            raise ProfileStoreCorrupt(f"unreadable: {e}") from e
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise ProfileStoreCorrupt(f"not JSON: {e}") from e
        if not isinstance(data, dict) or not isinstance(
                data.get("profiles"), dict):
            raise ProfileStoreCorrupt("missing profiles mapping")
        if data.get("version") != SCHEMA_VERSION:
            raise ProfileStoreCorrupt(f"version {data.get('version')!r} != "
                                      f"{SCHEMA_VERSION}")
        for pid, prof in data["profiles"].items():
            _validate_profile(pid, prof)
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def _mutate(self, fn):
        """Whole-file read-modify-write under flock; the lock file is opened
        fresh per call because flock is per open-file-description."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path(), "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                data = self.load()
                result = fn(data)
                self._save(data)
                return result
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    # ── lookup ───────────────────────────────────────────────────────────
    def get(self, profile_id: str) -> dict:
        prof = self.load()["profiles"].get(profile_id)
        if prof is None:
            raise UnknownProfile(profile_id)
        return prof

    def profile_for_room(self, room_id: str) -> "str | None":
        for pid, prof in sorted(self.load()["profiles"].items()):
            if room_id in prof["rooms"]:
                return pid
        return None

    # ── lead-only: identity and membership ───────────────────────────────
    def _require_lead(self, writer: str) -> None:
        if writer != self.lead_label:
            raise NotTheWriter(f"{writer!r} may not write seating state")

    def create(self, profile_id: str, rooms: dict, runtime: str, *,
               writer: str, context_policy=None, policy=None) -> dict:
        self._require_lead(writer)
        if not isinstance(profile_id, str) or not profile_id:
            raise PolicyViolation("profile_id must be a non-empty string")
        if runtime not in ("claude", "codex"):
            raise PolicyViolation(f"unsupported runtime {runtime!r}")
        rooms_v = _validate_rooms(rooms)
        cp = _validate_context_policy(context_policy)
        pol = _validate_policy(policy)

        def apply(data):
            if profile_id in data["profiles"]:
                raise PolicyViolation(f"{profile_id} already exists")
            prof = {
                "rooms": rooms_v, "runtime": runtime,
                "lineage": {"generation": 1, "active_session_id": None,
                            "previous_session_ids": []},
                "seat": {"core_id": None, "epoch": 0},
                "policy": pol, "context_policy": cp,
                "created_at": self.now(),
            }
            data["profiles"][profile_id] = prof
            return prof

        return self._mutate(apply)

    def set_rooms(self, profile_id: str, rooms: dict, *, writer: str,
                  context_policy=None) -> dict:
        """Membership changes revalidate the whole room set and the context
        policy together — adding a room is a sharing decision, not an edit."""
        self._require_lead(writer)
        rooms_v = _validate_rooms(rooms)

        def apply(data):
            prof = data["profiles"].get(profile_id)
            if prof is None:
                raise UnknownProfile(profile_id)
            prof["context_policy"] = _validate_context_policy(
                prof["context_policy"] if context_policy is None
                else context_policy)
            prof["rooms"] = rooms_v
            return prof

        return self._mutate(apply)

    # ── lead-only: seating ───────────────────────────────────────────────
    def seat(self, profile_id: str, core_id: str, *, writer: str) -> int:
        """Bind a core and return the new epoch. Every seating change bumps
        it, so a stale holder is fenced out by comparison, not by liveness."""
        self._require_lead(writer)
        if not isinstance(core_id, str) or not core_id:
            raise PolicyViolation("core_id must be a non-empty string")

        def apply(data):
            prof = data["profiles"].get(profile_id)
            if prof is None:
                raise UnknownProfile(profile_id)
            prof["seat"] = {"core_id": core_id,
                            "epoch": prof["seat"]["epoch"] + 1}
            return prof["seat"]["epoch"]

        return self._mutate(apply)

    def unseat(self, profile_id: str, *, writer: str) -> int:
        self._require_lead(writer)

        def apply(data):
            prof = data["profiles"].get(profile_id)
            if prof is None:
                raise UnknownProfile(profile_id)
            prof["seat"] = {"core_id": None,
                            "epoch": prof["seat"]["epoch"] + 1}
            return prof["seat"]["epoch"]

        return self._mutate(apply)

    def reseat(self, profile_id: str, core_id: str, *, writer: str) -> int:
        """One epoch bump, not two — an unseat/seat pair would leave a window
        in which neither the old nor the new core could be validated."""
        return self.seat(profile_id, core_id, writer=writer)

    # ── seat-fenced: lineage ─────────────────────────────────────────────
    def _fenced(self, prof: dict, core_id: str, seat_epoch: int) -> None:
        seat = prof["seat"]
        if seat["core_id"] != core_id or seat["epoch"] != seat_epoch:
            raise SeatFenced(
                f"holder {core_id}@{seat_epoch} is not the seat "
                f"{seat['core_id']}@{seat['epoch']}")

    def advance_session(self, profile_id: str, core_id: str, seat_epoch: int,
                        session_id: str) -> dict:
        """Record the provider session now continuing this profile."""
        if not isinstance(session_id, str) or not session_id:
            raise PolicyViolation("session_id must be a non-empty string")

        def apply(data):
            prof = data["profiles"].get(profile_id)
            if prof is None:
                raise UnknownProfile(profile_id)
            self._fenced(prof, core_id, seat_epoch)
            prof["lineage"]["active_session_id"] = session_id
            return prof["lineage"]

        return self._mutate(apply)

    def rotate(self, profile_id: str, core_id: str, seat_epoch: int,
               reason: str) -> int:
        """Close the current generation and open the next. The closed session
        is preserved, so a failed resume stays auditable rather than vanishing."""
        if reason not in ROTATE_REASONS:
            raise PolicyViolation(f"reason must be one of {ROTATE_REASONS}")

        def apply(data):
            prof = data["profiles"].get(profile_id)
            if prof is None:
                raise UnknownProfile(profile_id)
            self._fenced(prof, core_id, seat_epoch)
            lin = prof["lineage"]
            if lin["active_session_id"] is not None:
                lin["previous_session_ids"].append({
                    "session_id": lin["active_session_id"],
                    "generation": lin["generation"],
                    "reason": reason,
                    "ended_at": self.now(),
                })
            lin["generation"] += 1
            lin["active_session_id"] = None
            return lin["generation"]

        return self._mutate(apply)
