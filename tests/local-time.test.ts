import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { localTimeValue } from '../src/local_time.js';
import { buildVoiceTaskHeader } from '../src/task-bridge.js';

// Same instant and zones as tests/task-local-time-header.test.py, so the two helpers agree.
const INSTANT = new Date('2026-10-08T20:35:44.123Z');

describe('localTimeValue — the `local_time:` header value', () => {
	it('formats a fixed zone with its offset and IANA name', () => {
		assert.equal(localTimeValue(INSTANT, 'America/Los_Angeles'), '2026-10-08T13:35:44-07:00 America/Los_Angeles');
		assert.equal(localTimeValue(new Date('2026-12-01T12:00:00Z'), 'America/Los_Angeles'), '2026-12-01T04:00:00-08:00 America/Los_Angeles');
		assert.equal(localTimeValue(INSTANT, 'Asia/Kolkata'), '2026-10-09T02:05:44+05:30 Asia/Kolkata');
		assert.equal(localTimeValue(INSTANT, 'UTC'), '2026-10-08T20:35:44+00:00 UTC');
	});

	it('falls back to the bare offset when no zone resolves', () => {
		assert.match(localTimeValue(INSTANT, null), /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$/);
		assert.match(localTimeValue(INSTANT, 'Nowhere/Atlantis'), /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$/);
	});
});

describe('voice task header carries local_time above task:', () => {
	it('emits local_time right after the UTC timestamp, which is unchanged', () => {
		const lines = buildVoiceTaskHeader('task-1', '2026-10-08T20:35:44.123Z', 'owner-1', null).split('\n');
		assert.equal(lines[1], 'timestamp: 2026-10-08T20:35:44.123Z');
		assert.equal(lines[2], `local_time: ${localTimeValue(INSTANT)}`);
		assert.ok(!lines.some(l => l.startsWith('task:')), 'the header ends before task:, which the caller appends last');
	});
});
