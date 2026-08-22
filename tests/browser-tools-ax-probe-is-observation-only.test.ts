/**
 * The Accessibility probe is recorded, never consulted.
 *
 * Case A of the scroll bug (Accessibility off while `osascript` exits 0) cannot
 * be fixed until someone observes it, and it cannot be observed on a host where
 * the grant is on. So the probe is logged to earn that observation — but if it
 * ever starts steering behaviour, an unvalidated discriminator becomes policy,
 * which is the exact thing #3164 declined to ship.
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const SRC = readFileSync(
	join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'browser-tools.ts'),
	'utf8',
);

describe('axTrusted is observation-only', () => {
	it('is called exactly once, inside the log line', () => {
		// `function axTrusted()` contains the same token, so exclude the declaration —
		// counting it made this pin fail against correct code on first run.
		const calls = SRC.match(/(?<!function\s)axTrusted\(\)/g) ?? [];
		assert.equal(calls.length, 1, `axTrusted() should have one call site, found ${calls.length}`);
		const line = SRC.split('\n').find(l => /(?<!function\s)axTrusted\(\)/.test(l));
		assert.match(line!, /console\.log/, 'its only call site must be a log statement');
	});

	it('never reaches scrollOutcome — the verdict cannot depend on it', () => {
		const call = SRC.slice(SRC.indexOf('scrollOutcome({'), SRC.indexOf('scrollOutcome({') + 200);
		assert.doesNotMatch(call, /axTrusted|_axTrustedCache/,
			'the probe must not be passed into the policy function');
	});

	it('never appears in a conditional', () => {
		for (const l of SRC.split('\n')) {
			if (!/(?<!function\s)axTrusted\(\)/.test(l)) continue;
			assert.doesNotMatch(l, /\bif\s*\(|\?|&&|\|\|/,
				`the probe must not steer control flow: ${l.trim()}`);
		}
	});

	it('is memoized, so the extra osascript is once per process, not per scroll', () => {
		assert.match(SRC, /if \(_axTrustedCache !== undefined\) return _axTrustedCache;/,
			'must short-circuit on the cached value');
		// `undefined` is the only "not yet probed" sentinel — null is a real result
		// (probe unavailable) and must stay cached rather than re-running every call.
		assert.match(SRC, /_axTrustedCache: boolean \| null \| undefined/);
	});

	it('an unreadable probe is null, not a silent true', () => {
		assert.match(SRC, /catch \{ _axTrustedCache = null; \}/,
			'a failed probe must not be recorded as trusted');
	});
});
