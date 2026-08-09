"""device_store.py — per-device SCP credentials + pairing (opaque-bearer v0).

The dependency-light owner of the network-auth state the WSS transport enforces.
Two record kinds under <state>/auth/:

  pairing/<pairing_id>.json  one-time pairing tokens minted by the owner
  devices/<device_id>.json   long-term per-device credentials

Security: bearer tokens are stored **hashed** (sha256) — the plaintext is
returned exactly once (to the owner at mint, to the device at redeem) and never
written to disk. Each device credential carries its OWN granted-method set, so
authorization is per-device (a phone may submit tasks but not restart), replacing
the transport-wide read-only allowlist once a device is paired. Revoking a device
is deleting its file.

This module is transport-agnostic: it validates tokens and answers "who is this
and what may they call"; the WSS transport injects the resolved directory and
does the network I/O.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import time
from pathlib import Path

# A freshly-paired device's default grants: the read surface + submit/cancel its
# own work. Deliberately NOT terminal.input / sutando.stop / restart — those stay
# owner-local (UDS) until explicitly granted. The owner can widen per device.
DEFAULT_DEVICE_GRANTS = (
    "sutando.info", "sutando.status", "sutando.owner", "sutando.allowlist",
    "runtime.health", "runtime.details",
    "agent.list", "agent.status",
    "task.submit", "task.status", "task.get_result", "task.list",
    "task.list_results", "task.details", "task.cancel", "task.subscribe",
    "request.get", "request.wait", "request.list", "human_action.status",
)


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class DeviceStore:
    def __init__(self, auth_dir: str | Path):
        self.auth_dir = Path(auth_dir)
        self.pairing_dir = self.auth_dir / "pairing"
        self.devices_dir = self.auth_dir / "devices"
        for d in (self.auth_dir, self.pairing_dir, self.devices_dir):
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)

    # ── pairing (owner mints; device redeems) ────────────────────────────────
    def mint_pairing(self, label: str, grants=None, ttl_s: int = 600) -> str:
        """Owner-run: create a one-time pairing token. Returns the PLAINTEXT
        token (shown once — it is the QR payload); only its hash is stored."""
        token = secrets.token_urlsafe(24)
        pairing_id = secrets.token_hex(8)
        rec = {"pairing_id": pairing_id, "token_sha256": _sha256(token),
               "label": label,
               "granted_methods": list(grants or DEFAULT_DEVICE_GRANTS),
               "created_at": time.time(), "expires_at": time.time() + ttl_s,
               "used": False}
        self._write(self.pairing_dir / f"{pairing_id}.json", rec)
        return token

    def redeem_pairing(self, token: str, *, label: str | None = None,
                       capabilities=None) -> dict | None:
        """Device-run (once): exchange a valid pairing token for a long-term
        device credential. Returns {device_id, credential, granted_methods} with
        the PLAINTEXT credential (shown once), or None if the token is unknown/
        expired/used. Single-use: the pairing token is burned on success."""
        h = _sha256(token)
        now = time.time()
        for f in self.pairing_dir.glob("*.json"):
            try:
                rec = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if not hmac.compare_digest(rec.get("token_sha256", ""), h):
                continue
            if rec.get("used") or now > rec.get("expires_at", 0):
                return None
            device_id = secrets.token_hex(8)
            credential = secrets.token_urlsafe(24)
            drec = {"device_id": device_id, "token_sha256": _sha256(credential),
                    "label": label or rec.get("label") or device_id,
                    "granted_methods": rec.get("granted_methods",
                                               list(DEFAULT_DEVICE_GRANTS)),
                    "capabilities": list(capabilities or []),
                    "created_at": now, "last_seen_at": now}
            self._write(self.devices_dir / f"{device_id}.json", drec)
            rec["used"] = True
            rec["used_at"] = now
            rec["device_id"] = device_id
            self._write(f, rec)
            return {"device_id": device_id, "credential": credential,
                    "granted_methods": drec["granted_methods"]}
        return None

    # ── authentication (WSS connect) ─────────────────────────────────────────
    def authenticate(self, token: str) -> dict | None:
        """Resolve a presented device bearer to its record (with granted_methods),
        or None. Constant-time compare. Does not match pairing tokens — those go
        through pending_pairing()."""
        h = _sha256(token)
        for f in self.devices_dir.glob("*.json"):
            try:
                rec = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if hmac.compare_digest(rec.get("token_sha256", ""), h):
                return rec
        return None

    def pending_pairing(self, token: str) -> dict | None:
        """True (record) if the token is a still-valid, unused pairing token —
        the WSS transport uses this to offer pair.redeem on the connection."""
        h = _sha256(token)
        now = time.time()
        for f in self.pairing_dir.glob("*.json"):
            try:
                rec = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if (hmac.compare_digest(rec.get("token_sha256", ""), h)
                    and not rec.get("used")
                    and now <= rec.get("expires_at", 0)):
                return rec
        return None

    def record_hello(self, device_id: str, device_type: str | None,
                     capabilities) -> dict | None:
        """client.hello: record what a connected device SAYS it can do right now
        (its LIVE capabilities + form factor) onto its record. This is
        descriptive self-report, distinct from the credential's granted_methods
        (the DURABLE authorization) — advertising a capability never widens
        authz. Returns the updated record, or None if the device is unknown."""
        f = self.devices_dir / f"{device_id}.json"
        try:
            rec = json.loads(f.read_text())
        except (OSError, ValueError):
            return None
        if device_type is not None:
            rec["device_type"] = device_type
        if capabilities is not None:
            rec["capabilities"] = list(capabilities)
        rec["last_seen_at"] = time.time()
        self._write(f, rec)
        return rec

    def list_devices(self) -> list:
        out = []
        for f in sorted(self.devices_dir.glob("*.json")):
            try:
                rec = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            out.append({k: rec[k] for k in
                        ("device_id", "label", "granted_methods",
                         "capabilities", "created_at", "last_seen_at")
                        if k in rec})
        return out

    def revoke(self, device_id: str) -> bool:
        f = self.devices_dir / f"{device_id}.json"
        if f.exists():
            f.unlink()
            return True
        return False

    def _write(self, path: Path, rec: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec))
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600 — carries a token hash
        os.replace(tmp, path)
