#!/usr/bin/env bash
# Automated checks for the WhatsApp (wacli) skill — no network or wacli required.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SKILL="skills/whatsapp/SKILL.md"
fail() { echo "verify-whatsapp-skill: $*" >&2; exit 1; }

[[ -f "$SKILL" ]] || fail "missing $SKILL"

grep -qE '^name:[[:space:]]*whatsapp[[:space:]]*$' "$SKILL" || fail "SKILL.md must declare name: whatsapp"
grep -qE '^description:' "$SKILL" || fail "SKILL.md must have a description"
grep -q 'wacli auth' "$SKILL" || fail "SKILL.md should document wacli auth"
grep -q 'wacli send text' "$SKILL" || fail "SKILL.md should document wacli send text"
grep -q 'confirm message content' "$SKILL" || fail "SKILL.md should require confirming before send"

grep -q 'skills/whatsapp' .env.example || fail ".env.example should reference skills/whatsapp"
grep -q 'WACLI_DEVICE_LABEL' .env.example || fail ".env.example should document WACLI_DEVICE_LABEL"
grep -q 'steipete/tap/wacli' .env.example || fail ".env.example should document brew install steipete/tap/wacli"

grep -q 'skills/whatsapp/SKILL.md' CLAUDE.md || fail "CLAUDE.md should reference the whatsapp skill"

echo "verify-whatsapp-skill: ok"
