#!/usr/bin/env bash
# A vim swap of .env holds the same live secrets. Vim's deep swap range overlaps
# real 3-char extensions, so both directions are asserted, not just the first.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
FIXTURE="$(mktemp -d -t gitignore-swap-test.XXXXXX)"
trap 'rm -rf "$FIXTURE"' EXIT

git init -q "$FIXTURE"
cp "$REPO/.gitignore" "$FIXTURE/.gitignore"
cd "$FIXTURE"

pass=0
fail=0

check_ignored() {
    local path="$1" desc="$2"
    if git check-ignore -q "$path" 2>/dev/null; then
        echo "OK: $desc"; pass=$((pass + 1))
    else
        echo "FAIL: $desc — '$path' is NOT ignored, so \`add -A\` would stage a secret"
        fail=$((fail + 1))
    fi
}
refute_ignored() {
    local path="$1" desc="$2"
    if git check-ignore -q "$path" 2>/dev/null; then
        echo "FAIL: $desc — '$path' IS ignored; the rule over-denied and hides a real file"
        fail=$((fail + 1))
    else
        echo "OK: $desc"; pass=$((pass + 1))
    fi
}

# Shallow range: what vim reaches first, and what actually happened here.
for f in .env.swp .env.swo .env.swn .env.swa; do
    check_ignored "$f" "$f (shallow vim range) is ignored"
done

# Deep range: vim decrements past .swa to .svz and on down to .saa.
for f in .env.svz .env.saa .env.local.swp .env.production.saa; do
    check_ignored "$f" "$f (deep vim range) is ignored"
done

# DELIBERATELY SACRIFICED, pinned so it is a decision and not a surprise. A vim
# swap of .env is spelled exactly like a real .env.<3-char> file; secrets win here.
for f in .env.svg .env.sql .env.sas .env.sty .env.svc .env.local.svg; do
    check_ignored "$f" "$f is ignored — .env* namespace traded for swap safety"
done

# The collision the deep range creates. Every 3-char extension starting with `s`
# lives inside .saa-.swp, so a shape-matched rule silently hides real files.
for f in .foo.svg .schema.sql .data.sas .x.svc .theme.sty; do
    refute_ignored "$f" "hidden $f stays trackable"
done
# .swf is inside Vim's shallow range but is also a real extension; the range
# deliberately skips it, so a real Flash asset must stay trackable.
for f in movie.swf intro.swf; do
    refute_ignored "$f" "$f stays trackable — .swf excluded from the swap range"
done

# Same class as .swf: real extensions the earlier sw-range consumed wholesale.
for f in interface.swg audio.swa firmware.swi; do
    : > "$f"
    refute_ignored "$f" "$f stays trackable — real extension inside the old sw range"
done

# The cross-product cell: prefix AND a real sw extension satisfies BOTH
# discriminators, so the prefix-only controls above cannot catch it.
for f in .interface.swg _interface.swg .audio.swa _firmware.swi .audio.swi _lib.swg; do
    : > "$f"
    refute_ignored "$f" "$f stays trackable — prefix plus a real sw extension"
done

# Positive control: without this, the refutations above pass for the wrong
# reason if the narrowing went too far.
for f in .notes.swp _notes.swp .notes.swo _draft.swn; do
    : > "$f"
    check_ignored "$f" "$f is ignored — prefixed vim swap still caught"
done

for f in logo.svg query.sql style.scss run.sh app.swift; do
    refute_ignored "$f" "$f stays trackable"
done

# `.env` as a bare substring is not a dotenv boundary: these carry no secret and
# must stay trackable.
for f in diagram.environment.svg schema.environment.sql service.envoy.svc environment.sql env.svc; do
    : > "$f"
    refute_ignored "$f" "$f stays trackable — .env substring is not a dotenv boundary"
done

# Positive control for the pair above: real dotenv swap images, at the boundary.
for f in .env.swp .env.swo .env.local.saa secrets.env.swb myapp.env.swa; do
    : > "$f"
    check_ignored "$f" "$f is ignored — dotenv swap image at a real boundary"
done

echo
if [ "$fail" -gt 0 ]; then
    echo "gitignore-editor-swap-files: $fail failure(s), $pass passed"
    exit 1
fi
echo "gitignore-editor-swap-files: all $pass checks passed"
