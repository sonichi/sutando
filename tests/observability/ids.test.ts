import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { newTraceId, newUsageId, newSpanId, ulid } from '../../src/observability/ids.js';

const ULID_BODY = /^[0-9A-HJKMNP-TV-Z]{26}$/;
const TRACE = /^tr_[0-9A-HJKMNP-TV-Z]{26}$/;
const USAGE = /^ux_[0-9A-HJKMNP-TV-Z]{26}$/;
const SPAN = /^sp_[0-9a-f]{16}$/;

describe('kernel/_shared/ids', () => {
	it('mints trace/usage ids with the right prefix + Crockford body', () => {
		assert.match(newTraceId(), TRACE);
		assert.match(newUsageId(), USAGE);
		assert.match(ulid(), ULID_BODY);
	});

	it('mints span ids as sp_ + 16 hex', () => {
		assert.match(newSpanId(), SPAN);
	});

	it('is lexicographically time-sortable (time is the high-order chars)', () => {
		const early = newTraceId(1_000_000_000_000);
		const late = newTraceId(1_700_000_000_000);
		assert.ok(early < late, `${early} should sort before ${late}`);
	});

	it('is collision-free across 10k mints', () => {
		const seen = new Set<string>();
		for (let i = 0; i < 10_000; i++) seen.add(newTraceId());
		assert.equal(seen.size, 10_000);
	});

	it('never emits the ambiguous Crockford chars I L O U', () => {
		for (let i = 0; i < 200; i++) {
			const body = ulid();
			assert.doesNotMatch(body, /[ILOU]/);
		}
	});
});
