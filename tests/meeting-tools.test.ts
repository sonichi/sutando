import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';

const { joinGmeetTool, lookupMeetingIdTool, callContactTool } = await import('../src/meeting-tools.js');

// ── Tool metadata ─────────────────────────────────────────────────────────────

describe('joinGmeetTool — metadata', () => {
	it('has name join_gmeet', () => {
		assert.equal(joinGmeetTool.name, 'join_gmeet');
	});
	it('has execution inline', () => {
		assert.equal(joinGmeetTool.execution, 'inline');
	});
	it('has a description string', () => {
		assert.ok(typeof joinGmeetTool.description === 'string' && joinGmeetTool.description.length > 0);
	});
	it('has parameters schema', () => {
		assert.ok(joinGmeetTool.parameters !== null && joinGmeetTool.parameters !== undefined);
	});
});

describe('lookupMeetingIdTool — metadata', () => {
	it('has name lookup_meeting_id', () => {
		assert.equal(lookupMeetingIdTool.name, 'lookup_meeting_id');
	});
	it('has execution inline', () => {
		assert.equal(lookupMeetingIdTool.execution, 'inline');
	});
	it('has a description string', () => {
		assert.ok(typeof lookupMeetingIdTool.description === 'string' && lookupMeetingIdTool.description.length > 0);
	});
});

describe('callContactTool — metadata', () => {
	it('has name call_contact', () => {
		assert.equal(callContactTool.name, 'call_contact');
	});
	it('has execution inline', () => {
		assert.equal(callContactTool.execution, 'inline');
	});
	it('has a description string', () => {
		assert.ok(typeof callContactTool.description === 'string' && callContactTool.description.length > 0);
	});
});

// ── joinGmeetTool — URL normalization and guard ───────────────────────────────

describe('joinGmeetTool — empty input guard', () => {
	it('returns error for empty meetingCode', async () => {
		const result = await joinGmeetTool.execute({ meetingCode: '' }, null) as Record<string, unknown>;
		assert.ok('error' in result, 'empty meetingCode must return { error: ... }');
	});

	it('returns error for whitespace-only meetingCode', async () => {
		const result = await joinGmeetTool.execute({ meetingCode: '   ' }, null) as Record<string, unknown>;
		assert.ok('error' in result, 'whitespace-only meetingCode must return { error: ... }');
	});

	// The URL strip logic runs before the guard, so a URL with no code
	// (e.g. bare "https://meet.google.com/") strips to '' → also an error.
	it('returns error for bare domain URL with no code', async () => {
		const result = await joinGmeetTool.execute({ meetingCode: 'https://meet.google.com/' }, null) as Record<string, unknown>;
		assert.ok('error' in result, 'bare domain URL must return { error: ... }');
	});
});

// ── lookupMeetingIdTool — env absent/present ──────────────────────────────────

describe('lookupMeetingIdTool — execute', () => {
	const saved = {
		ZOOM_PERSONAL_MEETING_ID: process.env.ZOOM_PERSONAL_MEETING_ID,
		ZOOM_PERSONAL_PASSCODE: process.env.ZOOM_PERSONAL_PASSCODE,
		ZOOM_PASSCODE: process.env.ZOOM_PASSCODE,
	};

	after(() => {
		if (saved.ZOOM_PERSONAL_MEETING_ID === undefined) delete process.env.ZOOM_PERSONAL_MEETING_ID;
		else process.env.ZOOM_PERSONAL_MEETING_ID = saved.ZOOM_PERSONAL_MEETING_ID;
		if (saved.ZOOM_PERSONAL_PASSCODE === undefined) delete process.env.ZOOM_PERSONAL_PASSCODE;
		else process.env.ZOOM_PERSONAL_PASSCODE = saved.ZOOM_PERSONAL_PASSCODE;
		if (saved.ZOOM_PASSCODE === undefined) delete process.env.ZOOM_PASSCODE;
		else process.env.ZOOM_PASSCODE = saved.ZOOM_PASSCODE;
	});

	it('returns error when ZOOM_PERSONAL_MEETING_ID is not set', async () => {
		delete process.env.ZOOM_PERSONAL_MEETING_ID;
		delete process.env.ZOOM_PERSONAL_PASSCODE;
		delete process.env.ZOOM_PASSCODE;
		const result = await lookupMeetingIdTool.execute({}, null) as Record<string, unknown>;
		assert.ok('error' in result, 'missing env var must return { error: ... }');
		assert.ok((result.error as string).includes('ZOOM_PERSONAL_MEETING_ID'), 'error should mention the missing var');
	});

	it('returns meetingId when ZOOM_PERSONAL_MEETING_ID is set', async () => {
		process.env.ZOOM_PERSONAL_MEETING_ID = '123-456-7890';
		delete process.env.ZOOM_PERSONAL_PASSCODE;
		delete process.env.ZOOM_PASSCODE;
		const result = await lookupMeetingIdTool.execute({}, null) as Record<string, unknown>;
		assert.equal(result.meetingId, '123-456-7890');
	});

	it('returns null passcode when no passcode env vars set', async () => {
		process.env.ZOOM_PERSONAL_MEETING_ID = '123-456-7890';
		delete process.env.ZOOM_PERSONAL_PASSCODE;
		delete process.env.ZOOM_PASSCODE;
		const result = await lookupMeetingIdTool.execute({}, null) as Record<string, unknown>;
		assert.equal(result.passcode, null);
	});

	it('returns passcode from ZOOM_PERSONAL_PASSCODE when set', async () => {
		process.env.ZOOM_PERSONAL_MEETING_ID = '123-456-7890';
		process.env.ZOOM_PERSONAL_PASSCODE = 'secret123';
		delete process.env.ZOOM_PASSCODE;
		const result = await lookupMeetingIdTool.execute({}, null) as Record<string, unknown>;
		assert.equal(result.passcode, 'secret123');
	});

	it('falls back to ZOOM_PASSCODE when ZOOM_PERSONAL_PASSCODE absent', async () => {
		process.env.ZOOM_PERSONAL_MEETING_ID = '123-456-7890';
		delete process.env.ZOOM_PERSONAL_PASSCODE;
		process.env.ZOOM_PASSCODE = 'fallback456';
		const result = await lookupMeetingIdTool.execute({}, null) as Record<string, unknown>;
		assert.equal(result.passcode, 'fallback456');
	});

	it('instruction mentions passcode when passcode is set', async () => {
		process.env.ZOOM_PERSONAL_MEETING_ID = '123-456-7890';
		process.env.ZOOM_PERSONAL_PASSCODE = 'secret123';
		const result = await lookupMeetingIdTool.execute({}, null) as Record<string, unknown>;
		assert.ok((result.instruction as string).includes('Passcode'), 'instruction should mention Passcode when set');
	});

	it('instruction says "No passcode needed" when none set', async () => {
		process.env.ZOOM_PERSONAL_MEETING_ID = '123-456-7890';
		delete process.env.ZOOM_PERSONAL_PASSCODE;
		delete process.env.ZOOM_PASSCODE;
		const result = await lookupMeetingIdTool.execute({}, null) as Record<string, unknown>;
		assert.ok((result.instruction as string).includes('No passcode needed'), 'instruction should say "No passcode needed" when none set');
	});

	it('source field identifies env origin', async () => {
		process.env.ZOOM_PERSONAL_MEETING_ID = '123-456-7890';
		const result = await lookupMeetingIdTool.execute({}, null) as Record<string, unknown>;
		assert.ok(typeof result.source === 'string' && (result.source as string).includes('ZOOM_PERSONAL_MEETING_ID'));
	});
});
