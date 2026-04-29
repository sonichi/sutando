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

// Re-export recording/screen/browser tools from browser-tools
export { describeScreenTool, clickTool, scrollAndDescribeTool, playVideoTool, pauseVideoTool, resumeVideoTool, replayVideoTool, closeVideoTool, switchTabTool, closeTabTool, scrollTool, openUrlTool } from './browser-tools.js';
import { describeScreenTool, clickTool, scrollAndDescribeTool, screenRecordTool, playVideoTool, pauseVideoTool, resumeVideoTool, replayVideoTool, closeVideoTool, switchTabTool, closeTabTool, scrollTool, openUrlTool } from './browser-tools.js';

// --- File-open tool (moved out of recording-tools — generic file open, optionally fullscreen) ---

export const openFileTool: ToolDefinition = {
	name: 'open_file',
	description:
		'Open a file with the default macOS app. ALWAYS pass an absolute `path`. ' +
		'Use for: "open the file", "open that", "can you open it". ' +
		'If the user says "open the log" or similar, ASK which log they mean (voice-agent, discord-bridge, etc.) — do NOT guess. ' +
		'Known files: "diagnostic tracker" or "diagnostics" = /tmp/phone-diagnostics-tracker.html, ' +
		'"voice diagnostics" = /tmp/voice-diagnostics-tracker.html. ' +
		'Pass `fullscreen=true` if the user wants the file opened in fullscreen — works generically for any file type via Cmd+Ctrl+F to whichever app the OS routed the file to (QuickTime → Present mode, Preview → fullscreen PDF, Chrome → fullscreen page, etc.).',
	parameters: z.object({
		path: z.string().describe('Absolute file path to open.'),
		fullscreen: z.boolean().optional().describe('If true, send Cmd+Ctrl+F to the default app right after opening — generic native-fullscreen toggle, works for any file type (video, PDF, image, web page).'),
	}),
	execution: 'inline',
	async execute(args) {
		const { path, fullscreen } = args as { path: string; fullscreen?: boolean };
		console.log(`${ts()} [OpenFile] called (path=${path || 'none'}, fullscreen=${fullscreen || false})`);
		try {
			if (!path) return { error: 'No path provided. Pass an absolute file path. (For the most recent recording, call play_video — it auto-finds the file.)' };
			const filePath = path.replace(/^~/, process.env.HOME || '');
			if (!existsSync(filePath)) {
				console.log(`${ts()} [OpenFile] path "${filePath}" does not exist`);
				return { error: `File not found: ${filePath}. Do not invent paths — use the exact path returned by the tool that produced the file (e.g. record_screen_with_narration returns subtitled_path/narrated_path/recording_path). For the most recent recording without a known path, call play_video instead.` };
			}
			// execFileSync — no shell interpolation of caller-controlled filePath
			// (same CodeQL js/command-line-injection class as #27).
			// `open <path>` lets macOS's LaunchServices route to the user's default
			// app for that file type. open_file stays generic — no app forcing.
			execFileSync('open', [filePath], { timeout: 5_000 });
			if (fullscreen) {
				// Brief delay so the just-opened app becomes frontmost before
				// the keystroke lands. Cmd+Ctrl+F is the macOS native-fullscreen
				// toggle — every app that supports fullscreen handles it (QT
				// enters Present mode, Preview/Chrome/Pages all enter fullscreen).
				// No app-specific logic — open_file is generic.
				await new Promise(r => setTimeout(r, 1500));
				try {
					execFileSync('/usr/bin/osascript', ['-e', 'tell application "System Events" to keystroke "f" using {command down, control down}'], { timeout: 3_000 });
					console.log(`${ts()} [OpenFile] fullscreen keystroke sent (Cmd+Ctrl+F)`);
				} catch (err) {
					console.log(`${ts()} [OpenFile] fullscreen keystroke failed (non-fatal): ${err}`);
				}
			}
			const size = statSync(filePath).size;
			console.log(`${ts()} [OpenFile] opened ${filePath} (${(size / 1024 / 1024).toFixed(1)}MB)`);
			return {
				status: 'opened',
				path: filePath,
				size_mb: +(size / 1024 / 1024).toFixed(1),
				fullscreen: !!fullscreen,
			};
		} catch (err) {
			return { error: `open_file failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// Re-export meeting tools from meeting-tools
export { summonTool, dismissTool, joinZoomTool, joinGmeetTool, lookupMeetingIdTool, callContactTool } from './meeting-tools.js';
import { summonTool, dismissTool, joinZoomTool, joinGmeetTool, lookupMeetingIdTool, callContactTool } from './meeting-tools.js';

// --- Keyboard tool ---

export const pressKeyTool: ToolDefinition = {
	name: 'press_key',
	description:
		'Press a keyboard key or shortcut in the frontmost app. Use for: "press enter", "press escape", ' +
		'"press tab", "send the message" (Enter), "close the dialog" (Escape), "select all" (Cmd+A), ' +
		'"clear the input" (Cmd+A then Delete). Instant — do NOT use work for simple keystrokes.',
	parameters: z.object({
		key: z.string().describe('Key to press: enter, escape, tab, delete, space, up, down, left, right, or a letter'),
		modifiers: z.array(z.enum(['command', 'shift', 'control', 'option'])).optional().describe('Modifier keys'),
		app: z.string().optional().describe('Target app name (e.g. "QuickTime Player"). If set, activates it first.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { key, modifiers = [], app } = args as { key: string; modifiers?: string[]; app?: string };
		// Activate target app if specified
		if (app) {
			try { execSync(`osascript -e 'tell application "${app}" to activate'`, { timeout: 3_000 }); await new Promise(r => setTimeout(r, 300)); } catch {}
		}
		const keyMap: Record<string, number> = {
			'enter': 36, 'return': 36, 'escape': 53, 'esc': 53, 'tab': 48,
			'delete': 51, 'backspace': 51, 'space': 49,
			'up': 126, 'down': 125, 'left': 123, 'right': 124,
			'a': 0, 'c': 8, 'v': 9, 'x': 7, 'z': 6, 'f': 3, 's': 1, 'w': 13, 'q': 12,
		};
		const keyCode = keyMap[key.toLowerCase()];
		if (keyCode === undefined) {
			// Use keystroke for unknown keys
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
		console.log(`${ts()} [PressKey] ${app ? `(${app}) ` : ''}${modifiers.length ? modifiers.join('+') + '+' : ''}${key}`);
		return { status: 'pressed', key, modifiers, app };
	},
};

// --- Browser tools (scroll, switchTab) imported from browser-tools.ts above ---
// They include STT corrections for speech-garbled names and Chrome JS-based scrolling.

// Placeholder to maintain the export shape — the real tools are imported at the top
const _browserToolsImported = { switchTabTool, scrollTool }; // eslint-disable-line @typescript-eslint/no-unused-vars

// openUrlTool moved to browser-tools.ts — imported via the re-export at top.

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
		// Multi-line or long text: use clipboard paste (keystroke can't handle newlines)
		// Gemini sends literal \n (two chars backslash+n), not actual newlines
		const hasNewline = text.includes('\n') || text.includes('\r') || /\\n/.test(text) || text.length > 80;
		if (hasNewline) {
			try {
				let savedClipboard = '';
				try { savedClipboard = execSync('pbpaste', { encoding: 'utf-8', timeout: 2_000 }); } catch {}
				// Convert literal \n to actual newlines
				const pasteText = text.replace(/\\n/g, '\n').replace(/\\t/g, '\t');
				const tmpClip = `/tmp/sutando-typetext-clip-${Date.now()}.txt`;
				writeFileSync(tmpClip, pasteText);
				execSync(`pbcopy < ${tmpClip}`, { timeout: 2_000 });
				execSync(`osascript -e 'tell application "System Events" to keystroke "v" using command down'`, { timeout: 5_000 });
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
		// Single-line short text: use keystroke
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

// Get what the core agent (Claude Code proactive-loop) is currently doing.
// Lets voice-agent Gemini answer "what are you working on?" truthfully
// instead of guessing. Reads core-status.json written by the core agent.
export const getCoreStatusTool: ToolDefinition = {
	name: 'get_core_status',
	description:
		'Get what the core agent (Claude Code) is currently doing. Use when the user asks ' +
		'"what are you working on", "what are you up to", "are you busy", "anything running", ' +
		'or similar questions about background work. Instant file read.',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		try {
			const repoDir = new URL('..', import.meta.url).pathname.replace(/\/$/, '');
			const statusPath = join(repoDir, 'core-status.json');
			if (!existsSync(statusPath)) {
				return { status: 'idle', description: 'Core agent is not currently running.' };
			}
			const raw = readFileSync(statusPath, 'utf-8');
			const s = JSON.parse(raw) as { status?: string; ts?: number; step?: string };
			const nowSec = Math.floor(Date.now() / 1000);
			const ageSec = typeof s.ts === 'number' ? nowSec - s.ts : null;
			if (s.status === 'running' && ageSec !== null && ageSec < 600) {
				return {
					status: 'running',
					step: s.step || '(no step label)',
					ageSec,
					description: `Core agent is working on: ${s.step || 'an unlabeled task'} (started ${ageSec}s ago).`,
				};
			}
			return { status: 'idle', description: 'Core agent is idle right now.' };
		} catch (e) {
			return { status: 'unknown', description: `Could not read core status: ${e instanceof Error ? e.message : e}` };
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
			// All slide navigation uses DOM manipulation for reliability.
			// IMPORTANT: address slides by VISUAL POSITION (1-indexed) — querySelectorAll('.slide')[N-1] —
			// NOT by id="s"+N. The deck's slide IDs are non-contiguous (s1, s1b, s2, s2b, s3, s4, s5, s6,
			// s65, s7, s8 — 11 slides where the visual-7th is s5, not s7). Using id="s"+N silently misroutes
			// every "go to slide N" cue once any inter-slide (s1b/s2b/s65) is present.
			let js: string;
			if (action === 'goto' && slideNumber) {
				js = `var ss=document.querySelectorAll(\\".slide\\");for(var j=0;j<ss.length;j++){ss[j].classList.remove(\\"active\\")};var idx=${slideNumber}-1;if(idx>=0&&idx<ss.length){ss[idx].classList.add(\\"active\\");document.getElementById(\\"cur\\").textContent=String(${slideNumber})}`;
			} else {
				// next/previous: read current slide number, compute target visual position, set it.
				const dir = action === 'next' ? 1 : -1;
				js = `var cur=parseInt(document.getElementById(\\"cur\\").textContent)||1;var ss=document.querySelectorAll(\\".slide\\");var total=ss.length;var next=((cur-1+${dir}+total)%total)+1;for(var j=0;j<ss.length;j++){ss[j].classList.remove(\\"active\\")};ss[next-1].classList.add(\\"active\\");document.getElementById(\\"cur\\").textContent=String(next)`;
			}
			const script = `tell application "Google Chrome"
	repeat with w in windows
		set tabList to tabs of w
		repeat with i from 1 to count of tabList
			if URL of item i of tabList contains "index-sutando" or URL of item i of tabList contains "localhost:8888" or URL of item i of tabList contains "localhost:7877" or URL of item i of tabList contains "iclr-slides" then
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

// Toggle fullscreen on whatever app the user is currently looking at — generic.
// Picks the frontmost app, skips Zoom (which steals focus during screen share),
// and routes Cmd+Ctrl+F (macOS standard fullscreen) directly to that app's
// process. Process-explicit routing bypasses the keystroke focus race that
// otherwise defeats fullscreen during a Zoom screen-share.
export const fullscreenTool: ToolDefinition = {
	name: 'fullscreen',
	description:
		'Toggle fullscreen on whatever app the user is currently looking at — generic, works for the slide deck (Chrome) AND any other window (QuickTime, VSCode, Slack, etc). Skips Zoom when it has focus during screen-share. Use when user says "fullscreen", "enter fullscreen", "exit fullscreen", "make it full screen", "full screen". DO NOT call open_file with fullscreen=true to enter fullscreen on an already-open video — call this tool instead.',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		try {
			const script = `
tell application "System Events"
	-- Find the user's actual focus target. During Zoom screen share, Zoom's
	-- floating control bar can be the frontmost UI even when the user is
	-- interacting with a different window — skip Zoom and pick the next
	-- visible app the user was using.
	set frontApp to name of first application process whose frontmost is true
	if frontApp contains "zoom" then
		set candidates to name of every application process whose visible is true and (name does not contain "zoom") and background only is false
		if (count of candidates) > 0 then
			set frontApp to item 1 of candidates
		end if
	end if
end tell
tell application frontApp to activate
delay 0.2
-- Cmd+Ctrl+F is the macOS standard fullscreen keystroke and works for every
-- native + browser window (QuickTime, Chrome, VSCode, Slack, Mail, etc).
-- Route through the target process explicitly — that bypasses the focus
-- race that defeats a plain System Events keystroke when Zoom or another
-- overlay app holds keyboard focus through the activate.
tell application "System Events"
	tell process frontApp
		keystroke "f" using {command down, control down}
	end tell
end tell
return frontApp`;
			const target = execFileSync('/usr/bin/osascript', ['-e', script], { timeout: 5_000 }).toString().trim();
			console.log(`${ts()} [Fullscreen] Toggled ${target}`);
			return { status: 'toggled', target };
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

// IMPORTANT: Every tool defined in browser-tools.ts MUST be added to BOTH arrays below.
// Tools not registered here are invisible to Gemini — it will hallucinate actions instead
// of calling them (e.g. "I've closed the video" without actually closing it).
// screenRecordTool re-added — descriptions now clearly distinguish plain recording
// ("start recording") from narrated demo ("record for N seconds").
//
// Duplicate-name guard: gemini-3.1-flash-live-preview rejects duplicate tool
// names at bidiGenerateContent setup with ws code 1011 "Internal error
// encountered"; gemini-2.5 silently tolerated dupes (exact pattern of the
// Apr 9 migration bug #2 + an Apr 22-23 re-occurrence after a local skill
// re-registered an existing name). Throws loudly at module load so any
// future collision is caught in seconds, not after the next voice-agent
// restart fails to connect.
function assertUniqueToolNames(tools: ToolDefinition[]): ToolDefinition[] {
	const counts = new Map<string, number>();
	for (const t of tools) counts.set(t.name, (counts.get(t.name) ?? 0) + 1);
	const dupes = [...counts.entries()].filter(([, n]) => n > 1).map(([name]) => name);
	if (dupes.length > 0) {
		throw new Error(
			`[inline-tools] duplicate tool name(s): ${dupes.join(', ')}. ` +
			`Gemini 3.1 Live rejects dup names at setup (1011). ` +
			`Rename one side and retry.`
		);
	}
	return tools;
}

// Load tools from any skill that has a `manifest.json` with "enabled": true.
// Manifest shape:
//   { "name": "skill-name", "enabled": true, "access_tier": "owner",
//     "tools": "./tools.ts", "config": { "ENV_VAR": "value" } }
// - "enabled": false (or missing) → skill skipped
// - "tools" path → dynamic-imported, expects `export const tools: ToolDefinition[]`
// - "config" entries → surfaced to process.env (only set if not already defined)
// Originally added 2026-04-20, accidentally stripped by PR #505 (dup-name guard
// commit). Restored 2026-04-25 after the iclr-highlight skill went silent on
// the autonav cue — voice-agent had no way to call highlight_slide because the
// skill's tools were never being merged into inlineTools.
async function loadSkillManifestTools(): Promise<ToolDefinition[]> {
	// Scan the public-repo `skills/` dir AND the optional private skills dir
	// pointed to by `$SUTANDO_PRIVATE_DIR/skills/` (e.g.
	// `~/.sutando-memory-sync/skills/`). The private dir lets users keep
	// personal tooling with real per-file git history outside the public repo.
	// Order: public first, then private — same-name skills loaded from
	// private take precedence (last one wins via the dup-name guard below if
	// any; in practice they should be uniquely named).
	const dirsToScan: string[] = [join(process.cwd(), 'skills')];
	const privateRoot = process.env.SUTANDO_PRIVATE_DIR;
	if (privateRoot) {
		const expanded = privateRoot.replace(/^~/, process.env.HOME || '');
		dirsToScan.push(join(expanded, 'skills'));
	}
	const out: ToolDefinition[] = [];
	for (const skillsDir of dirsToScan) {
		if (!existsSync(skillsDir)) continue;
		let dirs: string[];
		try {
			dirs = readdirSync(skillsDir).filter(n => {
				try { return statSync(join(skillsDir, n)).isDirectory(); } catch { return false; }
			});
		} catch { continue; }
		for (const dirName of dirs) {
			const manifestPath = join(skillsDir, dirName, 'manifest.json');
			if (!existsSync(manifestPath)) continue;
			let manifest: { enabled?: boolean; tools?: string; config?: Record<string, string>; name?: string };
			try {
				manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
			} catch (err) {
				console.warn(`[skill-loader] bad manifest ${dirName} in ${skillsDir}:`, err instanceof Error ? err.message : err);
				continue;
			}
			if (!manifest.enabled) continue;
			for (const [k, v] of Object.entries(manifest.config || {})) {
				if (process.env[k] === undefined) process.env[k] = v;
			}
			if (!manifest.tools) continue;
			const toolsPath = join(skillsDir, dirName, manifest.tools.replace(/^\.\//, ''));
			try {
				// @ts-ignore — dynamic relative import resolved at runtime by tsx
				const mod = await import(toolsPath);
				if (Array.isArray(mod.tools)) {
					out.push(...mod.tools);
					console.log(`[skill-loader] loaded ${mod.tools.length} tool(s) from ${manifest.name || dirName} (${skillsDir})`);
				}
			} catch (err) {
				console.warn(`[skill-loader] failed to import ${dirName}/${manifest.tools} from ${skillsDir}:`, err instanceof Error ? err.message : err);
			}
		}
	}
	return out;
}
const personalTools = await loadSkillManifestTools();

export const inlineTools = assertUniqueToolNames([
	pressKeyTool, scrollTool, switchTabTool, closeTabTool, openUrlTool,
	switchAppTool, captureScreenTool, typeTextTool,
	volumeTool, brightnessTool, clipboardTool,
	cancelTaskTool, toggleTasksTool, getCurrentTimeTool, getCoreStatusTool, summonTool, dismissTool,
	joinZoomTool, joinGmeetTool, lookupMeetingIdTool, callContactTool,
	describeScreenTool, clickTool, scrollAndDescribeTool, screenRecordTool, openFileTool, playVideoTool, pauseVideoTool, resumeVideoTool, replayVideoTool, closeVideoTool, slideControlTool, fullscreenTool,
	showViewTool, readNoteTool, saveNoteTool, deleteNoteTool,
	...personalTools ]);

/** Tools available to any caller (including unverified) */
export const anyCallerTools = [
	getCurrentTimeTool,
	getCoreStatusTool,
];

/** Owner-only tools (require isOwner) */
export const ownerOnlyTools = [
	volumeTool, brightnessTool,
	pressKeyTool, scrollTool, switchTabTool, closeTabTool, openUrlTool,
	switchAppTool, captureScreenTool, typeTextTool,
	clipboardTool, cancelTaskTool, toggleTasksTool, summonTool, dismissTool,
	joinZoomTool, joinGmeetTool, callContactTool, slideControlTool, fullscreenTool,
	showViewTool, readNoteTool, saveNoteTool, deleteNoteTool,
	describeScreenTool, clickTool, scrollAndDescribeTool, screenRecordTool, openFileTool, playVideoTool, pauseVideoTool, resumeVideoTool, replayVideoTool, closeVideoTool,
];

/** Configurable tools — default to owner-only, can be opened to verified callers */
export const configurableTools = [
	lookupMeetingIdTool,
];
