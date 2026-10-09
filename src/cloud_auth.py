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
     Under the desktop host (SUTANDO_APP_SUPPORT in the core's environment, or
     SUTANDO_PACKAGED=1 on the sidecar) the Keychain is the ONLY record
     consulted: it is the session the app is signed into, the host writes no
     file, and a leftover cloud-auth.json from another workspace or the
     Electron era can hold nothing but a stale bearer, which made the engine
     act as a different account than the app showed, and keep acting as it
     after a sign-out (user feedback P1-11). Signed out in the Keychain means
     signed out; only the metering env (3) is still honoured there.
  3. The metering env the supervisor injects for signed-in runs.

Once the desktop has stamped the running core's station for a user
(state/station-core-stamp.json `cloud_user_id`), only a credential whose
/api/me id is that user is returned; the first matching candidate in the
order above wins. If none matches, the result is empty with `refused` set
(account_changed / account_unverified) instead of acting as another account.

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
from typing import Any, Callable, Iterator

from station_stamp import read_station_stamp

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


def keychain_candidates(get: Callable[[str], str | None] | None = None, *,
                        signed_out_is_terminal: bool = False) -> Iterator[tuple[str, str]]:
    """Every (apiBase, token) the Keychain holds, in read_keychain_auth's order."""
    get = get or keychain_get  # resolved at call time, so a patched reader is honoured
    origin = resolve_cloud_origin()
    if signed_out_is_terminal and get(origin_vault_key(origin)) == SIGNED_OUT_SENTINEL:
        return
    candidates = [origin]
    if origin == DEFAULT_CLOUD_ORIGIN:
        candidates.extend(RETIRED_CLOUD_ORIGINS)
    keys = [origin_vault_key(o) for o in candidates]
    # Pre-origin-scoping installs stored a bare, unscoped key.
    keys.append("AG2_CLOUD_TOKEN")
    for key in keys:
        tok = get(key)
        if tok and tok != SIGNED_OUT_SENTINEL:
            yield origin, tok


def read_keychain_auth(get: Callable[[str], str | None] | None = None, *,
                       signed_out_is_terminal: bool = False):
    """(apiBase, token) from the Tauri host's origin-scoped Keychain session.

    No cross-origin fallback except the host's own retired-production
    carry-over, mirrored here. `get` is injectable so callers that wrap this
    (report-feedback) keep their own patch points. With
    `signed_out_is_terminal` (the desktop host), the host's sign-out marker on
    the current origin ends the lookup: the retired-origin carry-over and the
    bare pre-scoping key are older sessions, which is exactly what a sign-out
    must not fall back to.
    """
    for found in keychain_candidates(get, signed_out_is_terminal=signed_out_is_terminal):
        return found
    return None, None


def keychain_first() -> bool:
    """Under the desktop host the Keychain session is the account the app shows;
    files are legacy readers there. The core's own environment carries
    SUTANDO_APP_SUPPORT (the desktop launcher exports it to every engine process,
    and channel_env_containment keys on the same variable); SUTANDO_PACKAGED=1
    reaches only the sidecar, so it is accepted but never relied on."""
    if os.environ.get("SUTANDO_PACKAGED") == "1":
        return True
    return bool((os.environ.get("SUTANDO_APP_SUPPORT") or "").strip())


class CloudAuth(tuple):
    """(apiBase, token), unpacking like the plain pair, plus why it is empty when a
    credential was refused: `refused` is account_changed or account_unverified."""

    def __new__(cls, base, token, refused=None, stamp_user_id=None, credential_user_ids=()):
        self = super().__new__(cls, (base, token))
        self.refused = refused
        self.stamp_user_id = stamp_user_id
        self.credential_user_ids = tuple(credential_user_ids)
        return self


_USER_IDS: dict[tuple[str, str], str] = {}


def credential_user_id(base: str, token: str) -> str | None:
    """The AG2 Cloud user a credential acts as, from /api/me; None when it cannot be told."""
    key = (normalize_base(base), token)
    if key not in _USER_IDS:
        try:
            me = cloud_request(key[0], token, "GET", "/api/me", timeout=10)
        except (CloudError, OSError, ValueError):
            return None
        uid = str((me or {}).get("id") or "") if isinstance(me, dict) else ""
        if not uid:
            return None
        _USER_IDS[key] = uid
    return _USER_IDS[key]


def _file_candidates(ws: Path) -> Iterator[tuple[str, str]]:
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
                    yield normalize_base(d.get("apiBase") or ""), d["token"]
        except Exception:
            continue


def _candidates(ws: Path, keychain_auth: Callable[[], tuple] | None) -> Iterator[tuple[str, str]]:
    host = keychain_first()
    if not host:
        yield from _file_candidates(ws)
    if keychain_auth is not None:
        base, tok = keychain_auth()[:2]
        if tok:
            yield base, tok
    else:
        yield from keychain_candidates(signed_out_is_terminal=host)
    base, tok = _metering_env_auth()
    if tok:
        yield base, tok


def read_cloud_auth(ws: Path, keychain_auth: Callable[[], tuple] | None = None,
                    user_id: Callable[[str, str], str | None] | None = None) -> CloudAuth:
    """Return (apiBase, token) if signed in to Sutando Cloud, else (None, None).

    Post-M1 the record lives at ``<workspace>/state/auth/cloud-auth.json``; the
    pre-M1 root ``<workspace>/cloud-auth.json`` is probed as a 30-day reader
    fallback. Both packaged-app workspace equivalents are also probed so the
    token is found even when running from a different checkout. The Tauri
    desktop writes no auth file at all — its session lives in the Keychain,
    probed next. Falls back to the metering env the supervisor injects.
    Under the desktop host the Keychain is the only record consulted (a file
    can hold nothing but a stale bearer, wrong again after a sign-out).

    With a station stamp naming a user, a credential is returned only if
    `user_id` (default: /api/me) says it is that user; see CloudAuth.refused.
    """
    stamped = str((read_station_stamp(ws) or {}).get("cloud_user_id") or "")
    if not stamped:
        for base, tok in _candidates(ws, keychain_auth):
            return CloudAuth(base, tok)
        return CloudAuth(None, None)
    user_id = user_id or credential_user_id
    tried: set[tuple[str, str]] = set()
    others: list[str] = []
    for base, tok in _candidates(ws, keychain_auth):
        # Keyed like the id cache: one token on an unusable base must still be tried on its real one.
        key = (normalize_base(base or DEFAULT_CLOUD_ORIGIN), tok)
        if key in tried:
            continue
        tried.add(key)
        uid = user_id(key[0], tok)
        if uid == stamped:
            return CloudAuth(base, tok, stamp_user_id=stamped, credential_user_ids=(uid,))
        if uid and uid not in others:
            others.append(uid)
    if not tried:
        return CloudAuth(None, None)
    return CloudAuth(None, None, "account_changed" if others else "account_unverified", stamped, others)


def refusal_message(auth: Any) -> str | None:
    """Owner-facing words for a refused CloudAuth (see read_cloud_auth), else None."""
    refused = getattr(auth, "refused", None)
    if refused == "account_changed":
        return (f"This agent's AG2 Cloud credentials belong to {', '.join(auth.credential_user_ids)}, not the "
                f"account the desktop app started it for ({auth.stamp_user_id}): sign in again from the desktop app.")
    if refused == "account_unverified":
        return ("Verifying which AG2 Cloud account this agent's credentials belong to is temporarily "
                "unavailable, so they were not used; try again in a moment.")
    return None


def _metering_env_auth():
    """(apiBase, token) from the metering env the supervisor injects for a signed-in
    run, else (None, None)."""
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
