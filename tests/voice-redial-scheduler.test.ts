/**
 * F5 — event-driven redial scheduler with exponential backoff.
 *
 * The blind 30s-tick/60s-throttle redial gave up to 60s of dead air after a
 * clean single failure, and after fast re-kills (2026-08-17 incident: fresh
 * lineages dying in 9s/11s) it redialed at a fixed cadence forever. These
 * tests pin the scheduler contract:
 *  - backoff ladder 1s, 2s, 4s, ... capped at 60s; jitter ±20%;
 *  - setup-ok resets the ladder only after 30s of stability — a 9s death
 *    must NOT reset it (the loop that defeats naive reset);
 *  - local disconnect (bodhi's own disconnect(): 1000 "local disconnect")
 *    never schedules;
 *  - the fatal-backoff gate is honoured at schedule AND fire time;
 *  - the 30s tick fallback defers to a pending scheduled dial.
 *
 * Imports the real module rather than restating its rules (repo convention:
 * a hand-copied reimplementation passes while production drifts).
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import {
	REDIAL_BASE_MS, REDIAL_CAP_MS, REDIAL_STABLE_MS, REDIAL_JITTER_FRAC,
	initialRedialState, isLocalDisconnect, backoffDelayMs, noteLifecycle,
	shouldEventDial, tickMayDial, noteDialed,
	type RedialState, type RedialLifecycleEvent,
} from '../src/voice-redial-scheduler.js';

/** random()=0.5 → jitter factor exactly 1.0 — the deterministic midpoint. */
const mid = (): number => 0.5;

const remoteClose: RedialLifecycleEvent = { kind: 'generation-close', code: 1011, reason: 'internal error' };
const localClose: RedialLifecycleEvent = { kind: 'generation-close', code: 1000, reason: 'local disconnect' };

function loss(state: RedialState, now: number, ev: RedialLifecycleEvent = remoteClose, fatalBackoffUntil = 0) {
	return noteLifecycle(state, ev, { now, fatalBackoffUntil, random: mid });
}

describe('backoffDelayMs', () => {
	it('follows the 1s, 2s, 4s, 8s ladder at the jitter midpoint', () => {
		assert.equal(backoffDelayMs(1, mid), 1_000);
		assert.equal(backoffDelayMs(2, mid), 2_000);
		assert.equal(backoffDelayMs(3, mid), 4_000);
		assert.equal(backoffDelayMs(4, mid), 8_000);
		assert.equal(backoffDelayMs(6, mid), 32_000);
	});

	it('caps at 60s (2^6 = 64s would exceed it) and stays capped', () => {
		assert.equal(backoffDelayMs(7, mid), REDIAL_CAP_MS);
		assert.equal(backoffDelayMs(8, mid), REDIAL_CAP_MS);
		assert.equal(backoffDelayMs(50, mid), REDIAL_CAP_MS);
	});

	it('jitters within ±20% of the capped base, symmetric at the extremes', () => {
		assert.equal(backoffDelayMs(1, () => 0), REDIAL_BASE_MS * (1 - REDIAL_JITTER_FRAC));
		assert.equal(backoffDelayMs(1, () => 0.9999999), Math.round(REDIAL_BASE_MS * (1 + REDIAL_JITTER_FRAC * (2 * 0.9999999 - 1))));
		for (const r of [0, 0.1, 0.25, 0.5, 0.75, 0.9999]) {
			const d = backoffDelayMs(7, () => r);
			assert.ok(d >= REDIAL_CAP_MS * 0.8 && d <= REDIAL_CAP_MS * 1.2, `failures=7 r=${r} → ${d}`);
		}
	});
});

