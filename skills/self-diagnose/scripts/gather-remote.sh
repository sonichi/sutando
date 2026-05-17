#!/usr/bin/env bash
# gather-remote.sh — cross-node diagnostic. Closes #421.
#
# Run gather.sh on a remote sutando node via SSH, pull the result back,
# also run gather.sh locally, then produce a structured comparison report.
# Use when "Sutando works on machine A but is broken on machine B" — surface
# the deltas (commit drift, service-state differences, log error counts,
# quota state, PR-view drift) without granting the remote any write access.
#
# Usage:
#   bash skills/self-diagnose/scripts/gather-remote.sh <ssh-target> [window]
#
# Examples:
#   bash skills/self-diagnose/scripts/gather-remote.sh mac-mini
#   bash skills/self-diagnose/scripts/gather-remote.sh user@macbook.local 6h
#
# Window defaults to 24h. Same format as gather.sh (Nh / Nd / Nw).
#
# Output:
#   /tmp/sutando-diagnose-cross-<ts>/local/    — gather.sh output for this node
#   /tmp/sutando-diagnose-cross-<ts>/remote/   — pulled-back gather.sh output
#   /tmp/sutando-diagnose-cross-<ts>/diff.md   — structured comparison
#   notes/diagnose-cross-node-<YYYY-MM-DD>.md  — persisted copy of diff.md
#
# Security
# --------
# Honors the constraints from #421:
#   - **Read-only on the remote.** The only thing executed is gather.sh,
#     which only reads files (git log, health-check, build_log tail, log
#     tails). No mutation, no shell exec beyond gather.sh itself.
#   - **Allowlist via gather.sh's existing scope.** gather.sh already
#     enumerates exactly what gets collected — no .env, no tokens, no
#     credentials. This script only rsyncs THAT directory back; nothing
#     else from the remote.
#   - **Per-session.** No persistent daemon; the SSH connection lives
#     only for the duration of the gather + rsync.
#   - **Secret redaction.** Inherits whatever gather.sh emits. Worth an
#     audit pass when this script sees adoption — see TODO at bottom.
#
# Requirements
# ------------
#   - `ssh <peer>` must succeed (peer in ~/.ssh/config or addressable).
#   - Peer must have a sutando checkout; this script looks for it at
#     ${SUTANDO_REMOTE_REPO:-~/Desktop/sutando} on the remote side. Pass
#     SUTANDO_REMOTE_REPO=/path/on/peer to override per-invocation.
#   - rsync available locally (macOS ships it).

set -euo pipefail

usage() {
    cat <<'USAGE' >&2
Usage: gather-remote.sh <ssh-target> [window]

  ssh-target   SSH alias or user@host (e.g. mac-mini, user@macbook.local)
  window       Time window for gather.sh (default: 24h; format Nh/Nd/Nw)

Env:
  SUTANDO_REMOTE_REPO   Path to sutando checkout on the remote (default: ~/Desktop/sutando)

Examples:
  gather-remote.sh mac-mini
  gather-remote.sh user@macbook.local 6h
USAGE
    exit 1
}

