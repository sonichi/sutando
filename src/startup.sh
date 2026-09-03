#!/bin/bash
# Sutando startup — starts available services + the selected core CLI.
# Usage: bash src/startup.sh [--with-app]   ./start.sh is the front door; --with-app builds + launches the menu-bar app (no launchd job).

set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"

# --with-app is opt-in and parsed here so an unknown flag cannot silently do
# nothing: every other argument is still ignored exactly as before.
WITH_APP=0
for _arg in "$@"; do
    case "$_arg" in --with-app) WITH_APP=1 ;; esac
done

# Resolve python3 ONCE, refusing Apple's Xcode-CLT stub. On a Mac without the
# developer tools `/usr/bin/python3` exists but raises a modal install dialog
# when executed, and this script spawns python3 ~15 times — dashboard.py and
# agent-api.py on a respawn backoff, so each retry re-raised it. Measured on a
# clean macOS 26.5 VM against Sutando 0.5.0-rc.2.
#
# $PY is empty when nothing runnable exists; no call site below may fall back to
# the bare name (that IS the stub).
#
# This used to promise "Python-backed services will be skipped" and continue.
# That promise could never be kept (CR #2599, @qingyun-wu): startup resolves the
# WORKSPACE at :586 via `sutando-config.sh workspace`, which is Python, and every
# Python-backed launch is downstream of it. So a no-interpreter run could not
# reach a single one of those skip branches — under `set -e` it died at the first
# required config lookup having just claimed it would carry on. Sutando has no
# meaningful degraded mode without an interpreter: the workspace, host label,
# core runtime and CLAUDE_CONFIG_DIR are all resolved through it.
#
# So fail once, here, with the fix — which still achieves this PR's actual goal.
# The goal was never "start anyway"; it was "never execute the stub", because
# executing it raises a ~19 GB modal that no exit-code check or 2>/dev/null can
# suppress. An actionable message beats that dialog.
. "$REPO/scripts/python-binary.sh"
PY="$(resolve_python "$REPO")"

if [ -z "$PY" ]; then
  {
    echo "✗ no runnable python3 (no \$SUTANDO_PY, no bundled runtime, no developer tools)"
    # Deliberately no literal system-interpreter path in this text: the
    # repo's hardcoded-path gate flags that token, and an allow-listed
    # exception here would weaken the scan for every other line in the file.
    echo "  Sutando cannot start: the workspace, host label and core runtime are"
    echo "  all resolved through Python. Deliberately not falling back to the"
    echo "  system python3 — on a Mac without the developer tools that is the"
    echo "  Xcode-CLT stub, and running it raises the install dialog rather"
    echo "  than returning an error."
    echo "  Fix: brew install python — or set \$SUTANDO_PY to an interpreter."
  } >&2
  exit 1
fi

# Must run after the PY-validity abort but before ANY workspace-derived write:
# a write below on a broken link materializes an unhealable real dir.
_wl_out="$("$PY" "$REPO/src/workspace_layout.py" --ensure)" || {
  echo "✗ workspace wiring broken and not auto-healable — refusing to start services onto a stranded workspace. Diagnose: $PY $REPO/src/workspace_layout.py --check" >&2
  exit 1
}
case "$_wl_out" in *'"action": "healed-'*) echo "🔧 workspace wiring healed: $_wl_out" >&2 ;; esac
cd "$REPO"

# Belt-and-suspenders startup log → always recoverable from /tmp (Lucy's Bug #5
# from Maddy v0.8 migration report, 2026-06-06 #design). The footgun: operator
# runs `nohup bash src/startup.sh > $SUTANDO_WORKSPACE/logs/startup-<ts>.log 2>&1 &`
# over SSH. Mid-run the v0.8 auto-migration `rm -rf`'s the legacy workspace dir
# the redirect was pointing at → log vanishes mid-write, postmortem impossible.
#
# Fix: also tee stdout + stderr to /tmp/sutando-startup-<ts>.log so a copy
# survives regardless of what the operator pointed their own redirect at. The
# tee path is announced early so operators always know where to look.
#
# Opt out with SUTANDO_STARTUP_NO_LOG=1 (e.g. when running inside a test
# harness that handles its own output capture).
if [ "${SUTANDO_STARTUP_NO_LOG:-0}" != "1" ]; then
  _STARTUP_LOG="/tmp/sutando-startup-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
  echo "📓 startup log → $_STARTUP_LOG" >&2
  exec > >(tee -a "$_STARTUP_LOG") 2> >(tee -a "$_STARTUP_LOG" >&2)
fi

# Export workspace root so child processes (skills, gather scripts, etc.) can
# resolve "the Sutando workspace" without walking dirname-relative paths that
# break when the script is invoked via a userSettings hardlink. Picked up by
# skills/self-diagnose/scripts/gather.sh and any other script that honors
# $SUTANDO_ROOT.
export SUTANDO_ROOT="$REPO"

# tsx runner for every TS service start below — resolution is config-resolved
# via sutando-config.sh (`tsx-bin` / `app-node-dir` are the single source of
# truth, shared with the launchd wrapper); `npx tsx` only as a last resort. A
# host may have no homebrew/nvm node/npx at all (the app-bundle runtime ships
# bare `node` only, no npx — the PATH prepend covers tsx's `#!/usr/bin/env node`
# re-exec). A raw `npx tsx` call site silently fails to start its service on
# such hosts (web-client outage 2026-07-17).
_APP_NODE_DIR="$(bash "$REPO/scripts/sutando-config.sh" app-node-dir)"
[ -d "$_APP_NODE_DIR" ] && case ":$PATH:" in *":$_APP_NODE_DIR:"*) ;; *) PATH="$_APP_NODE_DIR:$PATH"; export PATH ;; esac
# `|| true`: tsx-bin prints nothing AND returns nonzero on a bare bundled
# runtime (no tsx anywhere) — under set -e that exited startup at this line
# before the bundled-mode gate could run (Codex blocking finding #2).
_TSX_BIN="$(bash "$REPO/scripts/sutando-config.sh" tsx-bin || true)"
run_tsx() {
  if [ -n "$_TSX_BIN" ]; then
    "$_TSX_BIN" "$@"
  else
    npx tsx "$@"
  fi
}

# G1.5 node-bundle (owner-adopted design + owner review 2026-07-19).
# Bundled and dev are MUTUALLY EXCLUSIVE modes, not a preference (P1-2):
#   BUNDLED = the desktop-managed runtime is present — SUTANDO_NODE exported
#   (fail-closed if invalid, see node-bin) OR the bundled runtime discovered
#   at its canonical home (app-node-dir; covers launchd jobs without the env
#   var) — AND dist artifacts are shipped. Services run dist/<name>.js under
#   the pinned node; a missing artifact is an explicit PACKAGING ERROR, never
#   a tsx fallback (tsx/npm/node_modules deliberately don't exist here).
#   DEV = everything else; tsx-over-src exactly as before, so a stale dist/
#   can never shadow live src edits.
_NODE_BIN="$(bash "$REPO/scripts/sutando-config.sh" node-bin)" || {
  echo "✗ SUTANDO_NODE is set but invalid — desktop packaging error; refusing PATH fallback (G1.5 fail-closed)"
  exit 1
}
# At-rest discovery is scoped to the PACKAGED ENGINE COPY ONLY: the checkout
# must itself live inside the engine root that owns the runtime
# (<engine-root>/runtime/node/bin + <engine-root>/.../this repo). A dev
# checkout on a machine that merely HAS the app installed must stay dev even
# after `npm run build:bundle` — stale dist can never shadow live src
# (Codex finding #3).
_APP_ENGINE_ROOT="${_APP_NODE_DIR%/node/bin}"
_APP_ENGINE_ROOT="${_APP_ENGINE_ROOT%/runtime}"
# Mode comes from the MANAGED-RUNTIME SIGNAL ALONE (Codex re-review F2):
# explicit SUTANDO_NODE, or the at-rest runtime discovered while running AS
# the packaged engine copy. Artifact presence is then VALIDATED separately —
# a managed runtime with missing dist is a packaging error that fails closed,
# never a silent slide into dev/npm/tsx.
BUNDLED_MODE=0
if [ -n "${SUTANDO_NODE:-}" ]; then
  BUNDLED_MODE=1
elif [ -x "$_APP_NODE_DIR/node" ] && [ "${REPO#"$_APP_ENGINE_ROOT"/}" != "$REPO" ]; then
  BUNDLED_MODE=1
fi
if [ "$BUNDLED_MODE" = "1" ]; then
  # Validate EVERY artifact of the build:bundle contract, not a representative
  # one (external review on #2182): a missing voice/proxy/etc artifact would
  # otherwise fail inside a background job while boot still prints ✓.
  _MISSING_DIST=""
  # web-voice-transport.browser is the browser-side artifact the web UI loads
  # for voice; it is built by the same build:bundle contract but is NOT a node
  # service, so it has no run_node_service entry to catch its absence later.
  for _artifact in web-client voice-agent conversation-server credential-proxy boot emit-call-tiers web-voice-transport.browser; do
    [ -f "$REPO/dist/$_artifact.js" ] || _MISSING_DIST="$_MISSING_DIST $_artifact.js"
  done
  if [ -n "$_MISSING_DIST" ]; then
    echo "✗ bundled mode: required dist artifacts missing ($REPO/dist:$_MISSING_DIST) — desktop packaging error; refusing dev fallback (G1.5 fail-closed)"
    exit 1
  fi
fi
if [ -n "${SUTANDO_NODE:-}" ]; then
  _SUTANDO_NODE_DIR="$(dirname "$SUTANDO_NODE")"
  case ":$PATH:" in *":$_SUTANDO_NODE_DIR:"*) ;; *) PATH="$_SUTANDO_NODE_DIR:$PATH"; export PATH ;; esac