describe('noteLifecycle — scheduling', () => {
	it('a remote generation-close schedules the first dial at ~1s', () => {
		const r = loss(initialRedialState(), 100_000);
		assert.equal(r.scheduleDelayMs, 1_000);
		assert.equal(r.state.failures, 1);
		assert.equal(r.state.nextDialAt, 101_000);
	});

	it('setup-failed schedules exactly like a remote close', () => {
		const r = loss(initialRedialState(), 100_000, { kind: 'setup-failed', reason: 'timeout' });
		assert.equal(r.scheduleDelayMs, 1_000);
		assert.equal(r.state.failures, 1);
	});

	it('consecutive fast losses escalate the ladder', () => {
		let s = initialRedialState();
		const delays: number[] = [];
		let now = 100_000;
		for (let i = 0; i < 8; i++) {
			const r = loss(s, now, { kind: 'setup-failed' });
			s = r.state;
			delays.push(r.scheduleDelayMs ?? -1);
			now = s.nextDialAt + 500; // dial fails again shortly after
		}
		assert.deepEqual(delays, [1_000, 2_000, 4_000, 8_000, 16_000, 32_000, 60_000, 60_000]);
	});

	it('attempt-close alone never schedules (setup-failed is the verdict)', () => {
		const r = noteLifecycle(initialRedialState(), { kind: 'attempt-close', code: 1006 }, { now: 100_000, random: mid });
		assert.equal(r.scheduleDelayMs, null);
		assert.deepEqual(r.state, initialRedialState());
	});

	it('local disconnect never schedules a redial', () => {
		let s = initialRedialState();
		s = loss(s, 100_000, { kind: 'setup-failed' }).state; // failures=1
		const r = loss(s, 101_000, localClose);
		assert.equal(r.scheduleDelayMs, null);
		assert.equal(r.state.failures, 1); // 9s-class instability is remembered
	});

	it('a NON-local 1000 close still schedules (reason is part of the match)', () => {
		assert.equal(isLocalDisconnect({ kind: 'generation-close', code: 1000, reason: 'bye' }), false);
		const r = loss(initialRedialState(), 100_000, { kind: 'generation-close', code: 1000, reason: 'bye' });
		assert.equal(r.scheduleDelayMs, 1_000);
	});
});

describe('noteLifecycle — stability-window reset', () => {
	/** Ladder at failures=3, then a setup-ok at t, then a close at t+dt. */
	function afterOkThenClose(dt: number) {
		let s: RedialState = { failures: 3, nextDialAt: 0, setupOkAt: 0 };
		s = noteLifecycle(s, { kind: 'setup-ok' }, { now: 500_000, random: mid }).state;
		assert.equal(s.setupOkAt, 500_000);
		return loss(s, 500_000 + dt);
	}

	it('a close 9s after setup-ok does NOT reset the ladder (the incident loop)', () => {
		const r = afterOkThenClose(9_000);
		assert.equal(r.state.failures, 4);
		assert.equal(r.scheduleDelayMs, 8_000);
	});

	it('a close just under the 30s window still escalates', () => {
		const r = afterOkThenClose(REDIAL_STABLE_MS - 1);
		assert.equal(r.state.failures, 4);
	});

	it('a close at exactly 30s counts as stable — ladder resets to 1', () => {
		const r = afterOkThenClose(REDIAL_STABLE_MS);
		assert.equal(r.state.failures, 1);
		assert.equal(r.scheduleDelayMs, 1_000);
	});

	it('a stable connection ended by LOCAL disconnect clears the ladder without scheduling', () => {
		let s: RedialState = { failures: 5, nextDialAt: 0, setupOkAt: 0 };
		s = noteLifecycle(s, { kind: 'setup-ok' }, { now: 500_000, random: mid }).state;
		const r = loss(s, 500_000 + 600_000, localClose); // GoAway-style cycle 10min in
		assert.equal(r.scheduleDelayMs, null);
		assert.equal(r.state.failures, 0);
	});

	it('the stability proof is consumed: a setup-failed AFTER the reset-earning close starts at failures=2', () => {
		let s: RedialState = { failures: 3, nextDialAt: 0, setupOkAt: 0 };
		s = noteLifecycle(s, { kind: 'setup-ok' }, { now: 500_000, random: mid }).state;
		s = loss(s, 500_000 + 60_000).state; // stable → reset → failures=1
		assert.equal(s.failures, 1);
		const r = loss(s, 500_000 + 62_000, { kind: 'setup-failed' });
		assert.equal(r.state.failures, 2); // no second reset from the same setup-ok
		assert.equal(r.scheduleDelayMs, 2_000);
	});
});

