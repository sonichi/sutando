/**
 * Browser & screen tools — Chrome tab control, scrolling, screenshots, and vision descriptions.
 * Split from inline-tools.ts for readability.
 */

import { execSync, execFileSync } from 'node:child_process';
import { writeFileSync, unlinkSync, readFileSync, readlinkSync, existsSync, statSync, symlinkSync } from 'node:fs';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';

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
		if (demoState === 'done') return { status: 'done', description: 'Demo complete. Stop narrating. Tell the caller.' };
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
			if ((demoState as string) === 'done') return { status: 'done', description: 'Demo complete. Stop narrating.' };
			console.log(`${ts()} [DescribeScreen] ${fullDesc.slice(0, 120)}...`);
			return { status: 'ok', description: fullDesc, displays: paths.length, instruction: 'YOU MUST speak this description OUT LOUD to the caller NOW before calling any other tool.' };
		} catch (err) {
			return { error: `describe_screen failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// --- Scroll and Describe (concurrent) ---

async function captureScreen(): Promise<string | null> {
	try {
		const res = await fetch('http://localhost:7845/capture');
		const data = await res.json() as { status: string; path?: string };
		return data.status === 'ok' && data.path ? data.path : null;
	} catch { return null; }
}

function scrollDown(pixels: number = 600) {
	// Use widest-element heuristic (same as scrollTool) so embedded/nested scrollable containers work
	// NO keyboard fallback here — Chrome activate + Page Down disrupts narration audio capture
	// during recording (breaks subtitle generation). Interactive scrollTool has the keyboard
	// fallback for the Zoom screen share case; recording uses JS-only.
	const js = `(function(){var best=document.scrollingElement||document.documentElement,bw=0;document.querySelectorAll('*').forEach(function(el){var d=el.scrollHeight-el.clientHeight;if(d>50&&el.clientHeight>200){var w=el.getBoundingClientRect().width;if(w>bw){best=el;bw=w}}});best.scrollBy(0,${pixels})})()`;
	const tmpScroll = `/tmp/sutando-scroll-rec-${Date.now()}.scpt`;
	writeFileSync(tmpScroll, `tell application "Google Chrome" to tell active tab of front window to execute javascript "${js.replace(/"/g, '\\"')}"`);
	execSync(`osascript ${tmpScroll}`, { timeout: 5_000 });
	try { unlinkSync(tmpScroll); } catch {}
}

let demoState: 'idle' | 'recording' | 'done' = 'idle';

/** Reset recording state — call when a new phone call starts or previous recording is stuck */
export function resetDemoState(): void {
	if (demoState !== 'idle') {
		console.log(`${ts()} [DemoState] Reset from '${demoState}' → 'idle'`);
		demoState = 'idle';
	}
}