fi
run_node_service() {
  # $1 = dist basename (build-bundle artifact), $2 = ts entry (as run_tsx
  # expects it today — relative or absolute), rest = service args.
  _rns_dist="$REPO/dist/$1.js"
  _rns_entry="$2"
  shift 2
  if [ "$BUNDLED_MODE" = "1" ]; then
    if [ ! -f "$_rns_dist" ]; then
      echo "  ✗ bundled mode: dist artifact missing: $_rns_dist (packaging error — refusing tsx fallback)"
      return 1
    fi
    "$_NODE_BIN" "$_rns_dist" "$@"
  else
    run_tsx "$_rns_entry" "$@"
  fi
}

# Resolve the configured core BEFORE touching runtime-specific credentials or
# running an auth probe. .env remains loaded at its established point below;
# the helper consults it in a subshell only for SUTANDO_CORE_RUNTIME parity.
# shellcheck source=startup-runtime.sh
source "$REPO/src/startup-runtime.sh"
core_runtime="$(resolve_startup_core_runtime)"
[ -n "$core_runtime" ] || core_runtime="claude"

# Export workspace-scoped CLAUDE_CONFIG_DIR before services launch. Without it,
# init.sh + the bridge-launcher blocks below (L~262 proxy, L~429 telegram,
# L~449 discord, L~473 slack) probe `${CLAUDE_CONFIG_DIR:-$HOME/.claude}` and
# fall back to legacy `~/.claude/` when the env var is unset — meaning bridges
# read tokens / access lists from the pre-migration location even after a
# successful `claude-sutando --migrate`. Mirrors src/agent/claude/cli/start-cli.sh
# (Sutando.app's tmux-wrapped CLI launcher) — same machine-spawn pattern.
#
# Resolve-or-refuse is shared with start-cli; only the credential seeding is ours.
source "$REPO/src/claude_config_dir.sh"
if _ccd="$(resolve_claude_config_dir "$REPO" startup)"; then
    mkdir -p "$_ccd"
    # NOTE: the claude core-agent launcher + the `claude-sutando` config-dir
    # onboarding alias now live under src/agent/claude/ (start-cli.sh /
    # sutando-shell-setup.sh). This credentials seed stays INLINE here by design:
    # it must run in the same startup env (exports CLAUDE_CONFIG_DIR for the
    # bridges below), not in a separately-execed launcher. See
    # src/agent/claude/README.md for the full auth/onboarding map.
    # Auth-carry (v0.8 cold-start fix). Seed credentials + onboarding state from
    # $HOME/.claude/ so a cold `claude` core doesn't dead-end at the login wall
    # (.credentials.json) or trust-folder prompt (.claude.json) before reaching
    # /schedule-crons. Idempotent — only copies when the per-runtime dir lacks
    # the file. Without this, the watcher never starts on a fresh node (see
    # Lucy's #design 2026-06-06 17:12Z heads-up, Bug 1).
    #
    # Single-tenant assumption (Pro 21:18Z + Lucy 21:25Z reviews on PR #1496):
    # this whole-file copy binds the per-runtime dir to whatever account
    # $HOME/.claude owns — .claude.json carries `oauthAccount`, `userID`, the
    # `projects` map (per-dir history + MCP approval state), and `mcpServers`.
    # Fine for the current single-user-Mac reality. If per-runtime ever means
    # per-account, narrow this carry to onboarding flags only.
    #
    # Sync caveat: if CLAUDE_CONFIG_DIR lives under workspace/ and
    # workspace-sync is on, .claude.json propagates across the fleet. Trust
    # entries keyed by absolute checkout path don't collide between hosts.
    # Followup: consider narrowing CLAUDE_CONFIG_DIR to a per-host non-synced
    # subdir.
    if claude_auth_carry_enabled "$core_runtime"; then
    for _seed in .credentials.json .claude.json; do
      if [ ! -f "$_ccd/$_seed" ] && [ -f "$HOME/.claude/$_seed" ]; then
        # Mini 21:23Z: defensive log on cp failure (read-only target, disk full).
        # Lucy 21:25Z: log on success too so a stale-source case (expired creds
        # carried forward + masked by the idempotent guard) is diagnosable.
        if cp "$HOME/.claude/$_seed" "$_ccd/$_seed" 2>/dev/null; then
          [ "$_seed" = ".credentials.json" ] && chmod 600 "$_ccd/$_seed"
          echo "  ~ auth-carry: seeded $_seed from \$HOME/.claude/"
        else
          echo "  ~ auth-carry: cp failed for $_seed (check target perms + disk)"
        fi
      fi
    done
    # Env-token persist (Issue #1499, owner pick 02:42Z 2026-06-07 = Option A).
    # If `.credentials.json` is STILL absent after the auth-carry above AND a
    # known token env var is set, write the token to `.credentials.json` in
    # Claude Code's expected `claudeAiOauth` format so the next restart finds
    # auth on disk and doesn't dead-end at the login wall. Closes Lucy's
    # Maddy-reproduced gap: env-token-authed nodes never write the file
    # themselves, so the copy-only auth-carry above has nothing to copy.
    #
    # Mode 600 + parent-dir 700 = de-facto OAuth-on-disk standard (gh CLI,
    # kubectl). Per Sutando-Mini's #design 2026-06-07 weigh-in.
    #
    # Trade-off (documented in Issue #1499): token now on disk. Operators who
    # chose env-only specifically for off-disk security can opt out via
    # `SUTANDO_NO_PERSIST_TOKEN=1`.
    #
    # Schema (Claude Code's own format):
    #   {"claudeAiOauth": {"accessToken": "<token>"}}
    # We intentionally omit refreshToken + expiresAt — env-tokens don't have
    # a refresh flow (operator-managed rotation) and omitting expiresAt is
    # a valid claude-code config (treated as "no expiry tracking on disk").
    if [ ! -f "$_ccd/.credentials.json" ] && [ "${SUTANDO_NO_PERSIST_TOKEN:-0}" != "1" ]; then
      _env_token=""
      for _var in CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_AUTH_TOKEN; do
        eval "_val=\${$_var:-}"
        if [ -n "$_val" ]; then
          _env_token="$_val"
          _env_var_used="$_var"
          break
        fi
      done
      if [ -n "$_env_token" ]; then
        # python3 -c is the most portable jq-free way to write a tiny JSON
        # without shell-quoting hazards on the token value. Env vars
        # prefixed to the command (not appended as args) per POSIX.
        if _p="$_ccd/.credentials.json" _t="$_env_token" ${PY:-false} -c "
import json,os
p=os.environ['_p']
t=os.environ['_t']
json.dump({'claudeAiOauth':{'accessToken':t}}, open(p,'w'))
" 2>/dev/null; then
          chmod 600 "$_ccd/.credentials.json"
          echo "  ~ env-token-persist: wrote .credentials.json from \$$_env_var_used (mode 600)"
          # Sidecar provenance file (#1504) — never read by Claude Code.
          # Records how this credentials file was produced for audit/migration tools.
          _p="$_ccd/.credentials.source.json" _v="$_env_var_used" ${PY:-false} -c "
import json,os,datetime
p=os.environ['_p']; v=os.environ['_v']
json.dump({'source':'env','env_var':v,'carried_at':datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),'persist_block_version':1},open(p,'w'))
" 2>/dev/null && chmod 600 "$_ccd/.credentials.source.json" || true
        else
          echo "  ~ env-token-persist: write failed (check target perms + python3 on PATH)"
        fi
        unset _env_token
      fi
    fi
    fi
    export CLAUDE_CONFIG_DIR="$_ccd"
else
  _ccd_rc=$?
  # 2 = caller already scoped the config dir; nothing to seed, and the services
  # below still reach the intended credential store.
  [ "$_ccd_rc" = "2" ] || exit 1
  echo "  ~ CLAUDE_CONFIG_DIR=$CLAUDE_CONFIG_DIR (caller-provided; config helper absent)"
fi

# Boot gate (#2396): verify the SELECTED core can boot authenticated BEFORE any
# service launches. Claude retains its rich login-state gate; Codex checks the
# configured CODEX_HOME with `codex login status`. The shared escape hatch is
# SUTANDO_SKIP_AUTH_PREFLIGHT=1.
preflight_selected_core_auth "$core_runtime" "${CLAUDE_CONFIG_DIR:-}" || exit $?

# Git committer attribution: REMOVED (2026-05-21). This block used to set
# committer.name/committer.email from stand-identity.json so `git log %cn`
# showed which fleet host crafted a commit. But git 2.31+ honors committer.*
# config natively, making the COMMITTER a non-GitHub identity
# (`<host>@noreply.sutando.local`) — and CLA-Assistant gates on BOTH the
# author AND the committer. Result: every fleet commit was CLA-blocked
# (PR #947 and others, 2026-05-21). Per-host attribution is not worth
# blocking every PR; if still wanted, carry it in a commit-message trailer
# (CLA-Assistant ignores trailers), never in the committer identity.
#
# Actively clear any stale committer.* a prior startup wrote, so every
# fleet host self-heals on its next boot.
git -C "$REPO" config --unset committer.name 2>/dev/null || true
git -C "$REPO" config --unset committer.email 2>/dev/null || true

# Re-apply tracked plugin-cache patches (skills/plugin-patches/). Plugin caches
# are managed like node_modules — clobbered on update + invisible to git/sync —
# so a kept local edit must be re-applied per host. The applier is idempotent +
# fail-loud: it never force-applies and a stale/missing patch WARNs without
# failing startup. See skills/plugin-patches/README.md.
[ -n "$PY" ] && "$PY" "$REPO/skills/plugin-patches/apply-plugin-patches.py" || true

# Load optional .env configuration BEFORE init.sh. Two reasons must both hold:
#  1) init.sh resolves the workspace via `${SUTANDO_WORKSPACE/#~/$HOME}` with
#     fallback to `~/.sutando/workspace/`. If .env carries a SUTANDO_WORKSPACE=
#     override and we haven't sourced .env yet, init.sh seeds dirs and files
#     in the wrong location, leaving orphan ~/.sutando/workspace/ skeletons
#     on first-time installs (hosts without a separate .zshenv export).
#  2) Voice needs a Gemini key, but the Codex core, text web UI, dashboard,
#     API, and configured message bridges do not. Missing voice credentials
#     disable only the voice service instead of blocking the whole product.
configure_startup_runtime

