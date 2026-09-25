import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { cuePath, lineInstruction, toBeats, type ScriptItem } from '../skills/room-collab/present.js';

// The driver takes actions itself, so what it does and when must follow the script exactly.
describe('room-collab talk beats', () => {
	const steps: ScriptItem[][] = [
		[{ cue: 'slide', move: 1 }, { say: 'Hi.' }, { cue: 'highlight', topic: 'ag2' }, { say: 'AG2.' }],
		[{ say: 'Closing.' }, { cue: 'highlight', topic: 'clear' }],
	];

	it('carries each cue on the line that follows it, and keeps trailing cues', () => {
		assert.deepEqual(toBeats(steps), [
			{ step: 0, cues: [{ cue: 'slide', move: 1 }], say: 'Hi.' },
			{ step: 0, cues: [{ cue: 'highlight', topic: 'ag2' }], say: 'AG2.' },
			{ step: 1, cues: [], say: 'Closing.' },
			{ step: 1, cues: [{ cue: 'highlight', topic: 'clear' }], say: '' },
		]);
	});

	it('maps cues onto the relay, and never sends a pause', () => {
		assert.equal(cuePath({ cue: 'slide', move: 'next' }), '/slide/next');
		assert.equal(cuePath({ cue: 'slide', move: 5 }), '/slide/5');
		assert.equal(cuePath({ cue: 'highlight', topic: 'clear' }), '/highlight/clear');
		assert.equal(cuePath({ cue: 'pause', seconds: 2 }), null);
	});

	it('tells the model only the line, without a quote that could close the instruction', () => {
		const text = lineInstruction('say "hi"', 1, 3);
		assert.match(text, /Say ONLY this line/);
		assert.ok(text.includes("say 'hi'") && !text.includes('"hi"'));
	});
});