describe('noteLifecycle — pending-dial hygiene', () => {
	it('an attempt clears the pending dial (a dial is in flight)', () => {
		const s = loss(initialRedialState(), 100_000).state;
		assert.ok(s.nextDialAt > 0);
		const r = noteLifecycle(s, { kind: 'attempt' }, { now: 100_500, random: mid });
		assert.equal(r.state.nextDialAt, 0);
		assert.equal(r.state.failures, 1); // ladder survives the attempt
	});

	it('an attempt clears the stability credit — a new dial cannot inherit the old connection\'s', () => {
		// The tick's safety-net dial reaches `attempt` with the PREVIOUS
		// setupOkAt still set, because no close was observed to consume it.
		// Preserving it made the next setup-failed look like a stable
		// connection ending, resetting a 5-deep ladder to 1 (1s, not 32s) —
		// exactly the churn the backoff exists to prevent.
		let s: RedialState = { failures: 5, nextDialAt: 0, setupOkAt: 100_000 };
		s = noteLifecycle(s, { kind: 'attempt' }, { now: 200_000, random: mid }).state;
		assert.equal(s.setupOkAt, 0, 'credit from the dead connection must not carry');
		const r = loss(s, 201_000, { kind: 'setup-failed' });
		assert.equal(r.state.failures, 6, 'the ladder escalates, it does not reset');
		assert.equal(r.scheduleDelayMs, 32_000);
	});

	it('setup-ok clears the pending dial', () => {
		const s = loss(initialRedialState(), 100_000).state;
		const r = noteLifecycle(s, { kind: 'setup-ok' }, { now: 100_500, random: mid });
		assert.equal(r.state.nextDialAt, 0);
	});

	it('noteDialed consumes the schedule and keeps the ladder', () => {
		const s = loss(initialRedialState(), 100_000).state;
		const d = noteDialed(s);
		assert.equal(d.nextDialAt, 0);
		assert.equal(d.failures, 1);
	});
});

describe('fatal-backoff gate', () => {
	it('nextDialAt never lands before fatalBackoffUntil', () => {
		const fatal = 400_000;
		const r = loss(initialRedialState(), 100_000, remoteClose, fatal);
		assert.equal(r.state.nextDialAt, fatal);
		assert.equal(r.scheduleDelayMs, fatal - 100_000);
	});

	it('shouldEventDial refuses while the fatal gate is active (strict >, matching the tick)', () => {
		const base = { state: 'CLOSED', clientConnected: true, now: 200_000, nextDialAt: 150_000, fatalBackoffUntil: 0 };
		assert.equal(shouldEventDial(base), true);
		assert.equal(shouldEventDial({ ...base, fatalBackoffUntil: 200_000 }), false);
		assert.equal(shouldEventDial({ ...base, fatalBackoffUntil: 199_999 }), true);
	});
});

describe('shouldEventDial — fire-time preconditions', () => {
	const base = { state: 'CLOSED', clientConnected: true, now: 200_000, nextDialAt: 200_000, fatalBackoffUntil: 0 };

	it('dials when CLOSED, client attached, due, and no fatal gate', () => {
		assert.equal(shouldEventDial(base), true);
	});

	it('each precondition alone vetoes', () => {
		assert.equal(shouldEventDial({ ...base, nextDialAt: 0 }), false, 'nothing pending');
		assert.equal(shouldEventDial({ ...base, now: 199_999 }), false, 'not yet due');
		assert.equal(shouldEventDial({ ...base, state: 'CONNECTING' }), false, 'dial in flight');
		assert.equal(shouldEventDial({ ...base, state: 'ACTIVE' }), false, 'session healthy');
		assert.equal(shouldEventDial({ ...base, clientConnected: false }), false, 'nobody listening');
	});
});

describe('tick fallback interaction', () => {
	it('the tick defers to a pending scheduled dial', () => {
		assert.equal(tickMayDial({ now: 100_000, nextDialAt: 130_000 }), false);
	});

	it('the tick may dial once the schedule is due or absent', () => {
		assert.equal(tickMayDial({ now: 130_000, nextDialAt: 130_000 }), true);
		assert.equal(tickMayDial({ now: 100_000, nextDialAt: 0 }), true, 'no schedule → tick behaves as before');
	});
});