# v0.8 auto-migration helpers (PR #1440 safety hardening — Mini review).
# Sourced from a sibling file so the four guard functions (_realpath,
# _same_inode, _is_unsafe_for_migration, _color_warn) can be unit-tested
# without driving the full startup sequence. See tests/migration-safety-helpers.test.sh.
# shellcheck source=migration_safety_helpers.sh
source "$REPO/src/migration_safety_helpers.sh"

# v0.8 auto-migration: $SUTANDO_WORKSPACE is no longer honored by the resolver.
# If still set (shell rc, .env, launchd plist), detect any data at the env-
# pointed path and relocate to the new default before services start.
#
# Sentinel guard: state/auth/migrated-from-env.txt under the NEW workspace
# records a successful migration so a re-run doesn't retry. Sentinel is in
# state/auth/ specifically because that subtree is exempt from transient-state
# cleanup (per CLAUDE.md "Durable per-host install state").
#
# Safety layers (PR #1440 review — Mini):
#   B1: realpath + inode equality guard — skip if env == resolved workspace.
#   B2: archive BEFORE migrate, so the tarball is a true pre-migration snapshot.
#   B3: deny-list check — refuse rm -rf if env points at /, $HOME, repo, etc.
#   B4: NO_COLOR honored in the red banner (see _color_warn above).
#
# Failure mode: if sutando-migrate.sh --commit exits non-zero (collision that
# can't be auto-resolved, etc.), startup ABORTS — refusing to proceed with
# split state. Operator runs `bash scripts/sutando-migrate.sh --dry-run` to
# diagnose. The pre-migration archive is preserved for recovery.
if [ -n "${SUTANDO_WORKSPACE:-}" ]; then
  _ws_legacy="${SUTANDO_WORKSPACE/#\~/$HOME}"
  _ws_new="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null || true)"
  _migrate_sentinel="${_ws_new}/state/auth/migrated-from-env.txt"

  # Bug #2 fix (Lucy's Maddy report 2026-06-06): also honor sutando-migrate.sh's
  # OWN per-source sentinels (`state/.migrated-from-<tag>-<backup_id>`) — created
  # when the operator runs `sutando-migrate --commit` manually instead of letting
  # startup.sh auto-trigger it. Without this, manual-migrate flows leave SUTANDO_WORKSPACE
  # set + legacy dir non-empty (migrate copies but doesn't rm legacy — only startup
  # does that), so each boot re-fires the auto-migration loop.
  _migrate_script_sentinels_present=0
  if [ -n "$_ws_new" ] && ls "$_ws_new"/state/.migrated-from-* >/dev/null 2>&1; then
    _migrate_script_sentinels_present=1
  fi

  if [ -n "$_ws_new" ] && [ ! -f "$_migrate_sentinel" ] \
     && [ "$_migrate_script_sentinels_present" = "0" ] \
     && [ -d "$_ws_legacy" ] && [ -n "$(ls -A "$_ws_legacy" 2>/dev/null)" ]; then

    _legacy_real="$(_realpath "$_ws_legacy")"
    _new_real="$(_realpath "$_ws_new")"

    if [ -n "$_legacy_real" ] && [ -n "$_new_real" ] && [ "$_legacy_real" = "$_new_real" ]; then
      # B1: realpath equality — env points at the resolved workspace. No-op.
      echo "ℹ️  \$SUTANDO_WORKSPACE already points at the resolved workspace — no migration needed." >&2
    elif _same_inode "$_ws_legacy" "$_ws_new"; then
      # B1: same inode (symlink-equivalent) — no-op.
      echo "ℹ️  \$SUTANDO_WORKSPACE is symlink-equivalent to the resolved workspace — no migration needed." >&2
    elif _is_unsafe_for_migration "$_ws_legacy"; then
      # B3: deny-list — refuse to touch unsafe paths.
      echo "❌ refusing to auto-migrate \$SUTANDO_WORKSPACE — the path matches the deny-list" >&2
      echo "   (denies: /, system dirs, \$HOME and top-level subdirs, repo root, paths with '..')." >&2
      echo "   Likely a malformed \$SUTANDO_WORKSPACE in your shell/.env. Inspect + fix manually, then restart." >&2
      exit 1
    else
      _color_warn "📦 sutando v0.8 auto-migration: \$SUTANDO_WORKSPACE points at $_ws_legacy with data; relocating to $_ws_new (one-shot)."

      # B2: archive BEFORE migrate. Captures the pre-migration state so the
      # tarball is a genuine recovery snapshot. If --commit moves data first,
      # the tarball would only catch the residual / empty post-migrate dir.
      _legacy_archive="${_ws_legacy%/}-pre-v0.8-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
      if ! tar -czf "$_legacy_archive" -C "$(dirname "$_ws_legacy")" "$(basename "$_ws_legacy")" 2>/dev/null; then
        echo "❌ pre-migration archive of $_ws_legacy failed — aborting auto-migration." >&2
        echo "   Without a recovery snapshot, refusing the destructive migrate-and-delete." >&2
        exit 1
      fi
      echo "📦 pre-migration snapshot → $_legacy_archive" >&2

      if bash "$REPO/scripts/sutando-migrate.sh" --commit; then
        mkdir -p "$(dirname "$_migrate_sentinel")"
        printf 'migrated_from=%s\nmigrated_at=%s\nhostname=%s\narchive=%s\n' \
          "$_ws_legacy" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(hostname)" "$_legacy_archive" \
          > "$_migrate_sentinel"
        echo "✅ auto-migration complete — data moved into $_ws_new/" >&2

        # B3 second layer: re-verify deny-list before destructive rm. If
        # _ws_legacy somehow mutated between the initial check and now
        # (symlink swapped, env var rewritten), refuse rm + preserve archive.
        if _is_unsafe_for_migration "$_ws_legacy"; then
          echo "⚠️  refusing rm -rf on $_ws_legacy (failed deny-list re-check). Recovery archive preserved." >&2
        else
          rm -rf "$_ws_legacy"
          echo "🗑  removed legacy directory" >&2
          echo "   to restore: tar -xzf '$_legacy_archive' -C $(dirname "$_ws_legacy")" >&2
        fi
      else
        echo "❌ auto-migration failed — refusing to start with split state." >&2
        echo "   Recovery archive preserved at $_legacy_archive" >&2
        echo "   Diagnose: bash scripts/sutando-migrate.sh --dry-run" >&2
        exit 1
      fi
    fi
  fi

  # Whether migrated or not, unset $SUTANDO_WORKSPACE so child processes
  # (init.sh, bridges, voice-agent, etc.) get the v0.8 contract directly and
  # the resolver's deprecation nag stops firing on every subprocess spawn.
  unset SUTANDO_WORKSPACE
fi

# Exercise the new config loader as a startup banner. Surfaces:
#   • $SUTANDO_WORKSPACE legacy-escape-hatch warning (if env is set)
#   • .env-drift warning (if .env declares a value but config resolves differently)
#   • config parse errors (fails fast before init.sh tries to write to a bad path)
#
# stdout is discarded (just the JSON dump); stderr passes through so the user
# sees diagnostics live. A non-zero exit means malformed config — the parse
# error is already on stderr from this same invocation.
#
# Note: the actual workspace path used below in `WORKSPACE=...` still comes
# from the legacy resolver in this file. Wiring it to come from the loader
# is a follow-up change — this banner is the early-warning layer.
if [ -n "$PY" ] && ! "$PY" -m src.sutando_config >/dev/null; then
  echo "  ✗ sutando.config.json is malformed — fix the parse error above."
  exit 1
fi

# Wire the tracked git hooks. Idempotent — re-running prints "already
# installed" and exits 0. Done at startup so users don't have to remember
# `bash scripts/install-git-hooks.sh` after cloning. If a user only ever
# explores the repo without running startup.sh, the standalone script is
# still there as a fallback (mentioned in README).
bash "$REPO/scripts/install-git-hooks.sh" >/dev/null 2>&1 || true

# Wire the SessionStart hook that reminds the core agent to run /schedule-crons
# on every session start (including post-compaction). Idempotent — safe to run
# on every start. Crons are session-only, so without this, recurring jobs go
# dark whenever a session restarts without an explicit /schedule-crons invocation.
bash "$REPO/scripts/install-session-start-hook.sh" 2>&1 || true

# PERSONAL_CLAUDE.md compaction-reinject hook is Claude-only policy — wired
# at src/agent/claude/cli/start-cli.sh, the Claude launch chokepoint, not here.

# The sentinel is NOT cleared here. This runs ~850 lines before the
# `exec start-cli.sh` below, which clears it once a core is verified live.

# Auto-bootstrap: create-if-missing files and dirs that the agent + skills
# expect to exist (logs, state, tasks, results, notes, contextual-chips.json,
# pending-questions.md, build_log.md, crons.json, …). Idempotent — safe to
# run on every start. Replaces the bare `mkdir -p logs state` that used to
# live here. See src/init.sh for the full list.
bash "$REPO/src/init.sh" --auto

echo "Sutando startup..."
echo ""

# Preflight summary line — what env / CLI / perms are missing. One line, no
# blocking; problems are surfaced but startup continues so the user can fix
# things piece by piece.
bash "$REPO/src/init.sh" --preflight | tail -1

# Bundled mode (G1.5): BUNDLED_MODE is computed ONCE next to
# run_node_service (single source of truth — owner review P1-2: app-node-dir
# discovery counts as bundled too, not just the env var). The npm bootstrap
# + node/npx prereq blocks below are gated on it because a bundled runtime
# deliberately has NO npm/npx/node_modules (Codex finding #1).
if [ "$BUNDLED_MODE" = "1" ]; then
  echo "  ✓ bundled mode (pinned runtime + dist artifacts) — skipping dependency bootstrap"
fi

