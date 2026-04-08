/**
 * Browser & screen tools — Chrome tab control, scrolling, screenshots, and vision descriptions.
 * Split from inline-tools.ts for readability.
 */

import { execSync, execFileSync } from 'node:child_process';
import { writeFileSync, unlinkSync, readFileSync, existsSync, statSync } from 'node:fs';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';

const ts = () => new Date().toLocaleTimeString('en-US', { hour12: false });

// --- Scroll ---

export const scrollTool: ToolDefinition = {
	name: 'scroll',
	description:
		'Scroll the Chrome browser page. Use for: "scroll down", "scroll up", "scroll to top", "scroll to bottom".',
	parameters: z.object({
		direction: z.enum(['down', 'up', 'top', 'bottom']).describe('Scroll direction. Use "top" or "bottom" to jump to start/end of page.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { direction } = args as { direction: 'down' | 'up' | 'top' | 'bottom' };
		try {
			// Use Chrome's JavaScript scrollBy to avoid focus issues (Zoom steals keystrokes)
			if (direction === 'top') {
				execSync(`osascript -e 'tell application "Google Chrome" to tell active tab of front window to execute javascript "window.scrollTo(0, 0)"'`, { timeout: 5_000 });
			} else if (direction === 'bottom') {
				execSync(`osascript -e 'tell application "Google Chrome" to tell active tab of front window to execute javascript "window.scrollTo(0, document.body.scrollHeight)"'`, { timeout: 5_000 });
			} else {
				const amount = direction === 'down' ? 600 : -600;
				execSync(`osascript -e 'tell application "Google Chrome" to tell active tab of front window to execute javascript "window.scrollBy(0, ${amount})"'`, { timeout: 5_000 });
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
		const conditions = uniqueTerms.map(t => {
			const safe = t.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
			return `title of t contains "${safe}" or URL of t contains "${safe}"`;
		}).join(' or ');
		try {
			const script = `tell application "Google Chrome"\nset tabIndex to 0\nrepeat with w in windows\nset tabIndex to 0\nrepeat with t in tabs of w\nset tabIndex to tabIndex + 1\nignoring case\nif ${conditions} then\nset active tab index of w to tabIndex\nset index of w to 1\nactivate\nreturn title of t\nend if\nend ignoring\nend repeat\nend repeat\nreturn "not found"\nend tell`;
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
		// Fixes CodeQL #27 (js/command-line-injection): write to temp file instead of shell interpolation
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

async function describeScreenshot(imagePath: string): Promise<string> {
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
		const res = await fetch(
			`https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key=${apiKey}`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					contents: [{
						parts: [
							{ text: 'Describe what is on screen in exactly 1 short sentence (max 20 words). Quote the main heading. This will be spoken aloud.' },
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
	execSync(`osascript -e 'tell application "Google Chrome" to tell active tab of front window to execute javascript "window.scrollBy(0, ${pixels})"'`, { timeout: 5_000 });
}

let demoState: 'idle' | 'recording' | 'done' = 'idle';

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
			execSync('python3 skills/screen-record/scripts/record.py start', { timeout: 10_000 });
			const captureRes = await fetch('http://localhost:7845/capture');
			const captureData = await captureRes.json() as { status: string; path?: string };
			const firstDesc = captureData.path ? await describeScreenshot(captureData.path) : '';

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
				try { scrollDown(pxPerStep); } catch {}
				scrolledTotal += pxPerStep;
			}, SCROLL_INTERVAL_MS);

			// Auto-stop after duration
			setTimeout(() => {
				clearInterval(scrollInterval);
				try { execSync('python3 skills/screen-record/scripts/record.py stop', { timeout: 10_000 }); } catch {}
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
		session.transport.sendContent([
			{ role: 'user', text: '[System: You were narrating a screen demo. Continue where you left off — call describe_screen and keep narrating. Do NOT greet or say "I\'m back".]' },
		], true);
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
				session.transport.sendContent([
					{ role: 'user', text: '[System: Recording ending soon. Finish your current sentence and stop.]' },
				], true);
			} catch {}
			return;
		}
		try {
			const path = await captureScreen();
			if (!path) return;
			const desc = await describeScreenshot(path);
			if (!desc || desc === lastDesc) {
				if (desc === lastDesc) console.log(`${ts()} [Recording] skipped duplicate`);
				return;
			}
			lastDesc = desc;
			previousDescs.push(desc);
			if (!existsSync('/tmp/sutando-screen-record.pid')) return;
			const remaining = Math.round((durationMs - (Date.now() - startTime)) / 1000);
			const alreadySaid = previousDescs.slice(0, -1).map((d, i) => `${i + 1}. ${d.slice(0, 40)}`).join('; ');
			session.transport.sendContent([
				{ role: 'user', text: `[System: ${remaining}s left. Already narrated: ${alreadySaid || 'nothing yet'}. Now narrate this NEW content only (1 short sentence, no repeats): "${desc}"]` },
			], true);
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
			session.transport.sendContent([
				{ role: 'user', text: '[System: Recording just ended. Say "The recording is complete." immediately.]' },
			], true);
		} catch {}
	}, durationMs + 1000); // +1s buffer for auto-stop to finish
}

// Check file exists AND has meaningful size (>1KB). Prevents returning
// a recording that ffmpeg is still writing or a narrated file mid-mux.
function isReadableFile(path: string): boolean {
	try { return existsSync(path) && statSync(path).size > 1000; } catch { return false; }
}

function findRecording(version?: 'raw' | 'narrated'): string | null {
	try {
		const files = execSync('ls -t /tmp/sutando-recording-*.mov 2>/dev/null | grep -v narrated | grep -v subtitled | head -1', { timeout: 3_000 }).toString().trim();
		if (files && isReadableFile(files)) {
			if (version === 'raw') return files;
			const narrated = files.replace('.mov', '-narrated.mov');
			if (version === 'narrated') return isReadableFile(narrated) ? narrated : files;
			// Default: prefer narrated > raw
			if (isReadableFile(narrated)) return narrated;
			return files;
		}
	} catch {}
	return null;
}

export const playRecordingTool: ToolDefinition = {
	name: 'play_recording',
	description:
		'Control video playback. Use for ANY request about videos, recordings, or media files. ' +
		'Actions: "open" (just open, no playback), "play" (play + stream audio), "pause", "stop", "close" (quit player), "replay" (start from beginning), "status". ' +
		'IMPORTANT: "open" and "play" are DIFFERENT. "open the video" → action:"open". "play the video" → action:"play". ' +
		'"close this video" → action:"close". "replay from the top" or "start over" → action:"replay".',
	parameters: z.object({
		action: z.enum(['open', 'play', 'pause', 'stop', 'close', 'replay', 'status']).default('open'),
		path: z.string().optional().describe('File path. Omit for latest screen recording.'),
		version: z.enum(['raw', 'narrated']).optional().describe('Which version: "raw" (no narration) or "narrated" (with voice). Omit for best available. For subtitles, use the work tool to add them.'),
	}),
	execution: 'inline',
	async execute(args) {
		let { action, path: filePath, version } = args as { action: 'open' | 'play' | 'pause' | 'stop' | 'close' | 'replay' | 'status'; path?: string; version?: 'raw' | 'narrated' };
		const isReplay = action === 'replay';
		if (action === 'stop') action = 'pause';
		if (isReplay) action = 'play';
		demoState = 'idle';
		try {
			if (action === 'close') {
				try { execSync(`osascript -e 'tell application "QuickTime Player" to quit'`, { timeout: 5_000 }); } catch {}
				try { unlinkSync('/tmp/sutando-playback-pause'); } catch {}
				try { unlinkSync('/tmp/sutando-playback-path'); } catch {}
				console.log(`${ts()} [PlayRecording] closed`);
				return { status: 'closed', instruction: 'Video player closed.' };
			}

			if (action === 'pause') {
				try { writeFileSync('/tmp/sutando-playback-pause', '1'); } catch {}
				try { execSync(`osascript -e 'tell application "QuickTime Player"' -e 'if (count of documents) > 0 then' -e 'pause document 1' -e 'end if' -e 'end tell'`, { timeout: 5_000 }); } catch {}
				console.log(`${ts()} [PlayRecording] paused`);
				return { status: 'paused', instruction: 'Video paused. When user says continue/play/resume, call play_recording({action:"play"}) to resume. Say only "Paused." now.' };
			}

			if (action === 'status') {
				try {
					const out = execSync(`osascript -e 'tell application "QuickTime Player"' -e 'if (count of documents) > 0 then' -e 'set d to document 1' -e 'set p to playing of d' -e 'set c to current time of d' -e 'set dur to duration of d' -e 'return (p as text) & "|" & (c as text) & "|" & (dur as text)' -e 'else' -e 'return "none"' -e 'end if' -e 'end tell'`, { timeout: 5_000 }).toString().trim();
					if (out === 'none') return { status: 'no_video_open' };
					const [playing, current, duration] = out.split('|');
					return { status: playing === 'true' ? 'playing' : 'paused', current_seconds: +current, duration_seconds: +duration };
				} catch { return { status: 'no_video_open' }; }
			}

			let recPath = filePath ? filePath.replace(/^~/, process.env.HOME || '') : null;
			if (!recPath) {
				try { recPath = readFileSync('/tmp/sutando-playback-path', 'utf8').trim() || null; } catch {}
			}
			if (!recPath) recPath = findRecording(version);
			// Retry once after 3s — narration-tee mux takes ~1s after recording stops,
			// so the narrated file may not exist yet when Gemini immediately calls play.
			if (!recPath) {
				await new Promise(r => setTimeout(r, 3000));
				recPath = findRecording(version);
			}
			if (recPath && !isReadableFile(recPath)) recPath = null;
			if (!recPath) return { error: filePath ? `File not found: ${filePath}` : 'No screen recording found — recording may still be saving. Try again in a few seconds.' };

			writeFileSync('/tmp/sutando-playback-path', recPath);

			if (action === 'open') {
				execSync(`open "${recPath}"`, { timeout: 5_000 });
				const size = statSync(recPath).size;
				console.log(`${ts()} [PlayRecording] opened ${recPath} (${(size / 1024 / 1024).toFixed(1)}MB)`);
				return { status: 'opened', path: recPath, size_mb: +(size / 1024 / 1024).toFixed(1), instruction: 'File opened in QuickTime (not playing). When user says play/start, call play_recording({action:"play"}).' };
			}

			let seekSec = 0;
			let alreadyOpen = false;
			try {
				const c = execSync(`osascript -e 'tell application "QuickTime Player" to count of documents'`, { timeout: 2_000 }).toString().trim();
				if (parseInt(c) > 0) {
					alreadyOpen = true;
					if (isReplay) {
						// Replay: always seek to 0
						seekSec = 0;
					} else {
						const pos = execSync(`osascript -e 'tell application "QuickTime Player"' -e 'set d to document 1' -e 'return current time of d' -e 'end tell'`, { timeout: 3_000 }).toString().trim();
						const dur = execSync(`osascript -e 'tell application "QuickTime Player"' -e 'set d to document 1' -e 'return duration of d' -e 'end tell'`, { timeout: 3_000 }).toString().trim();
						const posNum = parseFloat(pos) || 0;
						const durNum = parseFloat(dur) || 1;
						seekSec = (posNum / durNum > 0.95) ? 0 : (posNum < 0.5 ? 0 : posNum);
					}
					if (seekSec === 0) {
						try { execSync(`osascript -e 'tell application "QuickTime Player"' -e 'set d to document 1' -e 'set current time of d to 0' -e 'end tell'`, { timeout: 3_000 }); } catch {}
					}
				}
			} catch {}

			if (!alreadyOpen) {
				execSync(`open "${recPath}"`, { timeout: 5_000 });
				for (let i = 0; i < 10; i++) {
					try {
						const c = execSync(`osascript -e 'tell application "QuickTime Player" to count of documents'`, { timeout: 2_000 }).toString().trim();
						if (parseInt(c) > 0) break;
					} catch {}
					await new Promise(r => setTimeout(r, 300));
				}
			}

			try { unlinkSync('/tmp/sutando-playback-pause'); } catch {}
			fetch(`http://localhost:${process.env.PHONE_PORT || '3100'}/play-audio`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ path: recPath, seekSec }),
			}).catch(() => {});
			await new Promise(r => setTimeout(r, 300));
			try {
				execSync(`osascript -e '
					tell application "QuickTime Player"
						activate
						play document 1
					end tell
				'`, { timeout: 5_000 });
			} catch {}
			console.log(`${ts()} [PlayRecording] play from ${seekSec}s`);

			const size = statSync(recPath).size;
			console.log(`${ts()} [PlayRecording] ${recPath} (${(size / 1024 / 1024).toFixed(1)}MB)`);
			return { status: 'playing', path: recPath, size_mb: +(size / 1024 / 1024).toFixed(1), narrated: recPath.includes('-narrated'), instruction: 'Video is playing. Say NOTHING. When user says pause/stop, call play_recording({action:"pause"}). When user says continue/play, call play_recording({action:"play"}).' };
		} catch (err) {
			return { error: `play_recording failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// --- Screen recording ---

let lastScreenRecordCall = 0;
const SCREEN_RECORD_COOLDOWN_MS = 5_000;

export const screenRecordTool: ToolDefinition = {
	name: 'screen_record',
	description:
		'Start or stop screen recording. Uses macOS screencapture -v for reliable .mov output.',
	parameters: z.object({
		action: z.enum(['start', 'stop']).describe('"start" begins recording, "stop" stops and saves the file'),
		duration_seconds: z.number().optional().describe('If provided with start, auto-stops after this many seconds.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { action, duration_seconds } = args as { action: 'start' | 'stop'; duration_seconds?: number };
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
				const capped = Math.min(duration_seconds || 20, 60);
				setTimeout(() => {
					try { execSync('python3 skills/screen-record/scripts/record.py stop', { timeout: 10_000 }); } catch {}
					demoState = 'done';
					console.log(`${ts()} [ScreenRecord] auto-stop after ${capped}s (requested ${duration_seconds}s)`);
				}, capped * 1000);
			}
			if (action === 'stop') demoState = 'done';
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

