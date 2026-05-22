import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

// Defense-in-depth layer for the Discord-attachment-RCE fix.
// `src/discord-bridge.py` now sanitizes attachment filenames before
// saving them to `/tmp/discord-inbox/`. This file pins the parallel
// fix on the use site: the phone-conversation fast path that consumed
// `/tmp/discord-inbox/*.jpg` paths must use execFileSync, not
// shell-spliced execSync.

const SRC = readFileSync(
	join(import.meta.dirname ?? '.', '..', 'skills/phone-conversation/scripts/conversation-server.ts'),
	'utf-8',
);

describe('conversation-server fast path — execFile for video-concat', () => {
	it('imports execFileSync', () => {
		assert.match(
			SRC,
			/import\s*\{[^}]*execFileSync[^}]*\}\s*from\s*['"]node:child_process['"]/,
			'conversation-server.ts must import execFileSync from node:child_process.',
		);
	});

	it('uses execFileSync(\'bash\', [scriptPath, image, video, \'3\']) for prepend-image', () => {
		assert.match(
			SRC,
			/execFileSync\(\s*['"]bash['"]\s*,\s*\[\s*scriptPath\s*,\s*image\s*,\s*video\s*,/,
			'fast path must use execFileSync(\'bash\', [scriptPath, image, video, \'3\']) — argv form ' +
				'so neither image nor video gets spliced into a shell command.',
		);
	});

	it('does NOT use execSync with template-literal `bash ... "${image}"`', () => {
		assert.doesNotMatch(
			SRC,
			/execSync\(`bash[^`]*\$\{image\}/,
			'conversation-server.ts contains the raw `execSync(\`bash ... "${image}"\`)` pattern again — ' +
				'this is the exact RCE vector closed by this PR. Use execFileSync.',
		);
	});
});
