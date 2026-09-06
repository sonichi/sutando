#!/usr/bin/env bash
# Sutando lint: forbid direct workspace-path resolution outside the loader.
#
# All path resolution for the workspace MUST go through
# `src/sutando_config.{py,ts}` (or the wrappers in `src/workspace_default.{py,ts}`
# once they delegate to the loader). This lint catches code that bypasses
# the loader and tries to discover the workspace by:
#
#   • reading $SUTANDO_WORKSPACE directly  (process.env.SUTANDO_WORKSPACE,
#     os.environ["SUTANDO_WORKSPACE"], os.getenv("SUTANDO_WORKSPACE"))
#   • hardcoding the legacy default path  (~/.sutando/workspace/)
#   • using the historic anti-pattern of walking up from __file__  (the
#     `Path(__file__).resolve().parent.parent` pattern that broke when
#     services launched from an app bundle's symlinked src/)
#
# Allowed files (the loader + thin compat wrappers):
#   src/sutando_config.py
#   src/sutando_config.ts
#   src/workspace_default.py
#   src/workspace_default.ts
#   src/util_paths.py  (delegates to workspace_default; retains last-resort fallback)
#   scripts/lint-workspace-resolution.sh  (this file)
#
# Usage:
#   bash scripts/lint-workspace-resolution.sh         # scan whole tree
#   bash scripts/lint-workspace-resolution.sh --diff  # scan only added/modified lines vs base
#
# The CI workflow `.github/workflows/lint-workspace-resolution.yml` calls
# this script with --diff so existing offenders stay un-flagged until
# explicit migration. Local scan (no flag) shows everything so authors
# can spot legacy debt voluntarily.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

mode="${1:-all}"  # all | --diff

