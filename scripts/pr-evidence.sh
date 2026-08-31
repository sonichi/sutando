#!/usr/bin/env bash
# Generates PR before/after evidence by RUNNING the commands and capturing their
# real output, so an author never transcribes it and therefore cannot invent it.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REF=""
WT=""
WTPARENT=""

usage() {
    cat <<'USAGE'
Usage:
  bash scripts/pr-evidence.sh 'cmd' ['cmd' ...]            run at the current HEAD
  bash scripts/pr-evidence.sh --at <ref> 'cmd' ['cmd' ...]  run at <ref>, workspace-pinned

Prints a markdown block: each command, its verbatim stdout+stderr, its exit code,
and a stamp naming the sha the block was produced at. Paste it whole.

--at builds the "before" half without touching the live checkout, and pins the
temporary worktree at the LIVE workspace. CONTRIBUTING warns why: an unpinned
worktree resolves its own empty workspace/ and every workspace-reading probe
reports clean no matter what the code does — false-clean evidence.
USAGE
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --at) REF="${2:?--at needs a ref}"; shift 2;;
        -h|--help) usage 0;;
        --) shift; break;;
        -*) echo "pr-evidence: unknown flag '$1'" >&2; usage 2;;
        *) break;;
    esac
done
[[ $# -gt 0 ]] || { echo "pr-evidence: no commands given" >&2; usage 2; }

# Failures are reported, not swallowed: a stranded worktree holds a pinned
# config, and silence is how it stays on disk unnoticed.
cleanup() {
    if [[ -n "$WT" && -d "$WT" ]]; then
        git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1 \
            || echo "pr-evidence: WARNING — could not remove worktree $WT" >&2
    fi
    if [[ -n "$WTPARENT" && -d "$WTPARENT" ]]; then
        rm -rf "$WTPARENT" \
            || echo "pr-evidence: WARNING — left $WTPARENT on disk" >&2
    fi
    return 0
}
trap cleanup EXIT

RUNDIR="$REPO"
if [[ -n "$REF" ]]; then
    git -C "$REPO" rev-parse --verify --quiet "$REF^{commit}" >/dev/null \
        || { echo "pr-evidence: '$REF' is not a commit in this repo" >&2; exit 2; }

    # Resolve unconditionally, before the worktree exists: an unpinned worktree
    # resolves its own empty workspace/ and reports clean whatever the code does.
    LIVE_WS="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null || true)"
    [[ -n "$LIVE_WS" ]] || {
        echo "pr-evidence: cannot resolve the live workspace — refusing --at rather than" >&2
        echo "pr-evidence: emitting evidence from an empty one." >&2
        exit 2
    }

    # mktemp gives 0700; `git worktree add` makes its own dir 0755, so the
    # worktree goes INSIDE the private parent rather than being it.
    WTPARENT="$(mktemp -d "${TMPDIR:-/tmp}/pr-evidence-XXXXXX")"
    WT="$WTPARENT/wt"
    git -C "$REPO" worktree add --detach --quiet "$WT" "$REF" \
        || { echo "pr-evidence: could not create a worktree at '$REF'" >&2; exit 2; }
    RUNDIR="$WT"

    # Bare python3 is the Xcode-CLT stub on a Mac without developer tools — the
    # exact modal this resolver exists to avoid.
    . "$REPO/scripts/python-binary.sh"
    PY_BIN="$(resolve_python "$REPO")"
    [[ -n "$PY_BIN" ]] || {
        echo "pr-evidence: no runnable python3 — cannot pin the worktree's workspace" >&2
        exit 2
    }
    # MINIMAL config, never a copy: the real one carries unrelated local
    # settings (vault, migrate) this tool has no need to duplicate.
    ( umask 077
      "$PY_BIN" - "$WT/sutando.config.local.json" "$LIVE_WS" <<'PY'
import json, pathlib, sys
pathlib.Path(sys.argv[1]).write_text(
    json.dumps({"workspace": {"path": sys.argv[2]}}, indent=2) + "\n")
PY
    ) || { echo "pr-evidence: could not pin the worktree's workspace" >&2; exit 2; }
    chmod 600 "$WT/sutando.config.local.json"
    echo "pr-evidence: worktree at $REF pinned to workspace $LIVE_WS" >&2
fi

# An empty sha would print a stamp that names no commit at all — worse than no
# stamp, because it still looks authoritative. Fail closed.
SHA="$(git -C "$RUNDIR" rev-parse HEAD 2>/dev/null || true)"
[[ -n "$SHA" ]] || {
    echo "pr-evidence: cannot resolve HEAD in $RUNDIR — refusing to stamp a block" >&2
    exit 2
}
LABEL="${REF:-HEAD}"

# The sha names a COMMIT; commands run against the working tree. Untracked or
# modified content is normal here, so record it rather than pretend it is absent.
tree_state() { git -C "$RUNDIR" status --porcelain 2>/dev/null; }
DIRTY_BEFORE="$(tree_state)"

# Body goes to a file, not a variable: $( ) strips trailing newlines, which
# silently welds a command onto its own output.
BODYFILE="$(mktemp "${TMPDIR:-/tmp}/pr-evidence-body-XXXXXX")"
for cmd in "$@"; do
    printf '$ %s\n' "$cmd" >> "$BODYFILE"
    before=$(wc -c < "$BODYFILE")
    ( cd "$RUNDIR" && eval "$cmd" ) >> "$BODYFILE" 2>&1
    rc=$?
    # Separate the marker WITHOUT inventing a line: only when the command wrote
    # something that is not already newline-terminated.
    if [[ "$(wc -c < "$BODYFILE")" -gt "$before" ]] \
       && [[ "$(tail -c1 "$BODYFILE" | wc -l)" -eq 0 ]]; then
        printf '\n' >> "$BODYFILE"
    fi
    printf '[exit %d]\n\n' "$rc" >> "$BODYFILE"
done
DIRTY_AFTER="$(tree_state)"

# Three states, and the third is the one a before-only check misses: a command
# that dirtied the tree while producing the very output being stamped.
if [[ -n "$DIRTY_BEFORE" && "$DIRTY_BEFORE" != "$DIRTY_AFTER" ]]; then
    PROV="$SHA+dirty (tree was modified DURING the run)"
elif [[ -z "$DIRTY_BEFORE" && -n "$DIRTY_AFTER" ]]; then
    PROV="$SHA+dirty (clean before, tree modified DURING the run)"
elif [[ -n "$DIRTY_BEFORE" ]]; then
    PROV="$SHA+dirty"
else
    PROV="$SHA"
fi

printf '<!-- pr-evidence %s %s -->\n' "$LABEL" "$PROV"
# shellcheck disable=SC2016  # the backticks are markdown, not command substitution
printf '**Evidence at `%s` (`%s`)** — generated by `scripts/pr-evidence.sh`, not transcribed.\n' \
    "$LABEL" "$PROV"
[[ "$PROV" == "$SHA" ]] || printf '\n> The working tree was NOT clean at that commit, so this output does not\n> come from `%s` alone.\n' "${SHA:0:8}"
printf '\n```\n'
cat "$BODYFILE"
printf '```\n'
rm -f "$BODYFILE"
