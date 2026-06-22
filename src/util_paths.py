"""Resolve personal-asset paths with private-dir-first lookup.

Each Stand has its own identity + avatar. These files are gitignored and
machine-local. Canonical home is `$SUTANDO_MEMORY_DIR/machine-<hostname>/`
so they live with the rest of the per-machine memory under the private
sync repo. Public-workspace fallback is preserved so existing installs
keep working until they migrate.

The env var `SUTANDO_MEMORY_DIR` is the canonical name per the 2026-05-18
workspace-design RFC (#858, Decision 2). The legacy name `SUTANDO_PRIVATE_DIR`
is honored as a fallback for one release with a deprecation warning on
every read (cron environments miss startup-only warnings, so logging at
every resolution is intentional).

Usage:
    from util_paths import personal_path
    si = personal_path("stand-identity.json")
    avatar = personal_path("stand-avatar.png")  # also tries assets/ in public
"""
from __future__ import annotations
import os
import socket
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent


def _memory_dir_env() -> str | None:
    """Return the resolved memory-dir env value, preferring the new name.

    Lookup order:
      1. `SUTANDO_MEMORY_DIR` (canonical post-#858 / #870)
      2. `SUTANDO_PRIVATE_DIR` (legacy, with deprecation warning emitted
         to stderr on every read — not just once at startup; cron and
         launchd environments miss startup-only warnings).

    Returns the raw env value (caller must `os.path.expanduser` if needed),
    or None when neither is set."""
    new = os.environ.get("SUTANDO_MEMORY_DIR")
    if new:
        return new
    legacy = os.environ.get("SUTANDO_PRIVATE_DIR")
    if legacy:
        # Every-read deprecation warning. This is loud by design — the
        # legacy alias will drop in the next release and silent users
        # would otherwise miss the cutover. See #870 for the rename plan.
        print(
            "[util_paths.py] DEPRECATION: SUTANDO_PRIVATE_DIR is the old name "
            "for the memory dir; set SUTANDO_MEMORY_DIR instead (this alias "
            "will be removed in the next release). See #870.",
            file=sys.stderr,
        )
        return legacy
    return None


def _workspace_root() -> Path:
    """Workspace root for runtime-state paths.

    Per the workspace contract (docs/workspace-contract.md): REPO_DIR is
    SOURCE-TREE-ONLY (exec'ing source files, git cwd, reading checked-in
    files). All user/runtime paths go through the workspace. Delegates to
    workspace_default.resolve_workspace() so the post-v0.8 canonical
    default (<repo>/workspace/) and PR #762's one-time legacy migration
    are honored in one call. ($SUTANDO_WORKSPACE is no longer honored
    for resolution per #1440; see `src/sutando_config.py`.)

    `migrate=False` — path resolution shouldn't trigger migrations on
    every call. Migration runs from src/startup.sh and the bridge boot
    paths where it belongs.
    """
    try:
        from workspace_default import resolve_workspace
        return resolve_workspace(migrate=False)
    except ImportError:
        # Inline fallback. NEVER REPO_DIR — that's source-tree, not workspace.
        env = os.environ.get("SUTANDO_WORKSPACE")
        if env:
            return Path(os.path.expanduser(env))
        return Path.home() / ".sutando" / "workspace"