# Install dependencies if needed (dev/source mode only — bundled installs
# run dist artifacts under the pinned node and need no node_modules).
if [ "$BUNDLED_MODE" != "1" ] && [ ! -d node_modules ]; then
  if command -v npm > /dev/null 2>&1 && npm install 2>/dev/null; then
    echo "  ✓ Dependencies installed (npm)"
  elif command -v pnpm > /dev/null 2>&1 && pnpm install 2>/dev/null; then
    echo "  ✓ Dependencies installed (pnpm)"
  elif command -v yarn > /dev/null 2>&1 && yarn install 2>/dev/null; then
    echo "  ✓ Dependencies installed (yarn)"
  else
    echo "  ✗ Could not install dependencies."
    echo "    Try: curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.0/install.sh | bash"
    echo "    Then: nvm install 24 && npm install"
    exit 1
  fi
fi

# Check CLI prerequisites. node/npx/python3, the selected core runtime, and
# fswatch are checked here because they are not needed for init.sh bootstrap.
# Bundled mode: node is $SUTANDO_NODE (its dir already heads PATH) and npx is
# intentionally absent (bare-node runtime, dist-first services) — skip both.
missing=0
if [ "$BUNDLED_MODE" != "1" ]; then
  if ! command -v node > /dev/null 2>&1; then echo "  ✗ node not found — brew install node"; missing=1; fi
  if ! command -v npx > /dev/null 2>&1; then echo "  ✗ npx not found — comes with node"; missing=1; fi
fi
if ! command -v python3 > /dev/null 2>&1; then echo "  ✗ python3 not found"; missing=1; fi
# sutando-config.sh REQUIRES python and exits 1 without it. Under `set -e` a
# bare command substitution here aborted the whole startup — after we had
# just printed that Python-backed services would be skipped (CR #2599,
# @qingyun-wu). Tolerate the failure and fall back, so the skip actually
# happens instead of being promised.
if ! command -v "$core_runtime" > /dev/null 2>&1; then
  echo "  ✗ $core_runtime CLI not found — required by core.runtime"
  missing=1
fi
if ! command -v fswatch > /dev/null 2>&1; then
  if command -v brew > /dev/null 2>&1; then
    echo "  ⚠ fswatch not found — installing via Homebrew..."
    brew install fswatch
    if command -v fswatch > /dev/null 2>&1; then
      echo "  ✓ fswatch installed"
    else
      echo "  ✗ fswatch installation failed"; missing=1
    fi
  else
    echo "  ✗ fswatch not found — brew install fswatch"; missing=1
  fi
fi

if [ $missing -eq 1 ]; then echo ""; echo "Fix the above and try again."; exit 1; fi

# Check macOS permissions (can't grant programmatically, just warn)
# Prevent display sleep (important for always-on Mac Mini — Zoom/summon fails on lock screen)
if ! pgrep -q caffeinate; then
  caffeinate -d -i -s &
  echo "  ✓ caffeinate started (prevents display sleep)"
else
  echo "  ✓ caffeinate already running"
fi

echo "Checking permissions..."
# macOS 15+ silently writes a tiny PNG when Screen Recording is denied (exit 0).
# Discriminator: real captures are hundreds-of-KB to MB; denied artifacts <2KB.
# An all-black 5120x2880 PNG compresses to ~43KB (PNG handles flat colors well),
# so 5KB is the safe floor — well above any denied output, well below any real
# capture even on a locked / dark / blank desktop.
PERM_OK=1
screencapture -x /tmp/sutando-permcheck.png 2>/dev/null || PERM_OK=0
if [ "$PERM_OK" -eq 1 ]; then
  # wc -c is portable across BSD (macOS) and GNU coreutils (Homebrew may override).
  permcheck_size=$(wc -c < /tmp/sutando-permcheck.png 2>/dev/null | tr -d ' ' || echo 0)
  if [ "${permcheck_size:-0}" -lt 5000 ]; then PERM_OK=0; fi
fi
rm -f /tmp/sutando-permcheck.png
if [ "$PERM_OK" -eq 0 ]; then
  echo "  ⚠ Screen Recording not granted (or stale)"
  echo "    → System Settings → Privacy & Security → Screen & System Audio Recording"
  echo "    → Add the app running this terminal (Terminal.app / iTerm2 / Warp / VS Code / Cursor / etc.)"
  echo "    → Fully Quit the terminal app, then re-open. macOS caches the perm until process restart."
  if lsof -i :7845 > /dev/null 2>&1; then
    echo "    → A screen-capture server is already running on :7845 with the old (denied) perm."
    echo "      Kill it before re-running: lsof -ti:7845 | xargs kill"
  fi
else
  echo "  ✓ Screen Recording"
fi

# Check Accessibility (needed for context drop shortcut)
# $REPO is overridable, so fall back to alongside this script: an unguarded
# source under `set -e` aborts the whole run.
__probe="$REPO/src/accessibility_probe.sh"
[ -f "$__probe" ] || __probe="$(cd "$(dirname "$0")" && pwd)/accessibility_probe.sh"
# 125 = could not check. NOT 0: a missing probe would otherwise print
# "✓ Accessibility", asserting a grant on a run where nothing was probed.
acc_rc=125
if [ -f "$__probe" ]; then
  # `|| rc=$?` keeps this exempt from `set -e`; a bare non-zero call aborts.
  source "$__probe"
  acc_rc=0; accessibility_probe || acc_rc=$?
fi
case $acc_rc in
  0)   echo "  ✓ Accessibility" ;;
  125) echo "  ⚠ Accessibility UNKNOWN — probe unavailable, nothing was checked" ;;
  124) echo "  ⚠ Accessibility UNKNOWN — probe timed out after ${ACCESSIBILITY_PROBE_TIMEOUT_S}s"
       echo "    This session cannot answer the prompt (headless/SSH); the grant may be fine." ;;
  *)   echo "  ⚠ Accessibility not granted"
       echo "    → System Settings → Privacy & Security → Accessibility"
       echo "    → Add Terminal.app or Shortcuts.app" ;;
esac
echo ""

# Install Claude Code skills (runs every startup, idempotent)
bash "$REPO/skills/install.sh" 2>/dev/null || true

# Resolve the runtime workspace via the canonical loader (M0 cutover).
# The loader (v0.8 — env override removed) implements:
#   1. sutando.config.local.json -> workspace.path (per-clone override)
#   2. sutando.config.json -> workspace.path (tracked defaults)
#   3. ${REPO_DIR}/workspace baked-in default
# Inline fallback retained for the rare case where the wrapper isn't
# present (extracted-tarball install, etc.). All service logs + tasks/
# results land under $WORKSPACE/logs etc. instead of the repo-root legacy
# paths. health-check + dashboard already read from $WORKSPACE/logs.
if [ -f "$REPO/scripts/sutando-config.sh" ]; then
  WORKSPACE="$(bash "$REPO/scripts/sutando-config.sh" workspace)"
else
  WORKSPACE="$REPO/workspace"
fi
mkdir -p "$WORKSPACE/logs" "$WORKSPACE/tasks" "$WORKSPACE/results" "$WORKSPACE/data" "$WORKSPACE/state"
LOGS_DIR="$WORKSPACE/logs"

# Self-heal: stand-identity.json was misclassified `rehome-state` by pre-#1540
# `sutando-migrate.sh`, which put it at `<workspace>/state/stand-identity.json`.
# Its reader (`personal_path()` / `personalPath()`) resolves
# `$SUTANDO_MEMORY_DIR/machine-<host>/<file>` → `<workspace>/<file>` ROOT —
# never `state/`. Affected hosts lost their Stand name (fell back to "Sutando")
# silently. #1540 fixed the migration tool; this one-shot mv un-strands hosts
# that already ran the old migration. Idempotent + safe: only fires when state/
# has the file AND root does not, so subsequent boots and unaffected hosts
# (never ran old migrate, or have a configured Stand name at root) are no-op.
if [ -f "$WORKSPACE/state/stand-identity.json" ] && [ ! -e "$WORKSPACE/stand-identity.json" ]; then
  mv "$WORKSPACE/state/stand-identity.json" "$WORKSPACE/stand-identity.json"
  echo "[startup] self-heal: moved stand-identity.json from state/ → workspace root (pre-#1540 migrate followup)" >&2
fi

# Offer to set up the `claude-sutando` shell alias once per host. The helper
# resolves CLAUDE_CONFIG_DIR via `bash scripts/sutando-config.sh claude-sutando-config-dir`
# (driven by `claude_sutando_config_dir.subdir` in sutando.config.json; default
# `.claude-sutando` under workspace), drift-detects an existing alias, and
# either appends or in-place rewrites the rc file. --auto guards via a
# per-host sentinel at $WORKSPACE/state/.shell-setup-prompted-<hostname> so
# this never re-pesters after the user's initial yes/no.
# Failures are non-fatal — startup.sh continues regardless.
if [ -x "$REPO/src/agent/claude/cli/sutando-shell-setup.sh" ]; then
  bash "$REPO/src/agent/claude/cli/sutando-shell-setup.sh" --auto || true
fi

reap_stale_task_watcher "$WORKSPACE/state/watch-tasks-stream.pid"

# Post-M0: repo-root tasks/results/data are NOT created. Pre-M0 this block
# ran `mkdir -p tasks results data` as back-compat for unmigrated scripts —
# but with everything routed through $WORKSPACE/{tasks,results,data} via the
# M0 helper, those empty repo-root dirs were just noise that polluted
# `git status` after every startup. If a stale script still writes to a bare
# relative path, that's a bug to fix in the script (grep `mkdir.*\btasks\b`
# under src/ + scripts/ + skills/), not a reason to keep this here.

# Archive stale results/*.txt (>24h) BEFORE any service starts iterating
# results/. Prevents the 2026-04-15 DM-flood class of incidents where a
# freshly-restarted task-bridge or discord-bridge poll loop sees a backlog
# of long-dead result files and re-delivers them. Post-mortem:
# notes/post-mortem-dm-flood-2026-04-15.md.
[ -n "$PY" ] && "$PY" "$REPO/src/archive-stale-results.py" || true

