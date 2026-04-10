/**
 * Inline tools — lightweight macOS actions that execute instantly without going through the core agent.
 * Shared between voice-agent.ts and phone conversation-server.ts.
 *
 * Add new tools here and they auto-appear in both voice and phone agents.
 */

import { execSync, execFileSync } from 'node:child_process';
import { writeFileSync, unlinkSync, readdirSync, readFileSync, existsSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';

const ts = () => new Date().toLocaleTimeString('en-US', { hour12: false });

// Re-export tools from chrome-tools, meeting-tools, and recording-tools
export { switchTabTool, openUrlTool } from './chrome-tools.js';
export { playVideoInMeetingTool, pauseVideoInMeetingTool, resumeVideoInMeetingTool, summonTool, dismissTool, joinZoomTool, joinGmeetTool, lookupMeetingIdTool, recordScreenWithNarrationTool } from './remote-meeting-tools.js';
export { screenRecordTool } from './recording-tools.js';
import { switchTabTool, openUrlTool } from './chrome-tools.js';
import { playVideoInMeetingTool, pauseVideoInMeetingTool, resumeVideoInMeetingTool, summonTool, dismissTool, joinZoomTool, joinGmeetTool, lookupMeetingIdTool, recordScreenWithNarrationTool } from './remote-meeting-tools.js';
import { screenRecordTool, demoState, setDemoState, findRecording, isReadableFile } from './recording-tools.js';

// --- Vision helpers (shared with recording-tools) ---

// Vision model — override via .env (default: flash-lite for this trivial 20-word task)
const VISION_MODEL = process.env.VISION_MODEL || 'gemini-3.1-flash-lite-preview';

export async function captureScreen(): Promise<string | null> {
	try {
		const res = await fetch('http://localhost:7845/capture');
		const data = await res.json() as { status: string; path?: string };
		return data.status === 'ok' && data.path ? data.path : null;
	} catch { return null; }
}

export async function describeScreenshot(imagePath: string, previousDescs: string[] = []): Promise<string> {
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
		if (previousDescs.length === 0) {
			prompt = 'Describe what is on screen in exactly 1 short sentence (max 20 words). Quote the main heading. This will be spoken aloud.';
		} else {
			const recent = previousDescs.slice(-3).map((d, i) => `${i + 1}. ${d}`).join(' | ');
			prompt = `You are narrating a screen recording aloud. Already spoken: ${recent}. Describe ONLY what is NEW or has changed. Use a natural continuation ("Scrolling down...", "Next...", "Now we see...", "Further down..."). Do NOT restart with "The screen shows/displays" — the viewer already knows what page this is. 1 short sentence, max 20 words.`;
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

// --- Scroll ---

export const scrollTool: ToolDefinition = {
	name: 'scroll',
	description:
		'Scroll the active application. Works in Chrome (JS scroll) and any other app (keyboard Page Down/Up). Use for: "scroll down", "scroll up", "scroll to top", "scroll to bottom".',
	parameters: z.object({
		direction: z.enum(['down', 'up', 'top', 'bottom']).describe('Scroll direction. Use "top" or "bottom" to jump to start/end of page.'),
		pixels: z.number().optional().describe('Number of pixels to scroll (default 600). Only works for down/up.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { direction, pixels } = args as { direction: 'down' | 'up' | 'top' | 'bottom'; pixels?: number };
		try {
			// Detect frontmost app
			let frontApp = 'Google Chrome';
			try {
				frontApp = execSync(`osascript -e 'tell application "System Events" to get name of first process whose frontmost is true'`, { timeout: 3_000 }).toString().trim();
			} catch {}

			if (frontApp === 'Google Chrome') {
				// Use Chrome's JavaScript scrollBy to avoid focus issues (Zoom steals keystrokes)
				if (direction === 'top') {
					execSync(`osascript -e 'tell application "Google Chrome" to tell active tab of front window to execute javascript "window.scrollTo(0, 0)"'`, { timeout: 5_000 });
				} else if (direction === 'bottom') {
					execSync(`osascript -e 'tell application "Google Chrome" to tell active tab of front window to execute javascript "window.scrollTo(0, document.body.scrollHeight)"'`, { timeout: 5_000 });
				} else {
					const amount = direction === 'down' ? (pixels ?? 600) : -(pixels ?? 600);
					execSync(`osascript -e 'tell application "Google Chrome" to tell active tab of front window to execute javascript "window.scrollBy(0, ${amount})"'`, { timeout: 5_000 });
				}
			} else {
				// Non-Chrome apps: use keyboard events
				if (direction === 'top') {
					execSync(`osascript -e 'tell application "System Events" to key code 115 using command down'`, { timeout: 5_000 }); // Cmd+Home
				} else if (direction === 'bottom') {
					execSync(`osascript -e 'tell application "System Events" to key code 119 using command down'`, { timeout: 5_000 }); // Cmd+End
				} else if (direction === 'down') {
					if (pixels) {
						// Approximate: each Page Down ~= 600px, use repeated key presses
						const presses = Math.max(1, Math.round(pixels / 600));
						for (let i = 0; i < presses; i++) {
							execSync(`osascript -e 'tell application "System Events" to key code 121'`, { timeout: 5_000 });
						}
					} else {
						execSync(`osascript -e 'tell application "System Events" to key code 121'`, { timeout: 5_000 }); // Page Down
					}
				} else {
					if (pixels) {
						const presses = Math.max(1, Math.round(pixels / 600));
						for (let i = 0; i < presses; i++) {
							execSync(`osascript -e 'tell application "System Events" to key code 116'`, { timeout: 5_000 });
						}
					} else {
						execSync(`osascript -e 'tell application "System Events" to key code 116'`, { timeout: 5_000 }); // Page Up
					}
				}
			}
			console.log(`${ts()} [Scroll] ${direction}${pixels ? ` ${pixels}px` : ''} (${frontApp})`);
			return { status: 'scrolled', direction };
		} catch (err) {
			return { error: `Scroll failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// --- Describe screen (vision) ---

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

// --- Open file (generalized from open_video) ---

export const openFileTool: ToolDefinition = {
	name: 'open_file',
	description:
		'Open a file with the default macOS app. Pass a path, or omit to open the latest screen recording. ' +
		'Use when user says "open the video", "open the file", "open that", "can you open it". ' +
		'Do NOT call play_video_in_meeting after this — wait for user to explicitly say "play".',
	parameters: z.object({
		path: z.string().optional().describe('File path to open. Omit to open the latest screen recording.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { path: filePath } = args as { path?: string };
		console.log(`${ts()} [OpenFile] called`);
		try {
			let target = filePath ? filePath.replace(/^~/, process.env.HOME || '') : null;
			// If Gemini hallucinated a path that doesn't exist, fall back to findRecording
			if (target && !existsSync(target)) {
				console.log(`${ts()} [OpenFile] path "${target}" does not exist, falling back to findRecording`);
				target = null;
			}
			// Fallback: find latest recording if no path given or path invalid
			// Poll up to 18s — subtitle burn happens async after recording stops
			if (!target) {
				for (let i = 0; i < 10; i++) {
					target = findRecording();
					// If we found a raw file but subtitled isn't ready yet, keep waiting
					if (target && target.includes('-subtitled')) break;
					if (target && i < 6) { await new Promise(r => setTimeout(r, 3000)); continue; }
					if (target) break; // after 18s, use whatever we have
					await new Promise(r => setTimeout(r, 3000));
				}
			}
			if (!target) return { error: 'No file found after 30s.' };
			// For recordings, track playback path so meeting video tools can find it
			if (target.includes('sutando-recording')) {
				writeFileSync('/tmp/sutando-playback-path', target);
				setDemoState('idle');
			}
			execSync(`open "${target}"`, { timeout: 5_000 });
			const size = existsSync(target) ? statSync(target).size : 0;
			console.log(`${ts()} [OpenFile] opened ${target} (${(size / 1024 / 1024).toFixed(1)}MB)`);
			return { status: 'opened', path: target, size_mb: +(size / 1024 / 1024).toFixed(1) };
		} catch (err) {
			return { error: `open_file failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// --- Keyboard tool ---

export const pressKeyTool: ToolDefinition = {
	name: 'press_key',
	description:
		'Press a keyboard key or shortcut. By default targets the frontmost app. ' +
		'Pass app to target a specific application (e.g. "QuickTime Player" to pause a video, "Safari" to close a tab). ' +
		'Use for: "press enter", "press escape", "press tab", "send the message" (Enter), ' +
		'"close the dialog" (Escape), "select all" (Cmd+A), "pause the video" (space, app=QuickTime Player), ' +
		'"close the video" (Cmd+Q, app=QuickTime Player). Instant — do NOT use work for simple keystrokes.',
	parameters: z.object({
		key: z.string().describe('Key to press: enter, escape, tab, delete, space, up, down, left, right, or a letter'),
		modifiers: z.array(z.enum(['command', 'shift', 'control', 'option'])).optional().describe('Modifier keys'),
		app: z.string().optional().describe('Target app name (e.g. "QuickTime Player", "Safari"). If set, activates the app before pressing the key.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { key, modifiers = [], app } = args as { key: string; modifiers?: string[]; app?: string };
		// Activate target app if specified
		if (app) {
			try {
				execSync(`osascript -e 'tell application "${app.replace(/"/g, '\\"')}" to activate'`, { timeout: 3_000 });
			} catch { console.log(`${ts()} [PressKey] Could not activate ${app}`); }
		}
		const keyMap: Record<string, number> = {
			'enter': 36, 'return': 36, 'escape': 53, 'esc': 53, 'tab': 48,
			'delete': 51, 'backspace': 51, 'space': 49,
			'up': 126, 'down': 125, 'left': 123, 'right': 124,
			'a': 0, 'c': 8, 'v': 9, 'x': 7, 'z': 6, 'f': 3, 's': 1, 'w': 13, 'q': 12,
		};
		const keyCode = keyMap[key.toLowerCase()];
		if (keyCode === undefined) {
			const modStr = modifiers.length ? ` using {${modifiers.map(m => m + ' down').join(', ')}}` : '';
			try {
				execSync(`osascript -e 'tell application "System Events" to keystroke "${key}"${modStr}'`, { timeout: 3_000 });
			} catch (err) {
				return { error: `press_key failed: ${err instanceof Error ? err.message : err}` };
			}
		} else {
			const modStr = modifiers.length ? ` using {${modifiers.map(m => m + ' down').join(', ')}}` : '';
			try {
				execSync(`osascript -e 'tell application "System Events" to key code ${keyCode}${modStr}'`, { timeout: 3_000 });
			} catch (err) {
				return { error: `press_key failed: ${err instanceof Error ? err.message : err}` };
			}
		}
		console.log(`${ts()} [PressKey] ${app ? `[${app}] ` : ''}${modifiers.length ? modifiers.join('+') + '+' : ''}${key}`);
		return { status: 'pressed', key, modifiers, app };
	},
};

// --- Browser tools (scroll, switchTab, openUrl) imported from split modules above ---

// --- macOS system tools ---

const APP_ALIASES: Record<string, string> = {
	'vs code': 'Visual Studio Code', 'vscode': 'Visual Studio Code',
	'chrome': 'Google Chrome', 'safari': 'Safari',
	'terminal': 'Terminal', 'finder': 'Finder',
	'slack': 'Slack', 'discord': 'Discord',
};

// System Events process names differ from app bundle names
const PROCESS_NAMES: Record<string, string> = {
	'Visual Studio Code': 'Code',
};

export const switchAppTool: ToolDefinition = {
	name: 'switch_app',
	description:
		'Switch to (activate) a macOS application. Use for: "switch to Chrome", "open Slack", "go to Terminal".',
	parameters: z.object({
		app: z.string().describe('Application name (e.g. "Google Chrome", "Slack", "Terminal", "Finder")'),
	}),
	execution: 'inline',
	async execute(args) {
		let { app } = args as { app: string };
		app = APP_ALIASES[app.toLowerCase()] ?? app;
		// Escape backslashes first, then quotes — prevents shell injection via osascript
		const safeApp = app.replace(/\\/g, '\\\\').replace(/'/g, "'\\''").replace(/"/g, '\\"');
		const processName = (PROCESS_NAMES[app] ?? app).replace(/\\/g, '\\\\').replace(/'/g, "'\\''").replace(/"/g, '\\"');
		try {
			execSync(`osascript -e 'tell application "${safeApp}" to activate' -e 'tell application "System Events" to set frontmost of process "${processName}" to true'`, { timeout: 10_000 });
			console.log(`${ts()} [SwitchApp] activated: ${app}`);
			return { status: 'switched', app };
		} catch (err) {
			return { error: `Failed to switch to ${app}: ${err instanceof Error ? err.message : err}` };
		}
	},
};

export const captureScreenTool: ToolDefinition = {
	name: 'capture_screen',
	description:
		'Capture a screenshot of the screen. Use for: "take a screenshot", "what\'s on my screen", "look at this". Supports multi-monitor: pass display=2 for secondary screen, display=3 for third, etc. Default captures the main display. Instant.',
	parameters: z.object({
		display: z.number().optional().describe('Display number: 1=main, 2=secondary, 3=third. Default: main display.'),
	}),
	execution: 'inline',
	async execute(args) {
		try {
			const { display } = args as { display?: number };
			// If no display specified, capture all displays
			const query = display ? `?display=${display}` : '?all=true';
			const res = await fetch(`http://localhost:7845/capture${query}`);
			const data = await res.json() as { status: string; path?: string; all_paths?: string[]; displays?: number; error?: string };
			if (data.status === 'ok' && data.path) {
				const label = data.displays && data.displays > 1
					? ` (${data.displays} displays)`
					: display ? ` display ${display}` : '';
				console.log(`${ts()} [Screen] Captured${label}: ${data.path}`);
				if (data.all_paths && data.all_paths.length > 1) {
					return { status: 'captured', paths: data.all_paths, displays: data.displays, note: 'Multiple displays captured. Each path is a separate screen.' };
				}
				return { status: 'captured', path: data.path };
			}
			return { status: 'failed', error: data.error || 'unknown error' };
		} catch {
			return { status: 'failed', error: 'Screen capture server not running' };
		}
	},
};

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
		const safeText = text.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
		try {
			execSync(`osascript -e 'tell application "System Events" to keystroke "${safeText}"'`, { timeout: 5_000 });
			console.log(`${ts()} [TypeText] typed: ${text.slice(0, 40)}`);
			return { status: 'typed', text };
		} catch (err) {
			return { error: `Type failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

export const volumeTool: ToolDefinition = {
	name: 'volume',
	description:
		'Adjust system volume. Use for: "turn it up", "mute", "set volume to 50%". Instant.',
	parameters: z.object({
		level: z.number().min(0).max(100).optional().describe('Volume level 0-100. Omit to mute/unmute.'),
		mute: z.boolean().optional().describe('true to mute, false to unmute'),
	}),
	execution: 'inline',
	async execute(args) {
		const { level, mute } = args as { level?: number; mute?: boolean };
		try {
			if (mute === true) {
				execSync(`osascript -e 'set volume with output muted'`, { timeout: 5_000 });
				console.log(`${ts()} [Volume] muted`);
				return { status: 'muted' };
			}
			if (mute === false) {
				execSync(`osascript -e 'set volume without output muted'`, { timeout: 5_000 });
				console.log(`${ts()} [Volume] unmuted`);
				return { status: 'unmuted' };
			}
			if (level !== undefined) {
				// Gemini sometimes passes 0-1 instead of 0-100 — normalize
				const normalizedLevel = level <= 1 && level > 0 ? Math.round(level * 100) : Math.round(level);
				execSync(`osascript -e 'set volume output volume ${normalizedLevel}'`, { timeout: 5_000 });
				console.log(`${ts()} [Volume] set to ${normalizedLevel}%`);
				return { status: 'set', level: normalizedLevel };
			}
			return { error: 'Specify level (0-100) or mute (true/false)' };
		} catch (err) {
			return { error: `Volume failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

export const brightnessTool: ToolDefinition = {
	name: 'brightness',
	description:
		'Adjust screen brightness. Use for: "brighter", "dim the screen", "set brightness to 50%". Instant.',
	parameters: z.object({
		level: z.number().min(0).max(100).describe('Brightness level 0-100'),
	}),
	execution: 'inline',
	async execute(args) {
		let { level } = args as { level: number };
		// Gemini sometimes passes 0-1 instead of 0-100 — normalize
		if (level <= 1 && level > 0) level = Math.round(level * 100);
		const bLevel = (level / 100).toFixed(2);
		try {
			execSync(`osascript -e 'tell application "System Events" to tell appearance preferences to set dark mode to false'`, { timeout: 1_000 }).toString();
		} catch {} // ignore — just trying to ensure display is active
		try {
			execSync(`brightness ${bLevel}`, { timeout: 5_000 });
			console.log(`${ts()} [Brightness] set to ${level}%`);
			return { status: 'set', level };
		} catch {
			// Fallback: use AppleScript key codes
			try {
				const steps = Math.round(level / 100 * 16);
				// Reset to 0 then go up
				for (let i = 0; i < 16; i++) execSync(`osascript -e 'tell application "System Events" to key code 107'`, { timeout: 1_000 }); // brightness down
				for (let i = 0; i < steps; i++) execSync(`osascript -e 'tell application "System Events" to key code 113'`, { timeout: 1_000 }); // brightness up
				console.log(`${ts()} [Brightness] set to ~${level}% via key codes`);
				return { status: 'set', level, method: 'key_codes' };
			} catch (err) {
				return { error: `Brightness failed: ${err instanceof Error ? err.message : err}` };
			}
		}
	},
};

export const clipboardTool: ToolDefinition = {
	name: 'clipboard',
	description:
		'Read or write the system clipboard. Use for: "what did I copy", "copy this text", "paste". Instant.',
	parameters: z.object({
		action: z.enum(['read', 'write']).describe('"read" to get clipboard contents, "write" to set them'),
		text: z.string().optional().describe('Text to write to clipboard (only for action="write")'),
	}),
	execution: 'inline',
	async execute(args) {
		const { action, text } = args as { action: 'read' | 'write'; text?: string };
		try {
			if (action === 'read') {
				const content = execSync(`pbpaste`, { timeout: 5_000 }).toString();
				console.log(`${ts()} [Clipboard] read: ${content.slice(0, 40)}`);
				return { status: 'read', content };
			} else {
				if (!text) return { error: 'No text provided to write' };
				execSync(`echo ${JSON.stringify(text)} | pbcopy`, { timeout: 5_000 });
				console.log(`${ts()} [Clipboard] wrote: ${text.slice(0, 40)}`);
				return { status: 'written', text };
			}
		} catch (err) {
			return { error: `Clipboard failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

export const cancelTaskTool: ToolDefinition = {
	name: 'cancel_task',
	description: 'Cancel the most recent pending task. Use when someone says "cancel", "nevermind", "stop that".',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		try {
			const tasksDir = join(process.cwd(), 'tasks');
			const resultsDir = join(process.cwd(), 'results');
			const files = readdirSync(tasksDir).filter(f => f.endsWith('.txt')).sort();
			if (files.length === 0) return { status: 'nothing_to_cancel' };
			const mostRecent = files[files.length - 1];
			const taskId = mostRecent.replace('.txt', '');
			// Write a cancelled result so the web UI shows it with the cancelled icon
			writeFileSync(join(resultsDir, mostRecent), 'Cancelled.');
			unlinkSync(join(tasksDir, mostRecent));
			console.log(`${ts()} [CancelTask] cancelled: ${taskId}`);
			return { status: 'cancelled', taskId };
		} catch (err) {
			return { error: `Cancel failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

export const toggleTasksTool: ToolDefinition = {
	name: 'toggle_tasks',
	description:
		'Collapse or expand all tasks in the web UI. Use for: "collapse tasks", "expand tasks", "hide tasks", "show tasks". Instant.',
	parameters: z.object({
		action: z.enum(['collapse', 'expand']).describe('"collapse" to hide all task results, "expand" to show them'),
	}),
	execution: 'inline',
	async execute(args) {
		const { action } = args as { action: 'collapse' | 'expand' };
		// Set data attribute on body — MutationObserver in the page picks it up and updates state
		const js = `document.body.dataset.taskAction = \\\"${action}\\\"; \\\"done\\\"`;
		try {
			execSync(`osascript -e 'tell application "Google Chrome"
				repeat with w in windows
					repeat with t in tabs of w
						if URL of t contains "localhost:8080" then
							execute t javascript "${js}"
							return "ok"
						end if
					end repeat
				end repeat
				return "not found"
			end tell'`, { timeout: 5_000 });
			console.log(`${ts()} [ToggleTasks] ${action}`);
			return { status: action === 'collapse' ? 'collapsed' : 'expanded' };
		} catch (err) {
			return { error: `Toggle tasks failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

export const getCurrentTimeTool: ToolDefinition = {
	name: 'get_current_time',
	description: 'Get the current date and time. Instant.',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		return { time: new Date().toLocaleString('en-US', { dateStyle: 'full', timeStyle: 'long' }) };
	},
};

const PHONE_PORT = Number(process.env.PHONE_PORT) || 3100;

// summonTool, dismissTool, joinZoomTool, joinGmeetTool, lookupMeetingIdTool
// moved to meeting-tools.ts


export const callContactTool: ToolDefinition = {
	name: 'call_contact',
	description:
		'Look up a phone number and call a contact. Searches macOS Contacts by name. Instant. ' +
		'Use for ANY contact lookup or phone call — "find Bob\'s number", "call Mary", "look up Susan\'s phone".',
	parameters: z.object({
		name: z.string().describe('Contact name to search for (e.g. "Bob", "Mary Smith")'),
		message: z.string().optional().describe('What to tell the person. They have no tools — include all details they might need.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { name, message } = args as { name: string; message?: string };
		try {
			// Ensure Contacts.app is running
			execSync('open -ga Contacts', { timeout: 5_000 });

			// Search contacts via AppleScript — use first name for fuzzy matching
			// (voice transcription often garbles last names, e.g. "Gmeets" vs "GMeet")
			const firstName = name.split(/\s+/)[0];
			const safeName = firstName.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
			const script = `tell application "Contacts"
	set output to ""
	set results to (every person whose name contains "${safeName}")
	if (count of results) > 10 then set results to items 1 thru 10 of results
	repeat with p in results
		set pName to name of p
		set pPhones to ""
		repeat with ph in phones of p
			set pPhones to pPhones & (value of ph) & ","
		end repeat
		set output to output & pName & "|||" & pPhones & "\\n"
	end repeat
	return output
end tell`;
			const raw = execSync(`osascript -e '${script.replace(/'/g, "'\\''")}'`, { timeout: 15_000 }).toString().trim();

			// Parse results
			const contacts: { name: string; phones: string[] }[] = [];
			for (const line of raw.split('\n')) {
				const trimmed = line.trim();
				if (!trimmed) continue;
				const parts = trimmed.split('|||');
				if (parts.length < 2) continue;
				const cName = parts[0].trim();
				const phones = parts[1].split(',').map(p => p.trim()).filter(Boolean);
				if (phones.length > 0) contacts.push({ name: cName, phones });
			}

			if (contacts.length === 0) {
				console.log(`${ts()} [CallContact] no contacts with phone found for "${name}"`);
				return { error: `No contacts with a phone number found for "${name}". Ask the user for the number or a different name.` };
			}

			if (contacts.length > 1) {
				console.log(`${ts()} [CallContact] multiple matches for "${name}": ${contacts.map(c => c.name).join(', ')}`);
				return {
					status: 'multiple_matches',
					matches: contacts.map(c => ({ name: c.name, phones: c.phones })),
					instruction: 'Multiple contacts found. Ask the user which one to call.',
				};
			}

			// Single match — look up and call
			const contact = contacts[0];
			const phone = contact.phones[0];

			const purpose = message || `Calling ${contact.name}`;

			console.log(`${ts()} [CallContact] calling ${contact.name}`);
			const res = await fetch(`http://localhost:${PHONE_PORT}/call`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ to: phone, message: purpose }),
			});
			const data = await res.json() as { callSid?: string; status?: string; error?: string };

			if (!res.ok) {
				return { error: `Phone server error: ${data.error || res.statusText}` };
			}

			console.log(`${ts()} [CallContact] call started: ${data.callSid}, purpose: ${purpose}`);
			return { status: 'calling', contact: contact.name, callSid: data.callSid, messageSent: purpose };
		} catch (err) {
			return { error: `call_contact failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// Slide control — navigate presentation slides
export const slideControlTool: ToolDefinition = {
	name: 'slide_control',
	description:
		'Control presentation slides. Use when user says "next slide", "previous slide", "go back", "go to slide 3". ' +
		'Sends arrow keys to the frontmost browser window.',
	parameters: z.object({
		action: z.enum(['next', 'previous', 'goto']).describe('Navigation action'),
		slideNumber: z.number().optional().describe('Slide number for goto action'),
	}),
	execution: 'inline',
	async execute(args) {
		const { action, slideNumber } = args as { action: 'next' | 'previous' | 'goto'; slideNumber?: number };
		try {
			// All slide navigation uses DOM manipulation for reliability
			let js: string;
			if (action === 'goto' && slideNumber) {
				js = `var ss=document.querySelectorAll(\\".slide\\");for(var j=0;j<ss.length;j++){ss[j].classList.remove(\\"active\\")};document.getElementById(\\"s${slideNumber}\\").classList.add(\\"active\\");document.getElementById(\\"cur\\").textContent=\\"${slideNumber}\\"`;
			} else {
				// next/previous: read current slide number, compute target, set it
				const dir = action === 'next' ? 1 : -1;
				js = `var cur=parseInt(document.getElementById(\\"cur\\").textContent)||1;var total=document.querySelectorAll(\\".slide\\").length;var next=((cur-1+${dir}+total)%total)+1;var ss=document.querySelectorAll(\\".slide\\");for(var j=0;j<ss.length;j++){ss[j].classList.remove(\\"active\\")};document.getElementById(\\"s\\"+next).classList.add(\\"active\\");document.getElementById(\\"cur\\").textContent=String(next)`;
			}
			const script = `tell application "Google Chrome"
	repeat with w in windows
		set tabList to tabs of w
		repeat with i from 1 to count of tabList
			if URL of item i of tabList contains "index-sutando" or URL of item i of tabList contains "localhost:8888" then
				tell item i of tabList to execute javascript "${js}"
				return "done"
			end if
		end repeat
	end repeat
end tell`;
			execSync(`osascript -e '${script}'`, { timeout: 15_000 });
			console.log(`${ts()} [Slides] ${action}${slideNumber ? ` → slide ${slideNumber}` : ''}`);
			return { status: 'done', action, slideNumber };
		} catch (err) {
			return { error: `Slide control failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// Toggle fullscreen on the presentation slides
export const fullscreenTool: ToolDefinition = {
	name: 'fullscreen',
	description:
		'Toggle fullscreen mode on the presentation slides. Use when user says "fullscreen", "enter fullscreen", "exit fullscreen", "make it full screen".',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		try {
			execSync(`osascript -e '
tell application "Google Chrome"
	activate
	repeat with w in windows
		set tabList to tabs of w
		repeat with i from 1 to count of tabList
			if URL of item i of tabList contains "index-sutando" or URL of item i of tabList contains "index-bodhi" or URL of item i of tabList contains "localhost:8888" then
				set active tab index of w to i
				set index of w to 1
				exit repeat
			end if
		end repeat
	end repeat
end tell
delay 0.3
tell application "System Events"
	keystroke "f"
end tell'`, { timeout: 5_000 });
			console.log(`${ts()} [Fullscreen] Toggled`);
			return { status: 'toggled' };
		} catch (err) {
			return { error: `Fullscreen toggle failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

/** All inline tools — import and spread into your tools list */
// ─── Notes tools ─────────────────────────────────────────
const NOTES_DIR = join(process.cwd(), 'notes');

export const showViewTool: ToolDefinition = {
	name: 'show_view',
	description: 'Switch the web UI to a specific view. Use when user says "show notes", "show tasks", "show activity", etc.',
	parameters: z.object({
		view: z.enum(['starter', 'tasks', 'notes', 'questions', 'activity']).describe('Which view to show'),
	}),
	execution: 'inline',
	async execute(args) {
		const { view } = args as { view: string };
		const dcPath = join(process.cwd(), 'dynamic-content.json');
		writeFileSync(dcPath, JSON.stringify({ type: 'view', view }));
		// Auto-clear after 3 seconds so it doesn't persist
		setTimeout(() => { try { unlinkSync(dcPath); } catch {} }, 3000);
		const labels: Record<string, string> = { starter: 'home', tasks: 'tasks', notes: 'notes', questions: 'questions', activity: 'activity' };
		return { status: 'ok', message: `Showing ${labels[view] || view}` };
	},
};

export const readNoteTool: ToolDefinition = {
	name: 'read_note',
	description: 'Read a specific note by name or slug. Speak the content to the user.',
	parameters: z.object({
		name: z.string().describe('Note name or slug to search for'),
	}),
	execution: 'inline',
	async execute(args) {
		const { name } = args as { name: string };
		try {
			const files = readdirSync(NOTES_DIR).filter(f => f.endsWith('.md'));
			const query = name.toLowerCase().replace(/\s+/g, '-');
			const match = files.find(f => f.toLowerCase().includes(query));
			if (!match) return { error: `No note matching "${name}" found` };
			let content = readFileSync(join(NOTES_DIR, match), 'utf-8');
			content = content.replace(/^---[\s\S]*?---\n/, ''); // strip frontmatter
			return { title: match.replace('.md', ''), content: content.slice(0, 2000) };
		} catch (e) { return { error: String(e) }; }
	},
};

export const saveNoteTool: ToolDefinition = {
	name: 'save_note',
	description: 'Save a note. Use for "take a note", "remember this", "save this".',
	parameters: z.object({
		title: z.string().describe('Short title for the note'),
		content: z.string().describe('The note content'),
		tags: z.string().optional().describe('Comma-separated tags'),
	}),
	execution: 'inline',
	async execute(args) {
		const { title, content, tags } = args as { title: string; content: string; tags?: string };
		const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
		const date = new Date().toISOString().slice(0, 10);
		const tagList = tags ? tags.split(',').map(t => t.trim()) : ['personal'];
		const md = `---\ntitle: ${title}\ndate: ${date}\ntags: [${tagList.join(', ')}]\n---\n\n${content}\n`;
		try {
			writeFileSync(join(NOTES_DIR, `${slug}.md`), md);
			return { status: 'saved', title, slug, path: `notes/${slug}.md` };
		} catch (e) { return { error: String(e) }; }
	},
};

export const deleteNoteTool: ToolDefinition = {
	name: 'delete_note',
	description: 'Delete a specific note by name or slug.',
	parameters: z.object({
		name: z.string().describe('Note name or slug to delete'),
	}),
	execution: 'inline',
	async execute(args) {
		const { name } = args as { name: string };
		try {
			const files = readdirSync(NOTES_DIR).filter(f => f.endsWith('.md'));
			const query = name.toLowerCase().replace(/\s+/g, '-');
			const match = files.find(f => f.toLowerCase().includes(query));
			if (!match) return { error: `No note matching "${name}" found` };
			unlinkSync(join(NOTES_DIR, match));
			return { status: 'deleted', title: match.replace('.md', '') };
		} catch (e) { return { error: String(e) }; }
	},
};

// IMPORTANT: Every tool from chrome-tools, meeting-tools, and recording-tools MUST be in BOTH arrays.
// Tools not registered here are invisible to Gemini — it will hallucinate actions instead
// of calling them (e.g. "I've closed the video" without actually closing it).
export const inlineTools = [
	pressKeyTool, scrollTool, switchTabTool, openUrlTool,
	switchAppTool, captureScreenTool, typeTextTool,
	volumeTool, brightnessTool, clipboardTool,
	cancelTaskTool, toggleTasksTool, getCurrentTimeTool, summonTool, dismissTool,
	joinZoomTool, joinGmeetTool, lookupMeetingIdTool, callContactTool,
	describeScreenTool, clickTool, recordScreenWithNarrationTool, openFileTool,
	playVideoInMeetingTool, pauseVideoInMeetingTool, resumeVideoInMeetingTool,
	screenRecordTool, slideControlTool, fullscreenTool,
	showViewTool, readNoteTool, saveNoteTool, deleteNoteTool, ];

/** Tools available to any caller (including unverified) */
export const anyCallerTools = [
	getCurrentTimeTool,
];

/** Owner-only tools (require isOwner) */
export const ownerOnlyTools = [
	volumeTool, brightnessTool,
	pressKeyTool, scrollTool, switchTabTool, openUrlTool,
	switchAppTool, captureScreenTool, typeTextTool,
	clipboardTool, cancelTaskTool, toggleTasksTool, summonTool, dismissTool,
	joinZoomTool, joinGmeetTool, callContactTool, slideControlTool, fullscreenTool,
	showViewTool, readNoteTool, saveNoteTool, deleteNoteTool,
	describeScreenTool, clickTool, recordScreenWithNarrationTool, openFileTool,
	playVideoInMeetingTool, pauseVideoInMeetingTool, resumeVideoInMeetingTool, screenRecordTool,
];

/** Configurable tools — default to owner-only, can be opened to verified callers */
export const configurableTools = [
	lookupMeetingIdTool,
];
