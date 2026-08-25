#!/usr/bin/env python3
"""Durable logical session profiles for the lead-follower pool.

A profile is the long-lived identity of a context: the rooms it covers, the
core seated on it, and a graph of generations. Cores and provider sessions are
both replaceable underneath it — the profile is what survives.

Ancestry is a graph, not a list. Each generation names its parent, so a chain,
a retry after a failed start, a runtime switch, and a branch off an older
generation are all the same shape, and per-generation transcript, digest and
room-watermark references have somewhere to live.

Three rules keep it consistent under concurrency:

- The lead alone writes seating state, and every seating change bumps a
  monotonic epoch.
- A core may write lineage only while holding the current epoch, so a core
  that died, was re-seated elsewhere and briefly revived is fenced out by
  comparison rather than by liveness.
- The head advances only when a child actually starts: begin_generation
  records a pending child, and promote_generation is a compare-and-set on
  (seat, head). A failed start leaves the previous head valid, so a profile is
  never without a recoverable generation.

This module owns profiles.json alone: schema, bounds, atomicity and failure
semantics. It holds no task logic — task ownership stays with the pool's
atomic-rename claim. Injected path/clock; stdlib only.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

SCHEMA_VERSION = 1

# Why this generation exists, recorded on the transition that created it.
TRANSITION_REASONS = ("initial", "rotated_for_size", "rotated_for_age",
                      "resume_failed", "crashed", "auth_death",
                      "runtime_switch", "branch", "manual")

GENERATION_STATUSES = ("pending", "active", "superseded", "failed")
RUNTIMES = ("claude", "codex")

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


class HeadMoved(Exception):
    """The head advanced under a pending generation; its promotion is void."""


class PolicyViolation(Exception):
    """A room, policy or enum value is missing or not an enumerated one."""


class UnknownProfile(Exception):
    """No profile with that id."""


class UnknownGeneration(Exception):
    """No generation with that id on this profile."""


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
        extra = sorted(set(spec) - {"read", "write"})
        if extra:
            raise PolicyViolation(f"room {room_id}: unknown keys {extra}")
        read, write = spec.get("read"), spec.get("write")
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


def _validate_generation(pid: str, gid: str, gen) -> None:
    required = {"session_id", "parent_generation_id", "runtime", "status",
                "transition_reason", "started_at", "ended_at",
                "transcript_ref", "digest_ref", "room_watermarks"}
    if not isinstance(gen, dict) or set(gen) != required:
        raise ProfileStoreCorrupt(f"{pid}/{gid}: generation keys must be "
                                  f"{sorted(required)}")
    if gen["status"] not in GENERATION_STATUSES:
        raise ProfileStoreCorrupt(f"{pid}/{gid}: bad status")
    if gen["runtime"] not in RUNTIMES:
        raise ProfileStoreCorrupt(f"{pid}/{gid}: bad runtime")
    if not isinstance(gen["room_watermarks"], dict):
        raise ProfileStoreCorrupt(f"{pid}/{gid}: bad room_watermarks")


def _validate_profile(pid: str, prof) -> None:
    if not isinstance(prof, dict):
        raise ProfileStoreCorrupt(f"{pid}: not a mapping")
    required = {"rooms", "seat", "policy", "context_policy",
                "head_generation_id", "generations", "created_at"}
    missing = required - set(prof)
    if missing:
        raise ProfileStoreCorrupt(f"{pid}: missing {sorted(missing)}")
    seat, gens = prof["seat"], prof["generations"]
    if not isinstance(seat, dict) or set(seat) != {"core_id", "epoch"}:
        raise ProfileStoreCorrupt(f"{pid}: bad seat")
    if not isinstance(seat["epoch"], int) or seat["epoch"] < 0:
        raise ProfileStoreCorrupt(f"{pid}: bad seat epoch")
    if not isinstance(gens, dict):
        raise ProfileStoreCorrupt(f"{pid}: bad generations")
    for gid, gen in gens.items():
        _validate_generation(pid, gid, gen)
    head = prof["head_generation_id"]
    if head is not None and head not in gens:
        raise ProfileStoreCorrupt(f"{pid}: head {head!r} is not a generation")


class ProfileStore:
    """Read-modify-write access to profiles.json under an exclusive lock."""

    def __init__(self, path, lead_label: str = "pool-lead",
                 now_fn=time.time) -> None:
        self.path = Path(path)
        self.lead_label = lead_label
        self.now = now_fn

    # ── storage ──────────────────────────────────────────────────────────
    def _lock_path(self) -> Path:
        """A sidecar the writer never replaces. Locking the data file instead
        would let a waiter inherit the pre-replace inode and clobber a newer one."""
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
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        # The rename itself is only durable once the directory entry is.
        dfd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def _mutate(self, fn):
        """Read-modify-write under flock on the sidecar. The read happens after
        the lock is held, so a waiter never applies onto a stale snapshot."""
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

    def head(self, profile_id: str) -> "dict | None":
        prof = self.get(profile_id)
        gid = prof["head_generation_id"]
        return None if gid is None else dict(prof["generations"][gid],
                                             generation_id=gid)

    def ancestry(self, profile_id: str) -> "list[str]":
        """Head first, walking parent links back. Cycles cannot occur — a
        parent always predates its child — but the seen-set makes that safe."""
        prof = self.get(profile_id)
        gid, seen, out = prof["head_generation_id"], set(), []
        while gid is not None and gid not in seen:
            seen.add(gid)
            out.append(gid)
            gid = prof["generations"][gid]["parent_generation_id"]
        return out

    def profile_for_room(self, room_id: str) -> "str | None":
        for pid, prof in sorted(self.load()["profiles"].items()):
            if room_id in prof["rooms"]:
                return pid
        return None

    # ── lead-only: identity and membership ───────────────────────────────
    def _require_lead(self, writer: str) -> None:
        if writer != self.lead_label:
            raise NotTheWriter(f"{writer!r} may not write seating state")

    def create(self, profile_id: str, rooms: dict, *, writer: str,
               context_policy=None, policy=None) -> dict:
        self._require_lead(writer)
        if not isinstance(profile_id, str) or not profile_id:
            raise PolicyViolation("profile_id must be a non-empty string")
        rooms_v = _validate_rooms(rooms)
        cp = _validate_context_policy(context_policy)
        pol = _validate_policy(policy)

        def apply(data):
            if profile_id in data["profiles"]:
                raise PolicyViolation(f"{profile_id} already exists")
            prof = {"rooms": rooms_v, "seat": {"core_id": None, "epoch": 0},
                    "policy": pol, "context_policy": cp,
                    "head_generation_id": None, "generations": {},
                    "created_at": self.now()}
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

    # ── seat-fenced: the generation graph ────────────────────────────────
    def _fenced(self, prof: dict, core_id: str, seat_epoch: int) -> None:
        seat = prof["seat"]
        if seat["core_id"] != core_id or seat["epoch"] != seat_epoch:
            raise SeatFenced(
                f"holder {core_id}@{seat_epoch} is not the seat "
                f"{seat['core_id']}@{seat['epoch']}")

    def begin_generation(self, profile_id: str, core_id: str, seat_epoch: int,
                         runtime: str, reason: str,
                         parent_generation_id: "str | None" = "HEAD") -> str:
        """Open a pending child of the current head. The head does NOT move —
        a session that never starts must not leave the profile headless."""
        if runtime not in RUNTIMES:
            raise PolicyViolation(f"runtime must be one of {RUNTIMES}")
        if reason not in TRANSITION_REASONS:
            raise PolicyViolation(f"reason must be one of {TRANSITION_REASONS}")

        def apply(data):
            prof = data["profiles"].get(profile_id)
            if prof is None:
                raise UnknownProfile(profile_id)
            self._fenced(prof, core_id, seat_epoch)
            parent = (prof["head_generation_id"]
                      if parent_generation_id == "HEAD"
                      else parent_generation_id)
            if parent is not None and parent not in prof["generations"]:
                raise UnknownGeneration(f"{profile_id}/{parent}")
            gid = f"g{len(prof['generations']) + 1}"
            prof["generations"][gid] = {
                "session_id": None, "parent_generation_id": parent,
                "runtime": runtime, "status": "pending",
                "transition_reason": reason, "started_at": self.now(),
                "ended_at": None, "transcript_ref": None, "digest_ref": None,
                "room_watermarks": {}}
            return gid

        return self._mutate(apply)

    def promote_generation(self, profile_id: str, core_id: str,
                           seat_epoch: int, generation_id: str,
                           session_id: str) -> str:
        """Compare-and-set the head: only a pending generation whose recorded
        parent is still the head may take it. Refuses otherwise."""
        if not isinstance(session_id, str) or not session_id:
            raise PolicyViolation("session_id must be a non-empty string")

        def apply(data):
            prof = data["profiles"].get(profile_id)
            if prof is None:
                raise UnknownProfile(profile_id)
            self._fenced(prof, core_id, seat_epoch)
            gen = prof["generations"].get(generation_id)
            if gen is None:
                raise UnknownGeneration(f"{profile_id}/{generation_id}")
            if gen["status"] != "pending":
                raise HeadMoved(f"{generation_id} is {gen['status']}")
            if prof["head_generation_id"] != gen["parent_generation_id"]:
                raise HeadMoved(
                    f"head is {prof['head_generation_id']!r}, "
                    f"parent was {gen['parent_generation_id']!r}")
            parent_id = gen["parent_generation_id"]
            if parent_id is not None:
                parent = prof["generations"][parent_id]
                parent["status"] = "superseded"
                parent["ended_at"] = self.now()
            gen["status"] = "active"
            gen["session_id"] = session_id
            prof["head_generation_id"] = generation_id
            return generation_id

        return self._mutate(apply)

    def fail_generation(self, profile_id: str, core_id: str, seat_epoch: int,
                        generation_id: str, reason: str) -> str:
        """Mark a pending child failed. The head is untouched, so the previous
        generation stays the one to recover from."""
        if reason not in TRANSITION_REASONS:
            raise PolicyViolation(f"reason must be one of {TRANSITION_REASONS}")

        def apply(data):
            prof = data["profiles"].get(profile_id)
            if prof is None:
                raise UnknownProfile(profile_id)
            self._fenced(prof, core_id, seat_epoch)
            gen = prof["generations"].get(generation_id)
            if gen is None:
                raise UnknownGeneration(f"{profile_id}/{generation_id}")
            if gen["status"] != "pending":
                raise HeadMoved(f"{generation_id} is {gen['status']}")
            gen["status"] = "failed"
            gen["transition_reason"] = reason
            gen["ended_at"] = self.now()
            return generation_id

        return self._mutate(apply)

    def annotate_generation(self, profile_id: str, core_id: str,
                            seat_epoch: int, generation_id: str, *,
                            transcript_ref=None, digest_ref=None,
                            room_watermarks=None) -> dict:
        """Attach the per-generation references. Kept separate from promotion
        because a transcript path and a digest become known at different times."""
        if room_watermarks is not None and not isinstance(room_watermarks, dict):
            raise PolicyViolation("room_watermarks must be a mapping")

        def apply(data):
            prof = data["profiles"].get(profile_id)
            if prof is None:
                raise UnknownProfile(profile_id)
            self._fenced(prof, core_id, seat_epoch)
            gen = prof["generations"].get(generation_id)
            if gen is None:
                raise UnknownGeneration(f"{profile_id}/{generation_id}")
            if transcript_ref is not None:
                gen["transcript_ref"] = transcript_ref
            if digest_ref is not None:
                gen["digest_ref"] = digest_ref
            if room_watermarks is not None:
                gen["room_watermarks"].update(room_watermarks)
            return gen

        return self._mutate(apply)
