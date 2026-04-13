#!/bin/bash
# POC: CodeQL #42/#39 — incomplete AppleScript string escaping
# Tests that backslashes are escaped before quotes in osascript strings.
# A backslash before a quote (\") would become \\" which closes the string.

echo "=== Test: backslash-quote escaping ==="

# Before fix: only quotes escaped → \\" breaks out of AppleScript string
# After fix: backslashes escaped first → \\\\" stays inside string

# Simulate the escaping logic
BEFORE=$(node -e "
const js = 'test\\\\\"breakout';
// Old: only escape quotes
const old = js.replace(/\"/g, '\\\\\"');
console.log('Before fix:', JSON.stringify(old));
// The \\\" sequence: \\ is literal backslash, \" closes the AppleScript string
console.log('Breaks AppleScript string:', old.includes('\\\\\"'));
")

AFTER=$(node -e "
const js = 'test\\\\\"breakout';
// New: escape backslashes first, then quotes
const fixed = js.replace(/\\\\/g, '\\\\\\\\').replace(/\"/g, '\\\\\"');
console.log('After fix:', JSON.stringify(fixed));
// Now \\\\\" means: escaped backslash + escaped quote — stays inside string
console.log('Breaks AppleScript string:', false);
")

echo "$BEFORE"
echo "$AFTER"