# Core heartbeat — per-host alive signal under state/cores/<hostname>.alive.
# Foundation for multi-core / cross-machine "who's running?" checks. Single
# instance per host; gracefully cleans up its .alive file on SIGTERM.
if ! pgrep -f "src/core_heartbeat.py" > /dev/null 2>&1; then
  echo "  Starting core heartbeat..."
  # The ✓ must live INSIDE the guard. `[ -n "$PY" ] && cmd &` followed by an
  # unconditional echo claims a start that never happened when no interpreter
  # resolved — and this one is the per-host liveness signal, so a false ✓ makes
  # the node look alive with nothing writing .alive.
  if [ -n "$PY" ]; then
    "$PY" "$REPO/src/core_heartbeat.py" > /tmp/core-heartbeat.log 2>&1 &
    echo "  ✓ core heartbeat"
  else
    echo "  ⊘ core heartbeat skipped — no runnable python3"
  fi
else
  echo "  ✓ core heartbeat (already running)"
fi

# Services-status emitter — aggregates sidecar liveness into
# state/services-status.json for the desktop Settings → Services surface.
# Single instance per host; ~30s cadence; SIGTERM-clean like the heartbeat.
if ! pgrep -f "$REPO/src/services_status.py" > /dev/null 2>&1; then
  echo "  Starting services-status emitter..."
  if [ -n "$PY" ]; then
    "$PY" "$REPO/src/services_status.py" > /tmp/services-status.log 2>&1 &
    echo "  ✓ services-status emitter"
  else
    echo "  ⊘ services-status emitter skipped — no runnable python3"
  fi
else
  echo "  ✓ services-status emitter (already running)"
fi

# 0. Credential proxy for quota tracking (port 7846).
# Prefer the launchd-supervised job (KeepAlive + ThrottleInterval=10s) so the
# proxy restarts on crash instead of leaving a proxy-routed core stranded on a
# dead port (#1086 / #1291). The wrapper evicts any stale manual holder of 7846
# before binding, so this composes with the legacy bare-& launch below. Falls
# back to that legacy launch on older checkouts that lack the launchd template,
# or if the install fails for any reason.
_PROXY_LABEL="com.sutando.credential-proxy"
_PROXY_INSTALLER="$REPO/src/install-credential-proxy-launchd.sh"
if [ -f "$_PROXY_INSTALLER" ] && [ -f "$REPO/src/launchd/$_PROXY_LABEL.plist" ]; then
  # An already-loaded job may carry a plist generated before the current pins existed;
  # the installer owns that comparison because it owns what the plist renders.
  if bash "$_PROXY_INSTALLER" is-current > /dev/null 2>&1; then
    echo "  ✓ credential proxy (launchd-supervised, already loaded, config current)"
  else
    echo "  Installing launchd-supervised credential proxy (fresh or runtime drift)..."
    if bash "$_PROXY_INSTALLER" install > /dev/null 2>&1; then
      # Wait for the supervised proxy to bind before the legacy-launch guard.
      for _ in $(seq 1 10); do lsof -i :7846 > /dev/null 2>&1 && break; sleep 0.5; done
      echo "  ✓ credential proxy (launchd-supervised)"
    else
      echo "  ⚠ launchd install failed — falling back to legacy launch"
    fi
  fi
fi
if ! lsof -i :7846 > /dev/null 2>&1; then
  echo "  Starting credential proxy (port 7846)..."
  # Same dist-only contract as the wrapper and the installer: a bundled host
  # ships dist/ and has no quota-tracker skill dir, so resolving the TS source
  # here would hand run_node_service a path that does not exist and leave the
  # host with no proxy at all ("Claude will connect directly").
  if [ "$BUNDLED_MODE" = "1" ]; then
    _PROXY_SCRIPT="$REPO/dist/credential-proxy.js"
  else
    # Run the proxy from THIS checkout so it resolves the same workspace as the
    # core/dashboard; fall back to the claude-home copy if the skill is absent.
    _PROXY_SCRIPT="$REPO/skills/quota-tracker/scripts/credential-proxy.ts"
    [ -f "$_PROXY_SCRIPT" ] || _PROXY_SCRIPT="$(bash "$REPO/scripts/sutando-config.sh" claude-home-path skills/quota-tracker/scripts/credential-proxy.ts)"
  fi
  run_node_service credential-proxy "$_PROXY_SCRIPT" > /tmp/credential-proxy.log 2>&1 &
  sleep 1
  if lsof -i :7846 > /dev/null 2>&1; then
    echo "  ✓ credential proxy"
    export ANTHROPIC_BASE_URL=http://localhost:7846
  else
    echo "  ⚠ credential proxy failed — Claude will connect directly (check /tmp/credential-proxy.log)"
  fi
else
  echo "  ✓ credential proxy (already running)"
  export ANTHROPIC_BASE_URL=http://localhost:7846
fi

# 0b. Local usage collector. Token/cost metrics are on by default so dashboard
# usage panels work out of the box; plaintext prompt/tool hooks retain their
# previous explicit SUTANDO_OBS_COLLECTOR=1 opt-in. Set the flag to 0 to disable
# the local collector entirely. Cloud forwarding remains separately opt-in.
# shellcheck source=src/observability/startup-policy.sh
source "$REPO/src/observability/startup-policy.sh"
OBS_COLLECTOR_READY=0
if obs_collector_enabled; then
  OBS_PORT="${SUTANDO_OBS_PORT:-4000}"
  if ! lsof -i :"$OBS_PORT" > /dev/null 2>&1; then
    echo "  Starting obs collector (port $OBS_PORT)..."
    SUTANDO_WORKSPACE="$WORKSPACE" SUTANDO_OBS_PORT="$OBS_PORT" \
      run_node_service boot "$REPO/src/observability/boot.ts" > "$LOGS_DIR/collector.log" 2>&1 &
    for _obs_wait in 1 2 3 4 5 6 7 8 9 10; do
      obs_collector_healthy "$OBS_PORT" && break
      sleep 0.2
    done
  fi
  if obs_collector_healthy "$OBS_PORT"; then
    OBS_COLLECTOR_READY=1
    echo "  ✓ obs collector (verified on $OBS_PORT)"
  else
    echo "  ⚠ obs collector unavailable or foreign listener on $OBS_PORT — metrics/hook capture disabled" >&2
  fi
  if [ "$OBS_COLLECTOR_READY" = "1" ]; then
    # Default-on path is metrics-only. Preserve the old explicit =1 behavior
    # for operators who intentionally opted into prompt/tool hook capture.
    if [ -z "${SUTANDO_OBS_METRICS_ENDPOINT:-}" ]; then
      export SUTANDO_OBS_METRICS_ENDPOINT="http://localhost:$OBS_PORT"
    fi
    if obs_hooks_enabled && [ -z "${SUTANDO_OBS_ENDPOINT:-}" ]; then
      export SUTANDO_OBS_ENDPOINT="http://localhost:$OBS_PORT"
    fi
  fi
else
  echo "  ~ obs collector (disabled via SUTANDO_OBS_COLLECTOR=0)"
fi
# A port can LISTEN while the service never responds (single-threaded server
# blocked on a silent connection, hung event loop). The lsof guards below only
# check LISTEN, so a wedged service is "already running" forever — exactly how
# the dashboard stayed unreachable for hours and voice-agent for 26h
# (2026-06-10). Probe with a real HTTP request before each guard; on timeout,
# kill the listener so the normal start path takes over. Any response byte
# counts as alive (a 404 or a WS handshake rejection is fine — curl exits 28
# only when nothing came back before the deadline). Probe path matches
# health-check.py's check_port: a cheap unknown path, NOT "/" (dashboard's "/"
# runs health-check.py as a subprocess — probing it from here would recurse).
reap_wedged_listener() {
  local port="$1" name="$2" rc=0
  lsof -i :"$port" -sTCP:LISTEN > /dev/null 2>&1 || return 0
  curl -s -o /dev/null -m 10 "http://127.0.0.1:$port/__liveness_probe__" || rc=$?
  if [ "$rc" -eq 28 ]; then
    echo "  ⚠ $name (port $port) listening but unresponsive — killing wedged listener"
    lsof -ti :"$port" -sTCP:LISTEN | xargs kill 2>/dev/null || true
    sleep 1
  fi
  return 0
}

# 1. Voice agent (Gemini Live on port 9900; optional)
if [ "${SKIP_VOICE:-}" = "1" ]; then
  echo "  ~ voice agent (disabled — no Gemini voice key)"
else
  # If a launchd job owns this service, delegate to launchd so the two managers
  # don't fight over port 9900 (issue #1888 bug 2 — duplicate listeners when
  # launchd respawns while startup.sh's direct process still holds the port).
  #
  # The wedge reap runs BEFORE the launchd ownership check intentionally:
  # KeepAlive only triggers on process exit, not on hang. A hung process can hold
  # the port indefinitely — reaping it first frees the port so the subsequent
  # kickstart (or launchd's own respawn on exit) gets a clean bind.
  # NOT the generic reap_wedged_listener: voice-agent's kill path goes through
  # the guarded voice-lock.py takeover (startup-runtime.sh, amendment T4) —
  # identity mismatch means nothing is signaled.
  reap_wedged_voice_agent 9900
  if launchctl print "gui/$(id -u)/com.sutando.voice-agent" > /dev/null 2>&1; then
    if ! lsof -i :9900 > /dev/null 2>&1; then
      launchctl kickstart "gui/$(id -u)/com.sutando.voice-agent" > /dev/null 2>&1 || true
      # Poll up to 5s — kickstart is known to silently no-op (restart-voice-agent.sh §4)
      _va_waited=0
      while [ "$_va_waited" -lt 5 ] && ! lsof -i :9900 > /dev/null 2>&1; do
        sleep 1; _va_waited=$(( _va_waited + 1 ))
      done
      if lsof -i :9900 > /dev/null 2>&1; then
        echo "  ✓ voice agent (launchd-supervised)"
      else
        echo "  ⚠ voice agent (launchd kickstart issued — port 9900 not yet bound)"
      fi
    else
      echo "  ✓ voice agent (launchd-supervised)"
    fi
  elif ! lsof -i :9900 > /dev/null 2>&1; then
    echo "  Starting voice agent (port 9900)..."
    run_node_service voice-agent src/voice-agent.ts > "$LOGS_DIR/voice-agent.log" 2>&1 &
    echo "  ✓ voice agent"
  else
    echo "  ✓ voice agent (already running)"
  fi
