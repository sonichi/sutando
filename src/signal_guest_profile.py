"""Guest research profile — the isolated, authenticated config the guest ``deep_dive``
worker runs under, provisioned and owned INSIDE Sutando.

Why this lives here and not in the desktop supervisor (design
``design-signal-room-core-capability.md``, "Sutando is a black box behind the gateway
API contract"): which engine executes a guest task, and what auth/config that engine
needs, are Sutando-internal facts. The desktop supervises the ``agent-api`` PROCESS and
consumes the contract (``POST /task`` guest, ``GET /result``, ``GET /capabilities``);
it never reaches into this layout.

What the profile is:
  * an isolated ``CLAUDE_CONFIG_DIR`` (0700) holding ONLY what the CLI needs to
    authenticate — a ``.claude.json`` **reconstructed from an allowlist** of account
    fields, never a wholesale copy of the owner's (which carries MCP servers, project
    history, hooks, plugins and settings that would undo the containment), plus a copy
    of the file-based credential when the owner uses that store;
  * ``ensure_guest_profile()`` — idempotent, single-flight, compare-and-replace, and
    the thing ``/capabilities`` calls BEFORE reporting availability, so a stock install
    converges to available without a task ever arriving;
  * **negative synchronization**: when the owner credential source disappears or turns
    unreadable (logout / removal / rotation to a store we cannot read), the copied
    guest credential is DELETED and readiness flips to ``worker_unauthenticated`` — a
    guest copy must never outlive the owner's own session.

Keyring-backed auth needs no copy: it is per-OS-user, so the worker sees it regardless
of the pinned ``HOME``. The account metadata is still required, hence the allowlist.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import threading
import time
from pathlib import Path

# Only these keys are carried into the guest ``.claude.json``. Everything else in the
# owner's file — mcpServers, projects, hooks, plugins, settings, history — is dropped
# by construction: an allowlist, so a NEW owner-side key can never silently leak in.
_CLAUDE_JSON_ALLOW = ("oauthAccount", "userID", "hasCompletedOnboarding")

# The credential file for the file-backed store (keyring installs simply lack it).
_CREDENTIALS_NAME = ".credentials.json"

_lock = threading.Lock()  # single-flight: /capabilities can be polled concurrently
_cache_lock = threading.Lock()  # guards the readiness cache against racing probes

# Cached readiness. A POSITIVE result is bounded so `available: true` cannot outlive a
# credential expiry by more than the TTL. A NEGATIVE result is cached only briefly:
# caching "unavailable" for five minutes would leave a freshly-logged-in owner falsely
# unavailable for that whole window.
_CACHE_TTL_S = 300.0
_NEG_CACHE_TTL_S = 10.0
# Keyed by the resolved profile path, so two workspaces cannot share a verdict.
_cache: dict[str, tuple[float, bool, str | None]] = {}


def owner_config_dir() -> Path:
    """The owner's Claude config root (``CLAUDE_CONFIG_DIR`` or ``~/.claude``)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(env) if env else Path.home() / ".claude"


def guest_home(workspace: Path | str | None = None) -> Path:
    """The isolated guest config root, under the CANONICAL workspace.

    No legacy home-relative fallback: guest credentials must live where the rest of
    this install's state lives, or a second clone would share them and workspace
    cleanup would miss them.
    """
    override = os.environ.get("SIGNAL_GUEST_CLAUDE_HOME", "").strip()
    if override:
        return Path(override)
    if workspace:
        base = Path(workspace)
    else:
        from workspace_default import resolve_workspace
        base = Path(resolve_workspace())
    return base / "state" / "signal-host" / "guest-claude-home"


def _read_owner_account() -> dict | None:
    """The allowlisted account fields from the owner's ``.claude.json``. ``None`` when
    it is absent/unreadable/not an object — treated as unauthenticated (fail-closed)."""
    src = owner_config_dir().parent / ".claude.json"
    if not src.exists():
        src = owner_config_dir() / ".claude.json"
    try:
        if src.is_symlink():  # never follow a planted link out of the owner's tree
            return None
        raw = json.loads(src.read_text())
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    picked = {k: raw[k] for k in _CLAUDE_JSON_ALLOW if k in raw}
    return picked or None


def _owner_credentials_path() -> Path | None:
    """The owner's file-backed credential, when that store is in use."""
    p = owner_config_dir() / _CREDENTIALS_NAME
    try:
        if p.is_symlink() or not p.is_file():
            return None
    except Exception:
        return None
    return p


