/**
 * Browser & screen tools — Chrome tab control, scrolling, screenshots, and vision descriptions.
 * Split from inline-tools.ts for readability.
 */

import { execSync, execFileSync } from 'node:child_process';
import { writeFileSync, unlinkSync, readFileSync, existsSync } from 'node:fs';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import { demoStateRef } from './recording-state.js';

const ts = () => new Date().toLocaleTimeString('en-US', { hour12: false });

/** Send text to Gemini via sendRealtimeInput when available, otherwise sendContent. */
export function injectText(session: any, text: string) {
	try {
		const transport = session?.transport;
		if (typeof transport?.session?.sendRealtimeInput === 'function') {
			transport.session.sendRealtimeInput({ text });
		} else if (typeof transport?.sendContent === 'function') {
			transport.sendContent([{ role: 'user', text }], true);
		} else {
			console.warn(`${ts()} [InjectText] No supported text injection method on transport`);
		}
	} catch (err) {
		console.error(`${ts()} [InjectText] Error:`, err);
	}
}

// Vision model — override via .env (default: flash-lite for this trivial 20-word task)
const VISION_MODEL = process.env.VISION_MODEL || 'gemini-3.1-flash-lite-preview';

// --- Scroll ---

export const scrollTool: ToolDefinition = {
	name: 'scroll',
	description:
		'Scroll the Chrome browser page. Use for: "scroll down", "scroll up", "scroll to top", "scroll to bottom". Use target for specific areas: "sidebar", "chat history", "code block".',
	parameters: z.object({
		direction: z.enum(['down', 'up', 'top', 'bottom']).describe('Scroll direction. Use "top" or "bottom" to jump to start/end of page.'),
		target: z.string().optional().describe('Optional: which area to scroll. E.g. "sidebar", "chat history", "nav", "code". Omit for main content.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { direction, target } = args as { direction: 'down' | 'up' | 'top' | 'bottom'; target?: string };
		try {
			// Target-specific selectors for common areas
			const targetSelector = target
				? (target.match(/side|nav|history|menu/i) ? 'nav' : target.match(/code/i) ? 'pre,code' : target)
				: '';
			// Use Chrome's JavaScript scroll. If target specified, find matching scrollable element.
			// Otherwise find the WIDEST scrollable container (main content over sidebars).
			// Use single quotes in JS to avoid double-quote escaping issues inside AppleScript
			const scrollFn = (cmd: string) => targetSelector
				? `(function(){var sel='${targetSelector}';var e=null;document.querySelectorAll(sel).forEach(function(el){if(!e&&el.scrollHeight-el.clientHeight>50)e=el});if(!e){var best=null,bh=0;document.querySelectorAll('*').forEach(function(el){var d=el.scrollHeight-el.clientHeight;if(d>50&&el.clientHeight>100&&el.getBoundingClientRect().width<500){if(d>bh){best=el;bh=d}}});e=best}if(e){${cmd}}})()`
				: `(function(){var best=document.scrollingElement||document.documentElement,bw=0;document.querySelectorAll('*').forEach(function(el){var d=el.scrollHeight-el.clientHeight;if(d>50&&el.clientHeight>200){var w=el.getBoundingClientRect().width;if(w>bw){best=el;bw=w}}});var e=best;${cmd}})()`;
			let js: string;
			if (direction === 'top') {
				js = scrollFn('e.scrollTop=0');
			} else if (direction === 'bottom') {
				js = scrollFn('e.scrollTop=e.scrollHeight');
			} else {
				const amount = direction === 'down' ? 600 : -600;
				js = scrollFn(`e.scrollBy(0,${amount})`);
			}
			const tmpScroll = `/tmp/sutando-scroll-${Date.now()}.scpt`;
			writeFileSync(tmpScroll, `tell application "Google Chrome" to tell active tab of front window to execute javascript "${js.replace(/"/g, '\\"')}"`);
			execSync(`osascript ${tmpScroll}`, { timeout: 5_000 });
			try { unlinkSync(tmpScroll); } catch {}
			// Also send keyboard scroll — Chrome may skip visual repaints during
			// Zoom screen share even though JS scrollBy updates scrollY. Keyboard
			// input forces a repaint through the OS input pipeline.
			if (!target) {
				const keyMap: Record<string, string> = { down: 'page down', up: 'page up', top: 'home', bottom: 'end' };
				const key = keyMap[direction];
				if (key) {
					try {
						execSync(`osascript -e 'tell application "Google Chrome" to activate' -e 'delay 0.1' -e 'tell application "System Events" to key code ${direction === 'down' ? '121' : direction === 'up' ? '116' : direction === 'top' ? '115 using command down' : '119 using command down'}'`, { timeout: 3_000 });
					} catch { /* keyboard fallback is best-effort */ }
				}
			}
			console.log(`${ts()} [Scroll] ${direction}`);
			return { status: 'scrolled', direction };
		} catch (err) {
			return { error: `Scroll failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

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

// --- Close current Chrome tab ---

export const closeTabTool: ToolDefinition = {
	name: 'close_tab',
	description:
		'Close the current Chrome tab (the active/frontmost tab). Use for: "close it", "close this tab", "close the tab", "close the page". Note: this is for closing browser tabs, NOT for ending the call (use hang_up for that) and NOT for closing video (use close_video).',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		try {
			execSync(`osascript -e 'tell application "Google Chrome" to tell front window to close active tab'`, { timeout: 5_000 });
			console.log(`${ts()} [CloseTab] closed active tab`);
			return { status: 'closed' };
		} catch (err) {
			return { error: `Failed to close tab: ${err instanceof Error ? err.message : err}` };
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

// --- Screen capture ---

export const captureScreenTool: ToolDefinition = {
	name: 'capture_screen',
	description:
		'Capture a screenshot of the screen. Use for: "take a screenshot", "what\'s on my screen", "look at this". Instant.',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		try {
			const res = await fetch('http://localhost:7845/capture');
			const data = await res.json() as { status: string; path?: string; error?: string };
			if (data.status === 'ok' && data.path) {
				console.log(`${ts()} [Screen] Captured: ${data.path}`);
				return { status: 'captured', path: data.path };
			}
			return { status: 'failed', error: data.error || 'unknown error' };
		} catch {
			return { status: 'failed', error: 'Screen capture server not running' };
		}
	},
};

// --- Type text ---

export const typeTextTool: ToolDefinition = {
	name: 'type_text',
	description:
		'Type text into the currently focused field. Use for: "type hello", "enter my email". Instant.',
	parameters: z.object({
		text: z.string().describe('The text to type'),
	}),
	execution: 'inline',
	async execute(args) {
		const { text } = args as { text: string };
		// Debug: log exactly what arrives from Gemini so we can trace newline issues
		console.log(`${ts()} [TypeText] input: ${JSON.stringify(text).slice(0, 200)} len=${text.length}`);
		// Multi-line or special-char text: use clipboard paste instead of
		// keystroke. AppleScript `keystroke` can't handle newlines, parens,
		// or other chars that break the osascript string. Clipboard approach:
		// save current clipboard → write text → Cmd+V → restore clipboard.
		// Check for actual newlines OR literal backslash-n (Gemini sends the latter)
		const hasNewline = text.includes('\n') || text.includes('\r') || /\\n/.test(text) || text.length > 80;
		console.log(`${ts()} [TypeText] hasNewline=${hasNewline} path=${hasNewline ? 'paste' : 'keystroke'}`);
		if (hasNewline) {
			try {
				let savedClipboard = '';
				try { savedClipboard = execSync('pbpaste', { encoding: 'utf-8', timeout: 2_000 }); } catch {}
				const tmpClip = `/tmp/sutando-typetext-clip-${Date.now()}.txt`;
				// Convert literal \n to actual newlines (Gemini sometimes sends escaped)
				const pasteText = text.replace(/\\n/g, '\n').replace(/\\t/g, '\t')
					.replace(/\\\\n/g, '\n').replace(/\\\\t/g, '\t');
				writeFileSync(tmpClip, pasteText);
				execSync(`pbcopy < ${tmpClip}`, { timeout: 2_000 });
				execSync(`osascript -e 'tell application "System Events" to keystroke "v" using command down'`, { timeout: 5_000 });
				// Brief delay for paste to complete, then restore clipboard
				execSync('sleep 0.3');
				if (savedClipboard) {
					const tmpRestore = `/tmp/sutando-typetext-restore-${Date.now()}.txt`;
					writeFileSync(tmpRestore, savedClipboard);
					execSync(`pbcopy < ${tmpRestore}`, { timeout: 2_000 });
					try { unlinkSync(tmpRestore); } catch {}
				}
				try { unlinkSync(tmpClip); } catch {}
				console.log(`${ts()} [TypeText] pasted (multi-line): ${text.slice(0, 40)}...`);
				return { status: 'typed', text };
			} catch (err) {
				return { error: `Paste failed: ${err instanceof Error ? err.message : err}` };
			}
		}
		// Single-line short text: use keystroke (faster, no clipboard disruption)
		const tmpFile = `/tmp/sutando-typetext-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.scpt`;
		try {
			const safeText = text.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
			writeFileSync(tmpFile, `tell application "System Events" to keystroke "${safeText}"`);
			execSync(`osascript ${tmpFile}`, { timeout: 5_000 });
			console.log(`${ts()} [TypeText] typed: ${text.slice(0, 40)}`);
			return { status: 'typed', text };
		} catch (err) {
			return { error: `Type failed: ${err instanceof Error ? err.message : err}` };
		} finally {
			try { unlinkSync(tmpFile); } catch {}
		}
	},
};

// --- Describe screen (vision) ---

async function describeScreenshot(imagePath: string, previousDescs: string[] = []): Promise<string> {
	const apiKey = process.env.GEMINI_API_KEY;
	if (!apiKey) return 'Vision description unavailable (no GEMINI_API_KEY)';
	try {
		// Fixes CodeQL #27 (js/command-line-injection): use execFileSync argv array instead of shell string
		const safePath = imagePath.replace(/[^a-zA-Z0-9_\-./]/g, '');
		const resized = safePath.endsWith('.png') ? safePath.replace(/\.png$/, '-sm.jpg') : safePath + '-sm.jpg';
		try {
			execFileSync('sips', ['-Z', '800', '-s', 'format', 'jpeg', safePath, '--out', resized], { timeout: 2_000, stdio: 'ignore' });
		} catch { /* use original if resize fails */ }
		const actualPath = existsSync(resized) ? resized : imagePath;
		const mimeType = actualPath.endsWith('.jpg') ? 'image/jpeg' : 'image/png';
		const imageData = readFileSync(actualPath).toString('base64');
		// Issue #189: when continuing a narration, the vision model should build
		// on what was already said instead of re-introducing the page every
		// time. First call: introduce with the heading. Later calls: flow on.
		let prompt: string;
		const guard = 'ONLY describe what you SEE in the image. Do NOT use external knowledge, search the web, or add facts not visible on screen.';
		if (previousDescs.length === 0) {
			prompt = `Describe what is on screen in exactly 1 short sentence (max 20 words). Quote the main heading. This will be spoken aloud. ${guard}`;
		} else {
			const recent = previousDescs.slice(-3).map((d, i) => `${i + 1}. ${d}`).join(' | ');
			prompt = `You are narrating a screen recording aloud. Already spoken: ${recent}. Describe ONLY what is NEW or has changed. Use a natural continuation ("Scrolling down...", "Next...", "Now we see...", "Further down..."). Do NOT restart with "The screen shows/displays" — the viewer already knows what page this is. 1 short sentence, max 20 words. ${guard}`;
		}
		const res = await fetch(
			`https://generativelanguage.googleapis.com/v1beta/models/${VISION_MODEL}:generateContent?key=${apiKey}`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					contents: [{
						parts: [
							{ text: prompt },
							{ inlineData: { mimeType, data: imageData } },
						],
					}],
					generationConfig: { maxOutputTokens: 40 },
				}),
			},
		);
		const data = await res.json() as any;
		if (!data?.candidates?.[0]) {
			const reason = data?.promptFeedback?.blockReason || data?.error?.message || JSON.stringify(data).slice(0, 200);
			console.log(`${new Date().toLocaleTimeString()} [DescribeScreen] API response: ${reason}`);
			return `Could not describe the screen. (${reason})`;
		}
		return data.candidates[0].content?.parts?.[0]?.text ?? 'Could not describe the screen.';
	} catch (err) {
		return `Vision error: ${err instanceof Error ? err.message : err}`;
	}
}

