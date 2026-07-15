import { describe, it, mock, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { deliverWithRetry } from '../src/inject-delivery.js';

// Behavior anchor for the retry-then-fallback control flow extracted from the
// web result-watcher (live-agent-runtime.ts). Must preserve: two attempts at
// +1.5s and +3s, then the fallback exactly once; the attempt is re-evaluated
// inside each timer (not at schedule time). Uses node:test mock timers.

describe('deliverWithRetry', () => {
	beforeEach(() => mock.timers.enable({ apis: ['setTimeout'] }));
	afterEach(() => mock.timers.reset());

	it('delivers on the first attempt → no fallback, no further attempts', () => {
		let attempts = 0, exhausted = 0;
		deliverWithRetry({ attempt: () => (attempts++, true), onExhausted: () => { exhausted++; } });
		mock.timers.tick(1500);
		assert.equal(attempts, 1);
		assert.equal(exhausted, 0);
		mock.timers.tick(5000);
		assert.equal(attempts, 1); // no second timer was scheduled
	});

	it('default schedule: two failed attempts (+1.5s, +3s) then fallback once', () => {
		let attempts = 0, exhausted = 0;
		deliverWithRetry({ attempt: () => (attempts++, false), onExhausted: () => { exhausted++; } });
		mock.timers.tick(1500);
		assert.equal(attempts, 1);
		assert.equal(exhausted, 0);
		mock.timers.tick(1500); // t = 3000
		assert.equal(attempts, 2);
		assert.equal(exhausted, 1);
		mock.timers.tick(5000);
		assert.equal(attempts, 2); // no more attempts, fallback not re-run
		assert.equal(exhausted, 1);
	});

	it('second attempt succeeds → no fallback', () => {
		let attempts = 0, exhausted = 0;
		deliverWithRetry({ attempt: () => (attempts++, attempts >= 2), onExhausted: () => { exhausted++; } });
		mock.timers.tick(1500); // attempt 1 fails
		mock.timers.tick(1500); // attempt 2 succeeds
		assert.equal(attempts, 2);
		assert.equal(exhausted, 0);
	});

	it('custom single-delay schedule → one attempt then fallback', () => {
		let attempts = 0, exhausted = 0;
		deliverWithRetry({ attempt: () => (attempts++, false), onExhausted: () => { exhausted++; }, delaysMs: [100] });
		mock.timers.tick(100);
		assert.equal(attempts, 1);
		assert.equal(exhausted, 1);
	});

	it('empty schedule → fallback immediately, no attempt', () => {
		let attempts = 0, exhausted = 0;
		deliverWithRetry({ attempt: () => (attempts++, false), onExhausted: () => { exhausted++; }, delaysMs: [] });
		assert.equal(attempts, 0);
		assert.equal(exhausted, 1);
	});
});
