/**
 * Scroll surfaces the setup step the OS already named.
 *
 * A denied scroll used to return `status: 'scrolled'`: the Chrome and keystroke
 * errors were logged and dropped, so the model was told the scroll worked and
 * the user heard nothing — even though Chrome's own error text names the exact
 * menu path that fixes it.
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { setupHint, scrollOutcome } from '../src/osascript-setup-hint.js';

const SRC = readFileSync(
	join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'browser-tools.ts'),
	'utf8',
);

describe('setupHint', () => {
	it('keeps the menu path Chrome names, and drops the URL (it would be read aloud)', () => {
		const hint = setupHint(
			'/tmp/sutando-scroll-1.scpt:71:909: execution error: Google Chrome got an error: ' +
			'Executing JavaScript through AppleScript is turned off. To turn it on, from the menu bar, ' +
			'go to View > Developer > Allow JavaScript from Apple Events. ' +
			'For more information: https://support.google.com/chrome/?p=applescript (12)',
		);
		assert.match(hint!, /View > Developer > Allow JavaScript from Apple Events/);
		assert.doesNotMatch(hint!, /https?:\/\//);
	});

	it('adds the fix for the two codes macOS does not explain', () => {
		assert.match(
			setupHint('36:48: execution error: System Events got an error: osascript is not allowed to send keystrokes. (1002)')!,
			/Privacy & Security > Accessibility/,
		);
		assert.match(
			setupHint('0:0: execution error: Not authorized to send Apple events to Google Chrome. (-1743)')!,
			/Privacy & Security > Automation/,
		);
	});

	it('returns null for an ordinary failure, so normal errors are untouched', () => {
		assert.equal(setupHint('Command failed: osascript /tmp/x.scpt\nsomething unrelated'), null);
		assert.equal(setupHint(''), null);
	});
});

describe('scroll reporting', () => {
	const HINT = ['Turn on AG2 Space in System Settings > Privacy & Security > Accessibility.'];

	it('BOTH paths denied -> setup_required, carrying the OS steps', () => {
		const r = scrollOutcome({ scrollMoved: null, keyDenied: true, hints: HINT, direction: 'down' });
		assert.equal(r.status, 'setup_required');
		assert.equal(r.moved, false);
		assert.deepEqual((r as { steps: string[] }).steps, HINT);
	});

	it('JS denied but the keystroke SUCCEEDED -> not setup_required', () => {
		// The adjacent input: Chrome ships "Allow JavaScript from Apple Events" off,
		// so the JS path is denied on a normal machine while Page Down still scrolls.
		const r = scrollOutcome({ scrollMoved: null, keyDenied: false, hints: HINT, direction: 'down' });
		assert.notEqual(r.status, 'setup_required',
			'a hint from the JS path must not report failure when the keystroke fallback ran');
		assert.equal(r.status, 'scrolled');
	});

	it('a denial with no keystroke failure never claims the scroll did not happen', () => {
		const r = scrollOutcome({ scrollMoved: null, keyDenied: false, hints: HINT, direction: 'up' });
		assert.doesNotMatch(JSON.stringify(r), /did not go through/);
	});

	it('JS reported no movement and nothing was denied -> at_limit', () => {
		const r = scrollOutcome({ scrollMoved: false, keyDenied: false, hints: [], direction: 'down' });
		assert.equal(r.status, 'at_limit');
		assert.equal(r.moved, false);
	});

	it('a real scroll stays scrolled even if the keystroke was denied', () => {
		const r = scrollOutcome({ scrollMoved: true, keyDenied: true, hints: HINT, direction: 'down' });
		assert.equal(r.status, 'scrolled');
		assert.equal(r.moved, true);
	});

	it('captures the keyboard fallback error rather than swallowing it', () => {
		assert.doesNotMatch(SRC, /catch \{ \/\* keyboard fallback is best-effort \*\/ \}/);
		assert.match(SRC, /catch \(e\) \{ _noteHint\(e\); _keyDenied = true; \}/);
	});
});
