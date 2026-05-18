// Mock tests for the multi-bot voice name-gate decision logic.
// Spec: notes/multi-bot-voice-gate-redesign.md (T1-T21 covered here;
// T22-T31 are integration tests for per-turn-ID + tool gate, deferred).

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
	createGate,
	decideForTurn,
	isAddressedBy,
	isAddressedToOther,
} from '../skills/discord-voice/scripts/name-gate.ts';

const LUCY_CFG = {
	instanceName: 'Lucy',
	otherInstances: ['Maddy'],
	otherAliases: ['Maddie', 'Mady'],
	primary: true,
};
const MADDY_CFG = {
	instanceName: 'Maddy',
	nameAliases: ['Maddie', 'Mady', 'Mandy'],
	otherInstances: ['Lucy'],
	primary: false,
};

describe('isAddressedBy — pattern detection', () => {
	it('greet+name with space', () => {
		assert.equal(isAddressedBy('Hi Lucy can you hear me', ['Lucy']), true);
	});
	it('greet+name with comma (T5/T6)', () => {
		assert.equal(isAddressedBy('Hi, Maddie. Can you hear me?', ['Maddie']), true);
	});
	it('greet+name with exclamation', () => {
		assert.equal(isAddressedBy('Hey Lucy!', ['Lucy']), true);
	});
	it('NAME comma-tag', () => {
		assert.equal(isAddressedBy('Lucy, can you help?', ['Lucy']), true);
	});
	it('NAME at sentence start with imperative', () => {
		assert.equal(isAddressedBy('Lucy can you check the time?', ['Lucy']), true);
	});
	it('NAME after . in mid-sentence', () => {
		assert.equal(isAddressedBy('OK. Lucy please answer.', ['Lucy']), true);
	});
	it('mere mention "thanks NAME" — NOT address', () => {
		assert.equal(isAddressedBy('Yes thanks Lucy', ['Lucy']), false);
	});
	it('possessive "NAME\'s answer" — NOT address', () => {
		assert.equal(isAddressedBy("Is Lucy's answer correct?", ['Lucy']), false);
	});
	it('NAME mid-sentence without comma — NOT address', () => {
		assert.equal(isAddressedBy('I told Lucy about it', ['Lucy']), false);
	});
	it('two names in same text — addresses BOTH that match', () => {
		// "Yes thanks Lucy. Hi Maddie, can you..." should hit Maddie pattern, not Lucy
		assert.equal(
			isAddressedBy('Yes thanks Lucy. Hi Maddie, can you answer?', ['Lucy']),
			false,
		);
		assert.equal(
			isAddressedBy('Yes thanks Lucy. Hi Maddie, can you answer?', ['Maddie']),
			true,
		);
	});
	it('empty text', () => {
		assert.equal(isAddressedBy('', ['Lucy']), false);
	});
	it('empty names list', () => {
		assert.equal(isAddressedBy('Hi Lucy', []), false);
	});
});