fi

# 1b. Call-tier advertisement (resident, re-emitting): write state/call-tiers.json
# so the runtime descriptor advertises which DIRECT call endpoints are reachable
# now (Track 9 availability-driven call-tier menu). Backgrounded — it probes
# tailscale with its own short timeout and never blocks the rest of startup;
# absent file just means the descriptor advertises no direct tiers (client falls
# back to cloud). `--interval 60` keeps it resident and re-emits every 60s so the
# advertisement tracks reachability changes AFTER boot (tailnet/VPN coming up
# post-startup would otherwise leave Direct(Tailscale) greyed until a restart).
run_node_service emit-call-tiers src/emit-call-tiers.ts --interval 60 > "$LOGS_DIR/emit-call-tiers.log" 2>&1 &
echo "  ✓ call-tiers advertisement (re-emit 60s)"

# 2. Web client (port 8080 by default; CLIENT_PORT may avoid a local conflict)
WEB_CLIENT_PORT="${CLIENT_PORT:-8080}"
reap_wedged_listener "$WEB_CLIENT_PORT" web-client
# Same launchd-deconflict guard as voice-agent above for the launchd-owned
# default port. A custom CLIENT_PORT remains a directly managed opt-in.
if [ "$WEB_CLIENT_PORT" = "8080" ] && launchctl print "gui/$(id -u)/com.sutando.web-client" > /dev/null 2>&1; then
  if ! lsof -i :"$WEB_CLIENT_PORT" > /dev/null 2>&1; then
    launchctl kickstart "gui/$(id -u)/com.sutando.web-client" > /dev/null 2>&1 || true
    _wc_waited=0
    while [ "$_wc_waited" -lt 5 ] && ! lsof -i :"$WEB_CLIENT_PORT" > /dev/null 2>&1; do
      sleep 1; _wc_waited=$(( _wc_waited + 1 ))
    done
    if lsof -i :"$WEB_CLIENT_PORT" > /dev/null 2>&1; then
      echo "  ✓ web client (launchd-supervised)"
    else
      echo "  ⚠ web client (launchd kickstart issued — port $WEB_CLIENT_PORT not yet bound)"
    fi
  else
    echo "  ✓ web client (launchd-supervised)"
  fi
elif ! lsof -i :"$WEB_CLIENT_PORT" > /dev/null 2>&1; then
  echo "  Starting web client (port $WEB_CLIENT_PORT)..."
  run_node_service web-client src/web-client.ts > "$LOGS_DIR/web-client.log" 2>&1 &
  echo "  ✓ web client"
else
  echo "  ✓ web client (already running on $WEB_CLIENT_PORT)"
fi

# 2b. Tailnet HTTPS front for browser wss:// voice reach (opt-in).
# When SUTANDO_TAILNET_SERVE is on, front the webUI with `tailscale serve` so a
# browser on another device on your tailnet can open wss:// to the /ws proxy
# (plain ws:// to a non-localhost host is blocked as mixed content from an HTTPS
# page). Best-effort: a missing prerequisite (tailscale down, HTTPS not enabled
# for the tailnet) must NOT fail startup — the helper prints its own diagnostics
# to the log. `tailscale serve --bg` is idempotent, so re-running each boot is
# safe. Pairs with SUTANDO_LAN_SHARE=1 (which the helper reminds you to set).
if [[ "${SUTANDO_TAILNET_SERVE:-}" =~ ^(1|true|yes|on)$ ]]; then
  echo "  Fronting webUI with tailscale serve (SUTANDO_TAILNET_SERVE on)..."
  if bash "$REPO/scripts/tailscale-serve-voice.sh" >> "$LOGS_DIR/tailscale-serve.log" 2>&1; then
    echo "  ✓ tailscale serve (browser wss:// tailnet reach)"
  else
    echo "  ⚠ tailscale serve skipped — see $LOGS_DIR/tailscale-serve.log"
  fi
fi

# 3. Dashboard (port 7844)
reap_wedged_listener 7844 dashboard
if ! lsof -i :7844 > /dev/null 2>&1; then
  echo "  Starting dashboard (port 7844)..."
  if [ -n "$PY" ]; then
    "$PY" src/dashboard.py > "$LOGS_DIR/dashboard.log" 2>&1 &
    echo "  ✓ dashboard"
  else
    echo "  ~ dashboard skipped (no runnable python3)"
  fi
else
  echo "  ✓ dashboard (already running)"
fi

# 4. Agent API (port 7843)
reap_wedged_listener 7843 agent-api
if ! lsof -i :7843 > /dev/null 2>&1; then
  echo "  Starting agent API (port 7843)..."
  if [ -n "$PY" ]; then
    "$PY" src/agent-api.py > "$LOGS_DIR/agent-api.log" 2>&1 &
    echo "  ✓ agent API"
  else
    echo "  ~ agent API skipped (no runnable python3)"
  fi
else
  echo "  ✓ agent API (already running)"
fi

# 5. Screen capture server (port 7845)
# Skip when Screen Recording perm is missing — otherwise we'd start a server
# that returns black-PNG denials, which is exactly the stale-7845 state the
# permcheck above warns about.
reap_wedged_listener 7845 screen-capture
if ! lsof -i :7845 > /dev/null 2>&1; then
  if [ "$PERM_OK" -eq 1 ]; then
    echo "  Starting screen capture (port 7845)..."
    if [ -n "$PY" ]; then
      "$PY" src/screen-capture-server.py > "$LOGS_DIR/screen-capture.log" 2>&1 &
      echo "  ✓ screen capture"
    else
      echo "  ⊘ screen capture skipped — no runnable python3"
    fi
  else
    echo "  ⊘ screen capture skipped — grant Screen Recording perm first, then re-run startup.sh"
  fi
else
  echo "  ✓ screen capture (already running)"
fi

# 5a-bis. Portfolio + research dashboard (port 8899) — idempotent self-guard.
# Serves the research webapp with the live (read-only) portfolio panel and keeps
# its snapshot fresh via a background refresher daemon. No-op if not initialised.
if [ -d "$REPO/skills/portfolio-research" ]; then
  if [ ! -d "$WORKSPACE/research/portfolio/webapp" ]; then
    bash "$REPO/skills/portfolio-research/scripts/init-evergreen-webapp.sh" \
      > "$LOGS_DIR/portfolio-dashboard.log" 2>&1 || true
  fi
  bash "$REPO/skills/portfolio-research/scripts/serve-dashboard.sh" \
    >> "$LOGS_DIR/portfolio-dashboard.log" 2>&1 || true
  echo "  ✓ portfolio dashboard (port 8899)"
fi

# Vault scanner preflight, shared by the three bridges that intercept secrets
# (telegram, discord, slack — all import src/vault_intercept.py).
#
# detect-secrets has been treated as a CI-only test dep, but it is a RUNTIME dep
# of a shipping feature: without it vault_intercept fails closed and REFUSES
# every unquoted `vault set KEY VALUE` — which is the form CLAUDE.md documents
# as the way to store a secret. Quoted values still work, so the degradation is
# safe but silent: nothing surfaced it until an owner tried to store a secret
# and got a refusal.
#
# This mirrors how discord.py / slack_bolt are handled below — name the missing
# dep and the exact fix at the startup console, don't auto-install.
#
# Checked PER INTERPRETER because each bridge is launched with whichever python
# had ITS client library, and those are frequently not the same binary.
_vault_scanner_check() {
  _vsc_py="$1"; _vsc_who="$2"
  [ -n "$_vsc_py" ] || return 0
  if ! "$_vsc_py" -c "import detect_secrets" >/dev/null 2>&1; then
    echo "  ~ $_vsc_who: detect-secrets missing in $_vsc_py — unquoted \`vault set\` will be REFUSED"
    # Both plain and --user installs are blocked by PEP 668 on stock
    # Homebrew/macOS python, so the fallback is named up front rather than
    # leaving the operator to rediscover it.
    echo "      fix: $_vsc_py -m pip install detect-secrets   (add --break-system-packages if PEP 668 blocks it)"
  fi
}

