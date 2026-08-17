import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { mayRetireSkipMarked } from '../src/skip_marker_ownership.js';

// results/ is shared by every consumer. Matching `task-*` plus a skip marker
// retired results this bridge never dispatched: the gateway's bookkeeping
// [no-send] was archived here, its tid left the owning bridge's in-flight
// ledger, and a substantive reply written to that path minutes later was
// never read by anyone. Measured on a peer host: five replies (2-5 KB) sat
// resident while the archived copy was the tiny [no-send] note.

const OWN = 'task-0000000000000000aa';
const OTHERS = 'task-0000000000000000bb';
const owns = (id: string) => id === OWN;

describe('task-bridge — only retire skip-marked results this bridge owns', () => {
	it('retires its own dispatched task', () => {
		assert.equal(mayRetireSkipMarked(`${OWN}.txt`, '[no-send]\nbookkeeping', owns), true);
	});

	it('leaves another consumer\'s result alone', () => {
		assert.equal(
			mayRetireSkipMarked(`${OTHERS}.txt`, '[no-send]\nbookkeeping', owns), false,
			'archiving a result this bridge never dispatched strands the owner\'s reply'
		);
	});

	it('honours [REPLIED] under the same ownership rule', () => {
		assert.equal(mayRetireSkipMarked(`${OWN}.txt`, '[REPLIED]', owns), true);
		assert.equal(mayRetireSkipMarked(`${OTHERS}.txt`, '[REPLIED]', owns), false);
	});

	it('ignores unmarked results even when owned', () => {
		assert.equal(mayRetireSkipMarked(`${OWN}.txt`, 'the actual reply', owns), false);
	});

	it('ignores non-task files', () => {
		assert.equal(mayRetireSkipMarked('proactive-1.txt', '[no-send]', () => true), false);
	});

	it('matches the marker case-insensitively and after leading space', () => {
		assert.equal(mayRetireSkipMarked(`${OWN}.txt`, '  [NO-SEND] x', owns), true);
	});
});
