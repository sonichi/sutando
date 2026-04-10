/**
 * Chrome-specific tools — tab switching and URL opening.
 * Split from browser-tools.ts for modularity.
 */

import { execSync } from 'node:child_process';
import { writeFileSync, unlinkSync } from 'node:fs';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';

const ts = () => new Date().toLocaleTimeString('en-US', { hour12: false });

// --- Tab switching ---

const TAB_ALIASES: Record<string, string> = {
	'github': 'github.com', 'repo': 'github.com', 'github repo': 'github.com',
	'gmail': 'mail.google.com', 'email': 'mail.google.com', 'inbox': 'mail.google.com',
	'calendar': 'calendar.google.com', 'gcal': 'calendar.google.com',
	'twitter': 'x.com', 'x': 'x.com',
	'dashboard': 'localhost:7844', 'sutando': 'localhost:8080', 'web client': 'localhost:8080',
	'gemini': 'gemini.google.com',
};

export const switchTabTool: ToolDefinition = {
	name: 'switch_tab',
	description:
		'Switch to a Chrome tab by keyword. Searches both tab titles and URLs. Use for: "switch to GitHub", "go to Gmail", "open the calendar tab".',
	parameters: z.object({
		keyword: z.string().describe('Keyword to match in tab title or URL (e.g., "GitHub", "Gmail", "calendar")'),
	}),
	execution: 'inline',
	async execute(args) {
		const { keyword } = args as { keyword: string };
		// Resolve aliases to URL patterns
		const alias = TAB_ALIASES[keyword.toLowerCase()];
		const searchTerms = alias ? [keyword, alias] : [keyword];
		// Split into individual words for fuzzy matching (speech-to-text often garbles multi-word names)
		const allTerms = [...searchTerms];
		for (const term of searchTerms) {
			const words = term.split(/\s+/).filter(w => w.length >= 4); // only words 4+ chars
			allTerms.push(...words);
		}
		const uniqueTerms = [...new Set(allTerms)];
		const safeTerms = uniqueTerms.map(t => t.replace(/\\/g, '\\\\').replace(/"/g, '\\"'));
		const urlConditions = safeTerms.map(t => `URL of t contains "${t}"`).join(' or ');
		const titleConditions = safeTerms.map(t => `title of t contains "${t}"`).join(' or ');
		try {
			// Two-pass match: URL first, then title.
			//
			// The naive `title OR URL contains keyword` returns the first tab
			// in window-walk order that matches ANYTHING — which is wrong when
			// a user says "switch to dashboard" and the walk order puts a
			// random X tweet that happens to contain the word "Sutando" in its
			// body text ahead of the actual Sutando Dashboard tab. URL is a
			// stronger signal than title: aliases in TAB_ALIASES are URL
			// patterns, and the user almost always means the app/site, not a
			// random tab whose body text mentions it. If no URL matches, fall
			// back to title.
			const script = `tell application "Google Chrome"
set tabIndex to 0
repeat with w in windows
set tabIndex to 0
repeat with t in tabs of w
set tabIndex to tabIndex + 1
ignoring case
if ${urlConditions} then
set active tab index of w to tabIndex
set index of w to 1
activate
return title of t
end if
end ignoring
end repeat
end repeat
set tabIndex to 0
repeat with w in windows
set tabIndex to 0
repeat with t in tabs of w
set tabIndex to tabIndex + 1
ignoring case
if ${titleConditions} then
set active tab index of w to tabIndex
set index of w to 1
activate
return title of t
end if
end ignoring
end repeat
end repeat
return "not found"
end tell`;
			const tmpFile = `/tmp/sutando-switchtab-${Date.now()}.scpt`;
			writeFileSync(tmpFile, script);
			const result = execSync(`osascript ${tmpFile}`, { timeout: 5_000 }).toString().trim();
			try { unlinkSync(tmpFile); } catch {}
			if (result === 'not found') {
				console.log(`${ts()} [SwitchTab] no tab matching "${keyword}"`);
				return { error: `No Chrome tab found matching "${keyword}"` };
			}
			console.log(`${ts()} [SwitchTab] switched to: ${result}`);
			return { status: 'switched', tab: result };
		} catch (err) {
			return { error: `Failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// --- Open URL ---

export const openUrlTool: ToolDefinition = {
	name: 'open_url',
	description:
		'Open a URL in a new Chrome tab. Use for: "open github.com", "go to that link".',
	parameters: z.object({
		url: z.string().describe('The URL to open'),
	}),
	execution: 'inline',
	async execute(args) {
		const { url } = args as { url: string };
		// Escape backslashes first, then quotes — prevents shell injection via osascript
		const safeUrl = url.replace(/\\/g, '\\\\').replace(/'/g, "'\\''").replace(/"/g, '\\"');
		try {
			execSync(`osascript -e 'tell application "Google Chrome" to tell front window to make new tab with properties {URL:"${safeUrl}"}'`, { timeout: 5_000 });
			console.log(`${ts()} [OpenURL] opened: ${url}`);
			return { status: 'opened', url };
		} catch (err) {
			return { error: `Failed to open ${url}: ${err instanceof Error ? err.message : err}` };
		}
	},
};