describe('decideForTurn — sticky state machine', () => {
	// T1
	it('T1: Lucy gets "Hi Lucy..." → allow', () => {
		const s = createGate(LUCY_CFG);
		assert.equal(decideForTurn(s, 'Hi Lucy, can you hear me?'), 'allow');
		assert.equal(s.lastAddressedToMe, true);
	});
	// T2
	it('T2: Maddy gets "Hi Lucy..." → drop', () => {
		const s = createGate(MADDY_CFG);
		assert.equal(decideForTurn(s, 'Hi Lucy, can you hear me?'), 'drop');
		assert.equal(s.lastAddressedToMe, false);
	});
	// T3
	it('T3: Lucy gets "Hi Maddy..." → drop', () => {
		const s = createGate(LUCY_CFG);
		assert.equal(decideForTurn(s, 'Hi Maddy, can you hear me?'), 'drop');
		assert.equal(s.lastAddressedToMe, false);
	});
	// T4
	it('T4: Maddy gets "Hi Maddy..." → allow', () => {
		const s = createGate(MADDY_CFG);
		assert.equal(decideForTurn(s, 'Hi Maddy, can you hear me?'), 'allow');
	});
	// T5
	it('T5: Lucy gets "Hi, Maddie. Can you hear me?" → drop', () => {
		const s = createGate(LUCY_CFG);
		assert.equal(decideForTurn(s, 'Hi, Maddie. Can you hear me?'), 'drop');
	});
	// T6
	it('T6: Maddy gets "Hi, Maddie. Can you hear me?" → allow (alias)', () => {
		const s = createGate(MADDY_CFG);
		assert.equal(decideForTurn(s, 'Hi, Maddie. Can you hear me?'), 'allow');
	});
	// T7
	it('T7: Lucy gets "Hey Lucy!" → allow', () => {
		const s = createGate(LUCY_CFG);
		assert.equal(decideForTurn(s, 'Hey Lucy!'), 'allow');
	});
	// T8
	it('T8: Lucy gets "Yes thanks Lucy. Hi Maddie, ..." → drop', () => {
		const s = createGate(LUCY_CFG);
		assert.equal(
			decideForTurn(s, 'Yes thanks Lucy. Hi Maddie, can you answer that math question?'),
			'drop',
		);
		assert.equal(s.lastAddressedToMe, false, 'sticky flipped to false');
	});
	// T9
	it('T9: Maddy gets "Yes thanks Lucy. Hi Maddie, ..." → allow', () => {
		const s = createGate(MADDY_CFG);
		assert.equal(
			decideForTurn(s, 'Yes thanks Lucy. Hi Maddie, can you answer that math question?'),
			'allow',
		);
	});
	// T10
	it('T10: Lucy gets "Maddie, is Lucy\'s answer correct?" → drop', () => {
		const s = createGate(LUCY_CFG);
		assert.equal(decideForTurn(s, "Maddie, is Lucy's answer correct?"), 'drop');
	});
	// T11
	it('T11: Maddy gets "Maddie, is Lucy\'s answer correct?" → allow', () => {
		const s = createGate(MADDY_CFG);
		assert.equal(decideForTurn(s, "Maddie, is Lucy's answer correct?"), 'allow');
	});

	// T12: sticky carries on un-named follow-up after Lucy was addressed
	it('T12: Lucy after T1 gets "What time is it?" → allow (sticky)', () => {
		const s = createGate(LUCY_CFG);
		decideForTurn(s, 'Hi Lucy, can you hear me?'); // T1
		assert.equal(decideForTurn(s, 'What time is it?'), 'allow');
	});
	// T13
	it('T13: Maddy after T2 (Lucy addressed) gets "What time is it?" → drop', () => {
		const s = createGate(MADDY_CFG);
		decideForTurn(s, 'Hi Lucy, can you hear me?'); // T2 on Maddy side
		assert.equal(decideForTurn(s, 'What time is it?'), 'drop');
	});
	// T14
	it('T14: Lucy gets "Hi Maddy" then "thank you" → drop (sticky flipped)', () => {
		const s = createGate(LUCY_CFG);
		decideForTurn(s, 'Hi Maddy'); // sticky → false
		assert.equal(decideForTurn(s, 'thank you'), 'drop');
	});

	// T15: cold opener with primary
	it('T15: Lucy primary=true, first turn "What time is it?" → allow', () => {
		const s = createGate(LUCY_CFG); // primary=true
		assert.equal(decideForTurn(s, 'What time is it?'), 'allow');
	});
	// T16: cold opener without primary
	it('T16: Maddy primary=false, first turn "What time is it?" → drop', () => {
		const s = createGate(MADDY_CFG); // primary=false
		assert.equal(decideForTurn(s, 'What time is it?'), 'drop');
	});

	// T17: ASR mishearing alias match
	it('T17: Maddy gets "Hi Mandy, can you hear me?" → allow (alias)', () => {
		const s = createGate(MADDY_CFG);
		assert.equal(decideForTurn(s, 'Hi Mandy, can you hear me?'), 'allow');
	});
	// T18: ASR mishearing not in aliases
	it('T18: Maddy gets "Hi Nadi, can you hear me?" → drop', () => {
		const s = createGate(MADDY_CFG);
		assert.equal(decideForTurn(s, 'Hi Nadi, can you hear me?'), 'drop');
	});

	// T19: empty text on Maddy (non-primary, sticky false) → drop
	it('T19: Maddy (non-primary) gets empty text → drop', () => {
		const s = createGate(MADDY_CFG);
		assert.equal(decideForTurn(s, ''), 'drop');
	});
	// T20: filler words preserve sticky
	it('T20: Lucy (primary) gets "uh um er" → allow (sticky inherits primary)', () => {
		const s = createGate(LUCY_CFG);
		assert.equal(decideForTurn(s, 'uh um er'), 'allow');
	});
	// T21: foreign-script text doesn't accidentally match
	it('T21: Lucy gets korean-ish text → unchanged (sticky)', () => {
		const s = createGate(LUCY_CFG); // primary=true → start allow
		assert.equal(decideForTurn(s, '이것은 내가 당신을 들을 수 있습니다'), 'allow');
	});
});

