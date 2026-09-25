import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { anchorPaths, cuePath, lineInstruction, toBeats, type ScriptItem } from '../skills/room-collab/present.js';

// The driver takes actions itself, so what it does and when must follow the script exactly.
describe('room-collab talk beats', () => {
	const steps: ScriptItem[][] = [
		[{ cue: 'slide', move: 1 }, { say: 'Hi.' }, { cue: 'highlight', topic: 'ag2' }, { say: 'AG2.' }],
		[{ say: 'Closing.' }, { cue: 'highlight', topic: 'clear' }],
	];

	it('carries each cue on the line that follows it, and keeps trailing cues', () => {
		assert.deepEqual(
			toBeats(steps).map((b) => [b.step, b.cues, b.say]),
			[
				[0, [{ cue: 'slide', move: 1 }], 'Hi.'],
				[0, [{ cue: 'highlight', topic: 'ag2' }], 'AG2.'],
				[1, [], 'Closing.'],
				[1, [{ cue: 'highlight', topic: 'clear' }], ''],
			],
		);
	});

	it('resolves every beat to an absolute position, so a replay cannot drift', () => {
		const talk: ScriptItem[][] = [
			[{ cue: 'slide', move: 3 }, { say: 'a' }, { cue: 'slide', move: 'next' }, { say: 'b' }],
			[{ cue: 'highlight', topic: 'trust' }, { say: 'c' }, { cue: 'slide', move: 'next' }, { say: 'd' }],
			[{ cue: 'slide', move: 'prev' }, { cue: 'slide', move: 'prev' }, { say: 'e' }, { cue: 'highlight', topic: 'clear' }, { say: 'f' }],
		];
		const anchors = toBeats(talk, { trust: 18 }).map((b) => b.anchor);
		assert.deepEqual(anchors, [
			{ slide: 3 },
			{ slide: 4 },
			{ topic: 'trust', slide: 18 },
			{ slide: 19 },
			{ slide: 17 },
			{ slide: 17 },
		]);
		// A topic the deck reaches by itself is re-sent as the topic, not as a slide number.
		assert.deepEqual(anchorPaths({ topic: 'trust', slide: 18 }), ['/highlight/trust']);
		assert.deepEqual(anchorPaths({ slide: 4 }), ['/slide/4']);
		assert.deepEqual(anchorPaths(null), ['/slide/1']);
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
