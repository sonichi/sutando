/**
 * #1600 M2 + M3 — ACCEPTANCE gate for the group-active "叫不醒" wake-path fix.
 *
 * Was a red repro (current behavior characterized); now the fix is in, so the
 * bare-name MISS cases flip to WAKE and the muted-reply chain plays. Covers the
 * real 2026-06-10 Susan+Chi session call phrasings (from workspace sqlite).
 *
 * The two compounding mechanisms this gates:
 *  M2 — bare "Lucy" / "Lucy." / "Loosey" and CJK "露西帮我看下屏幕" matched
 *       NEITHER isAddressedBy NOR isWakePhrase → never addressed. Fixed by
 *       isBareSummons (bare standalone name + CJK name-imperative).
 *  M3 — after a reconnect cleared the sticky addressed bit (restore-active sets
 *       lastAddressedToMe=false), the group-active gate required addressing; the
 *       bare-name miss left it muted, so the model's generated reply was
 *       suppressed ("🔇[muted] 当然可以…"). Fixed because the widened matcher +
 *       forceGate re-acquire the addressed bit → gate opens → reply plays.
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { decideSpeak } from '../skills/discord-voice/scripts/speak-gate.ts';
import { isAddressedBy, isWakePhrase, isBareSummons, createGate, decideForTurn } from '../skills/discord-voice/scripts/name-gate.ts';

const NAMES = ['Lucy', 'Lucie', 'Luci', 'Loosey', '露西', '露茜'];
// How the live (legacy, no-controller) gate now derives "addressed": the
// deterministic matchers, widened with isBareSummons.
const detAddressed = (t: string) => isAddressedBy(t, NAMES) || isWakePhrase(t, NAMES) || isBareSummons(t, NAMES);

describe('#1600 M2 — bare-name / CJK-imperative now wake (was 叫不醒)', () => {
	it('bare standalone name → addressed', () => {
		assert.equal(detAddressed('Lucy'), true);
		assert.equal(detAddressed('Lucy.'), true);
		assert.equal(detAddressed('Loosey'), true);
		assert.equal(detAddressed('露西'), true);
	});
	it('CJK name + imperative → addressed', () => {
		assert.equal(detAddressed('露西帮我看下屏幕'), true);
		assert.equal(detAddressed('露西，帮我看下屏幕'), true);
		assert.equal(detAddressed('露西打开那个链接'), true);
	});
	it('still matches the greet / comma / verb forms', () => {
		assert.equal(detAddressed('Lucy, can you turn the brightness up'), true);
		assert.equal(detAddressed('Hi Lucy, wake up'), true);
		assert.equal(detAddressed('Loosey are you there'), true);
	});
	it('does NOT over-match passing mentions (no false interjection)', () => {
		assert.equal(detAddressed('I told Lucy the answer'), false);
		assert.equal(detAddressed('thanks Lucy'), false);
		assert.equal(detAddressed('露西说的那个问题'), false);   // 说 = mention, not a summons
		assert.equal(detAddressed('露西说的对'), false);
	});
	it('isBareSummons is precise on its own', () => {
		assert.equal(isBareSummons('Lucy', NAMES), true);
		assert.equal(isBareSummons('lucy.', NAMES), true);
		assert.equal(isBareSummons('hey everyone Lucy is here', NAMES), false);
		assert.equal(isBareSummons('露西帮我', NAMES), true);
		assert.equal(isBareSummons('露西讲个笑话', NAMES), false); // 讲 not in the imperative set
	});
});

describe('#1600 M3 — reconnect-cleared group gate re-opens on a bare name', () => {
	// Reproduce the live chain WITHOUT a controller and WITHOUT peer names
	// (PEER_NAMES defaults empty) — exactly Susan's config.
	const mkGate = () => createGate({ instanceName: 'Lucy', nameAliases: ['Loosey', '露西'], primary: true, meetingMode: false });

	it('after restore-active cleared the bit, an UNADDRESSED group turn stays muted', () => {
		const g = mkGate();
		g.lastAddressedToMe = false;                       // what reconnect restore-active leaves
		decideForTurn(g, 'so what do you think about the design', true);
		const d = decideSpeak({ humanCount: 2, meetingMode: false, addressedToMe: g.lastAddressedToMe, allowAck: false, forceAudible: false });
		assert.equal(d.speak, false, 'unaddressed group turn correctly stays silent (no false interjection)');
	});

	it('a bare "Lucy" re-acquires the addressed bit → reply NOT muted', () => {
		const g = mkGate();
		g.lastAddressedToMe = false;                       // muted post-reconnect
		decideForTurn(g, 'Lucy', true);                    // the call that used to miss
		assert.equal(g.lastAddressedToMe, true, 'forceGate + isBareSummons re-acquired addressing');
		const d = decideSpeak({ humanCount: 2, meetingMode: false, addressedToMe: g.lastAddressedToMe, allowAck: false, forceAudible: false });
		assert.equal(d.speak, true, 'gate opens → the generated reply plays (M3 unmuted)');
		assert.equal(d.reason, 'group-active-addressed');
	});

	it('CJK summon also re-opens the gate', () => {
		const g = mkGate();
		g.lastAddressedToMe = false;
		decideForTurn(g, '露西帮我看下屏幕', true);
		assert.equal(g.lastAddressedToMe, true);
		assert.equal(decideSpeak({ humanCount: 2, meetingMode: false, addressedToMe: g.lastAddressedToMe, allowAck: false, forceAudible: false }).speak, true);
	});
});

describe('REAL Chi-session checklist — all calls now wake', () => {
	const WAKES = [
		'Hey, Lucy, will you open up that link and tell me the title',
		'Yeah, hi Lucy, connected to Discord screen.',
		'Lucy, can you turn the brightness up.',
		'Lucy, show me some new stuff.',
		'Lucy, what time is it?',
		'Hi Lucy, can you hear me?',
		'Lucy, show me your favorite dog breed.',
		'Hi Lucy',
		'Lucy, wake up.',
		'Lucy, 我和驰需要讨论一个事情，你可以 stay in meeting mode 吗？',
		'Lucy', 'Lucy.', 'Loosey',          // the bare-name calls that used to 叫不醒
	];
	for (const t of WAKES) {
		it(`✅ wakes: ${JSON.stringify(t.slice(0, 40))}`, () => assert.equal(detAddressed(t), true));
	}
});
