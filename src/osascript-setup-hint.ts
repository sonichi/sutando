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
