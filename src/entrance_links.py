#!/usr/bin/env python3
"""EntranceLink records — verified provider-identity ↔ Stand bindings (I2).

Verification is an explicit ACTION that calls the provider API and writes a
durable record under state/auth/; read surfaces (IdentityView) consume the
records and never make network calls. Credential material never enters a
record — only a sha256 fingerprint. Lifecycle per the owner ruling:
discovered → provider_verified → stand_linked → active → revoked.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LINKS_FILE = "entrance-links.json"


def links_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / "auth" / LINKS_FILE


def load_links(state_dir: str | Path) -> list:
    try:
        data = json.loads(links_path(state_dir).read_text())
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _load_links_for_mutation(state_dir: str | Path) -> list:
    # A mutation over a corrupt store would rebuild it empty, silently
    # destroying every prior binding record — refuse instead.
    path = links_path(state_dir)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
    except ValueError as e:
        raise ValueError(
            f"entrance-links store unreadable ({e}) — refusing to mutate; "
            "repair or move the file first") from e
    if not isinstance(data, list):
        raise ValueError(
            "entrance-links store is not a list — refusing to mutate; "
            "repair or move the file first")
    return data


def active_link(state_dir: str | Path, provider: str) -> "dict | None":
    for link in load_links(state_dir):
        if link.get("provider") == provider and link.get("status") == "active":
            return link
    return None


def _save_links(state_dir: str | Path, links: list) -> None:
    path = links_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    with os.fdopen(fd, "w") as f:
        json.dump(links, f, indent=1)
    os.replace(tmp, path)


def _fingerprint(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()[:16]


def _enrolled_stand_id(state_dir: str | Path) -> "str | None":
    try:
        rec = json.loads((Path(state_dir) / "auth" / "ag2space.json").read_text())
        return (rec.get("agent_id") or "").strip() or None
    except (OSError, ValueError):
        return None


def require_resolved_identity(state_dir: str | Path) -> str:
    """Identity mutations fail closed: an unresolved (placeholder) identity
    may read and diagnose but never create or change a binding."""
    sid = _enrolled_stand_id(state_dir)
    if not sid:
        raise PermissionError(
            "identity not resolved (no enrolled record) — binding mutations "
            "are refused; enroll first")
    return sid


def upsert_link(state_dir: str | Path, provider: str, provider_subject: dict,
                verification: dict, credential_fingerprint: str,
                display: "dict | None" = None) -> dict:
    # UNIQUE(provider, canonical subject): an active link for the provider is
    # replaced only by the SAME subject; a different subject must be explicit.
    stand_id = require_resolved_identity(state_dir)
    links = _load_links_for_mutation(state_dir)
    existing = [l for l in links
                if l.get("provider") == provider and l.get("status") == "active"]
    for l in existing:
        if l.get("provider_subject") != provider_subject:
            raise ValueError(
                f"active {provider} link exists with a different subject "
                f"({l.get('provider_subject')}) — revoke it explicitly first")
    link = existing[0] if existing else {
        "link_id": "link_" + secrets.token_hex(8),
        "provider": provider,
        "provider_subject": provider_subject,
        "status": "active",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    link["stand_id"] = stand_id
    if display:
        # display fields are labels for humans — never binding keys
        link["display"] = display
    link["verification"] = verification
    link["credential"] = {"kind": "bot_token", "status": "verified",
                          "fingerprint": credential_fingerprint}
    if not existing:
        links.append(link)
    _save_links(state_dir, links)
    return link


def authorize_link(state_dir: str | Path, provider: str,
                   authorized_by: str,
                   confirmation_ref: "str | None" = None) -> dict:
    """Explicit owner authorization: the act that turns a verified link into
    an active Stand binding. Never called automatically."""
    stand_id = require_resolved_identity(state_dir)
    links = _load_links_for_mutation(state_dir)
    for lk in links:
        if lk.get("provider") != provider or lk.get("status") != "active":
            continue
        if lk.get("stand_id") and lk["stand_id"] != stand_id:
            raise ValueError(
                f"link belongs to Stand {lk['stand_id']}, not {stand_id} — "
                "cross-Stand authorization refused")
        prev = {k: lk.get(k) for k in ("authorized_by", "authorized_at")}
        lk["authorized_by"] = authorized_by
        lk["authorized_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        if confirmation_ref:
            lk["confirmation_ref"] = confirmation_ref
        lk.setdefault("audit", []).append(
            {"op": "authorize", "at": lk["authorized_at"],
             "by": authorized_by, "prev": prev,
             "confirmation_ref": confirmation_ref})
        _save_links(state_dir, links)
        return lk
    raise ValueError(f"no active-eligible {provider} link to authorize")


def revoke_link(state_dir: str | Path, provider: str, revoked_by: str,
                reason: "str | None" = None) -> dict:
    """Revocation is layered: kills THIS binding only, never the Stand."""
    require_resolved_identity(state_dir)
    links = _load_links_for_mutation(state_dir)
    for lk in links:
        if lk.get("provider") == provider and lk.get("status") == "active":
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            lk["status"] = "revoked"
            lk["revocation"] = {"revoked_by": revoked_by, "revoked_at": now,
                                "reason": reason}
            lk.pop("authorized_by", None)
            lk.setdefault("audit", []).append(
                {"op": "revoke", "at": now, "by": revoked_by,
                 "reason": reason})
            _save_links(state_dir, links)
            return lk
    raise ValueError(f"no active {provider} link to revoke")


def verify_discord(state_dir: str | Path, token: str) -> dict:
    req = urllib.request.Request(
        "https://discord.com/api/v10/users/@me",
        headers={"Authorization": f"Bot {token}",
                 "User-Agent": "sutando-entrance-verify (https://ag2.ai, v0)"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        me = json.loads(resp.read().decode())
    subject = {"type": "bot_user", "id": str(me["id"])}
    display = {}
    name = me.get("global_name") or me.get("username")
    if name:
        display["name"] = name
    if me.get("avatar"):
        display["avatar_url"] = (f"https://cdn.discordapp.com/avatars/"
                                 f"{me['id']}/{me['avatar']}.png")
    verification = {
        "method": "discord_token_introspection",
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return upsert_link(state_dir, "discord", subject, verification,
                       _fingerprint(token), display=display or None)
