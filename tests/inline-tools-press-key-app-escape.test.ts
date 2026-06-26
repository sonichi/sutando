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
//   - switchAppTool escapes its `app` as `safeApp`
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
		assert.doesNotMatch(
			SRC,
			/tell application "\$\{app\}" to activate/,
			'src/inline-tools.ts contains the raw `tell application "${app}"` pattern again — ' +
				'a tool-call argument containing `"` would inject AppleScript and (via ' +
				'`do shell script`) execute arbitrary commands. Escape `app` to `safeApp` first.',
		);
	});

	it('uses safeApp (escaped) in the pressKey app-activation branch', () => {
		const pressKeyBranch = SRC.match(/pressKeyTool[\s\S]*?Activate target app[\s\S]{0,500}/);
		assert(pressKeyBranch, 'could not locate pressKeyTool app-activation branch');
		assert.match(
			pressKeyBranch[0],
			/safeApp/,
			'pressKeyTool must escape `app` to `safeApp` before passing to osascript ' +
				'(see switchAppTool for the canonical pattern).',
		);
	});

	it('the escape pattern is computed from app via .replace chain (AppleScript-only, no shell layer)', () => {
		// execFileSync bypasses the shell — only AppleScript string literal escaping needed:
		// backslash and double-quote. Single-quote shell escaping is explicitly NOT required.
		assert.match(
			SRC,
			/safeApp[\s\S]+?\.replace\(\/"\/g,\s*'\\\\"'\)/,
			'safeApp chain must include `.replace(/"/g, \'\\\\"\')` — the double-quote strip is the AppleScript string literal defense for the `tell application "X"` shape',
		);
	});

	it('uses execFileSync (not execSync) for osascript calls — no shell layer', () => {
		// execFileSync passes the AppleScript string as a direct argument to the osascript
		// binary, bypassing the shell entirely. This is safer than execSync which spawns a
		// shell and requires shell-level string escaping on top of AppleScript escaping.
		assert.ok(
			!SRC.includes("execSync(`osascript") && !SRC.includes('execSync(`osascript'),
			'inline-tools.ts must not use execSync for osascript — use execFileSync instead',
		);
		assert.match(
			SRC,
			/execFileSync\('osascript'/,
			'inline-tools.ts must use execFileSync for osascript calls',
		);
	});

	it('canonical escape pattern still in use in switchAppTool', () => {
		assert.match(SRC, /switchAppTool[\s\S]+?safeApp\s*=\s*app/);
	});
});