# Prefer one launchd job per configured channel bridge. A loaded job is not
# necessarily a running bridge: when a token is removed, the wrapper stays as
# an idle launchd sentinel. Kickstart that state after credentials return, and
# only report success once the actual bridge process exists.
channel_bridge_supervised() {
  local channel="$1"
  local label="com.sutando.$channel-bridge"
  local service="gui/$(id -u)/$label"
  local installer="$REPO/src/install-channel-bridge-launchd.sh"
  local template="$REPO/src/launchd/com.sutando.channel-bridge.plist"
  local pattern="src/$channel-bridge\\.py$"

  [ -f "$installer" ] && [ -f "$template" ] || return 1
  if launchctl print "$service" >/dev/null 2>&1; then
    if pgrep -f "$pattern" >/dev/null 2>&1; then
      return 0
    fi
    launchctl kickstart -k "$service" >/dev/null 2>&1 || return 1
  else
    bash "$installer" "$channel" >/dev/null 2>&1 || return 1
  fi
  for _ in $(seq 1 12); do
    pgrep -f "$pattern" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

# 6. Telegram bridge (optional — needs TELEGRAM_BOT_TOKEN, skip with SKIP_TELEGRAM=1)
if [ "${SKIP_TELEGRAM:-}" = "1" ]; then
  echo "  ~ telegram bridge (skipped via SKIP_TELEGRAM)"
elif _TG_ENV="$(bash "$REPO/scripts/sutando-config.sh" claude-home-path channels/telegram/.env)"; _tok_rc=0; "$PY" "$REPO/src/channel_token.py" --has TELEGRAM_BOT_TOKEN --env-file "$_TG_ENV" 2>/dev/null || _tok_rc=$?; if [ "$_tok_rc" -eq 0 ]; then true; elif [ "$_tok_rc" -eq 3 ]; then false; else [ -f "$_TG_ENV" ] && grep -q "TELEGRAM_BOT_TOKEN=" "$_TG_ENV" 2>/dev/null; fi; then
  if channel_bridge_supervised telegram; then
    echo "  ✓ telegram bridge (launchd-supervised)"
  elif ! pgrep -f "telegram-bridge" > /dev/null 2>&1; then
    echo "  Starting Telegram bridge..."
    # Pick an interpreter that can actually verify TLS. A cert-less framework
    # python (e.g. /Library/Frameworks/.../3.13 without certifi) resolves first
    # on some PATHs and then fails EVERY Telegram long-poll with
    # CERTIFICATE_VERIFY_FAILED — silently dropping all messages (cost us ~10h
    # on 2026-06-15, caught only by a stale-heartbeat health warning).
    _tg_tls_ok() { "$1" -c 'import urllib.request as u; u.urlopen("https://api.telegram.org",timeout=8)' >/dev/null 2>&1; }
    # Seed from the resolved interpreter, not the bare name: _tg_tls_ok EXECUTES
    # its argument, so a bare "python3" here runs the CLT stub before the probe
    # can judge anything.
    TGPY="$PY"
    if [ -n "$TGPY" ] && ! _tg_tls_ok "$TGPY"; then
      for _c in "$(pyenv which python3 2>/dev/null)" python3.12 python3.11; do
        [ -n "$_c" ] && command -v "$_c" >/dev/null 2>&1 && _tg_tls_ok "$_c" && TGPY="$_c" && break
      done
    fi
    if [ -n "$TGPY" ]; then
      "$TGPY" src/telegram-bridge.py > "$LOGS_DIR/telegram-bridge.log" 2>&1 &
      echo "  ✓ telegram bridge ($TGPY)"
      _vault_scanner_check "$TGPY" "telegram bridge"
    else
      echo "  ~ telegram bridge skipped (no runnable python3)"
    fi
  else
    echo "  ✓ telegram bridge (already running)"
  fi
else
  echo "  ~ telegram bridge (no token — optional)"
fi

# Remote gateway bridge (optional channel — primary + every named secondary
# lane). Defined as start_gateway_lanes() in startup-runtime.sh so it can also
# be invoked standalone via scripts/restart-gateway-lanes.sh, without the rest
# of this boot sequence (in particular without reap_stale_task_watcher above —
# see that function's call site for why re-running the whole of startup.sh
# just to reconnect a dropped lane is unsafe mid-session).
start_gateway_lanes

# 7. Discord bridge (optional — needs DISCORD_BOT_TOKEN + discord.py)
#
# `python3` on $PATH is unpredictable across installs (miniconda, system,
# Homebrew). The bridge itself self-rescues by re-execing under a known-good
# interpreter (see top of src/discord-bridge.py), but launching it with the
# right one in the first place avoids the wasted process + traceback noise.
# Probe a fixed list of candidates in priority order; first one with discord.py
# wins. Same probe is also what's used in the bridge's rescue fallback.
if [ "${SKIP_DISCORD:-}" = "1" ]; then
  echo "  ~ discord bridge (skipped via SKIP_DISCORD)"
elif _DC_ENV="$(bash "$REPO/scripts/sutando-config.sh" claude-home-path channels/discord/.env)"; _tok_rc=0; "$PY" "$REPO/src/channel_token.py" --has DISCORD_BOT_TOKEN --env-file "$_DC_ENV" 2>/dev/null || _tok_rc=$?; if [ "$_tok_rc" -eq 0 ]; then true; elif [ "$_tok_rc" -eq 3 ]; then false; else [ -f "$_DC_ENV" ] && grep -q "DISCORD_BOT_TOKEN=" "$_DC_ENV" 2>/dev/null; fi; then
  PYTHON_WITH_DISCORD=""
  for _p in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    # Same substitution as the slack loop below: probing EXECUTES the candidate,
    # so a bare `python3` here is the CLT stub on a Mac without developer tools.
    [ "$_p" = "python3" ] && _p="$PY"
    [ -n "$_p" ] || continue
    if command -v "$_p" >/dev/null 2>&1 && "$_p" -c "import discord" 2>/dev/null; then
      PYTHON_WITH_DISCORD="$_p"
      break
    fi
  done
  # Fallback (v0.8 cold-start fix). If none of the probed candidates had
  # discord.py importable (PATH stripped by launchctl, conda shim shadowing
  # homebrew, etc.) the original loop fell through silently. Per Lucy's
  # PR #1496 review: probe PATH `python3` for `import discord` before
  # falling back to it — avoids handing the bridge a guaranteed-fail
  # interpreter that would crash-loop on every boot. If THAT probe also
  # fails, keep the labeled skip with the pip-install hint (names the
  # missing dep + fix at the startup console).
  # Probe the RESOLVED interpreter, never a bare `python3`. `command -v python3`
  # succeeds against the Xcode-CLT stub and the `-c "import discord"` that
  # follows EXECUTES it — raising the modal this PR exists to remove (CR #2599,
  # @qingyun-wu). $PY is empty when nothing runnable exists, so the probe is
  # skipped entirely rather than falling back to the stub.
  if [ -z "$PYTHON_WITH_DISCORD" ] && [ -n "$PY" ] && "$PY" -c "import discord" 2>/dev/null; then
    PYTHON_WITH_DISCORD="$PY"
    echo "  ~ discord bridge using resolved python3 ($PY — no probed interp matched; it has discord.py)"
  fi
  if [ -z "$PYTHON_WITH_DISCORD" ]; then
    echo "  ~ discord bridge (no python with discord.py — run: /opt/homebrew/bin/pip3 install discord.py)"
  elif channel_bridge_supervised discord; then
    echo "  ✓ discord bridge (launchd-supervised)"
  elif ! pgrep -f "discord-bridge" > /dev/null 2>&1; then
    echo "  Starting Discord bridge with $PYTHON_WITH_DISCORD..."
    PYTHONUNBUFFERED=1 "$PYTHON_WITH_DISCORD" src/discord-bridge.py > "$LOGS_DIR/discord-bridge.log" 2>&1 &
    echo "  ✓ discord bridge"
    _vault_scanner_check "$PYTHON_WITH_DISCORD" "discord bridge"
  else
    echo "  ✓ discord bridge (already running)"
  fi
else
  echo "  ~ discord bridge (no token — optional)"
fi

# 7b. Slack bridge (optional — needs SLACK_BOT_TOKEN + SLACK_APP_TOKEN + slack_bolt)
# Probes the same Python-interpreter candidates as the discord bridge so a
# fresh-install miniconda env doesn't silently miss slack_bolt.
if [ "${SKIP_SLACK:-}" = "1" ]; then
  echo "  ~ slack bridge (skipped via SKIP_SLACK)"
elif _SL_ENV="$(bash "$REPO/scripts/sutando-config.sh" claude-home-path channels/slack/.env)"; _tok_rc=0; "$PY" "$REPO/src/channel_token.py" --has SLACK_BOT_TOKEN --env-file "$_SL_ENV" 2>/dev/null || _tok_rc=$?; _tok_rc2=0; "$PY" "$REPO/src/channel_token.py" --has SLACK_APP_TOKEN --env-file "$_SL_ENV" 2>/dev/null || _tok_rc2=$?; [ "$_tok_rc" -eq 0 ] && [ "$_tok_rc2" -ne 0 ] && _tok_rc=3; if [ "$_tok_rc" -eq 0 ]; then true; elif [ "$_tok_rc" -eq 3 ]; then false; else [ -f "$_SL_ENV" ] && grep -q "SLACK_BOT_TOKEN=" "$_SL_ENV" 2>/dev/null; fi; then
  PYTHON_WITH_SLACK=""
  for _p in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    # Substitute the RESOLVED interpreter for the bare `python3` candidate:
    # probing a candidate EXECUTES it, and a bare `python3` on a Mac without
    # developer tools is the CLT stub (CR #2599, @qingyun-wu). Empty $PY drops
    # out of the list rather than degrading to the stub.
    #
    # Done inside the loop rather than in the `for` list so the pre-existing
    # /opt/... literals stay on an UNCHANGED line — rewriting that line re-adds
    # them as new, and REVIEW.md's path scan flags added /opt/ paths.
    [ "$_p" = "python3" ] && _p="$PY"
    [ -n "$_p" ] || continue
    if command -v "$_p" >/dev/null 2>&1 && "$_p" -c "import slack_bolt" 2>/dev/null; then
      PYTHON_WITH_SLACK="$_p"
      break
    fi
  done
  if [ -z "$PYTHON_WITH_SLACK" ]; then
    echo "  ~ slack bridge (no python with slack_bolt — run: /opt/homebrew/bin/pip3 install slack_bolt)"
  elif channel_bridge_supervised slack; then
    echo "  ✓ slack bridge (launchd-supervised)"
  elif ! pgrep -f "slack-bridge" > /dev/null 2>&1; then
    echo "  Starting Slack bridge with $PYTHON_WITH_SLACK..."
    # Source the env file so SLACK_BOT_TOKEN / SLACK_APP_TOKEN reach the child.
    # The gate can now pass on a vault-only token, so this file need not exist.
    # Sourcing a missing path under the top-level `set -e` aborts ALL of startup
    # — every service after this line — to launch an OPTIONAL bridge. Guarded
    # with if/then/fi, not `&&`: a failing `&&` chain is itself non-zero and
    # would abort identically. (@john-the-dev on #2638.)
    if [ -f "$_SL_ENV" ]; then set -a; . "$_SL_ENV"; set +a; fi
    "$PYTHON_WITH_SLACK" src/slack-bridge.py > "$LOGS_DIR/slack-bridge.log" 2>&1 &
    echo "  ✓ slack bridge"
    _vault_scanner_check "$PYTHON_WITH_SLACK" "slack bridge"
  else
    echo "  ✓ slack bridge (already running)"
  fi
else
  echo "  ~ slack bridge (no token — optional)"
fi

# 8. Phone conversation server + ngrok (optional — needs Twilio + Gemini credentials)
if [ "${SKIP_PHONE:-}" = "1" ]; then
  echo "  ~ conversation server (skipped via SKIP_PHONE)"
elif ! phone_stack_enabled; then
  echo "  ~ conversation server (disabled — no Gemini voice key)"
# Anchored + non-empty value: the unanchored substring form also matched the
# commented template placeholder (`# TWILIO_ACCOUNT_SID=ACxxxxxxxxx`), starting
# conversation-server and a PUBLIC ngrok tunnel on hosts with no Twilio at all.
# Mirrors twilio_configured() in src/health-check.py — keep the two in sync.
elif grep -qE '^[[:space:]]*TWILIO_ACCOUNT_SID=[^[:space:]]' .env 2>/dev/null; then
  if ! pgrep -f "conversation-server" > /dev/null 2>&1; then
    echo "  Starting conversation server..."
    run_node_service conversation-server skills/phone-conversation/scripts/conversation-server.ts > /tmp/conversation-server.log 2>&1 &
    echo "  ✓ conversation server (port 3100)"
  else
    echo "  ✓ conversation server (already running)"
  fi
  if ! pgrep -f "ngrok" > /dev/null 2>&1; then
    echo "  Starting ngrok tunnel..."
    # If NGROK_DOMAIN is set in .env, use the reserved domain for a stable URL.
    # Otherwise ngrok picks a random subdomain and the Twilio webhook must be
    # updated manually on every restart.
    NGROK_DOMAIN_VAL=$(grep -E '^NGROK_DOMAIN=' .env 2>/dev/null | head -1 | cut -d'=' -f2- | tr -d '"' | tr -d "'")
    if [ -n "$NGROK_DOMAIN_VAL" ]; then
      ngrok http 3100 --domain="$NGROK_DOMAIN_VAL" --log=stdout > /tmp/ngrok.log 2>&1 &
    else
      ngrok http 3100 --log=stdout > /tmp/ngrok.log 2>&1 &
    fi
    sleep 3
    # `${PY:-cat} -c` was not a fallback: with no interpreter it runs `cat -c`,
    # which is not a valid cat invocation on either BSD or GNU. It fails, the
    # `|| echo ""` swallows it, and the operator is left with a running ngrok
    # tunnel and a stale WEBHOOK_BASE_URL — Twilio then posts to the previous
    # session's URL. Skip explicitly and say why instead.
    if [ -n "$PY" ]; then
      NGROK_URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null | "$PY" -c "import sys,json; d=json.load(sys.stdin); print(d['tunnels'][0]['public_url'])" 2>/dev/null || echo "")
    else
      NGROK_URL=""
      echo "  ⊘ ngrok URL not parsed — no runnable python3; set WEBHOOK_BASE_URL in .env manually"
    fi
    if [ -n "$NGROK_URL" ]; then
      # Update WEBHOOK_BASE_URL in .env — portable in-place edit.
      # `sed -i ''` is BSD-only; on Macs with Homebrew gnu-sed in PATH it
      # silently fails (treats '' as an input filename). tmpfile + mv works
      # on both. See #412 cold-review for the coreutils-in-PATH context.
      if grep -q "WEBHOOK_BASE_URL=" .env; then
        tmpfile=$(mktemp)
        sed "s|WEBHOOK_BASE_URL=.*|WEBHOOK_BASE_URL=$NGROK_URL|" .env > "$tmpfile" && mv "$tmpfile" .env
      else
        echo "WEBHOOK_BASE_URL=$NGROK_URL" >> .env
      fi
      if [ -n "$NGROK_DOMAIN_VAL" ]; then
        echo "  ✓ ngrok ($NGROK_URL — reserved domain, no Twilio update needed)"
      else
        echo "  ✓ ngrok ($NGROK_URL)"
        # Trailing slash is stripped because conversation-server.ts strips it
        # before binding WEBHOOK_BASE_URL; comparing unnormalised reads as drift.
        TWILIO_CFG_URL=$(grep -E '^TWILIO_WEBHOOK_URL=' .env 2>/dev/null | head -1 \
          | cut -d'=' -f2- | cut -d'#' -f1 | tr -d '"' | tr -d "'" | xargs | sed 's:/*$::')
        NGROK_CMP="${NGROK_URL%/}"
        if [ -z "$TWILIO_CFG_URL" ]; then
          echo "  ⚠ Point the Twilio webhook at: $NGROK_URL (no TWILIO_WEBHOOK_URL recorded)"
        elif [ "$TWILIO_CFG_URL" = "$NGROK_CMP" ]; then
          :
        elif ! printf '%s' "$TWILIO_CFG_URL" | grep -qE '\.ngrok(-free)?\.(app|io)$'; then
          # A non-ngrok tunnel (e.g. Funnel) is authoritative: the phone server
          # binds it and never starts ngrok, so this ngrok is not its tunnel.
          :
        else
          # The server binds WEBHOOK_BASE_URL from this var, so a stale value
          # leaves TwiML and <Stream> pointing at a tunnel that no longer exists.
          echo "  ⚠ ngrok URL moved — BOTH sides are stale:"
          echo "      was: $TWILIO_CFG_URL"
          echo "      now: $NGROK_URL"
          echo "      1. update the Twilio console webhook to the new URL"
          echo "      2. set TWILIO_WEBHOOK_URL=$NGROK_URL in .env and restart the"
          echo "         phone conversation server — it binds this at startup"
        fi
      fi
    else
      echo "  ✗ ngrok (failed to start)"
    fi
  else
    echo "  ✓ ngrok (already running)"
  fi
