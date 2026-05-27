import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';

/**
 * Tests for src/meeting-tools.ts — Google Meet join, meeting ID lookup,
 * and contact-call tools.
 *
 * callContactTool is deliberately not tested here: it requires live macOS
 * Contacts access (AppleScript) and a running phone server — those belong
 * in integration/E2E suites, not unit tests.
 *
 * Covers:
 *   lookupMeetingIdTool.execute():
 *     1. No ZOOM_PERSONAL_MEETING_ID → { error }.
 *     2. ZOOM_PERSONAL_MEETING_ID set → returns meetingId value.
 *     3. source field identifies the env var.
 *     4. No passcode env var → passcode is null.
 *     5. ZOOM_PERSONAL_PASSCODE set → passcode returned.
 *     6. ZOOM_PASSCODE fallback used when ZOOM_PERSONAL_PASSCODE absent.
 *     7. Instruction includes meeting ID when no passcode.
 *     8. Instruction includes both ID and passcode when passcode set.
 *   joinGmeetTool.execute() — input validation (safe, returns before any execSync):
 *     9. Empty meetingCode → { error: 'Invalid meeting code' }.
 *    10. URL with no code path (just domain + slash) → { error }.
 *    11. URL that reduces to only a query string → { error }.
 */

import { lookupMeetingIdTool, joinGmeetTool } from '../src/meeting-tools.js';

type LookupResult = Awaited<ReturnType<typeof lookupMeetingIdTool.execute>>;
type JoinResult = Awaited<ReturnType<typeof joinGmeetTool.execute>>;

function clearMeetingEnv() {
	delete process.env.ZOOM_PERSONAL_MEETING_ID;
	delete process.env.ZOOM_PERSONAL_PASSCODE;
	delete process.env.ZOOM_PASSCODE;
}

describe('lookupMeetingIdTool.execute() — no meeting ID', () => {
	before(() => clearMeetingEnv());
	after(() => clearMeetingEnv());

	it('returns { error } when ZOOM_PERSONAL_MEETING_ID is not set', async () => {
		const r = await lookupMeetingIdTool.execute({}) as LookupResult;
		assert.ok('error' in (r as object), 'expected error field');
		assert.match((r as { error: string }).error, /ZOOM_PERSONAL_MEETING_ID/);
	});
});

describe('lookupMeetingIdTool.execute() — meeting ID set, no passcode', () => {
	before(() => {
		clearMeetingEnv();
		process.env.ZOOM_PERSONAL_MEETING_ID = '123 456 7890';
	});
	after(() => clearMeetingEnv());

	it('returns the meeting ID value', async () => {
		const r = await lookupMeetingIdTool.execute({}) as LookupResult;
		assert.equal((r as { meetingId: string }).meetingId, '123 456 7890');
	});

	it('source field identifies the env var', async () => {
		const r = await lookupMeetingIdTool.execute({}) as LookupResult;
		assert.match((r as { source: string }).source, /ZOOM_PERSONAL_MEETING_ID/);
	});

	it('passcode is null when neither passcode var is set', async () => {
		const r = await lookupMeetingIdTool.execute({}) as LookupResult;
		assert.equal((r as { passcode: unknown }).passcode, null);
	});

	it('instruction includes the meeting ID', async () => {
		const r = await lookupMeetingIdTool.execute({}) as LookupResult;
		assert.match((r as { instruction: string }).instruction, /123 456 7890/);
	});

	it('instruction says "No passcode needed" when none set', async () => {
		const r = await lookupMeetingIdTool.execute({}) as LookupResult;
		assert.match((r as { instruction: string }).instruction, /No passcode needed/i);
	});
});

describe('lookupMeetingIdTool.execute() — with ZOOM_PERSONAL_PASSCODE', () => {
	before(() => {
		clearMeetingEnv();
		process.env.ZOOM_PERSONAL_MEETING_ID = '987 654 3210';
		process.env.ZOOM_PERSONAL_PASSCODE = 'secret42';
	});
	after(() => clearMeetingEnv());

	it('returns the passcode when ZOOM_PERSONAL_PASSCODE is set', async () => {
		const r = await lookupMeetingIdTool.execute({}) as LookupResult;
		assert.equal((r as { passcode: string }).passcode, 'secret42');
	});

	it('instruction includes both meeting ID and passcode', async () => {
		const r = await lookupMeetingIdTool.execute({}) as LookupResult;
		const instr = (r as { instruction: string }).instruction;
		assert.match(instr, /987 654 3210/);
		assert.match(instr, /secret42/);
	});
});

describe('lookupMeetingIdTool.execute() — ZOOM_PASSCODE fallback', () => {
	before(() => {
		clearMeetingEnv();
		process.env.ZOOM_PERSONAL_MEETING_ID = '111 222 3333';
		process.env.ZOOM_PASSCODE = 'fallback99';
		// ZOOM_PERSONAL_PASSCODE deliberately absent
	});
	after(() => clearMeetingEnv());

	it('uses ZOOM_PASSCODE as fallback when ZOOM_PERSONAL_PASSCODE absent', async () => {
		const r = await lookupMeetingIdTool.execute({}) as LookupResult;
		assert.equal((r as { passcode: string }).passcode, 'fallback99');
	});
});

describe('joinGmeetTool.execute() — input validation (returns before execSync)', () => {
	it('empty meetingCode string → { error: Invalid meeting code }', async () => {
		const r = await joinGmeetTool.execute({ meetingCode: '' }) as JoinResult;
		assert.ok('error' in (r as object), 'expected error for empty code');
		assert.match((r as { error: string }).error, /Invalid meeting code/);
	});

	it('bare domain URL with no code path → { error }', async () => {
		const r = await joinGmeetTool.execute({ meetingCode: 'https://meet.google.com/' }) as JoinResult;
		assert.ok('error' in (r as object));
		assert.match((r as { error: string }).error, /Invalid meeting code/);
	});

	it('URL with only a query string and no code → { error }', async () => {
		// After stripping domain and query, nothing remains
		const r = await joinGmeetTool.execute({ meetingCode: 'https://meet.google.com/?authuser=0' }) as JoinResult;
		assert.ok('error' in (r as object));
		assert.match((r as { error: string }).error, /Invalid meeting code/);
	});
});
