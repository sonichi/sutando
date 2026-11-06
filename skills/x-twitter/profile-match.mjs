/**
 * Which Chrome processes hold a given profile — the predicate behind a DESTRUCTIVE
 * cleanup, so it is exact rather than approximate.
 *
 * WHY THIS EXISTS: `releaseProfileLock()` SIGTERM/SIGKILLs every PID this predicate
 * returns. It used to test `line.includes('--user-data-dir=' + PROFILE_DIR)`, and a
 * substring test does not respect argument boundaries: with
 * `PROFILE_DIR=/tmp/x-profile`, a browser running `--user-data-dir=/tmp/x-profile-copy`
 * matched and was killed. qingyun reproduced exactly that on #2133 — someone else's live
 * browser, with their tabs and unsaved state, terminated by our lock cleanup.
 *
 * The value of `--user-data-dir=` must therefore END where our profile path ends. It is
 * a pure function in its own module for the same reason `composer-text.mjs` is: importing
 * `x-post-browser.mjs` would run the script (argv parsing, mkdir, Chrome resolution), so
 * the only testable shape is a separate module — and a test that re-implemented the match
 * could stay green while the shipped predicate regressed (#1414).
 *
 * FAILURE DIRECTION IS DELIBERATE. Where the match is uncertain this returns false, so
 * the cleanup kills nothing and the next launch may hit the singleton lock — a visible,
 * recoverable annoyance. The opposite failure destroys an unrelated browser session.
 */

const FLAG = '--user-data-dir=';

/**
 * NOT the production decider (see pidsFromLsofFields — open descriptors decide).
 * A strict "this row is definitely ours" predicate, kept because it documents the
 * boundary: flattened argv cannot say where a path ends, so it answers only when
 * the value is exact and fails closed on everything else.
 *
 * True when `line` (one `pgrep -fl` row) runs Chrome against exactly `profileDir`.
 *
 * `pgrep -fl` prints `PID <argv joined by single spaces>`, so an argument ends at the
 * next space or at end-of-line. A path containing spaces is therefore indistinguishable
 * from two arguments; such a profile simply never matches, which is the safe direction.
 */
export function lineHoldsProfile(line, profileDir) {
	if (!line || !profileDir) return false;
	// Renderer/GPU helpers inherit the parent's --user-data-dir. Killing them is both
	// pointless and noisy; the singleton lock is held by the browser process.
	if (line.includes('--type=')) return false;
	let i = 0;
	for (;;) {
		i = line.indexOf(FLAG, i);
		if (i === -1) return false;
		const value = line.slice(i + FLAG.length);
		// Exact, or exact-then-argument-boundary. `startsWith(profileDir)` alone is the
		// bug: "/tmp/x-profile" is a prefix of "/tmp/x-profile-copy".
		// EXACT only. A trailing space is undecidable in flattened argv — a following
		// `--flag` is equally part of a profile path literally named that (qingyun, #2133).
		if (value === profileDir) return true;
		i += FLAG.length;
	}
}

/** PIDs from `pgrep -fl` output whose Chrome holds exactly `profileDir`. */
export function pidsHoldingProfile(pgrepOutput, profileDir) {
	return String(pgrepOutput ?? '')
		.split('\n')
		.filter((l) => lineHoldsProfile(l, profileDir))
		.map((l) => l.trim().split(/\s+/)[0])
		.filter(Boolean);
}


/**
 * PIDs from `lsof -F pn +D <dir>` output. `+D` already scoped the search to that
 * directory, so the kernel did the matching and no path parsing is needed — which
 * is the point: a flattened argv cannot say where a path ends, an open file descriptor
 * can. Field format: a `p<pid>` line opens a process block, `n<path>` lines follow.
 */
export function pidsFromLsofFields(out) {
	const pids = [];
	let cur = null;
	for (const raw of String(out ?? '').split('\n')) {
		if (raw.startsWith('p')) {
			const pid = raw.slice(1).trim();
			cur = /^\d+$/.test(pid) ? pid : null;
		} else if (raw.startsWith('n') && cur && !pids.includes(cur)) {
			pids.push(cur);
		}
	}
	return pids;
}

/**
 * PIDs of Google-Chrome-for-Testing processes from `pgrep -fl` output, excluding
 * renderer/GPU helpers. This answers "is this pid GCfT", never "whose profile is it" —
 * the profile question belongs to pidsFromLsofFields().
 */
export function gcftPids(pgrepOutput) {
	return String(pgrepOutput ?? '')
		.split('\n')
		.filter((l) => l.trim() && !l.includes('--type='))
		.map((l) => l.trim().split(/\s+/)[0])
		.filter((p) => /^\d+$/.test(p));
}

/**
 * Decide holders from one `lsof -F pn +D <dir>` probe outcome — pure, so the
 * inconclusive-vs-confirmed-empty distinction is unit-testable without spawning lsof.
 *
 * `lsof` exits non-zero even when it PRINTED real holder lines (verified: one holder,
 * output correct, exit=1) — a thrown error is therefore not itself proof of "no
 * holders". Only a probe that produced no usable stdout at all is inconclusive:
 * permission denied, the binary missing, or our own timeout (`killed`).
 *
 * qingyun (#2133, 2026-08-24): the caller used to collapse both cases to an empty PID
 * list, and releaseProfileLock() then deleted the singleton lock files regardless —
 * risking a second Chrome launching against a still-active profile. Callers MUST fail
 * closed on `known: false`: treat it as "cannot say", never as "zero holders".
 */
/**
 * Did `execFileSync` kill this child on its own `timeout`? Node reports that as
 * `code: 'ETIMEDOUT'` with `signal: 'SIGTERM'` and NO `killed` property at all —
 * `killed` is set on the async `exec` error, not the sync one. Reading `e.killed`
 * therefore evaluates false on a real timeout, and the probe is then classified by
 * its stdout: a timed-out lsof that printed a partial holder list reads as a
 * complete one, and releaseProfileLock() SIGKILLs from it.
 */
export function execTimedOut(e) {
	if (!e || typeof e !== 'object') return false;
	return e.killed === true || e.code === 'ETIMEDOUT' || e.signal === 'SIGTERM';
}

export function classifyLsofProbe(outcome) {
	// A probe that measured nothing is the case the tri-state exists to name, so the
	// no-argument path answers "cannot say" rather than a confident zero holders.
	if (!outcome || typeof outcome !== 'object') return { known: false, pids: [] };
	const { threw, killed, stdout } = outcome;
	if (killed) return { known: false, pids: [] };
	if (threw && !(typeof stdout === 'string' && stdout.length > 0)) {
		return { known: false, pids: [] };
	}
	return { known: true, pids: pidsFromLsofFields(stdout) };
}