export const describeScreenTool: ToolDefinition = {
	name: 'describe_screen',
	description:
		'Describe what is currently visible on screen WITHOUT scrolling. Captures ALL connected displays by default. Use this to introduce/narrate the current view to the caller. Pass display=2 for secondary only.',
	parameters: z.object({
		display: z.number().optional().describe('Specific display (1=main, 2=secondary). Omit to capture all.'),
	}),
	execution: 'inline',
	async execute(args) {
		if (demoStateRef.value === 'done') return { status: 'done', description: 'Demo complete. Stop narrating. Tell the caller.' };
		try {
			const { display } = (args || {}) as { display?: number };
			const query = display ? `?display=${display}` : '?all=true';
			const captureRes = await fetch(`http://localhost:7845/capture${query}`);
			const captureData = await captureRes.json() as { status: string; path?: string; all_paths?: string[]; error?: string };
			if (captureData.status !== 'ok' || !captureData.path) {
				return { error: `Could not capture screen: ${captureData.error || 'unknown'}` };
			}
			const paths = captureData.all_paths || [captureData.path];
			const descriptions: string[] = [];
			for (let i = 0; i < paths.length; i++) {
				const label = paths.length > 1 ? `Display ${i + 1}: ` : '';
				const desc = await describeScreenshot(paths[i]);
				descriptions.push(label + desc);
			}
			const fullDesc = descriptions.join(' | ');
			if ((demoStateRef.value as string) === 'done') return { status: 'done', description: 'Demo complete. Stop narrating.' };
			console.log(`${ts()} [DescribeScreen] ${fullDesc.slice(0, 120)}...`);
			return { status: 'ok', description: fullDesc, displays: paths.length, instruction: 'YOU MUST speak this description OUT LOUD to the caller NOW before calling any other tool.' };
		} catch (err) {
			return { error: `describe_screen failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// --- Click ---

export const clickTool: ToolDefinition = {
	name: 'click',
	description:
		'Click at a specific screen coordinate. Use with describe_screen to identify where to click. Also supports keyboard shortcuts like "cmd+shift+5".',
	parameters: z.object({
		x: z.number().optional().describe('X coordinate on screen'),
		y: z.number().optional().describe('Y coordinate on screen'),
		shortcut: z.string().optional().describe('Keyboard shortcut to press instead of clicking (e.g. "cmd+shift+5")'),
	}),
	execution: 'inline',
	async execute(args) {
		const { x, y, shortcut } = args as { x?: number; y?: number; shortcut?: string };
		try {
			if (shortcut) {
				// Parse shortcut like "cmd+shift+5"
				const parts = shortcut.toLowerCase().split('+');
				const key = parts.pop()!;
				const modifiers = parts.map(m => {
					if (m === 'cmd' || m === 'command') return 'command down';
					if (m === 'shift') return 'shift down';
					if (m === 'ctrl' || m === 'control') return 'control down';
					if (m === 'alt' || m === 'option') return 'option down';
					return '';
				}).filter(Boolean).join(', ');
				const keyCode = key.length === 1 ? `"${key}"` : `${key}`;
				const cmd = modifiers
					? `tell application "System Events" to keystroke ${keyCode} using {${modifiers}}`
					: `tell application "System Events" to keystroke ${keyCode}`;
				execSync(`osascript -e '${cmd}'`, { timeout: 5_000 });
				console.log(`${ts()} [Click] shortcut: ${shortcut}`);
				return { status: 'pressed', shortcut };
			}
			if (x != null && y != null) {
				execSync(`osascript -e '
					tell application "System Events"
						click at {${Math.round(x)}, ${Math.round(y)}}
					end tell'`, { timeout: 5_000 });
				console.log(`${ts()} [Click] at (${x}, ${y})`);
				return { status: 'clicked', x, y };
			}
			return { error: 'Provide either x,y coordinates or a shortcut' };
		} catch (err) {
			return { error: `click failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// Re-export recording/video tools from recording-tools
export {
	scrollAndDescribeTool,
	openVideoTool,
	playVideoTool,
	resumeVideoTool,
	replayVideoTool,
	pauseVideoTool,
	closeVideoTool,
	screenRecordTool,
	scrollDown,
	resetDemoState,
	recordingState,
	stopActiveRecording,
	isRecordingActive,
	isRecordingMuted,
	setupRecordingHooks,
	onReconnect,
	onCallEnd,
	startRecordingNarration,
} from './recording-tools.js';
