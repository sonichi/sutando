// E2E-of-the-logic tests for the discord-voice multi-bot name-gate (the
// speak-gate that made meeting-mode safe). Pure functions, no Discord/Gemini —
// these lock the wake/silence behaviour that the live test surfaced bugs in.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
	isAddressedBy,
	createGate,
	decideForTurn,
} from '../skills/discord-voice/scripts/name-gate.js';

// Identity values come from the SAME runtime config the live code reads
// (SUTANDO_STAND_NAME / SUTANDO_PEER_NAMES), so NO real bot identity is baked
// into this test's source. Generic fallbacks keep it deterministic in CI.
const SELF = process.env.SUTANDO_STAND_NAME || 'self';
const PEER = (process.env.SUTANDO_PEER_NAMES || 'peer').split(',')[0].trim();

// --- isAddressedBy: address vs mere-mention -------------------------------
test('isAddressedBy: greeting + name addresses', () => {
	assert.equal(isAddressedBy(`hey ${SELF}`, [SELF]), true);
	assert.equal(isAddressedBy(`hi, ${SELF}`, [SELF]), true);
	assert.equal(isAddressedBy(`ok ${SELF}`, [SELF]), true);
});
test('isAddressedBy: comma/question tag addresses', () => {
	assert.equal(isAddressedBy(`${SELF}, what time is it`, [SELF]), true);
	assert.equal(isAddressedBy(`${SELF}?`, [SELF]), true);
});
test('isAddressedBy: imperative verb at clause start addresses', () => {
	assert.equal(isAddressedBy(`${SELF} check the PR`, [SELF]), true);
});
test('isAddressedBy: bare name addresses — #1600 M2 (66/66 live misses)', () => {
	// The single most common summon pattern in the 2026-06-10 live session
	// (66 bare "Lucy" / "Lucy." utterances, 0 matched pre-fix).
	assert.equal(isAddressedBy(SELF, [SELF]), true, 'bare name must address');
	assert.equal(isAddressedBy(`${SELF}.`, [SELF]), true, 'name + period must address');
	assert.equal(isAddressedBy(`${SELF}!`, [SELF]), true, 'name + exclamation must address');
	// Leading/trailing whitespace (common in ASR output) must not break it
	assert.equal(isAddressedBy(`  ${SELF}  `, [SELF]), true, 'whitespace-padded bare name must address');
	// Alias variant (e.g. "Loosey" → alias for "Lucy")
	const alias = SELF + 'ey';
	assert.equal(isAddressedBy(alias, [alias]), true, 'bare alias must address');
});
test('isAddressedBy: plain mention does NOT address', () => {
	assert.equal(isAddressedBy(`thanks ${SELF}`, [SELF]), false);
	assert.equal(isAddressedBy(`${SELF}'s answer was good`, [SELF]), false);
	// Multi-word utterance containing only the name is still not a bare-name summon
	assert.equal(isAddressedBy(`thank you ${SELF}`, [SELF]), false);
});
test('isAddressedBy: empty names / empty text never matches', () => {
	assert.equal(isAddressedBy(`${SELF}, hi`, []), false);
	assert.equal(isAddressedBy('', [SELF]), false);
});

// --- decideForTurn: the wake/silence state machine ------------------------
const gate = () => createGate({ instanceName: SELF, otherInstances: [PEER] });

test('decideForTurn: addressed to me → allow (and sticks)', () => {
	const g = gate();
	assert.equal(decideForTurn(g, `${SELF}, what time is it`), 'allow');
	// follow-up with no name carries the sticky allow
	assert.equal(decideForTurn(g, 'and the weather?'), 'allow');
});
test('decideForTurn: addressed to a peer → drop (and sticks)', () => {
	const g = gate();
	assert.equal(decideForTurn(g, `${PEER}, hello`), 'drop');
	assert.equal(decideForTurn(g, 'how are you?'), 'drop'); // sticky drop
});
test('decideForTurn: my-name flips a sticky drop back to allow', () => {
	const g = gate();
	decideForTurn(g, `${PEER}, you handle it`);      // drop
	assert.equal(decideForTurn(g, `actually ${SELF}, you do it`), 'allow');
});
test('decideForTurn: no peer configured → always allow (single-bot)', () => {
	const g = createGate({ instanceName: SELF, otherInstances: [] });
	assert.equal(decideForTurn(g, 'anything at all'), 'allow');
	assert.equal(decideForTurn(g, `${PEER}, hi`), 'allow'); // no peer set → gate off
});
test('decideForTurn: primary defaults to allow on a cold opener', () => {
	const g = createGate({ instanceName: SELF, otherInstances: [PEER], primary: true });
	assert.equal(decideForTurn(g, 'hello everyone'), 'allow'); // primary cold-open
});
test('decideForTurn: non-primary stays silent until named', () => {
	const g = createGate({ instanceName: SELF, otherInstances: [PEER], primary: false });
	assert.equal(decideForTurn(g, 'hello everyone'), 'drop'); // not addressed, not primary
});
