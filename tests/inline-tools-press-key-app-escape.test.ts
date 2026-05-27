import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

// Security regression guard for `pressKeyTool` in src/inline-tools.ts.
//
// Pre-fix: the tool accepted an optional `app` parameter and embedded
// it verbatim in `osascript -e 'tell application "${app}" to
// activate'`. A value containing `"` could break out of the AppleScript
// string literal and inject arbitrary AppleScript, which can shell out
// via `do shell script "..."` — arbitrary code execution from a tool-
// call argument.
//
// Adjacent code already had the right escape pattern:
//   - line ~161 escapes the `key` parameter as `safeKey`
//   - switchAppTool around line ~214 escapes its `app` as `safeApp`
//
// Only `pressKeyTool.execute`'s app-activation branch was missing the
// escape. Pin the fix so a future refactor that re-introduces the raw
// interpolation fails here.

const SRC = readFileSync(
	join(import.meta.dirname ?? '.', '..', 'src/inline-tools.ts'),
	'utf-8',
);

describe('inline-tools pressKey app activation — AppleScript injection guard', () => {
	it('does not interpolate raw `app` into the osascript tell-application command', () => {
		// The raw pattern is `tell application "${app}"` (no escape pass).
		// The fixed pattern uses `safeApp` after a 3-step strip
		// (backslash, single quote, double quote).
		assert.doesNotMatch(
			SRC,
			/tell application "\$\{app\}" to activate/,
			'src/inline-tools.ts contains the raw `tell application "${app}"` pattern again — ' +
				'a tool-call argument containing `"` would inject AppleScript and (via ' +
				'`do shell script`) execute arbitrary commands. Escape `app` to `safeApp` first.',
		);
	});

	it('uses safeApp (escaped) in the pressKey app-activation branch', () => {
		// The fixed branch should embed `safeApp`, not `app`, in the
		// tell-application command. We don't pin the exact escape lines
		// (those are widely-used in this file) — we pin that the
		// pressKey branch references safeApp.
		const pressKeyBranch = SRC.match(/pressKeyTool[\s\S]*?Activate target app[\s\S]{0,500}/);
		assert(pressKeyBranch, 'could not locate pressKeyTool app-activation branch');
		assert.match(
			pressKeyBranch[0],
			/safeApp/,
			'pressKeyTool must escape `app` to `safeApp` before passing to osascript ' +
				'(see switchAppTool for the canonical pattern).',
		);
	});

	it('the escape pattern is computed from app via .replace chain', () => {
		// Defensive: a partial escape (e.g. only single-quote) would
		// still allow `"` injection — exactly what happened pre-fix.
		// We pin that `safeApp` is computed via `app.replace(...)` and
		// includes a strip for `"` (the actual exploit vector here).
		assert.match(
			SRC,
			/safeApp\s*=\s*app\.replace\([\s\S]+?\.replace\([\s\S]+?\.replace\(/,
			'safeApp must chain at least 3 .replace() calls (backslash, single-quote, double-quote) — see switchAppTool for canonical pattern',
		);
		// And that the chain ends with a strip for `"` (`\\"` in the
		// regex matches the literal characters `\` then `"`).
		assert.match(
			SRC,
			/safeApp[\s\S]+?\.replace\(\/"\/g,\s*'\\\\"'\)/,
			'safeApp chain must include `.replace(/"/g, \'\\\\"\')` — the double-quote strip is the actual exploit-vector defense for the `tell application "X"` shape',
		);
	});

	it('canonical escape pattern still in use in switchAppTool', () => {
		// Reference assertion — pins that the canonical pattern hasn't
		// been silently changed elsewhere; pressKey now matches it.
		assert.match(SRC, /switchAppTool[\s\S]+?safeApp\s*=\s*app/);
	});
});
