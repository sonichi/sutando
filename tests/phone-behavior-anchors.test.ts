/**
 * Step-5b behavior anchors — the phone side's mirror of
 * tests/voice-behavior-anchors.test.ts (same proven sequence: tripwires
 * BEFORE any move; upgraded to real output snapshots once the tuned config
 * is extracted into an importable module).
 *
 * conversation-server.ts calls start() at module load (Twilio WS server +
 * ngrok), so it cannot be imported by tests — anchors are SHA-256 source
 * tripwires over the tuned regions:
 *
 * 1. buildAgent(callSession) — the per-call system-instruction factory:
 *    meeting IVR navigation, verified/unverified meeting prompts, child-call
 *    (outbound on behalf of owner) prompt, inbound variants, greeting rules.
 * 2. tryFastPath — the tuned fast-path routing rules (which requests bypass
 *    the work-tool round-trip).
 *
 * A tripwire firing means a tuned phone region changed: revert, or
 * consciously regenerate (ANCHOR_UPDATE=1) with the diff called out — the
 * extraction commit does exactly that, paired with an ordered string-entry
 * equivalence proof (the 5a-1 method).
 *
 * Run: npx tsx --test tests/phone-behavior-anchors.test.ts
 */
import { test } from 'node:test';
import assert from 'node:assert';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';

const REPO = join(new URL('.', import.meta.url).pathname, '..');
const FIXTURE = join(REPO, 'tests', 'fixtures', 'phone-behavior-anchors.json');
const UPDATE = process.env.ANCHOR_UPDATE === '1';

const cs = readFileSync(
	join(REPO, 'skills', 'phone-conversation', 'scripts', 'conversation-server.ts'), 'utf-8');

function region(startMarker: string, endMarker: string): string {
	const start = cs.indexOf(startMarker);
	assert.notStrictEqual(start, -1, `region start not found: ${startMarker}`);
	const end = cs.indexOf(endMarker, start + startMarker.length);
	assert.notStrictEqual(end, -1, `region end not found: ${endMarker}`);
	return cs.slice(start, end);
}

const anchors = {
	note: 'Step-5b phone anchors (tripwire stage). Deliberate tuned-prompt changes must regenerate via ANCHOR_UPDATE=1 with the diff called out in the PR.',
	regions: {
		build_agent: createHash('sha256').update(
			region('function buildAgent(callSession: CallSession): MainAgent {',
				'\n// --- Create VoiceSession for a call ---')).digest('hex'),
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

test('phone tripwires: tuned conversation-server regions unchanged', () => {
	assert.deepStrictEqual(anchors.regions, expected.regions);
});

test('tripwire breadth: the anchored regions are non-trivial', () => {
	const ba = region('function buildAgent(callSession: CallSession): MainAgent {',
		'\n// --- Create VoiceSession for a call ---');
	assert.ok(ba.split('\n').length > 200, 'buildAgent region unexpectedly small');
	assert.ok(ba.includes('CRITICAL IVR NAVIGATION'), 'IVR rules present');
	assert.ok(ba.includes('You are Sutando — NOT the person you are calling'), 'child-call identity rule present');
});
