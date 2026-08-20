/**
 * The session display choice for screen vision.
 *
 * `screencapture` defaults to one display, so on a multi-display Mac a stream
 * started without a choice watches a screen the user did not pick. The gate asks
 * once and the answer is held for the session — a stream that re-resolved per
 * frame would silently change what is being watched mid-conversation.
 *
 * Run: node --import tsx/esm tests/vision-display-choice.test.ts
 */

import { describe, it, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import {
	decideDisplayGate,
	describeDisplay,
	getSessionDisplay,
	setSessionDisplay,
	type DisplayInfo,
} from '../src/vision-tools.js';

const BUILTIN: DisplayInfo = { index: 1, width: 3024, height: 1964, name: 'Color LCD', is_main: true };
const EXTERNAL: DisplayInfo = { index: 2, width: 3840, height: 2160, name: 'U28E510', is_main: false };

describe('decideDisplayGate', () => {
	beforeEach(() => setSessionDisplay(null));

	it('asks when more than one display exists and nothing is chosen yet', () => {
		const gate = decideDisplayGate(null, undefined, [BUILTIN, EXTERNAL]);
		assert.equal(gate.kind, 'ask');
		assert.deepEqual(gate.kind === 'ask' && gate.displays.map(d => d.index), [1, 2]);
	});

	it('does not ask when there is only one display — the question has one answer', () => {
		assert.deepEqual(decideDisplayGate(null, undefined, [BUILTIN]), { kind: 'use', display: 1 });
	});

	it('does not ask when enumeration failed — streams the default rather than refusing', () => {
		assert.deepEqual(decideDisplayGate(null, undefined, []), { kind: 'use', display: null });
	});

	it('an explicit request wins and is what gets held', () => {
		assert.deepEqual(decideDisplayGate(null, 2, [BUILTIN, EXTERNAL]), { kind: 'use', display: 2 });
	});

	it('once chosen it never asks again, even with several displays attached', () => {
		assert.deepEqual(decideDisplayGate(2, undefined, [BUILTIN, EXTERNAL]), { kind: 'use', display: 2 });
	});

	it('a later explicit request overrides the held choice', () => {
		assert.deepEqual(decideDisplayGate(2, 1, [BUILTIN, EXTERNAL]), { kind: 'use', display: 1 });
	});
});

describe('session display state', () => {
	beforeEach(() => setSessionDisplay(null));

	it('starts unset so the first screen stream triggers the gate', () => {
		assert.equal(getSessionDisplay(), null);
	});

	it('holds the choice across reads', () => {
		setSessionDisplay(2);
		assert.equal(getSessionDisplay(), 2);
		assert.equal(getSessionDisplay(), 2);
	});
});

describe('describeDisplay', () => {
	it('names the display so the user can answer without knowing indices', () => {
		assert.equal(describeDisplay(BUILTIN), '1: Color LCD (main) 3024x1964');
		assert.equal(describeDisplay(EXTERNAL), '2: U28E510 3840x2160');
	});

	it('falls back to the index when the display could not be named', () => {
		assert.equal(describeDisplay({ index: 3, width: 1920, height: 1080 }), '3: Display 3 1920x1080');
	});
});
