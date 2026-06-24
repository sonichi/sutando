// Selected-text probe for screen-companion's vision_query (issue #1389, PR #1409).
//
// Tries two short-timeout probes in order:
//   1. AXSelectedText on the frontmost app's focused element (native apps)
//   2. window.getSelection() in Chrome's active tab (browser content)
//
// Returns the first non-empty result, or null. Single-purpose helper —
// kept tiny on purpose so vision_query stays focused on its mode handling.
//
// Timeout budget: 800ms per probe (Mini #1409 review). 3s × 2 = 6s worst-case
// is too slow for an inline voice tool; 800ms keeps total budget ≤ 1.6s.

import { execSync } from 'node:child_process';

export type SelectionSource = 'ax_selection' | 'chrome_js_selection';

export interface SelectionResult {
	text: string;
	source: SelectionSource;
}

const AX_SCRIPT = `try
  tell application "System Events" to tell (first process whose frontmost is true)
    return value of attribute "AXSelectedText" of (first UI element whose AXFocused is true)
  end tell
on error
  return ""
end try`;

const CHROME_JS_SCRIPT = `try
  tell application "Google Chrome" to tell active tab of front window to execute javascript "window.getSelection().toString()"
on error
  return ""
end try`;

function probe(script: string, label: string, timeoutMs = 800): string {
	try {
		return execSync(`osascript -e '${script}'`, { encoding: 'utf-8', timeout: timeoutMs }).trim();
	} catch (err) {
		// execSync timeout surfaces as an Error with .signal === 'SIGTERM' AND .code === 'ETIMEDOUT'.
		const e = err as { signal?: string; code?: string };
		if (e?.signal === 'SIGTERM' || e?.code === 'ETIMEDOUT') {
			console.error(`[read-selection] ${label} probe timed out — permission may be denied`);
		}
		return '';
	}
}

/** Read the user's current text selection. Tries AX first (native apps),
 *  then Chrome's window.getSelection() (browser content). Returns null when
 *  nothing is selected anywhere. */
export function readSelection(timeoutMs = 800): SelectionResult | null {
	const ax = probe(AX_SCRIPT, 'AX', timeoutMs);
	if (ax) return { text: ax, source: 'ax_selection' };
	const js = probe(CHROME_JS_SCRIPT, 'Chrome JS', timeoutMs);
	if (js) return { text: js, source: 'chrome_js_selection' };
	return null;
}