describe('gate disabled when no peers configured', () => {
	it('single-instance setup → always allow regardless of text', () => {
		const s = createGate({ instanceName: 'Lucy' }); // no otherInstances
		assert.equal(decideForTurn(s, 'Hi Maddy'), 'allow');
		assert.equal(decideForTurn(s, ''), 'allow');
		assert.equal(decideForTurn(s, 'anything'), 'allow');
	});
});

describe('isAddressedToOther — open-world drop', () => {
	const LUCY_NAMES = ['Lucy', 'Lucie', 'Lou', 'süssi'];

	it('"Hi Bob can you hear me" → true (open-world greet+unknown-name)', () => {
		assert.equal(isAddressedToOther('Hi Bob can you hear me?', LUCY_NAMES), true);
	});
	it('"Hi Daddy, what time is it?" → true (ASR mishearing not in OTHER list)', () => {
		assert.equal(isAddressedToOther('Hi Daddy, what time is it?', LUCY_NAMES), true);
	});
	it('"Bob, please answer" → true (commaTag at sentence start)', () => {
		assert.equal(isAddressedToOther('Bob, please answer', LUCY_NAMES), true);
	});
	it('"Bob can you check the time" → true (imperative at sentence start)', () => {
		assert.equal(isAddressedToOther('Bob can you check the time?', LUCY_NAMES), true);
	});
	it('"Hi Lucy can you hear me" → false (greet matches MY name)', () => {
		assert.equal(isAddressedToOther('Hi Lucy can you hear me?', LUCY_NAMES), false);
	});
	it('"Hi Lou what time is it" → false (alias matches MY name)', () => {
		assert.equal(isAddressedToOther('Hi Lou what time is it?', LUCY_NAMES), false);
	});
	it('"what time is it" → false (pronoun/question-word stopwords)', () => {
		assert.equal(isAddressedToOther('what time is it?', LUCY_NAMES), false);
	});
	it('"yes I can" → false (affirmation + pronoun stopwords)', () => {
		assert.equal(isAddressedToOther('yes I can', LUCY_NAMES), false);
	});
	it('"is this math?" → false (commaTag stopword filter on "this"/"math")', () => {
		// 'math' is not a stopword but isn't at clause start without the .!? boundary
		// and "is" / "this" ARE stopwords. So the commaTag pattern misses.
		assert.equal(isAddressedToOther('is this math?', LUCY_NAMES), false);
	});
	it('"thanks Bob" → false (mere mention, no greet/comma/imperative)', () => {
		assert.equal(isAddressedToOther('thanks Bob for that', LUCY_NAMES), false);
	});
	it('empty text → false', () => {
		assert.equal(isAddressedToOther('', LUCY_NAMES), false);
	});
});

describe('multiple sequential turns — realistic demo flow', () => {
	it('Lucy + Maddy demo: Hi Maddy → Yes I can → What time is it → Lucy answer', () => {
		const lucy = createGate(LUCY_CFG);
		const maddy = createGate(MADDY_CFG);

		// Turn 1: "Hi Maddy, can you hear me?"
		assert.equal(decideForTurn(lucy, 'Hi Maddy, can you hear me?'), 'drop');
		assert.equal(decideForTurn(maddy, 'Hi Maddy, can you hear me?'), 'allow');

		// Turn 2: "Yes I can, what's up?" (Maddy's own response — but
		// from owner-mic perspective, suppose owner says nothing this turn
		// or the bot's response leaks back as user text — gate should hold)
		// Simulate owner silence (no user turn fires; skip)

		// Turn 3: "What time is it?" — owner continues to Maddy
		assert.equal(decideForTurn(lucy, 'What time is it?'), 'drop', 'Lucy stays sticky-false');
		assert.equal(decideForTurn(maddy, 'What time is it?'), 'allow', 'Maddy sticky-true');

		// Turn 4: switch to Lucy
		assert.equal(decideForTurn(lucy, 'Lucy, what about you?'), 'allow');
		assert.equal(decideForTurn(maddy, 'Lucy, what about you?'), 'drop');
	});
});
