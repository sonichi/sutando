/**
 * Extract the user-actionable sentence from an osascript failure.
 *
 * Chrome and macOS name the fix inside the error text itself; the two TCC
 * codes that don't are given a one-line hint. Dependency-free so it stays
 * testable without the agent SDK.
 */

/** Returns the actionable sentence, or null when it is not a setup problem. */
export function setupHint(raw: string): string | null {
	const m = /execution error:\s*([\s\S]*?)\s*\((-?\d+)\)/.exec(raw || '');
	if (!m) return null;
	// The text is spoken aloud, so drop the trailing support URL.
	const text = m[1].replace(/\s*For more information:\s*\S+/i, '').replace(/\s+/g, ' ').trim();
	if (!text) return null;
	if (m[2] === '-1743') return `${text} Turn on AG2 Space for that app in System Settings > Privacy & Security > Automation.`;
	if (m[2] === '1002') return `${text} Turn on AG2 Space in System Settings > Privacy & Security > Accessibility, then quit and reopen AG2 Space.`;
	return text;
}

export type ScrollOutcome =
	| { status: 'setup_required'; moved: false; steps: string[]; message: string }
	| { status: 'at_limit'; moved: false; message: string }
	| { status: 'scrolled'; moved: boolean | null };

/**
 * Decide what a scroll attempt reports. Extracted so the decision is reachable
 * by a test — in-place it could be disabled without any assertion failing.
 */
export function scrollOutcome(o: {
	scrollMoved: boolean | null; keyDenied: boolean; hints: string[]; direction: string;
}): ScrollOutcome {
	// keyDenied is load-bearing: `osascript key code` exits 0 whether or not
	// anything handled the key, so only a THROW proves the fallback never ran.
	if (o.scrollMoved !== true && o.keyDenied && o.hints.length > 0) {
		return { status: 'setup_required', moved: false, steps: o.hints,
		         message: 'The scroll did not go through — a one-time setup step is needed. Tell the user that, then read the "steps" to them verbatim, in order. Do not add steps of your own.' };
	}
	if (o.scrollMoved === false) {
		return { status: 'at_limit', moved: false,
		         message: `Nothing scrolled — the page appears to be at the ${o.direction === 'down' ? 'bottom' : o.direction === 'up' ? 'top' : o.direction}. Tell the user it can't scroll further that way.` };
	}
	return { status: 'scrolled', moved: o.scrollMoved };
}