def _host_label() -> str:
    r"""Per-host directory label. Precedence:
      1. `$SUTANDO_HOST_LABEL` (or legacy `$SUTANDO_HOST_OVERRIDE`)
      2. macOS `scutil --get LocalHostName` (the stable Bonjour name)
      3. short `hostname`

    Why scutil before hostname: on DHCP networks `hostname` can drift (e.g. a
    Comcast residential lease returns `Chis-MBP.hsd1.wa.comcast.net` →
    `Chis-MBP`), while the Bonjour LocalHostName (`Chis-MacBook-Pro`) is stable.
    A drifting `hostname` splits per-host paths — two `hosts/<label>/` dirs,
    phantom `state/cores/<label>.alive`, and `personal_path()` falling back to
    the workspace root (2026-06-22 incident). `scutil` is macOS-only; on Linux
    it's absent and we fall through to `hostname`.

    Single source of truth for the per-host segment so the legacy
    `machine-<host>/` (memory-dir) and new `hosts/<host>/` (workspace)
    conventions stay in lockstep. Kept in lockstep with `_host()` in
    sync-workspace.sh (same precedence)."""
    env = os.environ.get("SUTANDO_HOST_LABEL") or os.environ.get("SUTANDO_HOST_OVERRIDE")
    if env:
        return env
    try:
        out = subprocess.run(
            ["scutil", "--get", "LocalHostName"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return socket.gethostname().split(".")[0]


def _private_machine_dir() -> Path | None:
    root = _memory_dir_env()
    if not root:
        return None
    expanded = os.path.expanduser(root)
    return Path(expanded) / f"machine-{_host_label()}"


def personal_path(filename: str, workspace: Path | None = None) -> Path:
    """Resolve a personal-asset path.

    Order: `<workspace>/hosts/<host>/<filename>` (new per-host home, #1717)
    → `$SUTANDO_MEMORY_DIR/machine-<host>/<filename>` (legacy memory-dir
    per-host) → `<workspace>/<filename>`.
    (Legacy `$SUTANDO_PRIVATE_DIR` is honored as a fallback with a
    deprecation warning — see `_memory_dir_env()`.)
    For files known to live under `assets/` in the public workspace
    (currently `stand-avatar.png`), also tries `<workspace>/assets/<filename>`
    before falling back to `<workspace>/<filename>`.

    The `hosts/<host>/` probe is read-side only and purely additive: when no
    such file exists, resolution is identical to the pre-#1717 behavior. This
    is the reader half of the per-host relocation — without it, moving a
    per-host file into `hosts/<host>/` would silently strand readers on the
    workspace-root fallback (the H4 regression).

    Returns the FIRST existing path. If none exist, returns the preferred
    private-dir path so the caller's `.exists()` check fails gracefully.
    """
    ws = workspace if workspace is not None else _workspace_root()

    # New per-host canonical home (workspace-as-git-repo, #1717). Probed first
    # so relocated files are found; absent → falls through to legacy order.
    host_dir = ws / "hosts" / _host_label()
    p = host_dir / filename
    if p.exists():
        return p

    private = _private_machine_dir()
    if private is not None:
        p = private / filename
        if p.exists():
            return p

    # Public workspace — assets/ first for avatar-style files, then root
    if filename in {"stand-avatar.png"}:
        p = ws / "assets" / filename
        if p.exists():
            return p

    p = ws / filename
    if p.exists():
        return p

    # Nothing exists; return preferred (private if configured, else workspace)
    if private is not None:
        return private / filename
    if filename in {"stand-avatar.png"}:
        return ws / "assets" / filename
    return ws / filename


def shared_personal_path(filename: str, workspace: Path | None = None) -> Path:
    """Resolve a shared-private path (notes, build_log, etc.) — files that
    sync across all of an owner's machines, not per-machine state.

    Order: `$SUTANDO_MEMORY_DIR/<filename>` (top-level, shared) → `<workspace>/<filename>`.
    (Legacy `$SUTANDO_PRIVATE_DIR` is honored as a fallback with a
    deprecation warning — see `_memory_dir_env()`.)

    Difference vs `personal_path`: this resolves to the top-level private dir,
    NOT `machine-<host>/`. Use for files like notes/, where every Mac in
    Chi's fleet should see the same content.

    Returns the FIRST existing path. If none exist, returns the preferred
    private path so the caller's `.exists()` check fails gracefully.
    """
    ws = workspace if workspace is not None else _workspace_root()

    root = _memory_dir_env()
    if root:
        private = Path(os.path.expanduser(root)) / filename
        if private.exists():
            return private
        # Fall back to workspace if private doesn't have it, but remember
        # the preferred private path for the "nothing exists" branch.
        p = ws / filename
        if p.exists():
            return p
        return private

    p = ws / filename
    return p


# ---------------------------------------------------------------------------
# Claude Code home directory — the host CLI's per-user state lives at
# `~/.claude/`. Sutando consumes several subpaths (channels/, projects/,
# skills/, settings.json, etc.); centralizing the resolution here keeps the
# host-CLI dependency surface a single grep.
#
# Why this helper: per the 2026-05-18 workspace-design RFC discussion, the
# dependency on `~/.claude/` is real (memory storage, channel tokens, skill
# discovery, slash-command write convention) and we accept it operationally —
# but we want the surface countable so a future swap is a 1-day grep+replace
# rather than a re-architecture. ANY new read/write into the Claude Code home
# directory should go through this helper.
#
# Resolution: prefer $CLAUDE_HOME if set (override / testing), else
# `~/.claude/`. Does NOT create the dir.
# ---------------------------------------------------------------------------

def claude_home_path(*subpath: str) -> Path:
    """Resolve a path under Claude Code's per-user home (`~/.claude/` by default).

    Pass subpath components positionally, e.g.:
        claude_home_path("channels", "discord", "access.json")
        claude_home_path("projects", project_slug, "memory", "MEMORY.md")
        claude_home_path("skills", skill_name)

    Resolution order:
      1. $CLAUDE_CONFIG_DIR (M2 workspace-scoped path; set by the
         `claude-sutando` shell function + start-cli.sh — when present,
         bridges + memory readers see the workspace's .claude-sutando/).
      2. $CLAUDE_HOME (legacy alt-host override, kept for tests).
      3. ~/.claude/ (default — vanilla `claude` users).

    The CLAUDE_CONFIG_DIR check goes first because for a claude-sutando
    install, that's where settings, sessions, channels, skills, and memory
    actually live post-migrate. The CLAUDE_HOME hatch still works for tests
    that need a non-default but non-workspace location.

    Companion env var: $SOURCE_CLAUDE_CONFIG_DIR (defaults to ~/.claude) is
    used by migration scripts (sutando-shell-setup.sh --migrate, src/migrate.sh)
    to refer to the READ-FROM source — i.e., where vanilla claude state lives
    historically. claude_home_path() does NOT consult it; this helper is for
    RUNTIME path resolution. Migration code uses SOURCE_CLAUDE_CONFIG_DIR
    directly to keep the read-side / write-side distinction visible.
    """
    ccd_env = os.environ.get("CLAUDE_CONFIG_DIR")
    home_env = os.environ.get("CLAUDE_HOME")
    if ccd_env:
        base = Path(os.path.expanduser(ccd_env))
    elif home_env:
        base = Path(os.path.expanduser(home_env))
    else:
        _emit_claude_home_fallback_banner_once()
        base = Path.home() / ".claude"
    if not subpath:
        return base
    return base.joinpath(*subpath)


def channel_access_path(source: str) -> Path:
    """Resolve `channels/<source>/access.json` with the ~30-day legacy fallback.

    Prefer the canonical claude_home_path() location. If that file does NOT
    exist but the pre-migration `~/.claude/channels/<source>/access.json`
    does, return the legacy path and emit a one-line stderr deprecation
    warning — per the CLAUDE.md migration policy (readers prefer canonical,
    fall back to legacy for ~30 days).

    Why this exists: bridges restarted under a fresh $CLAUDE_CONFIG_DIR
    before the channel-bridge migrate step copies channels/ would otherwise
    see no access.json at all — Telegram/Slack then re-arm TOFU onboarding
    and the next DM sender auto-enrolls as owner. Falling back to the
    populated legacy allowlist keeps access control continuous across the
    migration window. Writers (TOFU onboarding, /discord:access) use the
    same resolved path, so the legacy file stays the single source of truth
    until it is actually migrated.
    """
    canonical = claude_home_path("channels", source, "access.json")
    if canonical.exists():
        return canonical
    legacy = Path.home() / ".claude" / "channels" / source / "access.json"
    if legacy != canonical and legacy.exists():
        print(
            f"[util_paths] DEPRECATION: using legacy {legacy} — canonical "
            f"{canonical} missing. Run the channel-bridge migrate step "
            f"(scripts/sutando-migrate.sh) to relocate; this fallback is "
            f"removed ~30 days post-migration.",
            file=sys.stderr,
        )
        return legacy
    return canonical


# ---------------------------------------------------------------------------
# Fallback-banner gate — fires ONCE per process when claude_home_path() lands
# on the ~/.claude/ default because neither $CLAUDE_CONFIG_DIR nor $CLAUDE_HOME
# was set. Owner directive #design 2026-06-07 (Option A+ for channels migration):
# the silent ~/.claude/ fallback was load-bearing for any boot path that forgot
# to set CCD; the banner makes that miswiring visible without forcing a hard
# error in the deprecation window. Banner is suppressible via
# $SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER=1 for tests / scripts that intentionally
# exercise the ~/.claude/ path.
# ---------------------------------------------------------------------------

_CLAUDE_HOME_FALLBACK_BANNER_FIRED = False


def _emit_claude_home_fallback_banner_once() -> None:
    global _CLAUDE_HOME_FALLBACK_BANNER_FIRED
    if _CLAUDE_HOME_FALLBACK_BANNER_FIRED:
        return
    if os.environ.get("SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER") == "1":
        _CLAUDE_HOME_FALLBACK_BANNER_FIRED = True
        return
    _CLAUDE_HOME_FALLBACK_BANNER_FIRED = True
    print(
        "claude_home_path: $CLAUDE_CONFIG_DIR not set — falling back to ~/.claude/. "
        "Set CLAUDE_CONFIG_DIR before starting Sutando services (the `claude-sutando` "
        "shell function and src/startup.sh set it; ad-hoc launches must too) so "
        "channels/skills/hooks/sessions resolve to the workspace-scoped per-runtime "
        "location post-#1454. Suppress with SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER=1.",
        file=sys.stderr,
    )
