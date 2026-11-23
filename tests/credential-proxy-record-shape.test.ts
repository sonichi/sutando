/**
 * The proxy's record shape, pinned for src/quota_record.py: the status line
 * recorder writes the same fields, and tests/quota-record.test.py asserts it
 * against the same fixture.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { quotaStateFromHeaders } from '../skills/quota-tracker/scripts/credential-proxy.ts';

const fx = JSON.parse(readFileSync(new URL('./fixtures/quota-record-shape.parity.json', import.meta.url), 'utf8'));

test('quotaStateFromHeaders writes the fixture record', () => {
	assert.deepEqual(quotaStateFromHeaders(fx.prev, fx.headers, fx.model, fx.now), fx.proxy_record);
});
