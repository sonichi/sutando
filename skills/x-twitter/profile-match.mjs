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
		if (value === profileDir) return true;
		if (value.startsWith(profileDir + ' ')) {
			// A following FLAG proves the path ended at that space. A bare token does
			// not: "/tmp/x-profile copy" is equally a path containing a space.
			if (value.slice(profileDir.length + 1).startsWith('-')) return true;
		}
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
