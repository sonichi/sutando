#!/usr/bin/env bash
# The CLAUDE_CONFIG_DIR inline lint must catch every default chain that ends in
# a `.claude` literal — not only the flat spelling.
#
# `scripts/lint-claude-home-path.sh` exists to stop a silent fallback to
# ~/.claude that skips the #1534 deprecation banner. Its pattern used to be one
# literal, so the nested `${CLAUDE_CONFIG_DIR:-${CLAUDE_HOME:-$HOME/.claude}}`
# — which has exactly that defect — passed the gate.
#
# The false positives matter as much as the catches: the bare emptiness idiom
# `${CLAUDE_CONFIG_DIR:-}` is correct code in three files, and
# SOURCE_CLAUDE_CONFIG_DIR is a different concept the header excludes on purpose.
# A widened pattern that flags either would get the gate disabled.
#
# Run: bash tests/lint-claude-home-nested-fallback.test.sh
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LINT="$REPO/scripts/lint-claude-home-path.sh"
fails=0

probe() {  # $1 = label, $2 = expected flag|clean, $3 = line to plant
    local label="$1" want="$2" line="$3"
    local tmp; tmp="$(mktemp -d)"
    ( cd "$tmp" && git init -q . && mkdir -p scripts
      printf '#!/usr/bin/env bash\n%s\n' "$line" > scripts/probe.sh
      git add -A && git -c user.email=t@e -c user.name=t commit -qm x ) >/dev/null 2>&1
    local out rc
    out="$(cd "$tmp" && bash "$LINT" 2>&1)"; rc=$?
    rm -rf "$tmp"
    local got=clean; [ "$rc" -ne 0 ] && got=flag
    if [ "$got" != "$want" ]; then
        echo "  FAIL $label: want $want, got $got"
        echo "       line: $line"
        fails=$((fails + 1))
    else
        echo "  ok   $label ($got)"
    fi
}

echo "lint-claude-home-nested-fallback:"
probe "flat form"            flag  'B="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/channels"'
probe "nested CLAUDE_HOME"   flag  'B="${CLAUDE_CONFIG_DIR:-${CLAUDE_HOME:-$HOME/.claude}}"'
probe "tilde fallback"       flag  'B="${CLAUDE_CONFIG_DIR:-~/.claude}"'
probe "non-.claude fallback" flag  'B="${CLAUDE_CONFIG_DIR:-$HOME/.claude-sutando}"'
probe "emptiness idiom"      clean 'if [ -n "${CLAUDE_CONFIG_DIR:-}" ]; then :; fi'
probe "SOURCE_ variant"      clean 'S="${SOURCE_CLAUDE_CONFIG_DIR:-$HOME/.claude}/channels"'
probe "helper call"          clean 'B="$(bash "$REPO/scripts/sutando-config.sh" claude-home-path)"'

# The tree itself must be clean, or the gate is failing on main.
if bash "$LINT" >/dev/null 2>&1; then
    echo "  ok   repo tree is clean under the widened pattern"
else
    echo "  FAIL repo tree has an unmigrated inline fallback:"
    bash "$LINT" 2>&1 | grep -E '^lint-claude-home-path: forbidden|^  ' | head -6
    fails=$((fails + 1))
fi

if [ "$fails" -ne 0 ]; then
    echo "$fails failure(s)"
    exit 1
fi
echo "All inline-fallback spellings classified correctly."