if [ "$#" -lt 1 ] || [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
    usage
fi

PEER="$1"
WINDOW="${2:-24h}"
REMOTE_REPO="${SUTANDO_REMOTE_REPO:-\$HOME/Desktop/sutando}"

TS="$(date +%s)"
OUT="/tmp/sutando-diagnose-cross-$TS"
LOCAL_DIR="$OUT/local"
REMOTE_DIR="$OUT/remote"
mkdir -p "$LOCAL_DIR" "$REMOTE_DIR"

# Resolve our local repo root so we can invoke the LOCAL gather.sh + write
# the persisted notes/ copy. Mirrors gather.sh's own resolution.
LOCAL_REPO=""
if [ -n "${SUTANDO_ROOT:-}" ] && [ -f "$SUTANDO_ROOT/build_log.md" ]; then
    LOCAL_REPO="$SUTANDO_ROOT"
elif [ -f "$PWD/build_log.md" ]; then
    LOCAL_REPO="$PWD"
else
    DIR="$(cd "$(dirname "$0")" && pwd)"
    for _ in 1 2 3 4 5; do
        if [ -f "$DIR/build_log.md" ]; then
            LOCAL_REPO="$DIR"
            break
        fi
        DIR="$(dirname "$DIR")"
        [ "$DIR" = "/" ] && break
    done
fi
[ -z "$LOCAL_REPO" ] && { echo "gather-remote: cannot find local sutando repo (no build_log.md found)" >&2; exit 1; }

echo "gather-remote: peer=$PEER, window=$WINDOW, out=$OUT" >&2

# Quick reachability check before doing the heavy lift.
if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "$PEER" true 2>/dev/null; then
    echo "gather-remote: cannot reach $PEER over SSH (BatchMode failed)" >&2
    echo "  Check ~/.ssh/config, key auth, and connectivity. Manual test:" >&2
    echo "  ssh $PEER true" >&2
    exit 2
fi

# 1) Run gather.sh on the remote. Stream stdout to capture the OUT path
#    (gather.sh prints it as the last line).
echo "gather-remote: running gather.sh on $PEER..." >&2
REMOTE_OUT="$(ssh "$PEER" "
    set -e
    cd $REMOTE_REPO 2>/dev/null || { echo 'gather-remote: \$REMOTE_REPO not found on $PEER ($REMOTE_REPO)' >&2; exit 3; }
    bash skills/self-diagnose/scripts/gather.sh $WINDOW
" 2>/tmp/gather-remote-stderr.$$ | tail -1)"

if [ -z "$REMOTE_OUT" ] || [ "${REMOTE_OUT:0:5}" != "/tmp/" ]; then
    echo "gather-remote: remote gather.sh did not return a valid output path." >&2
    echo "  remote stderr:" >&2
    sed 's/^/    /' /tmp/gather-remote-stderr.$$ >&2 || true
    rm -f /tmp/gather-remote-stderr.$$
    exit 4
fi
rm -f /tmp/gather-remote-stderr.$$
echo "gather-remote: remote output at $PEER:$REMOTE_OUT" >&2

# 2) rsync the remote output dir back locally.
echo "gather-remote: rsyncing remote output back..." >&2
rsync -az "$PEER:$REMOTE_OUT/" "$REMOTE_DIR/"

# 3) Run gather.sh locally (parallelizable as future optim; doing serially
#    so failures from either side surface cleanly).
echo "gather-remote: running gather.sh locally..." >&2
LOCAL_OUT="$(bash "$LOCAL_REPO/skills/self-diagnose/scripts/gather.sh" "$WINDOW" 2>/dev/null | tail -1)"
if [ -d "$LOCAL_OUT" ]; then
    cp -R "$LOCAL_OUT/." "$LOCAL_DIR/"
fi

# 4) Build the comparison report.
DIFF_MD="$OUT/diff.md"
{
    echo "# Cross-node diagnostic — $(date +%Y-%m-%dT%H:%M:%S)"
    echo ""
    echo "**Local:** \`$(hostname -s)\` (sutando at \`$LOCAL_REPO\`)"
    echo "**Remote:** \`$PEER\` (sutando at \`$REMOTE_REPO\` per env)"
    echo "**Window:** $WINDOW"
    echo ""
    echo "## Repo commit alignment"
    LOCAL_HEAD="$(git -C "$LOCAL_REPO" rev-parse --short HEAD 2>/dev/null || echo '?')"
    REMOTE_GITLOG="$REMOTE_DIR/git-log.txt"
    REMOTE_HEAD="?"
    if [ -s "$REMOTE_GITLOG" ]; then
        REMOTE_HEAD="$(head -1 "$REMOTE_GITLOG" | awk '{print $1}')"
    fi
    echo "- local HEAD: \`$LOCAL_HEAD\`"
    echo "- remote HEAD (per git-log.txt first line): \`$REMOTE_HEAD\`"
    if [ "$LOCAL_HEAD" != "$REMOTE_HEAD" ] && [ "$REMOTE_HEAD" != "?" ]; then
        echo ""
        echo "  **⚠ DRIFT** — heads differ. Commits the remote may be missing (last 20):"
        echo ""
        echo "  \`\`\`"
        git -C "$LOCAL_REPO" log --oneline "$REMOTE_HEAD..$LOCAL_HEAD" 2>/dev/null | head -20 | sed 's/^/  /' || echo "  (could not compute; remote HEAD may not exist locally)"
        echo "  \`\`\`"
    fi
    echo ""

    echo "## Health check"
    for side in local remote; do
        H="$OUT/$side/health.txt"
        echo "### $side"
        if [ -s "$H" ]; then
            echo '```'
            cat "$H"
            echo '```'
        else
            echo "_(no health.txt — gather.sh may have failed)_"
        fi
        echo ""
    done

    echo "## Voice-agent error signals (count)"
    for side in local remote; do
        VS="$OUT/$side/voice-agent-signals.txt"
        if [ -f "$VS" ]; then
            n="$(wc -l < "$VS" | tr -d ' ')"
        else
            n="(none)"
        fi
        echo "- $side: **$n** lines"
    done
    echo ""

    echo "## Quota"
    for side in local remote; do
        Q="$OUT/$side/quota.txt"
        echo "### $side"
        if [ -s "$Q" ]; then
            echo '```'
            head -5 "$Q"
            echo '```'
        else
            echo "_(no quota.txt)_"
        fi
        echo ""
    done

    echo "## Open PRs (view drift)"
    for side in local remote; do
        P="$OUT/$side/prs-open.txt"
        if [ -f "$P" ]; then
            n="$(wc -l < "$P" | tr -d ' ')"
        else
            n="0"
        fi
        echo "- $side sees **$n** open PRs"
    done
    if [ -f "$OUT/local/prs-open.txt" ] && [ -f "$OUT/remote/prs-open.txt" ]; then
        echo ""
        echo "PR numbers only in **local** view:"
        echo '```'
        # Extract `#N` tokens from each side, diff the sets.
        grep -oE '#[0-9]+' "$OUT/local/prs-open.txt"  | sort -u > "$OUT/.local-prs"  || true
        grep -oE '#[0-9]+' "$OUT/remote/prs-open.txt" | sort -u > "$OUT/.remote-prs" || true
        comm -23 "$OUT/.local-prs" "$OUT/.remote-prs"
        echo '```'
        echo ""
        echo "PR numbers only in **remote** view:"
        echo '```'
        comm -13 "$OUT/.local-prs" "$OUT/.remote-prs"
        echo '```'
        rm -f "$OUT/.local-prs" "$OUT/.remote-prs"
    fi
    echo ""

    echo "## Files present in only one gather"
    echo ""
    diff -rq "$OUT/local" "$OUT/remote" 2>/dev/null | grep -E "^Only in " | head -40 || echo "_(no diff)_"
    echo ""

    echo "---"
    echo ""
    echo "_Raw gathers preserved at \`$OUT/{local,remote}/\` for deeper inspection. Re-run with a different \`window\` if you need a tighter slice._"
} > "$DIFF_MD"

# 5) Persist a copy of the diff under notes/ for review later.
PERSIST="$LOCAL_REPO/notes/diagnose-cross-node-$(date +%Y-%m-%d).md"
cp "$DIFF_MD" "$PERSIST" 2>/dev/null || true

echo "" >&2
echo "gather-remote: done." >&2
echo "  full output:  $OUT/" >&2
echo "  comparison:   $DIFF_MD" >&2
echo "  persisted:    $PERSIST" >&2

# Print the diff path on stdout so callers (the /self-diagnose skill, future
# tooling) can chain.
echo "$DIFF_MD"

# TODO: secret-redaction audit.
# gather.sh's outputs (build_log-tail.md, pending-questions.md, log tails)
# are file-scope reads. Confirm no transitive token leak before this script
# becomes user-facing — particularly the `health.txt` output, which today
# echoes service detail strings that could include URLs.
