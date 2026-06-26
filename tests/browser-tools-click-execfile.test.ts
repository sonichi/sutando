/**
 * Structural contract tests for browser-tools.ts click-tool security fix (#1451 follow-up).
 *
 * The click tool's `shortcut` parameter is user/voice-controlled. Before the fix, the
 * split('+').pop() key fragment was interpolated into `execSync(\`osascript -e '${cmd}'\`)`.
 * A single-quote in the key segment would break the outer shell single-quoted string,
 * enabling arbitrary command injection.
 *
 * After the fix, both click paths use `execFileSync('osascript', ['-e', cmd], ...)` which
 * passes the script as a separate argv element — no shell involved, no injection surface.
 *
 * These tests verify the fix structurally: the CHAT_HTML-equivalent approach, reading
 * the source file and asserting the correct patterns are present/absent.
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(join(__dirname, '../src/browser-tools.ts'), 'utf8');

/** Extract the body of the click tool handler from the source. */
function extractClickBody(): string {
	// Find the click tool definition — look for the shortcut/coordinate handler block.
	const markerStart = SRC.indexOf("'Click at a specific screen coordinate");
	if (markerStart < 0) return '';
	// Find the handler function body starting after the tool description.
	const handlerStart = SRC.indexOf('shortcut.toLowerCase().split', markerStart);
	if (handlerStart < 0) return '';
	// Walk forward ~600 chars — enough to cover the shortcut + coordinate branches.
	return SRC.slice(handlerStart, handlerStart + 800);
}

describe('browser-tools click tool security contract', () => {
	it('browser-tools.ts source file is readable', () => {
		assert.ok(SRC.length > 0, 'browser-tools.ts must be non-empty');
	});

	it('click tool shortcut branch uses execFileSync not execSync', () => {
		const body = extractClickBody();
		assert.ok(body.length > 0, 'could not locate click shortcut handler');

		// Must NOT use bare execSync in this region.
		// We check the shortcut branch specifically (ends before the coordinate branch).
		const shortcutSection = body.slice(0, body.indexOf('if (x != null') > 0 ? body.indexOf('if (x != null') : body.length);
		assert.ok(
			!shortcutSection.includes("execSync('osascript") && !shortcutSection.includes('execSync(`osascript'),
			'click shortcut branch must NOT use execSync (shell-interpolated — injection risk)',
		);
	});

	it('click tool shortcut branch uses execFileSync argv form', () => {
		const body = extractClickBody();
		assert.ok(body.length > 0, 'could not locate click shortcut handler');
		assert.ok(
			body.includes("execFileSync('osascript'"),
			"click tool must use execFileSync('osascript', ['-e', cmd]) to bypass shell",
		);
	});

	it('click tool coordinate branch uses execFileSync not execSync', () => {
		// Find the coordinate branch after the shortcut block.
		const coordMarker = SRC.indexOf("click at {");
		assert.ok(coordMarker > 0, 'click at coordinate code must exist');

		const region = SRC.slice(Math.max(0, coordMarker - 100), coordMarker + 200);
		assert.ok(
			!region.includes("execSync('osascript") && !region.includes('execSync(`osascript'),
			'click coordinate branch must NOT use execSync',
		);
		assert.ok(
			region.includes("execFileSync('osascript'"),
			"click coordinate branch must use execFileSync('osascript', ['-e', ...]) form",
		);
	});

	it('execFileSync is imported in browser-tools.ts', () => {
		assert.ok(
			SRC.includes('execFileSync'),
			'execFileSync must be imported/present in browser-tools.ts',
		);
	});
});
