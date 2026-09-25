"""Sutando Cloud session: find the owner's sutk_ bearer and call the cloud API.

The single owner of "how does engine code authenticate to Sutando Cloud"
(sutando.ag2.space). Before this module the lookup lived inside
skills/report-feedback/report-feedback.py; the marketplace skill needed the
same chain, and a second copy of an FNV-keyed Keychain derivation is exactly
the kind of drift that silently orphans every stored session. report-feedback
now delegates here.

Lookup order (read_cloud_auth):
  1. cloud-auth.json records ({apiBase, token}) — workspace, packaged-app
     workspace, legacy Electron location.
  2. The desktop host's Keychain session. The Tauri host stores the sutk_ ONLY
     there, under a key bound to the origin it was minted against
     (cloud_session.rs origin_key_suffix — mirrored byte-for-byte below).
  3. The metering env the supervisor injects for signed-in runs.

cloud_request() is the one HTTP path: https only, host allowlisted, bearer
never sent anywhere else, errors surfaced as CloudError(status, code, detail)
so callers branch on the server's `error` string rather than on prose.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

# Hosts the bearer may be sent to. Anything else aborts rather than forwarding
# the owner's token.
TRUSTED_API_HOSTS = frozenset({"sutando.ag2.ai", "sutando.ag2.space"})

DEFAULT_CLOUD_ORIGIN = "https://sutando.ag2.space"
# sutando.ag2.ai 307s to .space and clients drop Authorization across the
# cross-origin redirect, so a bearer sent there reads back as a bogus 401.
RETIRED_CLOUD_ORIGINS = ("https://sutando.ag2.ai",)
# What the desktop host overwrites the Keychain token with on sign-out (its
# vault CLI has no delete verb); must read as "not signed in".
SIGNED_OUT_SENTINEL = "__signed_out__"

REQUEST_TIMEOUT_S = 30


def normalize_base(base: str) -> str:
    """A retired production origin IS the current one — never send a bearer to
    it (the 307 to the new host drops Authorization → a misleading 401)."""
    base = (base or "").strip().rstrip("/")
    if base in RETIRED_CLOUD_ORIGINS or not base:
        return DEFAULT_CLOUD_ORIGIN
    return base


def fnv1a64(s: str) -> int:
    """FNV-1a 64-bit, byte-for-byte the desktop host's (cloud_session.rs)."""
    h = 0xCBF29CE484222325
    for b in s.encode():
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def origin_vault_key(origin: str) -> str:
    """Origin-scoped Keychain key, matching cloud_session.rs origin_key_suffix."""
    slug = "".join(c.upper() if (c.isascii() and c.isalnum()) else "_" for c in origin)
    return f"AG2_CLOUD_TOKEN_{slug}_{fnv1a64(origin):016X}"


def resolve_cloud_origin() -> str:
    """The env override, with a retired production origin read as the current one."""
    return normalize_base(os.environ.get("AG2_CLOUD_ORIGIN", ""))


def keychain_get(key: str) -> str | None:
    """Read one Keychain secret the way the engine vault does; None if absent."""
    if sys.platform != "darwin":
        return None
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-a", "sutando", "-s", key, "-w"],
            capture_output=True,
            timeout=10,
        )
        if r.returncode != 0:
            return None
        return r.stdout.decode().strip() or None
    except Exception:
        return None


def read_keychain_auth(get: Callable[[str], str | None] = keychain_get):
    """(apiBase, token) from the Tauri host's origin-scoped Keychain session.

    No cross-origin fallback except the host's own retired-production
    carry-over, mirrored here. `get` is injectable so callers that wrap this
    (report-feedback) keep their own patch points.
    """
    origin = resolve_cloud_origin()
    candidates = [origin]
    if origin == DEFAULT_CLOUD_ORIGIN:
        candidates.extend(RETIRED_CLOUD_ORIGINS)
    for o in candidates:
        tok = get(origin_vault_key(o))
        if tok and tok != SIGNED_OUT_SENTINEL:
            return origin, tok
    # Pre-origin-scoping installs stored a bare, unscoped key.
    tok = get("AG2_CLOUD_TOKEN")
    if tok and tok != SIGNED_OUT_SENTINEL:
        return origin, tok
    return None, None


