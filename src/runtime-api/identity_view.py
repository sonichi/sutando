#!/usr/bin/env python3
"""Read-only identity surface for THIS agent (the Sutando Server "smallest
slice"): sutando.info / sutando.status / sutando.owner / sutando.allowlist.

Sources are the workspace's existing records — nothing is invented here:
  info      daemon-resolved actor id + self descriptors handed in by the server
  status    state/core-status.json ({status, step, ts}) + own heartbeat age
  owner     explicit ownership metadata per channel access.json (tofuOwner,
            tierMap entries of tier "owner") — absent fields stay absent,
            an allowlist entry is NOT assumed to be the owner
  allowlist channels/<source>/access.json allowFrom, verbatim

Everything is dependency-injected (state dir, channels dir, identity fields)
so the dispatcher stays path-free and tests compose tmp dirs.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from agents_view import ALIVE_MAX_AGE_S


class IdentityView:
    def __init__(self, state_dir: str | Path, actor_id: str,
                 channels_dir: str | Path | None = None,
                 host_label: str | None = None):
        self.state_dir = Path(state_dir)
        self.actor_id = actor_id
        self.channels_dir = Path(channels_dir) if channels_dir else None
        self.host_label = host_label

    # ── sutando.info ────────────────────────────────────────────────────────
    def info(self) -> dict:
        out = {"agentId": self.actor_id}
        if self.host_label:
            out["hostLabel"] = self.host_label
        beat = self._own_beat()
        for k in ("pid", "started_at", "locality", "socket"):
            if beat.get(k) is not None:
                out[k] = beat[k]
        return out

    # ── sutando.status ──────────────────────────────────────────────────────
    def status(self) -> dict:
        out: dict = {}
        cs = self.state_dir / "core-status.json"
        try:
            payload = json.loads(cs.read_text())
            for k in ("status", "step", "ts"):
                if payload.get(k) is not None:
                    out[k] = payload[k]
        except (OSError, ValueError):
            out["status"] = "unknown"
        beat = self._own_beat()
        if "beatAgeS" in beat:
            out["alive"] = beat["beatAgeS"] < ALIVE_MAX_AGE_S
            out["beatAgeS"] = beat["beatAgeS"]
        return out

    # ── sutando.owner ───────────────────────────────────────────────────────
    def owner(self) -> dict:
        owners: dict = {}
        for name, acc in self._channels():
            entry: dict = {}
            if acc.get("tofuOwner"):
                entry["tofuOwner"] = acc["tofuOwner"]
            tier_owners = [uid for uid, tier in (acc.get("tierMap") or {}).items()
                           if tier == "owner"]
            if tier_owners:
                entry["tierOwners"] = tier_owners
            if entry:
                owners[name] = entry
        return {"owners": owners}

    # ── sutando.allowlist ───────────────────────────────────────────────────
    def allowlist(self) -> dict:
        channels: dict = {}
        for name, acc in self._channels():
            if isinstance(acc.get("allowFrom"), list):
                channels[name] = acc["allowFrom"]
        return {"channels": channels}

    # ── internals ───────────────────────────────────────────────────────────
    def _channels(self):
        if self.channels_dir is None or not self.channels_dir.is_dir():
            return
        for d in sorted(self.channels_dir.iterdir()):
            f = d / "access.json"
            if not f.is_file():
                continue
            try:
                yield d.name, json.loads(f.read_text())
            except (OSError, ValueError):
                continue  # unreadable channel config ≠ broken identity surface

    def _own_beat(self) -> dict:
        if not self.host_label:
            return {}
        f = self.state_dir / "cores" / f"{self.host_label}.alive"
        try:
            age = time.time() - f.stat().st_mtime
            payload = json.loads(f.read_text())
        except (OSError, ValueError):
            return {}
        return {**payload, "beatAgeS": round(age, 1)}
