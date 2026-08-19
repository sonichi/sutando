/**
 * A hung connect must not strand the session in CONNECTING forever (#2963).
 *
 * The health monitor recovers only from CLOSED. bodhi transitions
 * CLOSED→CONNECTING inline, so a connect that FAILS FAST flips back and is
 * recoverable — but one that HANGS stays CONNECTING, where the guard can never
 * see it. Observed live: 23 minutes in CONNECTING with a client attached, mic
 * captured at 23.4 fps, nothing reaching the model.
 *
 * Imports the real predicate rather than restating its rules: a hand-copied
 * reimplementation passes while production drifts. It lives in its own module
 * because importing voice-agent.ts boots a voice agent — main() is unguarded.
 */
import {
	DEFAULT_STUCK_CONNECTING_MS, MIN_STUCK_CONNECTING_MS, nextConnectingTick, parseStuckConnectingMs, shouldForceClosed,
} from '../src/voice-connect-watchdog.js';

let failed = 0;
function check(name: string, got: unknown, want: unknown): void {
	const ok = got === want;
	console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${name}`);
	if (!ok) { console.log(`       got ${String(got)}, want ${String(want)}`); failed++; }
}

const T = 120_000;              // threshold under test
const base = {
	state: 'CONNECTING', clientConnected: true, connectingSince: 1_000_000,
	now: 1_000_000 + T + 1, lastReconnectAt: 0, fatalBackoffUntil: 0, thresholdMs: T,
};

// 1. The defect case: hung past the threshold, client attached.
check('a hung CONNECTING past the threshold forces CLOSED', shouldForceClosed(base), true);

// 2. CONTROLS — each precondition alone must veto. Without these, a predicate
//    that simply returned true would satisfy the case above.
check('...but not before the threshold',
	shouldForceClosed({ ...base, now: base.connectingSince + T - 1 }), false);
check('...not exactly AT the threshold (strict >)',
	shouldForceClosed({ ...base, now: base.connectingSince + T }), false);
check('...not without a client attached',
	shouldForceClosed({ ...base, clientConnected: false }), false);
check('...not from any other state',
	shouldForceClosed({ ...base, state: 'ACTIVE' }), false);
check('...not from CLOSED, which the existing guard already handles',
	shouldForceClosed({ ...base, state: 'CLOSED' }), false);

// 3. connectingSince === 0 is "not yet timing", not "stuck since the epoch".
//    Treating 0 as a real timestamp would fire on the very first tick.
check('an unset connectingSince never fires',
	shouldForceClosed({ ...base, connectingSince: 0 }), false);

// 4. The 60s reconnect throttle still applies, so this cannot become a loop.
check('a recent reconnect throttles the force',
	shouldForceClosed({ ...base, lastReconnectAt: base.now - 30_000 }), false);
check('...and an old one does not',
	shouldForceClosed({ ...base, lastReconnectAt: base.now - 61_000 }), true);

// 5. Fatal backoff wins: a terminal cause (bad key, exhausted quota) must back
//    off rather than reconnect forever against a wall.
check('an active fatal backoff suppresses the force',
	shouldForceClosed({ ...base, fatalBackoffUntil: base.now + 1 }), false);
check('...and an expired one does not',
	shouldForceClosed({ ...base, fatalBackoffUntil: base.now - 1 }), true);

// 6. The live shape from the incident: 23 minutes stuck, nothing else wrong.
check('the observed incident would have recovered',
	shouldForceClosed({ ...base, now: base.connectingSince + 23 * 60_000 }), true);


// --- VOICE_STUCK_CONNECTING_MS parsing (review nit: `Number(x) || d` ate 0 and typos) ---

check('an unset override takes the default', parseStuckConnectingMs(undefined), DEFAULT_STUCK_CONNECTING_MS);
check('an empty override takes the default', parseStuckConnectingMs('   '), DEFAULT_STUCK_CONNECTING_MS);
check('0 is honoured, not swallowed', parseStuckConnectingMs('0'), 0);
check('a valid override is used verbatim', parseStuckConnectingMs('180000'), 180_000);

// The safety margin is enforced, not assumed: a positive threshold below the
// upstream dial deadline would let the watchdog kill legitimate dials.
{
	const warnings: string[] = [];
	check('a positive sub-floor override clamps to the floor',
		parseStuckConnectingMs('5000', (m) => { warnings.push(m); }), MIN_STUCK_CONNECTING_MS);
	check('...and warns once', warnings.length, 1);
	check('...naming the variable', warnings[0]?.includes('VOICE_STUCK_CONNECTING_MS'), true);
}
check('the floor itself is accepted verbatim', parseStuckConnectingMs('60000'), MIN_STUCK_CONNECTING_MS);
check('threshold 0 disables the force entirely',
	shouldForceClosed({ ...base, thresholdMs: 0, now: 1e12 }), false);

for (const bad of ['abc', '12x', 'NaN', '-1']) {
	const warnings: string[] = [];
	check(`${bad} falls back to the default`,
		parseStuckConnectingMs(bad, (m) => { warnings.push(m); }), DEFAULT_STUCK_CONNECTING_MS);
	check(`${bad} warns exactly once`, warnings.length, 1);
	check(`${bad} names the variable in the warning`,
		warnings[0]?.includes('VOICE_STUCK_CONNECTING_MS'), true);
}

// --- The tick LIFECYCLE (review blocker): the hang clock keys on state alone.
//     A client detaching mid-hang must not reset it — a user reloading the
//     panel is the common human response to a hung session, and resetting
//     would push recovery out indefinitely.
const t0 = 2_000_000;
const tickBase = {
	state: 'CONNECTING', clientConnected: true, now: t0,
	lastReconnectAt: 0, fatalBackoffUntil: 0, thresholdMs: T,
};
check('first CONNECTING observation arms the clock',
	nextConnectingTick({ ...tickBase, connectingSince: 0 }).connectingSince, t0);
check('...without firing on the arming tick',
	nextConnectingTick({ ...tickBase, connectingSince: 0, now: t0 + T + 1 }).forceClose, false);
check('a client detaching mid-hang does NOT reset the clock',
	nextConnectingTick({ ...tickBase, clientConnected: false, connectingSince: t0, now: t0 + 60_000 }).connectingSince, t0);
check('...and does not fire while detached',
	nextConnectingTick({ ...tickBase, clientConnected: false, connectingSince: t0, now: t0 + T + 1 }).forceClose, false);
check('a reload straddling the hang still fires from the ORIGINAL arm time',
	nextConnectingTick({ ...tickBase, connectingSince: t0, now: t0 + T + 1 }).forceClose, true);
check('leaving CONNECTING resets the clock',
	nextConnectingTick({ ...tickBase, state: 'ACTIVE', connectingSince: t0 }).connectingSince, 0);
check('...and never fires from another state',
	nextConnectingTick({ ...tickBase, state: 'CLOSED', connectingSince: t0, now: t0 + T + 1 }).forceClose, false);

console.log(failed ? `FAILED: ${failed} check(s)` : 'voice stuck-CONNECTING watchdog: all checks passed');
process.exit(failed ? 1 : 0);

