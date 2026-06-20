/**
 * Browser & screen tools — Chrome tab control, scrolling, screenshots, and vision descriptions.
 * Split from inline-tools.ts for readability.
 */

import { execSync, execFileSync } from 'node:child_process';
import { writeFileSync, unlinkSync, readFileSync, existsSync, mkdirSync, renameSync } from 'node:fs';
import { join } from 'node:path';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import { demoStateRef } from './recording-state.js';
import { resolveWorkspace } from './workspace_default.js';

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
const VISION_MODEL = process.env.VISION_MODEL || 'gemini-3.1-flash-lite';

// --- Scroll ---

export const scrollTool: ToolDefinition = {
	name: 'scroll',
	description:
		'Scroll the currently focused application. Works in Chrome, VS Code, or any app. Use for: "scroll down", "scroll up", "scroll to top", "scroll to bottom". Pass amount=small for "scroll a little" / "a bit", large for "scroll a lot". Use target for specific areas in Chrome: "sidebar", "chat history", "code block".',
	parameters: z.object({
		direction: z.enum(['down', 'up', 'top', 'bottom']).describe('Scroll direction. Use "top" or "bottom" to jump to start/end of page.'),
		amount: z.enum(['small', 'medium', 'large']).optional().describe('How far to scroll for down/up. small=150px, medium=400px (default), large=800px. Ignored for top/bottom.'),
		target: z.string().optional().describe('Optional: which area to scroll in Chrome. E.g. "sidebar", "chat history", "nav", "code". Omit for main content.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { direction, amount, target: _rawTarget } = args as { direction: 'down' | 'up' | 'top' | 'bottom'; amount?: 'small' | 'medium' | 'large'; target?: string };
		// "window"/"page"/"main" etc. mean the MAIN page, not a CSS selector. The model
		// habitually passes target:"window" (2026-06-09 live test): the selector branch
		// matched nothing, its <500px-wide fallback skipped GitHub's full-width scroller,
		// and every scroll silently no-oped while still reporting success. Normalize
		// main-ish targets to "no target" so they take the robust multi-candidate path.
		const target = _rawTarget && /^(window|page|main|screen|browser|content|body|document|whole page|the page)$/i.test(_rawTarget.trim())
			? undefined : _rawTarget;
		const px = amount === 'small' ? 150 : amount === 'large' ? 800 : 400;
		try {
			// Check which app is frontmost
			let frontApp = '';
			try {
				frontApp = execSync(`osascript -e 'tell application "System Events" to get name of first application process whose frontmost is true'`, { timeout: 2_000 }).toString().trim();
			} catch {}
			const isChrome = frontApp === 'Google Chrome';
			console.log(`${ts()} [Scroll] frontApp=${frontApp} direction=${direction} isChrome=${isChrome}`);

			let _scrollMoved: boolean | null = null;  // null = couldn't determine (no AppleEvent result)
			if (isChrome && !target) {
				// Chrome: try EACH scrollable element (widest first) until one ACTUALLY
				// MOVES, then fall back to window scroll. The old code scrolled only the
				// single widest element — if that element was already maxed (after a prior
				// scroll, or a short sub-container), nothing moved yet the tool still
				// reported success (Susan 2026-06-09: "first scroll worked, second didn't —
				// I wanted it to keep scrolling to my comment"). The JS RETURNS whether
				// anything moved, so a real no-op is visible (logged + in the result) and
				// the keyboard fallback below still fires for screen-share repaint.
				const op = direction === 'top' ? 'e.scrollTop=0'
					: direction === 'bottom' ? 'e.scrollTop=e.scrollHeight'
					: `e.scrollBy(0,${direction === 'down' ? px : -px})`;
				const winOp = direction === 'top' ? 'window.scrollTo(0,0)'
					: direction === 'bottom' ? 'window.scrollTo(0,document.body.scrollHeight)'
					: `window.scrollBy(0,${direction === 'down' ? px : -px})`;
				const js = `(function(){`
					+ `var list=[];document.querySelectorAll('*').forEach(function(el){var d=el.scrollHeight-el.clientHeight;if(d>50&&el.clientHeight>200)list.push(el)});`
					+ `list.sort(function(a,b){return b.getBoundingClientRect().width-a.getBoundingClientRect().width});`
					+ `var se=document.scrollingElement||document.documentElement;if(list.indexOf(se)<0)list.push(se);`
					+ `var moved=null,info=null;for(var i=0;i<list.length;i++){var e=list[i];var b4=e.scrollTop;${op};if(e.scrollTop!==b4){moved=e;info={tag:e.tagName,cls:(e.className&&e.className.toString?e.className.toString().slice(0,40):''),b4:b4,aft:e.scrollTop};break}}`
					+ `var wy0=window.scrollY;if(!moved){${winOp}}var wy1=window.scrollY;`
					+ `return JSON.stringify({moved:!!moved||wy1!==wy0,via:moved?'el':'window',cands:list.length,info:info,wy0:wy0,wy1:wy1});})()`;
				const tmpScroll = `/tmp/sutando-scroll-${Date.now()}.scpt`;
				writeFileSync(tmpScroll, `tell application "Google Chrome" to tell active tab of front window to execute javascript "${js.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`);
				let _out = '';
				try { _out = execSync(`osascript ${tmpScroll}`, { timeout: 5_000 }).toString().trim(); }
				catch (e) { console.log(`${ts()} [Scroll] osascript error: ${e instanceof Error ? e.message : e}`); }
				try { unlinkSync(tmpScroll); } catch {}
				if (_out) {
					try {
						const d = JSON.parse(_out);
						_scrollMoved = !!d.moved;
						const inf = d.info ? `${d.info.tag}.${d.info.cls} ${d.info.b4}->${d.info.aft}` : `window ${d.wy0}->${d.wy1}`;
						console.log(`${ts()} [Scroll] moved=${d.moved} via=${d.via} cands=${d.cands} ${inf}`);
						if (!d.moved) console.log(`${ts()} [Scroll] *** NO MOVEMENT — ${d.cands} scrollable(s), none moved + window at ${direction} limit`);
					} catch { console.log(`${ts()} [Scroll] unparseable out: ${_out.slice(0, 80)}`); }
				} else { console.log(`${ts()} [Scroll] osascript returned EMPTY (AppleEvent denied/dropped or no front Chrome window)`); }
			} else if (isChrome && target) {
				// Chrome with target selector. Same observability contract as the
				// no-target path (2026-06-09): report whether anything ACTUALLY moved —
				// this branch used to scroll-or-not in total silence and always claim
				// success, which made "scroll is broken" undiagnosable from the log.
				const targetSelector = target.match(/side|nav|history|menu/i) ? 'nav' : target.match(/code/i) ? 'pre,code' : target;
				const scrollFn = (cmd: string) =>
					`(function(){var sel='${targetSelector}';var e=null;try{document.querySelectorAll(sel).forEach(function(el){if(!e&&el.scrollHeight-el.clientHeight>50)e=el})}catch(_){}if(!e){var best=null,bh=0;document.querySelectorAll('*').forEach(function(el){var d=el.scrollHeight-el.clientHeight;if(d>50&&el.clientHeight>100&&el.getBoundingClientRect().width<500){if(d>bh){best=el;bh=d}}});e=best}if(!e)return JSON.stringify({found:false,moved:false});var b4=e.scrollTop;${cmd};return JSON.stringify({found:true,moved:e.scrollTop!==b4,b4:b4,aft:e.scrollTop});})()`;
				let js: string;
				if (direction === 'top') js = scrollFn('e.scrollTop=0');
				else if (direction === 'bottom') js = scrollFn('e.scrollTop=e.scrollHeight');
				else js = scrollFn(`e.scrollBy(0,${direction === 'down' ? px : -px})`);
				const tmpScroll = `/tmp/sutando-scroll-${Date.now()}.scpt`;
				writeFileSync(tmpScroll, `tell application "Google Chrome" to tell active tab of front window to execute javascript "${js.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`);
				let _out = '';
				try { _out = execSync(`osascript ${tmpScroll}`, { timeout: 5_000 }).toString().trim(); }
				catch (e) { console.log(`${ts()} [Scroll] osascript error (target branch): ${e instanceof Error ? e.message : e}`); }
				try { unlinkSync(tmpScroll); } catch {}
				try {
					const d = JSON.parse(_out);
					_scrollMoved = !!d.moved;
					console.log(`${ts()} [Scroll] target="${target}" sel="${targetSelector}" found=${d.found} moved=${d.moved} ${d.found ? `${d.b4}->${d.aft}` : ''}`);
				} catch { console.log(`${ts()} [Scroll] target="${target}" out unparseable/empty: ${_out.slice(0, 80)}`); }
			}

			// Keyboard scroll on the frontmost app (works in any app, no focus steal)
			const keyCode = direction === 'down' ? '121' : direction === 'up' ? '116' : direction === 'top' ? '115 using command down' : '119 using command down';
			try {
				execSync(`osascript -e 'tell application "System Events" to key code ${keyCode}'`, { timeout: 3_000 });
			} catch { /* keyboard fallback is best-effort */ }

			console.log(`${ts()} [Scroll] ${direction} (app: ${frontApp})`);
			// Honest result: if the JS pass found nothing moved (page already at the
			// direction's limit), say so instead of falsely claiming a scroll — so the
			// model can tell the user "looks like we're at the bottom" rather than insist.
			if (_scrollMoved === false) {
				return { status: 'at_limit', direction, app: frontApp, moved: false,
				         message: `Nothing scrolled — the page appears to be at the ${direction === 'down' ? 'bottom' : direction === 'up' ? 'top' : direction}. Tell the user it can't scroll further that way.` };
			}
			return { status: 'scrolled', direction, app: frontApp, moved: _scrollMoved };
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
		'Switch to a Chrome tab by keyword. Searches both tab titles and URLs. Use for: "switch to GitHub", "go to Gmail", "open the calendar tab". ' +
		'NOT for "share screen" / "screen share" / "show my screen" — those are screen-share workflows, route to share_screen instead.',
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

// Strip query string for log lines so signed/OAuth URLs don't leak token
// query-params to the logfile verbatim. The error-return path keeps the full
// URL so callers/users still see exactly what they tried to open. Per
// Susan's PR #919 review: regex strip is safer than `new URL()`-based
// redaction since the failure path is often an unparseable URL.
const redactQuery = (u: string): string => JSON.stringify(u.replace(/\?.*$/, '?…'));

export const openUrlTool: ToolDefinition = {
	name: 'open_url',
	description:
		'Open a URL in Chrome. Reuses the active tab when the target shares origin (scheme + host + port) with what\'s already in the active tab; spawns a new tab only for cross-origin URLs. Use for: "open github.com", "go to that link".',
	parameters: z.object({
		url: z.string().describe('The URL to open'),
	}),
	execution: 'inline',
	async execute(args) {
		const { url: rawUrl } = args as { url: string };
		// Normalize spoken-URL artifacts before handing to osascript. The LLM
		// sometimes passes a URL with surrounding whitespace from voice
		// transcription, or with embedded spaces that AppleScript / Chrome
		// reject as "Invalid URL entered. (5)" — opaque error. Trim and
		// reject-up-front so the caller gets a clear diagnostic + the
		// arg appears in the log, instead of three silent osascript errors.
		const url = (rawUrl || '').trim();
		if (!url) {
			console.log(`${ts()} [OpenURL] rejected empty url`);
			return { error: 'Failed to open: empty URL' };
		}
		// `\s` already covers U+00A0 nbsp + U+3000 ideographic space + U+2028/2029
		// + U+FEFF BOM. The uncaught class is zero-width characters
		// (U+200B/200C/200D/2060) — none are in `\s` and none are stripped by
		// `.trim()`. An LLM/voice transcription emitting one would slip
		// through and fail opaquely at osascript again. Reject both up front
		// for symmetric observable errors. Per Susan's PR #919 review.
		if (/\s/.test(url)) {
			console.log(`${ts()} [OpenURL] rejected url with whitespace: ${redactQuery(url)}`);
			return { error: `Failed to open: URL contains whitespace (got ${JSON.stringify(url)})` };
		}
		if (/[\u200B\u200C\u200D\u2060]/.test(url)) {
			console.log(`${ts()} [OpenURL] rejected url with zero-width char: ${redactQuery(url)}`);
			return { error: `Failed to open: URL contains zero-width character (got ${JSON.stringify(url)})` };
		}
		// Escape backslashes first, then quotes — prevents shell injection via osascript
		const safeUrl = url.replace(/\\/g, '\\\\').replace(/'/g, "'\\''").replace(/"/g, '\\"');
		// Parse target origin (scheme + host + port). If unparseable, fall back to new-tab behavior.
		let targetOrigin = '';
		try { targetOrigin = new URL(url).origin; } catch { /* not a real URL, e.g. "about:blank" — let Chrome handle */ }
		try {
			// Query active-tab URL to decide reuse vs new-tab. If origin matches, set URL on active
			// tab; otherwise open a new tab. Falls back to new-tab on any error so callers never
			// silently fail to open the URL.
			let reused = false;
			if (targetOrigin) {
				try {
					const activeUrl = execSync(`osascript -e 'tell application "Google Chrome" to get URL of active tab of front window'`, { timeout: 3_000 }).toString().trim();
					if (activeUrl) {
						let activeOrigin = '';
						try { activeOrigin = new URL(activeUrl).origin; } catch {}
						if (activeOrigin && activeOrigin === targetOrigin) {
							execSync(`osascript -e 'tell application "Google Chrome" to set URL of active tab of front window to "${safeUrl}"'`, { timeout: 5_000 });
							reused = true;
						}
					}
				} catch { /* fall through to new-tab */ }
			}
			if (!reused) {
				execSync(`osascript -e 'tell application "Google Chrome" to tell front window to make new tab with properties {URL:"${safeUrl}"}'`, { timeout: 5_000 });
			}
			console.log(`${ts()} [OpenURL] ${reused ? 'reused active tab' : 'opened new tab'}: ${url}`);
			return { status: reused ? 'reused' : 'opened', url };
		} catch (err) {
			// Log the URL too — the prior version returned the URL only in the
			// error string, which voice-agent's stdout strips by the time it
			// reaches the log, leaving "Invalid URL entered. (5)" with no
			// hint of what URL voice actually passed. 2026-05-19 incident:
			// three back-to-back open_url failures with no observable arg.
			console.log(`${ts()} [OpenURL] FAILED url=${redactQuery(url)} err=${err instanceof Error ? err.message : err}`);
			return { error: `Failed to open ${url}: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// --- Screen capture ---

// captureScreenTool moved — canonical version lives in inline-tools.ts.
// typeTextTool moved — canonical version lives in inline-tools.ts.

// --- Describe screen (vision) ---

async function describeScreenshot(imagePath: string, previousDescs: string[] = []): Promise<string> {
	// Prefer free-tier voice key (gemini-3.1-flash-lite-preview is free-tier eligible on REST
	// generateContent — verified 2026-05-14). Falls back to paid GEMINI_API_KEY if voice key absent.
	const apiKey = process.env.GEMINI_VOICE_API_KEY || process.env.GEMINI_API_KEY;
	if (!apiKey) return 'Vision description unavailable (no GEMINI_VOICE_API_KEY or GEMINI_API_KEY)';
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
				execFileSync('osascript', ['-e', cmd], { timeout: 5_000 });
				console.log(`${ts()} [Click] shortcut: ${shortcut}`);
				return { status: 'pressed', shortcut };
			}
			if (x != null && y != null) {
				execFileSync('osascript', ['-e', `tell application "System Events"\n\tclick at {${Math.round(x)}, ${Math.round(y)}}\nend tell`], { timeout: 5_000 });
				console.log(`${ts()} [Click] at (${x}, ${y})`);
				return { status: 'clicked', x, y };
			}
			return { error: 'Provide either x,y coordinates or a shortcut' };
		} catch (err) {
			return { error: `click failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// --- Point at (Pointer Teacher) ---

// gemini-3-flash-preview with Gemini's NATIVE point format and thinking
// disabled was proven by the Pointer Teacher grill POCs to land 1–3 px on real
// UI targets in VSCode. On the same screenshot, gemini-3.1-flash-lite was
// 108 px off and Clicky's raw-pixel `[POINT:x,y]` prompt was 23–69 px off.
// See docs/adr/0001-pointer-teacher-brain.md. Do NOT swap the model or the
// prompt format without re-running that POC — both choices are load-bearing.
const POINTER_MODEL = process.env.POINTER_MODEL || 'gemini-3-flash-preview';
// IPC: Sutando.app watches <workspace>/state via DispatchSource and flies the
// bezier pointer to whatever lands here. Go through resolveWorkspace() so we
// agree with the Swift side's `AppDelegate.workspace` (added in #837) — not
// process.cwd(), which silently bifurcates when SUTANDO_WORKSPACE points
// anywhere other than the launch CWD (closes #934).
const POINTER_STATE_DIR = join(resolveWorkspace(), 'state');
const POINTER_CMD_PATH = join(POINTER_STATE_DIR, 'pointer-cmd.json');

// Atomic publish to the pointer IPC file. The temp name is unique per call
// (pid + ms + random) — a fixed per-process name lets two overlapping point_at
// calls clobber each other's write/rename (Codex review, high). The rename is
// atomic and the Swift side's monotonic `ts` guard decides which command wins,
// so no lock is needed.
function publishPointerCmd(cmd: Record<string, unknown>): void {
	mkdirSync(POINTER_STATE_DIR, { recursive: true });
	const tmpCmd = `${POINTER_CMD_PATH}.${process.pid}.${Date.now()}.${Math.random().toString(36).slice(2, 9)}.tmp`;
	writeFileSync(tmpCmd, JSON.stringify(cmd));
	renameSync(tmpCmd, POINTER_CMD_PATH);
}

export const pointAtTool: ToolDefinition = {
	name: 'point_at',
	description:
		'Physically point at something on the user\'s screen — flies an on-screen marker to it and gives you what to say. This is the embodied "show me where" / teaching gesture: use it whenever the user asks where something is or how to do something in the app in front of them ("where do I commit?", "show me the search", "point at the deploy button", "teach me — where do I start?"). Captures the main display, locates the target, and animates a pointer there. After it returns you MUST speak the returned `say` sentence aloud.',
	parameters: z.object({
		query: z.string().describe('What to point at, in plain words — a control, icon, menu, or region. E.g. "the commit button", "the Source Control icon", "where do I run the app".'),
	}),
	execution: 'inline',
	async execute(args) {
		const { query } = args as { query: string };
		// Free-tier eligible voice key preferred (the POC proved gemini-3-flash-preview
		// works on it); falls back to the paid key. Same precedence as describe_screen.
		const apiKey = process.env.GEMINI_VOICE_API_KEY || process.env.GEMINI_API_KEY;
		if (!apiKey) return { error: 'point_at unavailable (no GEMINI_VOICE_API_KEY or GEMINI_API_KEY)' };
		if (!query?.trim()) return { error: 'point_at needs a query (what to point at)' };
		try {
			// 0. Clear any overlay still on screen from a previous point_at
			// before screenshotting. The :7845 server shells out to
			// `screencapture`, which grabs the raw framebuffer and ignores
			// NSWindow.sharingType — so the only way to keep a stale pointer
			// out of the shot (and out of the model's input, which would bias
			// the next target) is to tell the Swift overlay to hide, then give
			// the dir-watcher → main-thread orderOut a moment to land before we
			// capture (Codex review, high). ~250ms is negligible against the
			// ~8s capture + ~60s model budget below.
			publishPointerCmd({ hide: true, ts: Date.now() / 1000 });
			await new Promise(r => setTimeout(r, 250));
			// 1. capture the main display (single-display scope guard) via :7845.
			// Timeout-bounded — point_at is on the sub-second inline lane and must
			// never hang it if the capture server is wedged.
			const capRes = await fetch('http://localhost:7845/capture?display=1', { signal: AbortSignal.timeout(8_000) });
			if (!capRes.ok) return { error: `point_at capture HTTP ${capRes.status}` };
			const cap = await capRes.json() as { status: string; path?: string; error?: string };
			if (cap.status !== 'ok' || !cap.path) return { error: `point_at capture failed: ${cap.error || 'unknown'}` };
			// Downscale; sips -Z preserves aspect, so 0–1 normalized coords map
			// straight onto the display with no extra transform (open item #2).
			// Per-invocation temp path + success flag so a failed sips can never
			// feed a stale screenshot from a previous call into the model.
			const small = `/tmp/pointer-shot-${process.pid}-${Date.now()}.jpg`;
			let resized = false;
			try {
				execFileSync('sips', ['-s', 'format', 'jpeg', '-Z', '1568', cap.path, '--out', small], { timeout: 4_000, stdio: 'ignore' });
				resized = existsSync(small);
			} catch { /* fall back to the full-size capture below */ }
			const imgPath = resized ? small : cap.path;
			const imageData = readFileSync(imgPath).toString('base64');
			if (resized) { try { unlinkSync(small); } catch { /* best-effort cleanup */ } }

			// 2. resolve the Target via Gemini's native point format (thinking off)
			const prompt =
				`Point to ${query}. Return ONLY minified JSON, no prose, no code fence: ` +
				`{"point":[y,x],"label":"<3 words>","say":"<one friendly spoken sentence ` +
				`(max 20 words) telling me where it is and what to do>"}. ` +
				`point is [y, x] normalized to 0-1000 over the whole image.`;
			const res = await fetch(
				`https://generativelanguage.googleapis.com/v1beta/models/${POINTER_MODEL}:generateContent?key=${apiKey}`,
				{
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({
						contents: [{ parts: [{ text: prompt }, { inlineData: { mimeType: 'image/jpeg', data: imageData } }] }],
						// thinkingBudget:0 is required — gemini-3-flash-preview is a
						// thinking model and burns the token budget reasoning instead
						// of answering, truncating the point. Proven in the POC.
						generationConfig: { temperature: 0, maxOutputTokens: 1200, thinkingConfig: { thinkingBudget: 0 } },
					}),
					signal: AbortSignal.timeout(60_000),
				},
			);
			const data = await res.json().catch(() => null) as any;
			if (!res.ok) {
				return { error: `point_at model HTTP ${res.status}: ${data?.error?.message || JSON.stringify(data)?.slice(0, 160) || 'no body'}` };
			}
			if (!data?.candidates?.[0]) {
				const reason = data?.promptFeedback?.blockReason || data?.error?.message || JSON.stringify(data).slice(0, 160);
				return { error: `point_at model error: ${reason}` };
			}
			const txt = (data.candidates[0].content?.parts?.[0]?.text ?? '').trim();
			const cleaned = txt.replace(/^```(?:json)?|```$/gm, '').trim();
			let y: number, x: number, label = query, say = '';
			try {
				const obj = JSON.parse(cleaned);
				y = Number(obj.point[0]); x = Number(obj.point[1]);
				label = obj.label || query; say = obj.say || '';
			} catch {
				// Fallback: pull the first [y, x] pair out of a noisier reply.
				const m = cleaned.match(/\[\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\]/);
				if (!m) return { error: `point_at unparseable reply: ${txt.slice(0, 160)}` };
				y = Number(m[1]); x = Number(m[2]);
			}
			// Reject garbage before it reaches the overlay — a NaN/out-of-range
			// point would otherwise fly the marker off-screen or be silently
			// dropped by the Swift side while we still report success.
			if (![x, y].every(n => Number.isFinite(n) && n >= 0 && n <= 1000)) {
				return { error: `point_at got an out-of-range point [${y}, ${x}] for "${query}"` };
			}

			// 3. hand the Target to the Swift overlay (runs in the real GUI session).
			// Atomic publish via the shared helper (unique sibling temp →
			// rename), so the dir-watcher never reads a half-written JSON and
			// concurrent calls can't collide.
			const cmd = {
				nx: Math.round((x / 1000) * 1e5) / 1e5,
				ny: Math.round((y / 1000) * 1e5) / 1e5,
				label, say, ts: Date.now() / 1000,
			};
			publishPointerCmd(cmd);
			console.log(`${ts()} [PointAt] "${query}" -> nx=${cmd.nx} ny=${cmd.ny} label="${label}"`);
			return {
				status: 'pointing',
				label,
				say,
				instruction: 'A pointer is now flying to the target on the user\'s screen. Speak the `say` sentence aloud to the user NOW, then keep teaching.',
			};
		} catch (err) {
			return { error: `point_at failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// Re-export recording/video tools from recording-tools
export {
	scrollAndDescribeTool,
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