export const scrollAndDescribeTool: ToolDefinition = {
	name: 'scroll_and_describe',
	description:
		'Record a demo video with narration. Call ONCE with duration_seconds. It starts recording, auto-scrolls, and returns a first description. ' +
		'Do NOT announce "starting recording" — SPEAK the returned description as your first words. ' +
		'New descriptions will be pushed as the page scrolls — speak each one. NEVER repeat earlier narration. ' +
		'Recording auto-stops. Do NOT call this more than once per recording.',
	parameters: z.object({
		duration_seconds: z.number().optional().describe('Target duration in seconds (default 15, max 60). ALWAYS seconds, never minutes.'),
	}),
	execution: 'inline',
	async execute(args) {
		const MAX_DURATION = 60;
		const rawDuration = (args as { duration_seconds?: number }).duration_seconds ?? 15;
		const duration_seconds = Math.min(rawDuration, MAX_DURATION);
		if (rawDuration > MAX_DURATION) console.log(`${ts()} [ScrollAndDescribe] capped duration from ${rawDuration}s to ${MAX_DURATION}s`);
		try {
			// Prevent duplicate recordings
			if (demoState === 'recording') return { status: 'already_recording', message: 'Already recording.' };
			// Reset from previous recording — allow new one
			if (demoState === 'done') demoState = 'idle';
			demoState = 'recording';

			// Scroll to top
			execSync(`osascript -e 'tell application "System Events" to key code 126 using command down'`, { timeout: 5_000 });

			// Start recording + first describe_screen in parallel
			try { unlinkSync(LIVE_TRANSCRIPT_SRT_PATH); } catch {}
			execSync('python3 skills/screen-record/scripts/record.py start', { timeout: 10_000 });
			const captureRes = await fetch('http://localhost:7845/capture');
			const captureData = await captureRes.json() as { status: string; path?: string };
			const firstDesc = captureData.path ? await describeScreenshot(captureData.path) : '';
			// Set subtitle baseline — pick whichever transcript was updated more recently.
			// Voice agent writes to -voice.txt; phone conversation-server writes to -CA{sid}.txt via symlink.
			const voiceTranscript = '/tmp/sutando-live-transcript-voice.txt';
			let phoneTranscript = '';
			try { phoneTranscript = readlinkSync(LIVE_TRANSCRIPT_SYMLINK); } catch {}
			if (existsSync(voiceTranscript) && phoneTranscript && existsSync(phoneTranscript)) {
				// Both exist — use whichever was modified more recently
				const vMtime = statSync(voiceTranscript).mtimeMs;
				const pMtime = statSync(phoneTranscript).mtimeMs;
				liveTranscriptResolvedPath = pMtime > vMtime ? phoneTranscript : voiceTranscript;
			} else {
				liveTranscriptResolvedPath = (phoneTranscript && existsSync(phoneTranscript)) ? phoneTranscript : (existsSync(voiceTranscript) ? voiceTranscript : '');
			}
			liveTranscriptRecordingStart = Date.now();
			liveTranscriptBaselineLines = countTranscriptLines();

			// Adaptive scroll speed: one pass top-to-bottom over the full duration
			let pageHeight = 5000; // fallback
			try {
				pageHeight = parseInt(execSync(`osascript -e 'tell application "Google Chrome" to tell active tab of front window to execute javascript "document.body.scrollHeight - window.innerHeight"'`, { timeout: 3_000 }).toString().trim()) || 5000;
			} catch {}
			const SCROLL_INTERVAL_MS = 2500;
			const totalScrollSteps = (duration_seconds * 1000) / SCROLL_INTERVAL_MS;
			const pxPerStep = Math.ceil(pageHeight / totalScrollSteps);
			// Write scroll info for conversation-server to calculate description timing
			const viewportHeight = 900; // approximate
			const msPerViewport = Math.round((viewportHeight / pxPerStep) * SCROLL_INTERVAL_MS);
			writeFileSync('/tmp/sutando-scroll-info.json', JSON.stringify({ pageHeight, pxPerStep, msPerViewport, duration_seconds }));
			console.log(`${ts()} [ScrollAndDescribe] page=${pageHeight}px, ${totalScrollSteps} steps, ${pxPerStep}px/step, ${msPerViewport}ms/viewport`);
			let scrolledTotal = 0;
			const scrollInterval = setInterval(() => {
				if (scrolledTotal >= pageHeight) return; // stop at bottom
				try { scrollDown(pxPerStep); } catch (e) { console.error(`${ts()} [ScrollAndDescribe] scroll failed:`, e); }
				scrolledTotal += pxPerStep;
			}, SCROLL_INTERVAL_MS);

			// Auto-stop after duration — wait for narration-tee mux, then burn subtitles.
			// Capture start time: if user starts a 2nd recording before this timer fires,
			// liveTranscriptRecordingStart will be overwritten. Only clear if still ours.
			const myRecStart = liveTranscriptRecordingStart;
			setTimeout(async () => {
				clearInterval(scrollInterval);
				let stopResult: any = {};
				try {
					const raw = execSync('python3 skills/screen-record/scripts/record.py stop', { timeout: 10_000 }).toString().trim();
					stopResult = JSON.parse(raw);
				} catch {}
				// Explicitly flush narration-tee (it normally triggers on next audio chunk,
				// but after recording stops Gemini may not send audio for seconds).
				try {
					const { cleanup: flushNarrationTee } = await import('../skills/screen-record/scripts/narration-tee.js');
					flushNarrationTee();
				} catch {}
				// Wait for narrated.mov to exist (narration-tee mux ~2-6s after flush)
				const narrated = stopResult.path ? stopResult.path.replace('.mov', '-narrated.mov') : '';
				for (let w = 0; w < 8; w++) {
					if (narrated && isReadableFile(narrated)) break;
					await new Promise(r => setTimeout(r, 2000));
				}
				// Burn live transcript subtitles on narrated version only
				if (liveTranscriptRecordingStart > 0 && narrated && isReadableFile(narrated)) {
					const subtitled = burnLiveTranscriptSubtitles(narrated);
					if (subtitled) console.log(`${ts()} [ScrollAndDescribe] subtitle burned: ${subtitled}`);
					else console.log(`${ts()} [ScrollAndDescribe] subtitle burn failed (no transcript lines or ffmpeg error)`);
				}
				if (liveTranscriptRecordingStart === myRecStart) liveTranscriptRecordingStart = 0;
				demoState = 'done';
				console.log(`${ts()} [ScrollAndDescribe] auto-stop`);
			}, duration_seconds * 1000);

			console.log(`${ts()} [ScrollAndDescribe] recording started with first desc`);
			return {
				status: 'recording',
				first_description: firstDesc,
				message: `SPEAK THIS NOW: "${firstDesc}" — this is your narration. Auto-stops in ${duration_seconds}s.`,
			};
		} catch (err) {
			return { error: `scroll_and_describe failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// --- Recording state + narration controller ---

/** Shared mute state — conversation-server checks this in audio output handler */
export const recordingState = { muted: false };

/** Stop any active screen recording */
export function stopActiveRecording(): void {
	try { execSync('python3 skills/screen-record/scripts/record.py stop', { timeout: 5_000 }); } catch {}
}

/** Check if a recording is currently active */
export function isRecordingActive(): boolean {
	return existsSync('/tmp/sutando-screen-record.pid');
}

/** Check if recording audio should be muted */
export function isRecordingMuted(): boolean {
	return recordingState.muted;
}

/**
 * Set up all recording hooks on a voice session.
 * Call once per session — handles tool triggers, reconnect, and cleanup automatically.
 */
export function setupRecordingHooks(session: any): void {
	// Start narration when scroll_and_describe is called
	session.eventBus?.subscribe?.('tool.call', (e: any) => {
		if (e?.toolName === 'scroll_and_describe') {
			setTimeout(() => {
				if (isRecordingActive()) startRecordingNarration(session);
			}, 4000);
		}
	});
}

/** Called on Gemini reconnect — nudge to continue narrating if recording active */
export function onReconnect(session: any): void {
	if (!isRecordingActive()) return;
	try {
		injectText(session, '[System: You were narrating a screen demo. Continue where you left off — call describe_screen and keep narrating. Do NOT greet or say "I\'m back".]');
	} catch {}
}

/** Called on call end — stop any active recording */
export function onCallEnd(): void {
	stopActiveRecording();
}

/**
 * Start narration controller for an active recording.
 * Called by conversation-server when scroll_and_describe starts.
 * Handles: description pushing, stop detection, mute/unmute, reconnect narration.
 */
let narrationActive = false;

export function startRecordingNarration(session: any): void {
	if (narrationActive) return; // prevent duplicate controllers
	narrationActive = true;

	// Read scroll info for description interval + duration
	let descIntervalMs = 8000;
	let durationMs = 30000;
	try {
		if (existsSync('/tmp/sutando-scroll-info.json')) {
			const info = JSON.parse(readFileSync('/tmp/sutando-scroll-info.json', 'utf8'));
			descIntervalMs = Math.max(Math.round((info.msPerViewport || 8000) * 0.7), 5000);
			durationMs = (info.duration_seconds || 30) * 1000;
			console.log(`${ts()} [Recording] interval: ${descIntervalMs}ms, duration: ${durationMs}ms`);
		}
	} catch {}

	let lastDesc = '';
	const previousDescs: string[] = []; // track all narrated descriptions
	const startTime = Date.now();
	const STOP_PUSHING_BEFORE_END_MS = 8000; // stop pushing 8s before recording ends

	const pushDescription = async () => {
		if (!existsSync('/tmp/sutando-screen-record.pid')) return;
		// Stop pushing near the end so Gemini finishes naturally
		const elapsed = Date.now() - startTime;
		if (elapsed > durationMs - STOP_PUSHING_BEFORE_END_MS) {
			console.log(`${ts()} [Recording] near end — stopped pushing`);
			clearInterval(descTimer);
			try {
				injectText(session, '[System: Recording ending soon. Finish your current sentence and stop.]');
			} catch {}
			return;
		}
		try {
			const path = await captureScreen();
			if (!path) return;
			const desc = await describeScreenshot(path, previousDescs);
			if (!desc || desc === lastDesc) {
				if (desc === lastDesc) console.log(`${ts()} [Recording] skipped duplicate`);
				return;
			}
			lastDesc = desc;
			previousDescs.push(desc);
			if (!existsSync('/tmp/sutando-screen-record.pid')) return;
			const remaining = Math.round((durationMs - (Date.now() - startTime)) / 1000);
			const alreadySaid = previousDescs.slice(0, -1).map((d, i) => `${i + 1}. ${d.slice(0, 40)}`).join('; ');
			injectText(session, `[System: ${remaining}s left. Already narrated: ${alreadySaid || 'nothing yet'}. Now narrate this NEW content only (1 short sentence, no repeats): "${desc}"]`);
			console.log(`${ts()} [Recording] pushed: ${desc.slice(0, 60)}...`);
		} catch (err) {
			console.log(`${ts()} [Recording] push error: ${err}`);
		}
	};

	// Early first push + interval
	setTimeout(pushDescription, 5000);
	const descTimer = setInterval(pushDescription, descIntervalMs);

	// Timer-based stop — uses known duration, no polling
	// Stop pushing 8s before end (already handled in pushDescription)
	// At exactly durationMs: clear timers, send "recording complete"
	setTimeout(() => {
		clearInterval(descTimer);
		narrationActive = false;
		console.log(`${ts()} [Recording] timer fired — sending stop`);
		try {
			injectText(session, '[System: Recording just ended. Say "The recording is complete." immediately.]');
		} catch {}
	}, durationMs + 1000); // +1s buffer for auto-stop to finish
}

// Check file exists AND has meaningful size (>1KB). Prevents returning
// a recording that ffmpeg is still writing or a narrated file mid-mux.
function isReadableFile(path: string): boolean {
	try { return existsSync(path) && statSync(path).size > 1000; } catch { return false; }
}

function findRecording(version?: 'raw' | 'narrated' | 'subtitled'): string | null {
	try {
		const files = execSync('ls -t /tmp/sutando-recording-*.mov 2>/dev/null | grep -v narrated | grep -v subtitled | head -1', { timeout: 3_000 }).toString().trim();
		if (files && isReadableFile(files)) {
			if (version === 'raw') return files;
			const narrated = files.replace('.mov', '-narrated.mov');
			const subtitled = narrated.replace('.mov', '-subtitled.mov');
			if (version === 'subtitled') return isReadableFile(subtitled) ? subtitled : (isReadableFile(narrated) ? narrated : files);
			if (version === 'narrated') return isReadableFile(narrated) ? narrated : files;
			// Default (no version): prefer subtitled > narrated > raw
			if (isReadableFile(subtitled)) return subtitled;
			if (isReadableFile(narrated)) return narrated;
			return files;
		}
	} catch {}
	return null;
}

// Video playback tools — split from a single polymorphic playRecordingTool into 6
// single-purpose tools. Gemini selects more reliably with narrow descriptions than
// with one tool that has an "action" enum. The old tool caused persistent confusion
// between "open" and "play" (42% of calls in diagnostics had wrong action selection).
export const openVideoTool: ToolDefinition = {
	name: 'open_video',
	description:
		'Open the latest screen recording in QuickTime. Finds the best available version (subtitled > narrated > raw). ' +
		'Use when user says "open the video", "open the recording", "can you open it".',
	parameters: z.object({
		path: z.string().optional().describe('File path. Omit for latest recording.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { path: filePath } = args as { path?: string };
		console.log(`${ts()} [OpenVideo] called`);
		demoState = 'idle';
		try {
			let recPath = filePath ? filePath.replace(/^~/, process.env.HOME || '') : null;
			if (!recPath) recPath = findRecording();
			if (!recPath) { await new Promise(r => setTimeout(r, 3000)); recPath = findRecording(); }
			if (!recPath || !isReadableFile(recPath)) return { error: 'No recording found. Try again in a few seconds.' };
			writeFileSync('/tmp/sutando-playback-path', recPath);
			execSync(`open "${recPath}"`, { timeout: 5_000 });
			try { execSync(`osascript -e 'tell application "QuickTime Player" to activate'`, { timeout: 3_000 }); } catch {}
			const size = statSync(recPath).size;
			console.log(`${ts()} [OpenVideo] opened ${recPath} (${(size / 1024 / 1024).toFixed(1)}MB)`);
			return { status: 'opened', path: recPath, size_mb: +(size / 1024 / 1024).toFixed(1), instruction: 'File opened. When user says play, call play_video.' };
		} catch (err) {
			return { error: `open_video failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

/** Helper: start QuickTime playback + stream audio to phone */
async function startPlayback(seekSec: number = 0): Promise<{ status: string; path?: string; error?: string; instruction?: string }> {
	let recPath: string | null = null;
	try { recPath = readFileSync('/tmp/sutando-playback-path', 'utf8').trim() || null; } catch {}
	if (!recPath) recPath = findRecording();
	if (!recPath) return { status: 'error', error: 'No video to play. Open a video first with open_video.' };
	let alreadyOpen = false;
	try {
		const c = execSync(`osascript -e 'tell application "QuickTime Player" to count of documents'`, { timeout: 2_000 }).toString().trim();
		alreadyOpen = parseInt(c) > 0;
	} catch {}
	if (!alreadyOpen) {
		execSync(`open "${recPath}"`, { timeout: 5_000 });
		for (let i = 0; i < 10; i++) {
			try { const c = execSync(`osascript -e 'tell application "QuickTime Player" to count of documents'`, { timeout: 2_000 }).toString().trim(); if (parseInt(c) > 0) break; } catch {}
			await new Promise(r => setTimeout(r, 300));
		}
	}
	if (seekSec === 0) {
		try { execSync(`osascript -e 'tell application "QuickTime Player"' -e 'set d to document 1' -e 'set current time of d to 0' -e 'end tell'`, { timeout: 3_000 }); } catch {}
	}
	try { unlinkSync('/tmp/sutando-playback-pause'); } catch {}
	fetch(`http://localhost:${process.env.PHONE_PORT || '3100'}/play-audio`, {
		method: 'POST', headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ path: recPath, seekSec }),
	}).catch(() => {});
	await new Promise(r => setTimeout(r, 300));
	try { execSync(`osascript -e 'tell application "QuickTime Player"' -e 'activate' -e 'play document 1' -e 'end tell'`, { timeout: 5_000 }); } catch {}
	return { status: 'playing', path: recPath, instruction: 'Video is playing. Say NOTHING.' };
}

export const playVideoTool: ToolDefinition = {
	name: 'play_video',
	description: 'Play the video from the beginning. Use ONLY when user explicitly says "play" or "play it".',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		console.log(`${ts()} [PlayVideo] called`);
		try { return await startPlayback(0); } catch (err) { return { error: `${err}` }; }
	},
};

export const resumeVideoTool: ToolDefinition = {
	name: 'resume_video',
	description: 'Resume the paused video from where it stopped. Use ONLY when user says "resume", "continue", "go on".',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		console.log(`${ts()} [ResumeVideo] called`);
		try {
			try { unlinkSync('/tmp/sutando-playback-pause'); } catch {}
			try { execSync(`osascript -e 'tell application "QuickTime Player"' -e 'activate' -e 'play document 1' -e 'end tell'`, { timeout: 5_000 }); } catch {}
			return { status: 'playing', instruction: 'Video resumed. Say NOTHING.' };
		} catch (err) { return { error: `${err}` }; }
	},
};