# Patterns that signal direct workspace resolution.
# Each pattern is a single ERE alternation we feed grep -E.
PATTERN_ENV='(process\.env|process\.env\[)["'\'']?SUTANDO_WORKSPACE|os\.environ(\.get)?\(["'\'']SUTANDO_WORKSPACE|os\.environ\[["'\'']SUTANDO_WORKSPACE|os\.getenv\(["'\'']SUTANDO_WORKSPACE'
PATTERN_HARDCODED_HOME='\.sutando/workspace'
PATTERN_REPO_WALK='Path\(__file__\)\.resolve\(\)\.parent\.parent'

# LINE-scoped opt-out. A file-level ALLOWED entry excuses the WHOLE file, which is a
# blind spot rather than an exemption: @john-the-dev demonstrated it on #2639 by adding a
# real `Path(__file__).resolve().parent.parent / "workspace"` to an allowlisted file and
# watching this lint exit 0. Every explicitly-listed repo-tooling script carries the same
# hole. A trailing pragma exempts ONE line and leaves every other line in that file
# visible — the token-specific discipline REVIEW.md already requires of fixture
# exclusions, so one exemption cannot hide a second real offender.
PRAGMA_ALLOW_REPO_ROOT='lint-workspace-resolution: allow-repo-root'

# Doc-notation pattern (markdown + SKILL.md only). Flags the legacy
# env-var-as-path form `$SUTANDO_WORKSPACE/<anything>` in user-facing
# docs — the canonical form post-M0 is `<workspace>/path` with the
# workspace resolution helper documented inline. Bare mentions of the
# env var (e.g. "set $SUTANDO_WORKSPACE to...") aren't flagged — the
# `/` suffix is the discriminator that picks "path literal" out of
# "env-var-as-name". See workspace/results/task-1780443422880.txt for
# the root-cause analysis that motivated this rule (rec a).
PATTERN_DOC_ENV_PATH='\$SUTANDO_WORKSPACE/'

# Allowed files — these may legitimately reference the patterns above
# because they implement the resolution contract, are tests that need to
# set up sys.path / env state before exercising the loader, OR are bash
# scripts that retain the legacy fallback idiom
# `WS="${SUTANDO_WORKSPACE:-$HOME/.sutando/workspace}"` as a defensive
# branch when the wrapper script isn't reachable (e.g. non-checkout
# installs). The fallback path is documented in each script's comments;
# new contributors should still go through the wrapper.
#
# scripts/check-python39-compat.py is the same category: it scans tracked
# files under src/ to enforce the interpreter floor, so it needs the REPO
# root and has no workspace to resolve. Listed explicitly rather than
# spelled `parents[1]` to dodge the regex — evading the lint by rewording
# would defeat the point of having it.
#
# skills/review-preflight/scripts/review-preflight.py is the same category again: it reads REVIEW.md
# from the REPO root to print the review criteria, and has no workspace to
# resolve. It prefers `git rev-parse --show-toplevel` and only walks from
# __file__ when git is unavailable. Listed here rather than reworded to
# `parents[1]`, per the note above — dodging the regex would defeat the lint.
#
# scripts/hermetic-workspace-guard.py is the same category once more, and the
# distinction is worth stating because the file's NAME suggests otherwise: it
# resolves the REPO root only to locate `tests/hermetic-workspace-exemptions.txt`
# and to put `src/` on sys.path. The workspace it actually cares about is obtained
# THROUGH the loader (`from workspace_default import resolve_workspace`) — which is
# the contract this lint exists to enforce, so the guard already complies where it
# counts. Listed here rather than reworded to `parents[1]`, per the note above.
#
# scripts/pool-session-digest.py is the same category: it walks to the REPO
# ROOT solely to import src/util_paths.claude_home_path, the sanctioned resolver
# for the Claude home. It reads no workspace path at all.
#
# scripts/gen-src-map.py uses PATTERN_REPO_WALK to resolve the REPO ROOT
# (to read tracked files under src/ and write docs/), NOT a workspace — same
# category as scripts/check-utc-z-strftime.py and scripts/dedup-conversation-store.py,
# which predate this --diff lint and are grandfathered. A repo-tooling script has
# no workspace to go through the wrapper for; the wrapper resolves the workspace,
# which is the wrong directory here.
ALLOWED='^(src/sutando_config\.(py|ts)|src/workspace_default\.(py|ts)|src/util_paths\.py|src/startup\.sh|src/migration_safety_helpers\.sh|scripts/lint-workspace-resolution\.sh|scripts/lint-sutando-home-path\.sh|scripts/install-git-hooks\.sh|scripts/sutando-config\.sh|scripts/sync-memory\.sh|scripts/sutando-migrate\.sh|scripts/sweep-stranded-claims\.sh|scripts/gen-src-map\.py|scripts/check-python39-compat\.py|skills/review-preflight/scripts/review-preflight\.py|tests/.+\.(test\.)?(py|ts|sh)|src/[^/]+\.test\.py|packages/ag2-sparrow/.*\.py)$'

# Allowed .md files — legitimate uses of `$SUTANDO_WORKSPACE/path` in
# prose, e.g. the workspace contract docs that DESCRIBE the legacy form
# as part of the resolution order, the migration script docs that talk
# about pre-M0 sources, and CHANGELOG/KNOWN_ISSUES that record history.
# Anywhere else, prose paths should use `<workspace>/...` notation.
ALLOWED_DOC='^(docs/workspace-(config|design|contract)\.md|CHANGELOG\.md|KNOWN_ISSUES\.md|CLAUDE\.md|skills/sutando-migrate/SKILL\.md)$'

# Pick which files to scan.
if [[ "$mode" == "--diff" ]]; then
  base="${BASE_REF:-origin/main}"
  # Added or modified files vs base.
  files="$(git diff --name-only --diff-filter=AM "$base"...HEAD)"
else
  # All tracked files of relevant types — code (.py/.ts/.tsx/.sh/.bash) + docs (.md).
  files="$(git ls-files -- '*.py' '*.ts' '*.tsx' '*.sh' '*.bash' '*.md')"
fi

if [[ -z "$files" ]]; then
  echo "✓ no files to scan"
  exit 0
fi

offenders=""

for f in $files; do
  # Skip non-existing (file deleted in this diff) + non-text.
  [[ -f "$f" ]] || continue

  # Markdown branch: scan for legacy `$SUTANDO_WORKSPACE/path` notation.
  # Docs got the lint treatment in rec (a) of task-1780443422880 — `.md`
  # files were the silent backlog the original code-only lint missed.
  if [[ "$f" =~ \.md$ ]]; then
    if grep -E -q "$ALLOWED_DOC" <<< "$f"; then continue; fi
    if [[ "$mode" == "--diff" ]]; then
      added="$(git diff "$base"...HEAD -- "$f" | grep -E '^\+[^+]' || true)"
      if grep -E -q "$PATTERN_DOC_ENV_PATH" <<< "$added"; then
        hits="$(grep -E -n "$PATTERN_DOC_ENV_PATH" <<< "$added" | head -3 || true)"
        offenders+="  • $f (doc)"$'\n'"$hits"$'\n'
      fi
    else
      if grep -E -l "$PATTERN_DOC_ENV_PATH" "$f" >/dev/null 2>&1; then
        hits="$(grep -E -n "$PATTERN_DOC_ENV_PATH" "$f" | head -3 || true)"
        offenders+="  • $f (doc)"$'\n'"$hits"$'\n'
      fi
    fi
    continue
  fi

  # Code branch (the original lint).
  [[ "$f" =~ \.(py|ts|tsx|sh|bash)$ ]] || continue
  if grep -E -q "$ALLOWED" <<< "$f"; then continue; fi

  if [[ "$mode" == "--diff" ]]; then
    # Only flag lines ADDED in the diff (begin with `+` but not `+++`).
    # A pragma'd line is exempt from the REPO-ROOT walk ONLY, and only when it does
    # not also derive a workspace — otherwise the pragma becomes a way to hide the very
    # thing this lint exists to catch, which is the file-level blind spot again at line
    # granularity. Keep the line whenever it mentions a workspace, so a spoofed pragma
    # still fails. The pragma text itself contains "workspace", so it is stripped
    # before that test — otherwise the marker always matches its own guard.
    added="$(git diff "$base"...HEAD -- "$f" | grep -E '^\+[^+]' \
      | awk -v p="$PRAGMA_ALLOW_REPO_ROOT" '{ rest=$0; sub(p,"",rest); if (index($0,p)==0 || tolower(rest) ~ /workspace/) print }' || true)"
    if grep -E -q "$PATTERN_ENV|$PATTERN_HARDCODED_HOME|$PATTERN_REPO_WALK" <<< "$added"; then
      hits="$(grep -E -n "$PATTERN_ENV|$PATTERN_HARDCODED_HOME|$PATTERN_REPO_WALK" <<< "$added" | head -3 || true)"
      offenders+="  • $f"$'\n'"$hits"$'\n'
    fi
  else
    _scan="$(awk -v p="$PRAGMA_ALLOW_REPO_ROOT" '{ rest=$0; sub(p,"",rest); if (index($0,p)==0 || tolower(rest) ~ /workspace/) print }' "$f" || true)"
    if grep -E -q "$PATTERN_ENV|$PATTERN_HARDCODED_HOME|$PATTERN_REPO_WALK" <<< "$_scan"; then
      hits="$(grep -E -n "$PATTERN_ENV|$PATTERN_HARDCODED_HOME|$PATTERN_REPO_WALK" <<< "$_scan" | head -3 || true)"
      offenders+="  • $f"$'\n'"$hits"$'\n'
    fi
  fi
done

if [[ -n "$offenders" ]]; then
  if [[ "$mode" == "--diff" ]]; then
    echo "✖ lint refused: new code introduces direct workspace-path resolution."
  else
    echo "ℹ existing files reference direct workspace-path resolution:"
  fi
  echo ""
  printf '%s' "$offenders"
  echo ""
  echo "All resolution MUST go through src/sutando_config.{py,ts}."
  echo "  Python: from src.sutando_config import resolve_workspace"
  echo "  TS:     import { resolveWorkspace } from './sutando_config.js'"
  echo "  Shell:  bash scripts/sutando-config.sh workspace"
  echo "  Docs:   use <workspace>/path notation (NOT \$SUTANDO_WORKSPACE/path)"
  if [[ "$mode" == "--diff" ]]; then
    exit 1
  fi
fi

if [[ "$mode" == "--diff" ]]; then
  echo "✓ no new workspace-resolution anti-patterns in this diff."
fi
exit 0