else
  echo "  ~ conversation server (no Twilio creds — optional)"
fi

echo ""

# Verify services actually started (wait a moment, then check ports)
sleep 3
echo "Verifying services..."
VERIFY_PORTS="$WEB_CLIENT_PORT:web-client 7844:dashboard 7843:agent-api"
# Only verify 7845 when we actually tried to start it. Same condition as the start
# branch: a deliberate skip must not render as a crash pointing at an empty log.
if [ "${PERM_OK:-0}" = "1" ]; then
  VERIFY_PORTS="$VERIFY_PORTS 7845:screen-capture"
fi
if [ "${SKIP_VOICE:-}" != "1" ]; then
  VERIFY_PORTS="9900:voice-agent $VERIFY_PORTS"
fi
if phone_stack_enabled && grep -qE '^[[:space:]]*TWILIO_ACCOUNT_SID=[^[:space:]]' .env 2>/dev/null; then
  VERIFY_PORTS="$VERIFY_PORTS 3100:conversation-server"
fi
if [ "${OBS_COLLECTOR_READY:-0}" = "1" ]; then
  VERIFY_PORTS="$VERIFY_PORTS ${SUTANDO_OBS_PORT:-4000}:collector"
fi
# A single probe races a service still binding, so retry briefly. The deadline is
# GLOBAL: per-port it would serialise to ports x settle seconds before core launch.
VERIFY_SETTLE_S="${VERIFY_SETTLE_S:-10}"
verify_deadline=$(( $(date +%s) + VERIFY_SETTLE_S ))
for port_name in $VERIFY_PORTS; do
  port="${port_name%%:*}"
  name="${port_name##*:}"
  # COUNT SLEEPS, never two `date` samples: at 1s granularity the two can
  # straddle a boundary and report 1s for a port that never waited at all.
  waited=0
  while ! lsof -i :"$port" > /dev/null 2>&1 && [ "$(date +%s)" -lt "$verify_deadline" ]; do
    sleep 1
    waited=$((waited + 1))
  done
  if lsof -i :"$port" > /dev/null 2>&1; then
    if [ "$waited" -gt 0 ]; then
      echo "  ✓ $name (port $port, after ${waited}s)"
    else
      echo "  ✓ $name (port $port)"
    fi
  else
    echo "  ✗ $name (port $port) — check $LOGS_DIR/${name}.log"
  fi
done
echo ""


# Delegate to the runtime dispatcher — canonical sutando-core launch command.
# Sutando.app and health recovery use this same Claude-or-Codex selection.
#
# Restore stdout/stderr to the terminal first when the operator is
# interactive: the tee-redirect at the top of this script makes fd 1 a PIPE,
# so start-cli.sh's `[ -t 1 ]` interactivity test always chose the detached
# branch under startup/restart — the user was never auto-attached to the
# core tmux session (owner report 2026-06-11, biting since at least 06-07).
# stdin is untouched by the tee exec, so `-t 0` still tells the truth;
# launchd / Sutando.app runs have non-TTY stdin and keep the detached path.
# The startup log keeps everything except the interactive session itself.
# A startup launched from a tmux pane (notably the self-upgrade service pane)
# must stay detached. Restoring /dev/tty there makes the runtime launcher try
# to attach to sutando-core from inside tmux, which blocks startup forever and
# leaves the old core running without completing recovery.
# --with-app runs BEFORE the exec below (which replaces this process) and is
# guarded: its installer must never take the core down (`set -e` is on).
if [ "$WITH_APP" -eq 1 ]; then
    # --launch, not --supervise: a login-persistent launchd job is a separate
    # decision the user makes explicitly.
    echo "→ menu-bar app (--with-app): building + launching" >&2
    if bash "$REPO/scripts/install-menu-bar-app.sh" --launch; then
        echo "  ✓ menu-bar app launched" >&2
        echo "    auto-start at login is opt-in: bash scripts/install-menu-bar-app.sh --supervise" >&2
    else
        echo "  ✗ menu-bar app setup failed (exit $?) — the core is unaffected and still starting." >&2
    fi
fi

if [ -t 0 ] && [ -z "${TMUX:-}" ]; then
    exec >/dev/tty 2>&1
fi
exec bash "$REPO/src/agent/start-cli.sh"