export const replayVideoTool: ToolDefinition = {
	name: 'replay_video',
	description: 'Replay the video from the beginning. Use when user says "start over", "replay", "play again".',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		console.log(`${ts()} [ReplayVideo] called`);
		try { return await startPlayback(0); } catch (err) { return { error: `${err}` }; }
	},
};

// "continue" intentionally NOT in pause_video — it belongs on resume_video.
// Adding it here caused Gemini to pause when user said "continue".
export const pauseVideoTool: ToolDefinition = {
	name: 'pause_video',
	description:
		'Pause the video. Use when user says "pause", "stop", or "hold".',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		console.log(`${ts()} [PauseVideo] called`);
		try { writeFileSync('/tmp/sutando-playback-pause', '1'); } catch {}
		try { execSync(`osascript -e 'tell application "QuickTime Player"' -e 'if (count of documents) > 0 then' -e 'pause document 1' -e 'end if' -e 'end tell'`, { timeout: 5_000 }); } catch {}
		return { status: 'paused', instruction: 'Paused. When user says play/resume, call play_video.' };
	},
};

export const closeVideoTool: ToolDefinition = {
	name: 'close_video',
	description:
		'Close the video player. Use when user says "close the video", "close it".',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		console.log(`${ts()} [CloseVideo] called`);
		try { execSync(`osascript -e 'tell application "QuickTime Player" to quit'`, { timeout: 5_000 }); } catch {}
		try { unlinkSync('/tmp/sutando-playback-pause'); } catch {}
		try { unlinkSync('/tmp/sutando-playback-path'); } catch {}
		return { status: 'closed' };
	},
};