def read_cloud_auth(ws: Path, keychain_auth: Callable[[], tuple] | None = None):
    """Return (apiBase, token) if signed in to Sutando Cloud, else (None, None).

    Post-M1 the record lives at ``<workspace>/state/auth/cloud-auth.json``; the
    pre-M1 root ``<workspace>/cloud-auth.json`` is probed as a 30-day reader
    fallback. Both packaged-app workspace equivalents are also probed so the
    token is found even when running from a different checkout. The Tauri
    desktop writes no auth file at all — its session lives in the Keychain,
    probed next. Falls back to the metering env the supervisor injects.
    """
    seen: set[str] = set()
    _app_ws = Path.home() / ".sutando" / "repo" / "workspace"
    for p in (
        ws / "state" / "auth" / "cloud-auth.json",
        ws / "cloud-auth.json",
        _app_ws / "state" / "auth" / "cloud-auth.json",
        _app_ws / "cloud-auth.json",
        Path.home() / "Library" / "Application Support" / "@stando" / "ui" / "cloud-auth.json",
    ):
        rp = str(p)
        if rp in seen:
            continue
        seen.add(rp)
        try:
            if p.exists():
                d = json.loads(p.read_text())
                if d.get("token"):  # signed in == has token (matches desktop)
                    return normalize_base(d.get("apiBase") or ""), d["token"]
        except Exception:
            continue

    base, tok = (keychain_auth or read_keychain_auth)()
    if tok:
        return base, tok

    hdrs = os.environ.get("SUTANDO_METERING_HEADERS")
    if hdrs:
        try:
            auth = json.loads(hdrs).get("Authorization", "")
            tok = auth.split(" ", 1)[1] if auth.lower().startswith("bearer ") else auth
            base = os.environ.get("SUTANDO_METERING_ENDPOINT", "").replace("/api/usage/v2", "")
            if tok:
                return normalize_base(base), tok
        except Exception:
            pass
    return None, None


class CloudError(Exception):
    """A cloud call that did not succeed. `code` is the server's JSON `error`
    string when it sent one (e.g. tier_required, insufficient_credits), else a
    local reason (not_signed_in, untrusted_host, network)."""

    def __init__(self, status: int, code: str, detail: str = "", body: Any = None) -> None:
        super().__init__(f"{status} {code}: {detail}".strip())
        self.status = status
        self.code = code
        self.detail = detail
        self.body = body if isinstance(body, dict) else {}


def check_trusted_base(base: str, insecure_hosts: frozenset[str] = frozenset()) -> None:
    """Refuse to send a bearer to anything but an allowlisted https origin."""
    parsed = urllib.parse.urlsplit(base)
    host = parsed.hostname or ""
    if parsed.username or parsed.password:
        raise CloudError(0, "untrusted_host", f"refusing userinfo in {base!r}")
    if host in insecure_hosts:
        return
    if parsed.scheme != "https" or host not in TRUSTED_API_HOSTS:
        raise CloudError(0, "untrusted_host", f"refusing to send credentials to {base!r}")


def cloud_request(
    base: str,
    token: str | None,
    method: str,
    path: str,
    body: Any = None,
    *,
    insecure_hosts: frozenset[str] = frozenset(),
    timeout: float = REQUEST_TIMEOUT_S,
) -> Any:
    """One JSON call to the cloud API; returns the decoded body (or None).

    Redirects are not followed with the bearer: urllib's default handler would
    replay Authorization to wherever a 30x points, so a redirect surfaces as a
    CloudError instead.
    """
    if not path.startswith("/api/"):
        raise ValueError(f"cloud path must start with /api/: {path!r}")
    base = normalize_base(base)
    check_trusted_base(base, insecure_hosts)
    headers = {"Accept": "application/json", "User-Agent": "sutando-engine"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:
            pass
        parsed = _decode(raw)
        code = parsed.get("error") if isinstance(parsed, dict) else None
        detail = parsed.get("detail", "") if isinstance(parsed, dict) else ""
        raise CloudError(exc.code, str(code or f"http_{exc.code}"), str(detail or ""), parsed) from None
    except urllib.error.URLError as exc:
        raise CloudError(0, "network", str(exc.reason)) from None
    except TimeoutError:
        raise CloudError(0, "network", "request timed out") from None
    return _decode(raw)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return {"detail": raw[:300].decode("utf-8", "ignore")}
