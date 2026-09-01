#!/usr/bin/env bash
# gather.sh read logs and results from the REPO, but both live under the WORKSPACE.
#
# `<repo>/logs` does not exist post-#1454 (the workspace-revamp rollup moved
# logs/ and results/ under the workspace). So `VLOG="$REPO/logs/voice-agent.log"`
# made the `[ -f "$VLOG" ]` guard false and the entire voice-agent block was
# skipped with NO error, and the same for the discord-bridge block; the
# `find "$REPO/results"` produced an empty file. A /self-diagnose run therefore
# reported "no transport events" for a log nobody had opened — the collector was
# dead and a healthy system looks identical.
#
# Observed 2026-08-03: voice-agent-signals.txt, voice-agent-size.txt and
# discord-bridge-recent.txt were all absent from the run directory, and
# results-recent-paths.txt was 0 bytes, while the real workspace logs held
# 431/226/7/6 matching lines.
#
# The existing scripts/lint-workspace-resolution.sh cannot catch this class: it
# refuses $SUTANDO_WORKSPACE reads, the legacy hardcoded install path, and
# Path(__file__).parent.parent repo-walks — not a CORRECTLY-resolved $REPO
# pointed at a workspace-owned subdirectory. (Describing that legacy path by
# name here would itself trip scripts/lint-sutando-home-path.sh, which is
# correct: this file does not own install-path resolution.)
#
# Run: bash tests/self-diagnose-reads-workspace-paths.test.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GATHER="$REPO_ROOT/skills/self-diagnose/scripts/gather.sh"
fails=0
checks=0

check() {
	local desc="$1"; shift
	checks=$((checks + 1))
	if "$@"; then
		echo "  ok   $desc"
	else
		echo "  FAIL $desc"
		fails=$((fails + 1))
	fi
}

[ -f "$GATHER" ] || { echo "FATAL: $GATHER not found"; exit 1; }

# Strip comments and blank lines: prose may legitimately DISCUSS the old path
# (the fix's own comment does). Only executable lines are in scope.
CODE="$(mktemp)"; trap 'rm -f "$CODE"' EXIT
sed -e 's/[[:space:]]*#.*$//' -e '/^[[:space:]]*$/d' "$GATHER" > "$CODE"

# The workspace-owned directories, per CLAUDE.md's Workspace contract.
for dir in logs results tasks state notes; do
	# `notes` has a DOCUMENTED $REPO fallback with a dated TODO — exempt it by
	# name rather than by loosening the pattern, so the exemption is visible.
	[ "$dir" = "notes" ] && continue
	check "no executable line reads \$REPO/$dir (it lives under the workspace)" \
		bash -c '! grep -qE "\\\$\{?REPO(_DIR)?\}?/'"$dir"'" "$1"' _ "$CODE"
done

# Positive control: the check above is only meaningful if the pattern CAN match.
# Without this, a typo'd regex would report a clean pass forever.
SENTINEL="$(mktemp)"; trap 'rm -f "$CODE" "$SENTINEL"' EXIT
printf 'VLOG="$REPO/logs/voice-agent.log"\n' > "$SENTINEL"
check "CONTROL: the pattern actually matches the defect it is written to catch" \
	bash -c 'grep -qE "\\\$\{?REPO(_DIR)?\}?/logs" "$1"' _ "$SENTINEL"

# The script must resolve the workspace through the canonical shim, not invent it.
check "gather.sh resolves the workspace via sutando-config.sh" \
	grep -qE 'WS="\$\(bash "\$REPO/scripts/sutando-config.sh" workspace\)"' "$GATHER"

# And the three repointed reads must now be workspace-relative.
for pat in 'VLOG="\$WS/logs/voice-agent.log"' 'DLOG="\$WS/logs/discord-bridge.log"' 'find "\$WS/results"'; do
	check "reads via \$WS: $pat" grep -qE "$pat" "$GATHER"
done

echo
if [ "$fails" -eq 0 ]; then
	echo "PASS ($checks checks)"
	exit 0
fi
echo "FAILED: $fails/$checks"
exit 1