// --- Screen recording ---

let lastScreenRecordCall = 0;
const SCREEN_RECORD_COOLDOWN_MS = 5_000;

// --- Live transcript subtitle tracking ---
// When subtitle=true, captures conversation transcript during recording
// and burns it as SRT into the video when recording stops.
// Symlink points to the active call's transcript (phone or voice agent)
const LIVE_TRANSCRIPT_SYMLINK = '/tmp/sutando-live-transcript.txt';
const LIVE_TRANSCRIPT_SRT_PATH = '/tmp/sutando-live-transcript-subtitle.srt';
let liveTranscriptRecordingStart = 0;
let liveTranscriptBaselineLines = 0;
// Resolved path to the call-specific transcript file, captured at recording start.
// A concurrent call (e.g. Zoom join) can overwrite the symlink, so we resolve it
// once and use the resolved path for the entire recording lifecycle.
let liveTranscriptResolvedPath = '';

function countTranscriptLines(): number {
	try {
		const p = liveTranscriptResolvedPath || LIVE_TRANSCRIPT_SYMLINK;
		if (!existsSync(p)) return 0;
		return readFileSync(p, 'utf8').split('\n').filter(l => l.startsWith('[')).length;
	} catch { return 0; }
}

/** Generate SRT from transcript lines added since recording started, then burn into video. */
function burnLiveTranscriptSubtitles(videoPath: string): string | null {
	if (liveTranscriptRecordingStart === 0) return null;
	try {
		const p = liveTranscriptResolvedPath || LIVE_TRANSCRIPT_SYMLINK;
		if (!existsSync(p)) return null;
		const allLines = readFileSync(p, 'utf8').split('\n').filter(l => l.startsWith('['));
		const newLines = allLines.slice(liveTranscriptBaselineLines);
		if (newLines.length === 0) return null;

		// Convert wall-clock timestamps to relative (from recording start)
		const startWall = (() => {
			const d = new Date(liveTranscriptRecordingStart);
			return (d.getHours() * 3600 + d.getMinutes() * 60 + d.getSeconds()) * 1000;
		})();

		const fmtTime = (ms: number): string => {
			const s = Math.floor(ms / 1000);
			const h = Math.floor(s / 3600);
			const m = Math.floor((s % 3600) / 60);
			const sec = s % 60;
			const millis = ms % 1000;
			return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')},${String(millis).padStart(3, '0')}`;
		};

		const entries: { text: string; timeMs: number }[] = [];
		for (const line of newLines) {
			const match = line.match(/^\[(\d{2}):(\d{2}):(\d{2})\]\s+(.+)$/);
			if (!match) continue;
			const [, hh, mm, ss, content] = match;
			// Exclude caller speech — already audible in the narrated audio track.
			// Subtitles only show Sutando's screen descriptions to avoid redundancy.
			if (content.startsWith('Caller:') || content.startsWith('User:')) continue;
			const text = content.replace(/^Sutando:\s*/, '');
			// Skip short conversational responses — long lines starting with filler are screen descriptions
			if (text.length < 50 && /^(Sure|OK|Okay|Got it|I'll|I can|I'm |The recording|Is there|Hello|Hi |Done|Thanks|Already|Let me|Paused)/i.test(text)) continue;
			const wallMs = (Number(hh) * 3600 + Number(mm) * 60 + Number(ss)) * 1000;
			entries.push({ text, timeMs: Math.max(0, wallMs - startWall) });
		}
		if (entries.length === 0) return null;

		// Split long entries (>15 words) into smaller chunks for readable subtitles
		const MAX_WORDS = 15;
		const chunked: { text: string; timeMs: number }[] = [];
		for (const e of entries) {
			const words = e.text.split(' ');
			if (words.length <= MAX_WORDS) {
				chunked.push(e);
			} else {
				const nChunks = Math.ceil(words.length / MAX_WORDS);
				for (let i = 0; i < nChunks; i++) {
					chunked.push({
						text: words.slice(i * MAX_WORDS, (i + 1) * MAX_WORDS).join(' '),
						timeMs: e.timeMs,
					});
				}
			}
		}

		// Auto-align: STT timestamps have ~12s lag, so wall-clock times are unreliable.
		// Distribute entries evenly across recording duration instead.
		// +5000 tail padding accounts for final description still displaying; *6000 fallback
		// when all timestamps collapse to the same second (single burst of descriptions).
		const totalDurationMs = chunked[chunked.length - 1].timeMs - chunked[0].timeMs;
		const recordingDurationMs = totalDurationMs > 0 ? totalDurationMs + 5000 : chunked.length * 6000;
		const interval = recordingDurationMs / chunked.length;
		for (let i = 0; i < chunked.length; i++) {
			chunked[i].timeMs = Math.round(i * interval);
		}
		const entries2 = chunked;

		let srt = '';
		for (let i = 0; i < entries2.length; i++) {
			const start = entries2[i].timeMs;
			const end = i < entries2.length - 1 ? entries2[i + 1].timeMs : start + 5000;
			srt += `${i + 1}\n${fmtTime(start)} --> ${fmtTime(end)}\n${entries2[i].text}\n\n`;
		}

		writeFileSync(LIVE_TRANSCRIPT_SRT_PATH, srt);
		console.log(`${ts()} [ScreenRecord] live transcript SRT: ${entries.length} blocks`);

		const outPath = videoPath.replace('.mov', '-subtitled.mov');
		execSync(
			// Match source bitrate to avoid 6x size inflation (narrated ~400kbps, subtitle burn was 2300kbps with -q:v 65)
			`/opt/homebrew/bin/ffmpeg -y -i "${videoPath}" -vf "subtitles=${LIVE_TRANSCRIPT_SRT_PATH}:force_style='FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,MarginV=30'" -c:v h264_videotoolbox -b:v 500k -c:a aac "${outPath}"`,
			{ timeout: 120_000 }
		);
		if (existsSync(outPath)) {
			console.log(`${ts()} [ScreenRecord] live transcript subtitles burned: ${outPath}`);
			return outPath;
		}
	} catch (err) {
		console.log(`${ts()} [ScreenRecord] live transcript subtitle failed: ${err}`);
	}
	return null;
}

