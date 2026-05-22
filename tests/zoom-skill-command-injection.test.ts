import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

// Security regression guard for `skills/zoom/tools.ts`.
//
// Pre-fix: three call sites used the shape
//   execSync(`open "${zoomUrl}"`)
// where `zoomUrl` interpolated the user-controlled `pwd` field directly:
//   zoomUrl = `zoommtg://zoom.us/join?confno=${cleanId}&pwd=${pwd}`;
//
// `pwd` comes from `passcode ?? getZoomPasscode()` — Gemini tool argument
// or `$ZOOM_PERSONAL_PASSCODE`. A passcode like `"; rm -rf ~/.config; #`
// would break out of the quoted shell argument in execSync (which uses
// `/bin/sh -c`) and execute arbitrary commands.

const SRC = readFileSync(
	join(import.meta.dirname ?? '.', '..', 'skills/zoom/tools.ts'),
	'utf-8',
);

describe('zoom tools — command-injection guard', () => {
	it('does not use execSync with template-literal `open "${zoomUrl}"`', () => {
		assert.doesNotMatch(
			SRC,
			/execSync\(`open\s*"\$\{[a-zA-Z]*[uU]rl\}/,
			'skills/zoom/tools.ts contains the raw `execSync(\`open "${...Url}"\`)` pattern again — ' +
				'this splices user-controlled `pwd` into a shell command. Use execFileSync(\'open\', [url]) instead.',
		);
	});

	it('uses execFileSync(\'open\', [...]) for the Zoom URL open call', () => {
		assert.match(
			SRC,
			/execFileSync\(\s*['"]open['"]\s*,\s*\[/,
			'skills/zoom/tools.ts must use `execFileSync(\'open\', [url])` to invoke `open` — the array form ' +
				'bypasses `/bin/sh -c` so no value spliced into argv is interpreted as shell syntax.',
		);
	});

	it('URL-encodes `pwd` before embedding in the deeplink URL', () => {
		assert.match(
			SRC,
			/encodeURIComponent\(pwd\)/,
			'skills/zoom/tools.ts must URL-encode `pwd` before embedding in the deeplink URL ' +
				'(defense-in-depth: prevents `&` / `#` / non-ASCII in passcodes from confusing the URL).',
		);
	});

	it('all three open-URL sites use the safe pattern (no leftover execSync template-literals)', () => {
		const matches = SRC.match(/execSync\(`open\s*"\$\{/g);
		assert.equal(
			matches,
			null,
			`Found ${matches?.length} occurrence(s) of the unsafe shell-template-literal pattern. ` +
				'Every site that opens a user-controlled URL must use execFileSync.',
		);
	});
});
