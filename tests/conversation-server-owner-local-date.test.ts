import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

// Pin the fix for sonichi/sutando#1243 — voice agent computed "tomorrow" wrong,
// off by one day. Root cause: system prompt carried no owner-local date
// anchor, so Gemini resolved date-relative phrases against UTC / server-local
// instead of the owner's timezone. Whenever the call landed after ~5pm PT,
// "tomorrow" rolled to UTC's tomorrow — one day ahead of owner-local.
//
// Fix injects a date-context line into the "## Known info" section at
// session-open, naming the owner-local today/tomorrow/yesterday so the model
// has explicit absolute YYYY-MM-DD values to pass to tools.

const SRC = readFileSync(
	join(import.meta.dirname ?? '.', '..', 'skills/phone-conversation/scripts/conversation-server.ts'),
	'utf-8',
);

describe('conversation-server — owner-local date context (sonichi#1243)', () => {
	it('reads OWNER_TZ from env with America/Los_Angeles default', () => {
		assert.match(
			SRC,
			/const\s+OWNER_TZ\s*=\s*process\.env\.OWNER_TZ\s*\?\?\s*['"]America\/Los_Angeles['"]/,
			'must read OWNER_TZ from env with a sensible default — env override lets non-Pacific owners fix without code change.',
		);
	});

	it('defines an ownerLocalDateContext helper that computes today/tomorrow/yesterday', () => {
		assert.match(
			SRC,
			/function\s+ownerLocalDateContext\s*\(/,
			'must define an ownerLocalDateContext helper.',
		);
		assert.match(
			SRC,
			/toLocaleDateString\(['"]en-CA['"][^)]*timeZone:\s*tz/,
			'helper must format dates with timeZone:tz so output is owner-local, not UTC.',
		);
	});

	it('helper computes tomorrow and yesterday by adding/subtracting 86_400_000 ms', () => {
		assert.match(
			SRC,
			/now\.getTime\(\)\s*\+\s*86[_]?400[_]?000/,
			'tomorrow must be computed by adding 1 day in ms — naive Date.setDate() drifts across DST.',
		);
		assert.match(
			SRC,
			/now\.getTime\(\)\s*-\s*86[_]?400[_]?000/,
			'yesterday must be computed by subtracting 1 day in ms.',
		);
	});

	it('helper returns an explicit instruction to use owner-local dates (not UTC)', () => {
		assert.match(
			SRC,
			/owner-local/i,
			'helper output must use the phrase "owner-local" so the model anchors against the right clock.',
		);
		assert.match(
			SRC,
			/never\s+against\s+UTC/i,
			'helper must explicitly tell the model NOT to resolve against UTC.',
		);
	});

	it('injects ownerLocalDateContext() into the "## Known info" block', () => {
		const re = /## Known info[\s\S]{0,400}?ownerLocalDateContext\(\)/;
		assert.match(
			SRC,
			re,
			'## Known info section must include ownerLocalDateContext() — that is where the date anchor is delivered to Gemini.',
		);
	});
});