export const screenRecordTool: ToolDefinition = {
	name: 'screen_record',
	description:
		'Start or stop screen recording. Uses ffmpeg avfoundation for reliable .mov output. ' +
		'When starting, ASK the user if they want live transcript subtitles burned into the recording.',
	parameters: z.object({
		action: z.enum(['start', 'stop']).describe('"start" begins recording, "stop" stops and saves the file'),
		duration_seconds: z.number().optional().describe('If provided with start, auto-stops after this many seconds.'),
		subtitle: z.boolean().optional().describe('If true, burn live conversation transcript as subtitles into the recording. Ask the user before setting this.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { action, duration_seconds, subtitle } = args as { action: 'start' | 'stop'; duration_seconds?: number; subtitle?: boolean };
		// Hard block: if already recording, refuse to start again
		if (action === 'start' && demoState === 'recording') {
			console.log(`${ts()} [ScreenRecord] BLOCKED duplicate start (already recording)`);
			return { status: 'already_recording', message: 'Recording is already in progress. Do NOT call screen_record start again.' };
		}
		const now = Date.now();
		if (now - lastScreenRecordCall < SCREEN_RECORD_COOLDOWN_MS) {
			return { status: 'cooldown', message: 'Wait a few seconds.' };
		}
		lastScreenRecordCall = now;
		try {
			const result = execSync(`python3 skills/screen-record/scripts/record.py ${action}`, { timeout: 10_000 }).toString().trim();
			// Auto-stop timer — cap at 60s regardless of what Gemini requests
			if (action === 'start') {
				demoState = 'recording';
				// Track transcript baseline for live subtitle generation (only if user wants subtitles)
				if (subtitle) {
					try { liveTranscriptResolvedPath = readlinkSync(LIVE_TRANSCRIPT_SYMLINK); } catch { liveTranscriptResolvedPath = ''; }
					liveTranscriptRecordingStart = Date.now();
					liveTranscriptBaselineLines = countTranscriptLines();
					try { unlinkSync(LIVE_TRANSCRIPT_SRT_PATH); } catch {}
					console.log(`${ts()} [ScreenRecord] live transcript subtitles enabled`);
				} else {
					liveTranscriptRecordingStart = 0;
				}
				const capped = Math.min(duration_seconds || 20, 60);
				setTimeout(() => {
					try {
						const stopResult = execSync('python3 skills/screen-record/scripts/record.py stop', { timeout: 10_000 }).toString().trim();
						const stopParsed = JSON.parse(stopResult);
						if (liveTranscriptRecordingStart > 0 && stopParsed.path && stopParsed.exists) {
							const narrated = stopParsed.path.replace('.mov', '-narrated.mov');
							burnLiveTranscriptSubtitles(isReadableFile(narrated) ? narrated : stopParsed.path);
						}
					} catch {}
					demoState = 'done';
					liveTranscriptRecordingStart = 0;
					console.log(`${ts()} [ScreenRecord] auto-stop after ${capped}s (requested ${duration_seconds}s)`);
				}, capped * 1000);
			}
			if (action === 'stop') {
				demoState = 'done';
				const parsed = JSON.parse(result);
				// Burn live transcript subtitles only if enabled at start
				if (liveTranscriptRecordingStart > 0 && parsed.path && parsed.exists) {
					const narrated = parsed.path.replace('.mov', '-narrated.mov');
					const subtitled = burnLiveTranscriptSubtitles(isReadableFile(narrated) ? narrated : parsed.path);
					if (subtitled) {
						parsed.subtitled_path = subtitled;
						console.log(`${ts()} [ScreenRecord] transcript subtitles: ${subtitled}`);
					}
				}
				liveTranscriptRecordingStart = 0;
				console.log(`${ts()} [ScreenRecord] ${action}: ${JSON.stringify(parsed)}`);
				return parsed;
			}
			const parsed = JSON.parse(result);
			console.log(`${ts()} [ScreenRecord] ${action}: ${result}`);
			return parsed;
		} catch (err) {
			return { error: `screen_record failed: ${err instanceof Error ? err.message : err}` };
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

// --- Screen recording ---