def _write_private(path: Path, data: str) -> None:
    """0600 atomic write (tmp + rename) inside the guest home."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _replace_if_changed(path: Path, want: str) -> None:
    """Compare-and-replace, but never trust what is already there.

    A symlinked or non-regular child is REPLACED (a planted link would otherwise
    redirect a credential write), and the mode is repaired even when the content
    already matches — a content-equal file left at 0644 would still be a leak.
    """
    try:
        st = path.lstat()
    except FileNotFoundError:
        _write_private(path, want)
        return
    except Exception:
        _write_private(path, want)
        return
    if not stat.S_ISREG(st.st_mode):
        try:
            path.unlink()
        except Exception:
            pass
        _write_private(path, want)
        return
    try:
        if path.read_text() == want:
            if stat.S_IMODE(st.st_mode) != 0o600:
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # repair, do not skip
            return
    except Exception:
        pass
    _write_private(path, want)


def ensure_guest_profile(workspace: Path | str | None = None) -> tuple[bool, str | None]:
    """Idempotent, single-flight provisioning. Returns ``(ready, reason)`` where
    ``reason`` is ``None`` when ready, else a machine-readable cause.

    Called by ``/capabilities`` BEFORE it computes availability, so a stock install
    converges without any task arriving; failures are simply retried next probe.
    """
    with _lock:
        home = guest_home(workspace)
        account = _read_owner_account()
        creds = _owner_credentials_path()

        # --- negative synchronization ------------------------------------------
        # Owner credentials gone/unreadable => the guest copy must not survive.
        if account is None:
            if not _purge_credentials(home):
                # A guest credential we could not remove is worse than unavailable —
                # report the failure rather than implying the copy is gone.
                return False, "guest_profile_purge_failed"
            return False, "worker_unauthenticated"

        try:
            # mkdir(exist_ok=True) would happily accept an existing SYMLINK here and
            # write credentials through it. Require a real directory we own.
            try:
                st = home.lstat()
                if not stat.S_ISDIR(st.st_mode):
                    return False, "guest_profile_missing"
            except FileNotFoundError:
                home.mkdir(parents=True, exist_ok=False)
            os.chmod(home, stat.S_IRWXU)  # 0700
        except Exception:
            return False, "guest_profile_missing"

        try:
            _replace_if_changed(home / ".claude.json", json.dumps(account, indent=2) + "\n")
            # Settings: an explicit empty object. --setting-sources already excludes
            # user/project/local files, and --strict-mcp-config excludes MCP; this is
            # belt-and-braces so nothing in the guest root itself adds surface.
            _replace_if_changed(home / "settings.json", "{}\n")
            if creds is not None:
                want = creds.read_text()
                _replace_if_changed(home / _CREDENTIALS_NAME, want)
            else:
                # Keyring store: no copy to make, and any stale copy must go. A copy
                # we cannot delete blocks readiness (never silently kept).
                if not _purge_credentials(home):
                    return False, "guest_profile_purge_failed"
        except Exception:
            return False, "guest_profile_missing"

        return True, None


def _purge_credentials(home: Path) -> bool:
    """Delete a copied guest credential (negative sync). Returns True when the file is
    gone afterwards — a credential we cannot remove must fail readiness, not pass
    silently."""
    target = home / _CREDENTIALS_NAME
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return True
    except Exception:
        return not target.exists()


def invalidate_readiness_cache() -> None:
    """Drop the cached readiness — called when a worker run reports an auth failure so
    ``available: true`` cannot persist past a credential expiry."""
    with _cache_lock:
        _cache.clear()


def guest_profile_ready(workspace: Path | str | None = None, *, now: float | None = None
                        ) -> tuple[bool, str | None]:
    """Cached ``ensure_guest_profile``, sized for the capability probe's budget.

    Positive verdicts hold for ``_CACHE_TTL_S`` (bounded staleness against an expiry);
    negative ones for only ``_NEG_CACHE_TTL_S``, so a login is reflected promptly.
    """
    stamp = time.monotonic() if now is None else now
    key = str(guest_home(workspace))
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            age = stamp - cached[0]
            ttl = _CACHE_TTL_S if cached[1] else _NEG_CACHE_TTL_S
            if age < ttl:
                return cached[1], cached[2]
    ok, reason = ensure_guest_profile(workspace)
    with _cache_lock:
        _cache[key] = (stamp, ok, reason)
    return ok, reason


def guest_env_overrides(home: Path) -> dict:
    """The config/home pins the worker spawn adds to its allowlisted env."""
    return {"CLAUDE_CONFIG_DIR": str(home), "HOME": str(home)}
