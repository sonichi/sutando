/**
 * Step-5b behavior anchors — post-extraction stage: the tuned per-call
 * instruction assembly lives in the importable phone-agent-config.ts, so
 * the anchors assert REAL factory output across the full call-variant
 * matrix (upgraded from the pre-move source tripwires, exactly the 5a
 * sequence).
 *
 * Deterministic inputs: fixed owner name/tz/now, pinned safe-context /
 * repo-URL / stand-identity overrides, fixed tool-name lists — so every
 * variant's full instruction string hashes identically on any machine.
 * tryFastPath keeps its source tripwire (it stays in conversation-server).
 *
 * Regenerate (deliberately): ANCHOR_UPDATE=1 npx tsx tests/phone-behavior-anchors.test.ts
 * Run: npx tsx --test tests/phone-behavior-anchors.test.ts
 */
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import assert from 'node:assert';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';

const REPO = join(fileURLToPath(new URL('.', import.meta.url)), '..');
const FIXTURE = join(REPO, 'tests', 'fixtures', 'phone-behavior-anchors.json');
const UPDATE = process.env.ANCHOR_UPDATE === '1';

const cfg = await import('../skills/phone-conversation/scripts/phone-agent-config.js');

const DEPS = {
	ownerName: 'AnchorOwner',
	ownerTz: 'America/Los_Angeles',
	googleSearch: true,
	inlineToolNames: ['tool_a', 'tool_b'],
	availableToolNames: ['tier_x', 'tier_y'],
	overrides: {
		safeContext: 'Owner said: anchor line',
		repoUrl: 'https://github.com/sonichi/sutando',
		standIdentityJson: '{"name":"AnchorStand"}',
		now: new Date('2026-07-07T12:00:00Z'),
	},
};

const base = { isMeeting: false, meetingId: null, passcode: null, callerVerified: false, parentCallSid: null, purpose: null, isOwner: false };
const VARIANTS: Record<string, Parameters<typeof cfg.buildPhoneInstructions>[0]> = {
	meeting_verified: { ...base, isMeeting: true, meetingId: '123456', passcode: '9999', callerVerified: true },
	meeting_unverified: { ...base, isMeeting: true, meetingId: '123456', callerVerified: false },
	child_call: { ...base, parentCallSid: 'CAparent', purpose: 'book a table', callerVerified: true },
	inbound_owner: { ...base, isOwner: true },
	inbound_verified: { ...base, callerVerified: true },
	inbound_unverified: { ...base },
	outbound_owner: { ...base, isOwner: true, purpose: 'remind about the meeting' },
	outbound_owner_no_search: { ...base, isOwner: true, purpose: 'remind about the meeting' },
};

const outputs: Record<string, string> = {};
for (const [name, session] of Object.entries(VARIANTS)) {
	const deps = name === 'outbound_owner_no_search' ? { ...DEPS, googleSearch: false } : DEPS;
	outputs[name] = cfg.buildPhoneInstructions(session, deps);
}

const cs = readFileSync(join(REPO, 'skills', 'phone-conversation', 'scripts', 'conversation-server.ts'), 'utf-8');
function region(startMarker: string, endMarker: string): string {
	const start = cs.indexOf(startMarker);
	assert.notStrictEqual(start, -1, `region start not found: ${startMarker}`);
	const end = cs.indexOf(endMarker, start + startMarker.length);
	assert.notStrictEqual(end, -1, `region end not found: ${endMarker}`);
	return cs.slice(start, end);
}

const anchors = {
	note: 'Step-5b phone anchors (post-extraction: real factory output across the call-variant matrix). Deliberate prompt changes regenerate via ANCHOR_UPDATE=1 with the diff called out.',
	variant_hashes: Object.fromEntries(Object.entries(outputs).map(
		([k, v]) => [k, createHash('sha256').update(v).digest('hex')])),
	regions: {
		try_fast_path: createHash('sha256').update(
			region('function tryFastPath(callSession: CallSession, task: string):',
				'\nfunction ')).digest('hex'),
	},
};

if (UPDATE || !existsSync(FIXTURE)) {
	writeFileSync(FIXTURE, JSON.stringify(anchors, null, 1) + '\n');
	console.log(`phone anchor fixture ${UPDATE ? 'regenerated' : 'created'}: ${FIXTURE}`);
}
const expected = JSON.parse(readFileSync(FIXTURE, 'utf-8'));

test('phone variant matrix: every call-variant instruction string matches', () => {
	assert.deepStrictEqual(anchors.variant_hashes, expected.variant_hashes);
});

test('variant semantics: the tuned conditionals land correctly', () => {
	assert.ok(outputs.meeting_verified.includes('You have full capabilities — make calls'));
	assert.ok(outputs.meeting_verified.includes('send_dtmf with digits "123456#"'));
	assert.ok(outputs.meeting_verified.includes('send_dtmf with digits "9999#"'));
	assert.ok(outputs.meeting_unverified.includes('AI note-taker in a meeting'));
	assert.ok(outputs.child_call.includes('NOT the person you are calling'));
	assert.ok(outputs.child_call.includes('tier_x, tier_y'));
	assert.ok(outputs.inbound_owner.includes('Your owner AnchorOwner is calling you.'));
	assert.ok(outputs.inbound_owner.includes('Owner said: anchor line'));
	assert.ok(outputs.inbound_owner.includes('built-in Web search'), 'googleSearch grounding');
	assert.ok(outputs.outbound_owner_no_search.includes('use the work tool to look it up'), 'no-search grounding');
	assert.ok(outputs.inbound_unverified.includes('cannot access files, control the screen, or delegate tasks'));
	assert.ok(outputs.outbound_owner.includes('You are calling your owner AnchorOwner.'));
	assert.ok(outputs.outbound_owner.includes('Owner-local time: '));
	assert.ok(!outputs.inbound_verified.includes('## Tools'), 'owner-only sections stay owner-only');
});

test('tryFastPath tripwire unchanged', () => {
	assert.deepStrictEqual(anchors.regions, expected.regions);
});
