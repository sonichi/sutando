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

from state_records import read_beat, read_record

from agents_view import ALIVE_MAX_AGE_S


class IdentityView:
    def __init__(self, state_dir: str | Path, actor_id: str,
                 channels_dir: str | Path | None = None,
                 host_label: str | None = None,
                 instance: str | None = None):
        self.state_dir = Path(state_dir)
        self.actor_id = actor_id
        self.channels_dir = Path(channels_dir) if channels_dir else None
        self.host_label = host_label
        self.instance = instance or "default"

    # ── sutando.info ────────────────────────────────────────────────────────
    def info(self) -> dict:
        # Identity only — runtime internals live on runtime.details. The
        # daemon-resolved instanceId says WHICH installation is answering.
        out = {"agentId": self.actor_id, "instanceId": self.instance}
        if self.host_label:
            out["hostLabel"] = self.host_label
        beat = self._own_beat()
        if beat.get("locality") is not None:
            out["locality"] = beat["locality"]
        return out

    # ── sutando.status ──────────────────────────────────────────────────────
    def status(self) -> dict:
        out: dict = {}
        payload = read_record(self.state_dir / "core-status.json")
        if payload is None:
            out["status"] = "unknown"
        else:
            for k in ("status", "step", "ts"):
                if payload.get(k) is not None:
                    out[k] = payload[k]
        beat = self._own_beat()
        if "beatAgeS" in beat:
            out["alive"] = 0 <= beat["beatAgeS"] < ALIVE_MAX_AGE_S
            out["beatAgeS"] = beat["beatAgeS"]
        return out

    # ── sutando.stand (Stand Card) ──────────────────────────────────────────
    def stand_card(self, details: bool = False) -> dict:
        # One complete safe summary; owners[] vs owner_evidence[] never mix.
        rec = self._stand_record()
        enrolled = self._enrolled()
        stand: dict = {}
        if enrolled.get("agent_id"):
            stand["stand_id"] = enrolled["agent_id"]
        for k in ("display_name", "status"):
            if rec.get(k):
                stand[k] = rec[k]
        owners = []
        for o in (rec.get("owners") or []):
            if isinstance(o, dict) and o.get("person_id"):
                row = {k: o[k] for k in ("person_id", "display_name", "role")
                       if o.get(k)}
                row["verification"] = "explicit_owner_binding"
                owners.append(row)
        evidence = [{"provider": name, "subject": acc["tofuOwner"]}
                    for name, acc in self._channels() if acc.get("tofuOwner")]
        # No instances: heartbeat rows are incarnation/runtime data, not
        # identity; Installations return when a real record exists (S1+).
        card = {"stand": stand, "owners": owners, "owner_evidence": evidence,
                "channels": self.entrances(details)["channels"],
                "devices": self._devices(details)}
        return card

    def _devices(self, details: bool = False) -> list:
        # Paired-peer registry — deliberately parallel to state/auth/
        # device.json (THIS host's own install identity); never merged.
        out = []
        ddir = self.state_dir / "auth" / "devices"
        if not ddir.is_dir():
            return out
        for f in sorted(ddir.glob("*.json")):
            try:
                rec = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            row = {k: rec[k] for k in ("device_id", "label", "device_type")
                   if rec.get(k) not in (None, "", "None")}
            # a credential-backed pairing record (hashed token, owner-minted)
            # is a formal enrollment; anything else is just configuration
            row["status"] = ("enrolled" if rec.get("token_sha256")
                             else "configured_unverified")
            if details and rec.get("granted_methods"):
                row["granted_methods"] = rec["granted_methods"]
            if rec.get("last_seen_at") not in (None, "None"):
                row["last_seen_at"] = rec["last_seen_at"]
            out.append(row)
        return out

    # ── sutando.resolve ─────────────────────────────────────────────────────
    def resolve(self, provider: str, subject: str) -> dict:
        # Reverse index over EntranceLink records. Ambiguity is a loud,
        # structured error — never auto-pick between Stands.
        sid = subject.rsplit(":", 1)[-1].strip()
        links, store_corrupt = self._links_store()
        if store_corrupt:
            # fail loud: "unresolved because unreadable" is a different claim
            # from "no such binding" and callers must not conflate them
            return {"resolved": False, "store_corrupt": True,
                    "provider": provider, "subject": subject}
        hits, unauthorized = [], 0
        for lk in links:
            if lk.get("provider") != provider or lk.get("status") != "active":
                continue
            subj = lk.get("provider_subject") or {}
            if str(subj.get("id", "")) != sid:
                continue
            # resolve() is an AUTHORITY read: provider verification alone
            # never answers "this subject IS this Stand" — authorization does
            if lk.get("authorized_by"):
                hits.append(lk)
            else:
                unauthorized += 1
        stands = sorted({lk.get("stand_id", "") for lk in hits})
        if not hits:
            out = {"resolved": False, "provider": provider, "subject": subject}
            if unauthorized:
                out["verified_unlinked"] = True
            return out
        if len(stands) > 1:
            return {"resolved": False, "conflict": True,
                    "provider": provider, "subject": subject,
                    "candidates": [
                        {"stand_id": lk.get("stand_id"),
                         "link_id": lk.get("link_id"),
                         "verification": lk.get("verification")}
                        for lk in hits]}
        lk = hits[0]
        return {"resolved": True, "stand_id": lk.get("stand_id"),
                "link": {"link_id": lk.get("link_id"),
                         "provider": provider,
                         "provider_subject": lk.get("provider_subject"),
                         "display": lk.get("display"),
                         "status": lk.get("status"),
                         "verification": lk.get("verification")}}

    def _links_store(self) -> "tuple[list, bool]":
        # (links, corrupt): an absent store is empty, but a present-yet-
        # unparseable one must surface as corruption, never as "no links".
        path = self.state_dir / "auth" / "entrance-links.json"
        if not path.is_file():
            return [], False
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return [], True
        return (data, False) if isinstance(data, list) else ([], True)

    # ── sutando.entrances ───────────────────────────────────────────────────
    def entrances(self, details: bool = False) -> dict:
        # I1 evidence projection: folder facts only, no provider calls and
        # no credential reads — nothing may claim more than unverified.
        out = []
        if self.channels_dir is not None and self.channels_dir.is_dir():
            for d in sorted(self.channels_dir.iterdir()):
                if not d.is_dir():
                    continue
                out.append(self._entrance(d, details))
        return {"channels": out}

    def _entrance(self, d: Path, details: bool = False) -> dict:
        ent: dict = {"provider": d.name}
        links, store_corrupt = self._links_store()
        link = next((lk for lk in links
                     if lk.get("provider") == d.name
                     and lk.get("status") == "active"), None)
        evidence: dict = {}
        env = d / ".env"
        if env.is_file():
            evidence["credential_present"] = True
        acc = d / "access.json"
        policy_invalid = False
        if acc.is_file():
            try:
                payload = json.loads(acc.read_text())
                evidence["policy_present"] = True
                if isinstance(payload, dict) and payload.get("tofuOwner"):
                    evidence["owner_id"] = payload["tofuOwner"]
            except (OSError, ValueError):
                policy_invalid = True
        if d.name == "ag2space":
            enrolled = self._enrolled()
            if enrolled.get("agent_id"):
                evidence["subject_evidence"] = enrolled["agent_id"]
        if store_corrupt:
            # a broken links store must not read as "no binding" — that would
            # silently demote an authorized channel to configured_unverified
            ent["status"] = "policy_invalid"
            ent["policy_error"] = "entrance-links store unreadable"
        elif policy_invalid:
            ent["status"] = "policy_invalid"
        elif link:
            # introspection proves credential->subject; ONLY an explicit
            # owner authorization on the link makes the Stand binding active
            authorized = bool(link.get("authorized_by"))
            ent["status"] = "active" if authorized else "verified_unlinked"
            ent["identity"] = link.get("provider_subject")
            if link.get("display"):
                ent["display"] = link["display"]
            ent["verification"] = link.get("verification")
            ent["stand_binding"] = ("authorized" if authorized else "absent")
            if authorized:
                ent["authorized_by"] = link["authorized_by"]
            if details and link.get("credential"):
                ent["credential"] = link["credential"]
        elif evidence:
            ent["status"] = "configured_unverified"
        else:
            ent["status"] = "not_configured"
        if evidence:
            ent["evidence"] = evidence
        if details:
            ent["storage"] = {"type": "channel_directory", "directory": str(d)}
        return ent

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
    def _stand_record(self) -> dict:
        try:
            # AUTHORITY record (owner-confirmed OwnerBinding) — deliberately
            # parallel to hosts/<h>/stand-identity.json (persona); never merged.
            rec = json.loads((self.state_dir / "auth" / "stand.json").read_text())
            return rec if isinstance(rec, dict) else {}
        except (OSError, ValueError):
            return {}

    def _enrolled(self) -> dict:
        try:
            rec = json.loads((self.state_dir / "auth" / "ag2space.json").read_text())
            return rec if isinstance(rec, dict) else {}
        except (OSError, ValueError):
            return {}

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
        return read_beat(self.state_dir / "cores" / f"{self.host_label}.alive")
