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
import re
import socket
import subprocess
import sys
from pathlib import Path

# The sibling import must resolve both top-level (src/ on sys.path) and
# package-style (`src.util_paths`), where src/ itself is not on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sutando_config import config_get, config_get_env_first  # noqa: E402

def _memory_dir_env() -> str | None:
    """Return the resolved memory-dir env value, preferring the new name.

    Lookup order:
      1. `SUTANDO_MEMORY_DIR` (canonical post-#858 / #870)
      2. `SUTANDO_PRIVATE_DIR` (legacy, with deprecation warning emitted
         to stderr on every read — not just once at startup; cron and
         launchd environments miss startup-only warnings).

    Returns the raw env value (caller must `os.path.expanduser` if needed),
    or None when neither is set."""
    new = config_get_env_first("SUTANDO_MEMORY_DIR")
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

    Delegates to workspace_default.resolve_workspace() so the post-v0.8
    canonical default (<repo>/workspace/) is honored. ($SUTANDO_WORKSPACE is
    no longer honored for resolution per #1440; see `src/sutando_config.py`.)

    `migrate=False` — path resolution shouldn't trigger migrations on
    every call. Migration runs from src/startup.sh and the bridge boot
    paths where it belongs.
    """
    try:
        from workspace_default import resolve_workspace
        return resolve_workspace(migrate=False)
    except ImportError:
        # workspace_default not on sys.path (standalone invocation outside the
        # repo). Locate the repo root via git, then call sutando-config.sh to
        # honor sutando.config.local.json and the M0 override chain.
        try:
            toplevel = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=5,
            )
            if toplevel.returncode == 0 and toplevel.stdout.strip():
                _sh = Path(toplevel.stdout.strip()) / "scripts" / "sutando-config.sh"
                result = subprocess.run(
                    ["bash", str(_sh), "workspace"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return Path(result.stdout.strip())
        except Exception:
            pass
        # Last-ditch default: the canonical post-v0.8 home-dir location
        # (~/sutando-workspace, matching sutando_config.py resolve_workspace) —
        # The pre-v0.8 dotted path is no longer RESOLVED to, but may still exist
        # and still take writes — health-check reports that divergence.
        return Path.home() / "sutando-workspace"


def legacy_dotted_workspace() -> Path:
    """The pre-v0.8 dotted workspace dir. NOT resolved to any more — but it can
    still exist on disk and still take writes, which is why one file owns the
    literal instead of each caller writing it fresh."""
    return Path.home() / ".sutando" / "workspace"


def legacy_dotted_workspace_path(*subpath: str) -> Path:
    """Subpath under `legacy_dotted_workspace()`, which owns the literal.
    Reaches unmigrated pre-v0.8 content; never resolves the live workspace."""
    return legacy_dotted_workspace().joinpath(*subpath)


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
    env = config_get("SUTANDO_HOST_LABEL") or os.environ.get("SUTANDO_HOST_OVERRIDE")
    # Strip before testing: a blank-but-set override (`SUTANDO_HOST_LABEL=" "`,
    # trivially produced by an unquoted expansion in a launcher) is truthy in
    # Python, so `if env:` returned the whitespace itself as the label. That
    # yields `hosts/   /` and `state/cores/   .alive` — the same per-host path
    # split as the 2026-06-22 DHCP-drift incident above, but self-inflicted and
    # far harder to spot in a directory listing. Blank means "not set": fall
    # through to scutil/hostname. Matches SutandoConfig.hostLabel() in
    # src/Sutando/main.swift, which already trims.
    env = (env or "").strip()
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
    explicit_ws = workspace is not None
    ws = workspace if explicit_ws else _workspace_root()

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

    # Nothing exists — this is the path the caller will CREATE. It must stay
    # inside the workspace the caller named, or their isolation is a fiction.
    #
    # The read probes above may legitimately return a legacy machine-<host>/
    # file: that is the migration fallback and it is load-bearing while those
    # files still exist. But the WRITE destination is a different question, and
    # answering it from $SUTANDO_MEMORY_DIR ignored the argument entirely — so
    # `personal_path("x.md", <fresh tmp>)` handed back a path in the operator's
    # real, vault-SYNCED memory tree. A test that passed a tmpdir believing it
    # was isolated wrote into Chi's vault instead; ALPHA/BRAVO fixtures reached
    # it that way (#2452). Passing a tmpdir is not isolation unless the resolved
    # path is actually inside it.
    #
    # When no workspace was passed the caller has accepted ambient resolution,
    # so the env-based private dir remains correct and behavior is unchanged.
    # The escape exists ONLY when a private dir is configured: that is the branch
    # that answered from the environment instead of from `ws`. When it is None the
    # code already fell through to `ws / filename`, which is inside the workspace
    # and needs no change — and `tests/util-paths-hosts-resolution.test.py` pins
    # that as a deliberate #1717 decision ("the fix is read-side only; write
    # target is untouched"). So keep the write target where #1717 put it, the
    # workspace root, and change only WHOSE workspace answers.
    if private is not None:
        if explicit_ws:
            return ws / filename
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

def claude_home_path(*subpath: str, vanilla: bool = False) -> Path:
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

    `vanilla=True` asks a DIFFERENT question: where does *stock* Claude Code
    keep this, regardless of where Sutando relocated its own config? It skips
    $CLAUDE_CONFIG_DIR entirely and reads $SOURCE_CLAUDE_CONFIG_DIR (the name
    the note below already gives that location) before the default. Use it ONLY
    to read state written by a tool that hardcodes the stock home and does not
    honour $CLAUDE_CONFIG_DIR — that surface stays as countable as this one.
    A caller who wants the relocated dir wants the default form; collapsing the
    two is the bug sonichi#2629 is about, where the official discord plugin
    wrote approval markers to the stock home and the bridge read the relocated
    one, so confirmations were silently never sent.

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
    ccd_env = None if vanilla else os.environ.get("CLAUDE_CONFIG_DIR")
    home_env = (os.environ.get("SOURCE_CLAUDE_CONFIG_DIR") if vanilla
                else config_get_env_first("CLAUDE_HOME"))
    if ccd_env:
        base = Path(os.path.expanduser(ccd_env))
    elif home_env:
        base = Path(os.path.expanduser(home_env))
    else:
        # `vanilla=True` landing here is the CORRECT answer, not a fallback, so
        # it must not fire the "nothing is configured" banner.
        if not vanilla:
            _emit_claude_home_fallback_banner_once()
        base = Path.home() / ".claude"
    if not subpath:
        return base
    return base.joinpath(*subpath)


def claude_project_slug(path: str | Path) -> str:
    """Derive the project slug Claude Code uses under `projects/<slug>/`.

    Claude Code dashes every non-alphanumeric character of the path, not
    just "/" — matching only "/" resolves to a nonexistent dir on any path
    containing a space or dot (e.g. a desktop-bundled checkout under
    "Application Support/space.ag2.app/"). Every caller must derive the slug
    through this one function rather than re-implementing the regex, so the
    derivation can't drift out of sync again.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


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

def write_private_text(path: "Path", text: str) -> None:
    """Write ``text`` to ``path`` as an owner-only (0600) file, with no window.

    ``Path.write_text()`` creates at the process umask — commonly 0644 — so the
    familiar ``write_text(...)`` + ``os.chmod(..., 0o600)`` pair leaves the file
    world-readable for the interval between the two calls. For access-control
    data (allowlists, owner ids, tier maps) that interval is the whole exposure.

    ``os.open`` applies the mode at CREATION, and ``fchmod`` covers the case
    where the file already existed (the mode argument is ignored then). O_TRUNC
    rather than O_EXCL deliberately: this is used on best-effort paths wrapped in
    broad excepts, and O_EXCL would turn a leftover temp file from a crash into a
    permanent silent failure.
    """
    # Order matters for DATA INTEGRITY, not just permissions. O_TRUNC empties the
    # file at OPEN, so with `O_CREAT|O_TRUNC` then fchmod, a hardening failure
    # leaves an EXISTING backup empty -- a permission error would destroy exactly
    # the durable copy that exists to survive a wipe. (The old
    # write_text()+chmod at least failed with correct data and loose perms.)
    #
    # So: open WITHOUT O_TRUNC, harden first, and only truncate once nothing can
    # still fail destructively.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)   # existing file: mode arg above was ignored
        os.ftruncate(fd, 0)    # nothing destroyed until hardening succeeded
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "w") as fh:  # fdopen takes ownership of fd from here
        fh.write(text)


# Unset instance keeps the historic name; mirrored in src/watcher_sentinel.sh
# and a test asserts the two namers agree.
WATCHER_SENTINEL_STEM = "watch-tasks-stream"


def watcher_sentinel_path(state_dir, instance: "str | None" = None) -> Path:
    """The sentinel this instance writes. No instance -> the historic name."""
    if instance is None:
        instance = os.environ.get("SUTANDO_INSTANCE", "")
    instance = re.sub(r"[^A-Za-z0-9._-]", "-", instance.strip()).strip(".-")
    suffix = f"-{instance}" if instance else ""
    return Path(state_dir) / f"{WATCHER_SENTINEL_STEM}{suffix}.pid"


def watcher_sentinel_paths(state_dir) -> "list[Path]":
    """Every sentinel present, historic name first.

    A reader must consider all of them: asking about one file answers about one
    watcher, and on a pool host the others are equally real.
    """
    state = Path(state_dir)
    bare = state / f"{WATCHER_SENTINEL_STEM}.pid"
    found = [bare] if bare.exists() else []
    try:
        rest = sorted(p for p in state.glob(f"{WATCHER_SENTINEL_STEM}-*.pid")
                      if p.is_file())
    except OSError:
        rest = []
    return found + rest
