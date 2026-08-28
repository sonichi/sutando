/**
 * Extract the user-actionable sentence from an osascript failure.
 *
 * Chrome and macOS name the fix inside the error text itself; the two TCC
 * codes that don't are given a one-line hint. Dependency-free so it stays
 * testable without the agent SDK.
 */

/** Returns the actionable sentence, or null when it is not a setup problem. */
export function setupHint(raw: string): string | null {
	// Line-scoped and greedy so a parenthesised number inside the sentence cannot
	// truncate it; the multiline form is the fallback for errors that wrap.
	const m = /execution error:\s*([^\n]*)\s*\((-?\d+)\)/.exec(raw || '')
		?? /execution error:\s*([\s\S]*?)\s*\((-?\d+)\)/.exec(raw || '');
	if (!m) return null;
	// The text is spoken aloud, so drop the trailing support URL.
	const text = m[1].replace(/\s*For more information:\s*\S+/i, '').replace(/\s+/g, ' ').trim();
	if (!text) return null;
	if (m[2] === '-1743') return `${text} Turn on AG2 Space for that app in System Settings > Privacy & Security > Automation.`;
	if (m[2] === '1002') return `${text} Turn on AG2 Space in System Settings > Privacy & Security > Accessibility, then quit and reopen AG2 Space.`;
	return text;
}

export type KeystrokeOutcome =
	| { status: 'setup_required'; steps: string[]; message: string }
	| { error: string };

/**
 * Decide what a failed keystroke/paste reports. Same extraction rationale as
 * scrollOutcome: in-place this could be disabled without a test failing.
 *
 * `scroll` already turns an OS denial into steps the model reads aloud; the
 * keystroke tools returned the raw error, so the user heard "I don't have
 * permission" with nothing to act on.
 */
export function keystrokeOutcome(prefix: string, raw: string): KeystrokeOutcome {
	const hint = setupHint(raw);
	// No hint means an ordinary failure — keep the raw text so real errors read
	// unchanged rather than being dressed up as a setup problem.
	if (!hint) return { error: `${prefix}: ${raw}` };
	return {
		status: 'setup_required',
		steps: [hint],
		message: `${prefix} did not go through — a one-time setup step is needed. `
			+ 'Tell the user that, then read the "steps" to them verbatim, in order. '
			+ 'Do not add steps of your own.',
	};
}

export type ScrollOutcome =
	| { status: 'setup_required'; moved: false; steps: string[]; message: string }
	| { status: 'at_limit'; moved: false; message: string }
	| { status: 'scrolled'; moved: boolean | null };

/**
 * Decide what a scroll attempt reports. Extracted so the decision is reachable
 * by a test — in-place it could be disabled without any assertion failing.
 */
/** Fallback step when macOS refuses a keystroke without text setupHint can parse.
 *  Deliberately hedged: the throw proves refusal, not which grant is missing. */
const UNEXPLAINED_DENIAL_STEP =
	'macOS refused the keystroke without saying why. Check System Settings > Privacy & Security '
	+ '> Accessibility for AG2 Space, turn it on if it is off, then quit and reopen AG2 Space.';

export function scrollOutcome(o: {
	scrollMoved: boolean | null; keyDenied: boolean; hints: string[]; direction: string;
}): ScrollOutcome {
	// JS is authoritative: `false` means the page really is at its limit, so that
	// answer outranks a keystroke denial, which says nothing about the page.
	if (o.scrollMoved === false) {
		return { status: 'at_limit', moved: false,
		         message: `Nothing scrolled — the page appears to be at the ${o.direction === 'down' ? 'bottom' : o.direction === 'up' ? 'top' : o.direction}. Tell the user it can't scroll further that way.` };
	}
	// keyDenied is load-bearing: `osascript key code` exits 0 whether or not
	// anything handled the key, so only a THROW proves the fallback never ran.
	if (o.scrollMoved === null && o.keyDenied) {
		// An unparseable denial is still a denial: the JS gave no answer and the
		// keystroke threw, so reporting `scrolled` would assert a move nothing saw.
		return { status: 'setup_required', moved: false,
		         steps: o.hints.length > 0 ? o.hints : [UNEXPLAINED_DENIAL_STEP],
		         message: 'The scroll did not go through — a one-time setup step is needed. Tell the user that, then read the "steps" to them verbatim, in order. Do not add steps of your own.' };
	}
	return { status: 'scrolled', moved: o.scrollMoved };
}
