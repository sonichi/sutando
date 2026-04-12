/**
 * Inline tools — lightweight macOS actions that execute instantly without going through the core agent.
 * Shared between voice-agent.ts and phone conversation-server.ts.
 *
 * Add new tools here and they auto-appear in both voice and phone agents.
 */

import { execSync } from 'node:child_process';
import { writeFileSync, unlinkSync, readdirSync, readFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';

const ts = () => new Date().toLocaleTimeString('en-US', { hour12: false });

// Re-export recording/screen/browser tools from browser-tools
export { describeScreenTool, clickTool, scrollAndDescribeTool, openVideoTool, playVideoTool, pauseVideoTool, resumeVideoTool, replayVideoTool, closeVideoTool, switchTabTool, scrollTool } from './browser-tools.js';
import { describeScreenTool, clickTool, scrollAndDescribeTool, openVideoTool, playVideoTool, pauseVideoTool, resumeVideoTool, replayVideoTool, closeVideoTool, switchTabTool, scrollTool } from './browser-tools.js';

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
	}),
	execution: 'inline',
	async execute(args) {
		const { key, modifiers = [] } = args as { key: string; modifiers?: string[] };
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
		console.log(`${ts()} [PressKey] ${modifiers.length ? modifiers.join('+') + '+' : ''}${key}`);
		return { status: 'pressed', key, modifiers };
	},
};

// --- Browser tools (scroll, switchTab) imported from browser-tools.ts above ---
// They include STT corrections for speech-garbled names and Chrome JS-based scrolling.

// Placeholder to maintain the export shape — the real tools are imported at the top
const _browserToolsImported = { switchTabTool, scrollTool }; // eslint-disable-line @typescript-eslint/no-unused-vars

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

const ZOOM_PMI = process.env.ZOOM_PERSONAL_MEETING_ID ?? '';

const ZOOM_PASSCODE = process.env.ZOOM_PERSONAL_PASSCODE ?? '';

const PHONE_PORT = Number(process.env.PHONE_PORT) || 3100;
const ZOOM_DEFAULT_SHARE_SCREEN = process.env.ZOOM_DEFAULT_SHARE_SCREEN !== 'false'; // default true

export const summonTool: ToolDefinition = {
	name: 'summon',
	description:
		'Summon Sutando\'s screen — opens Zoom with screen sharing so the user can see and control remotely. ' +
		'Use when user says "summon", "share my screen", "start zoom", "let me see your screen". ' +
		'Instant — do NOT use work for this.' +
		(ZOOM_PMI ? ` Default meeting: ${ZOOM_PMI}.` : ''),
	parameters: z.object({
		meetingId: z.string().optional().describe('Zoom meeting ID. Omit for personal room.'),
		passcode: z.string().optional().describe('Passcode. Omit for personal room.'),
		shareScreen: z.boolean().optional().describe('Share screen after joining (default: true)'),
		dialIn: z.boolean().optional().describe('Also dial into the meeting via phone for voice (default: false). Only if user explicitly asks.'),
	}),
	execution: 'inline',
	async execute(args, ctx) {
		const { meetingId, passcode, shareScreen = ZOOM_DEFAULT_SHARE_SCREEN, dialIn = false } = args as { meetingId?: string; passcode?: string; shareScreen?: boolean; dialIn?: boolean };
		const pwd = passcode ?? ZOOM_PASSCODE;
		const cleanId = (meetingId ?? ZOOM_PMI).replace(/\D/g, '');
		if (!cleanId || cleanId.length < 6) return { error: `Invalid meeting ID: "${meetingId}"` };

		try {
			// Check if already in a Zoom meeting
			let alreadyInMeeting = false;
			try {
				const winNames = execSync(`osascript -e 'tell application "System Events" to return name of every window of process "zoom.us"'`, { timeout: 3_000 }).toString().trim();
				alreadyInMeeting = winNames.includes('Zoom Meeting') || winNames.includes('zoom share') || winNames.includes('floating video');
			} catch {}

			if (alreadyInMeeting) {
				console.log(`${ts()} [Summon] Already in a Zoom meeting — skipping join, going straight to screen share`);
			} else {
				// Use the running Zoom app if available — avoids login prompt from zoommtg:// protocol
				const zoomRunning = (() => { try { execSync('pgrep -f "zoom.us"', { timeout: 2_000 }); return true; } catch { return false; } })();

				if (zoomRunning) {
					console.log(`${ts()} [Summon] Zoom running — joining via app`);
					const joinUrl = `https://zoom.us/j/${cleanId}${pwd ? '?pwd=' + pwd : ''}`;
					execSync(`open "${joinUrl}"`, { timeout: 10_000 });
				} else {
					console.log(`${ts()} [Summon] Launching Zoom`);
					let zoomUrl = `zoommtg://zoom.us/join?confno=${cleanId}`;
					if (pwd) zoomUrl += `&pwd=${pwd}`;
					execSync(`open "${zoomUrl}"`, { timeout: 10_000 });
				}

				// Wait for Zoom preview window, then click Join button
				console.log(`${ts()} [Summon] Waiting for Zoom preview window...`);
			await new Promise(r => setTimeout(r, 2000));
			try {
				// Click Join button using cliclick (no Quartz dependency needed)
				const previewCoords = execSync(`osascript -e '
					tell application "zoom.us" to activate
					tell application "System Events"
						tell process "zoom.us"
							repeat with w in windows
								try
									set wName to name of w
									if wName contains "Meeting" or wName contains "Personal" or wName contains "Preview" then
										set wPos to position of w
										set wSize to size of w
										return (item 1 of wPos as text) & "," & (item 2 of wPos as text) & "," & (item 1 of wSize as text) & "," & (item 2 of wSize as text)
									end if
								end try
							end repeat
						end tell
					end tell
					return "not_found"
				'`, { timeout: 10_000 }).toString().trim();
				if (previewCoords !== 'not_found') {
					const [px, py, pw, ph] = previewCoords.split(',').map(Number);
					const jx = px + Math.round(pw * 0.80);
					const jy = py + Math.round(ph * 0.93);
					try { execSync(`cliclick c:${jx},${jy}`, { timeout: 3_000 }); } catch {}
					console.log(`${ts()} [Summon] Join button clicked at (${jx},${jy})`);
				} else {
					console.log(`${ts()} [Summon] No preview window — may have auto-joined`);
				}
			} catch (err) {
				console.log(`${ts()} [Summon] Join click failed (may have auto-joined): ${err}`);
			}
			} // end: not already in meeting

			// If phone dial-in requested, wait for host to join before dialing
			if (dialIn) {
				console.log(`${ts()} [Summon] Waiting for desktop to join as host...`);
				let hostJoined = false;
				for (let i = 0; i < 20; i++) {
					try {
						const winNames = execSync(`osascript -e 'tell application "System Events" to return name of every window of process "zoom.us"'`, { timeout: 3_000 }).toString().trim();
						if (winNames.includes('Zoom Meeting') || winNames.includes('Meeting')) {
							hostJoined = true;
							break;
						}
					} catch {}
					await new Promise(r => setTimeout(r, 1000));
				}
				console.log(`${ts()} [Summon] Host joined: ${hostJoined}`);
				if (hostJoined) {
					console.log(`${ts()} [Summon] Waiting 3s for Zoom server to register host...`);
					await new Promise(r => setTimeout(r, 3000));
				}
			}

			// Phone dial-in only when explicitly requested (not all meetings support it)
			let phoneJoined = false;
			if (dialIn) try {
				const ping = await fetch(`http://localhost:${PHONE_PORT}/health`, { signal: AbortSignal.timeout(2000) });
				if (ping.ok) {
					console.log(`${ts()} [Summon] Phone server available — dialing into meeting for voice`);
					const res = await fetch(`http://localhost:${PHONE_PORT}/meeting`, {
						method: 'POST',
						headers: { 'Content-Type': 'application/json' },
						body: JSON.stringify({ meetingId: cleanId, passcode: pwd, platform: 'zoom' }),
					});
					const data = await res.json() as { callSid?: string; error?: string };
					if (data.callSid) {
						phoneJoined = true;
						console.log(`${ts()} [Summon] Phone call placed: ${data.callSid} — voice agent stays connected until phone joins`);
						// Mute Zoom mic + speaker so voice agent doesn't pick up Zoom audio
						try {
							execSync(`osascript -e '
								tell application "System Events"
									tell process "zoom.us"
										-- Mute mic (Cmd+Shift+A)
										keystroke "a" using {command down, shift down}
									end tell
								end tell
								-- Mute system audio so Zoom speaker doesn\'t bleed into voice agent mic
								set volume output volume 0
							'`, { timeout: 5_000 });
							console.log(`${ts()} [Summon] Zoom mic + system audio muted`);
						} catch { console.log(`${ts()} [Summon] Zoom mute failed`); }
						// Voice agent stays alive — system audio muted prevents Zoom speaker
						// from being picked up by voice agent mic
					} else {
						console.log(`${ts()} [Summon] Phone join failed: ${data.error}`);
					}
				}
			} catch {
				console.log(`${ts()} [Summon] Phone server not available — screen share only`);
			}

			// Handle audio dialogs
			await new Promise(r => setTimeout(r, 1000));
			if (dialIn) {
				// Phone handles audio — close the "Join audio" window
				try {
					execSync(`osascript -e '
						tell application "zoom.us" to activate
						delay 0.5
						tell application "System Events"
							tell process "zoom.us"
								repeat with w in windows
									if name of w is "Join audio" then
										click button 1 of w
										return "closed Join audio"
									end if
								end repeat
							end tell
						end tell
					'`, { timeout: 5_000 });
					console.log(`${ts()} [Summon] Join audio window closed (phone handles audio)`);
				} catch { console.log(`${ts()} [Summon] No Join audio window to close`); }
			} else {
				// No phone dial-in — handle audio dialog
				// Detect if machine has audio input (mic)
				const hasMic = (() => { try { return execSync(`system_profiler SPAudioDataType 2>/dev/null | grep -c "Input"`, { timeout: 5_000 }).toString().trim() !== '0'; } catch { return false; } })();
				console.log(`${ts()} [Summon] Audio input detected: ${hasMic}`);

				// Handle the audio dialogs using cliclick for reliable clicking
				// Zoom uses web-based UI that AppleScript can't access by button name
				for (let attempt = 0; attempt < 3; attempt++) {
					await new Promise(r => setTimeout(r, 1500));
					try {
						// Get the audio dialog window position
						const coords = execSync(`osascript -e '
							tell application "System Events"
								tell process "zoom.us"
									repeat with w in windows
										set wName to name of w
										if wName contains "audio" or wName contains "Audio" then
											set wPos to position of w
											set wSize to size of w
											return (item 1 of wPos as text) & "," & (item 2 of wPos as text) & "," & (item 1 of wSize as text) & "," & (item 2 of wSize as text)
										end if
									end repeat
								end tell
							end tell
							return "none"
						'`, { timeout: 5_000 }).toString().trim();

						if (coords === 'none') {
							console.log(`${ts()} [Summon] No audio dialog found (attempt ${attempt + 1})`);
							break;
						}

						const [x0, y0, w, h] = coords.split(',').map(Number);
						if (hasMic) {
							// Click "Join with Computer Audio" — centered blue button, ~50% across, ~55% down
							const bx = x0 + Math.round(w * 0.5);
							const by = y0 + Math.round(h * 0.55);
							execSync(`cliclick c:${bx},${by}`, { timeout: 3_000 });
							console.log(`${ts()} [Summon] Clicked Join with Computer Audio at (${bx},${by})`);
						} else {
							// No mic — first click dismisses to "Continue without audio?" dialog
							// Then click "Continue" (left button, ~40% across, ~75% down)
							const bx = x0 + Math.round(w * 0.5);
							const by = y0 + Math.round(h * 0.55);
							execSync(`cliclick c:${bx},${by}`, { timeout: 3_000 });
							console.log(`${ts()} [Summon] Clicked audio dialog at (${bx},${by}), checking for confirmation...`);
							await new Promise(r => setTimeout(r, 1000));
							// Check if "continue without audio" confirmation appeared
							try {
								const coords2 = execSync(`osascript -e '
									tell application "System Events"
										tell process "zoom.us"
											repeat with w in windows
												set wName to name of w
												if wName contains "audio" or wName contains "Audio" then
													set wPos to position of w
													set wSize to size of w
													return (item 1 of wPos as text) & "," & (item 2 of wPos as text) & "," & (item 1 of wSize as text) & "," & (item 2 of wSize as text)
												end if
											end repeat
										end tell
									end tell
									return "none"
								'`, { timeout: 5_000 }).toString().trim();
								if (coords2 !== 'none') {
									const [x2, y2, w2, h2] = coords2.split(',').map(Number);
									// "Continue" button is left of center, ~38% across, ~72% down
									const cx = x2 + Math.round(w2 * 0.38);
									const cy = y2 + Math.round(h2 * 0.72);
									execSync(`cliclick c:${cx},${cy}`, { timeout: 3_000 });
									console.log(`${ts()} [Summon] Clicked Continue (no audio) at (${cx},${cy})`);
								}
							} catch {}
						}
						break;
					} catch (e) {
						console.log(`${ts()} [Summon] Audio dialog handling attempt ${attempt + 1} failed: ${e}`);
					}
				}
			}

			// Wait for Zoom meeting window to appear (adaptive, up to 30s)
			console.log(`${ts()} [Summon] Waiting for Zoom meeting window...`);
			let zoomReady = false;
			for (let i = 0; i < 30; i++) {
				try {
					const check = execSync(`osascript -e 'tell application "System Events" to return (count of windows of process "zoom.us")'`, { timeout: 3_000 }).toString().trim();
					if (parseInt(check) > 0) { zoomReady = true; break; }
				} catch {}
				await new Promise(r => setTimeout(r, 1000));
			}

			if (shareScreen && zoomReady) {
				console.log(`${ts()} [Summon] Zoom ready — sharing screen...`);
				try {
					// Screen share: try menu bar first (most reliable), fall back to keyboard shortcut
					execSync(`osascript -e '
						tell application "zoom.us" to activate
						delay 2
						-- Try menu bar: Meeting > Share Screen (most reliable)
						try
							tell application "System Events"
								tell process "zoom.us"
									click menu item "Share Screen" of menu "Meeting" of menu bar 1
								end tell
							end tell
						on error
							-- Fallback: keyboard shortcut
							tell application "System Events"
								tell process "zoom.us"
									keystroke "s" using {command down, shift down}
								end tell
							end tell
						end try
						delay 3
						-- Enable "Share sound" checkbox so computer audio goes through Zoom
						tell application "System Events"
							tell process "zoom.us"
								try
									set soundCB to checkbox "Share sound" of window 1
									if value of soundCB is 0 then click soundCB
								end try
							end tell
						end tell
						delay 0.5
						-- If share dialog appeared, click Share button or press Enter
						tell application "System Events"
							tell process "zoom.us"
								try
									-- Look for Share button in the share dialog
									set shareButtons to buttons of window 1 whose title is "Share"
									if (count of shareButtons) > 0 then
										click item 1 of shareButtons
									else
										keystroke return
									end if
								on error
									keystroke return
								end try
							end tell
						end tell
					'`, { timeout: 15_000 });
					console.log(`${ts()} [Summon] Screen share started`);
					// Audio dialog handling removed — it steals focus from Zoom's
					// screen share, causing it to drop 2-5s after starting (975b8dd).
					// Rely on Zoom's "Automatically join computer audio" setting instead.
					// Mute is handled via Cmd+Shift+A hotkey below.
				} catch (err) {
					console.log(`${ts()} [Summon] Screen share failed: ${err}`);
				}
			} else if (shareScreen) {
				console.log(`${ts()} [Summon] Zoom window not detected after 30s — skipping screen share`);
			}

			// Mute Zoom audio after joining. Zoom presents two choices on entry:
			// "Join Audio" or "Test Speaker & Microphone" (ringtone test). With
			// "Automatically join computer audio" enabled, it skips the dialog and
			// joins audio directly — avoiding the ringtone test. But audio is now
			// live, so we must mute immediately. Phone handles audio via Twilio.
			try {
				execSync(`osascript -e '
					tell application "zoom.us" to activate
					delay 0.5
					tell application "System Events"
						tell process "zoom.us"
							click menu item "Mute audio" of menu "Meeting" of menu bar 1
						end tell
					end tell
				'`, { timeout: 5_000 });
				console.log(`${ts()} [Summon] Muted Zoom audio (phone handles audio)`);
			} catch {
				console.log(`${ts()} [Summon] Could not mute Zoom audio`);
			}

			// Close the zoom.us tab that Chrome opened during join (prevents scroll
			// targeting the wrong tab and reduces user confusion)
			try {
				execSync(`osascript -e '
					tell application "Google Chrome"
						repeat with w in windows
							set tabCount to count of tabs of w
							repeat with i from tabCount to 1 by -1
								set t to tab i of w
								if URL of t contains "zoom.us" then
									close t
								end if
							end repeat
						end repeat
					end tell
				'`, { timeout: 5_000 });
				console.log(`${ts()} [Summon] Closed zoom.us tab(s) in Chrome`);
			} catch {
				console.log(`${ts()} [Summon] No zoom.us tabs to close`);
			}

			return {
				status: 'summoned',
				meetingId: cleanId,
				screenShare: shareScreen,
				phoneAgent: phoneJoined,
				instruction: phoneJoined
					? 'Screen is shared and Sutando is dialing in via phone. Voice stays connected.'
					: 'Zoom meeting joined with screen sharing and computer audio. Voice stays connected.',
			};
		} catch (err) {
			return { error: `Summon failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// Dismiss — leave the current Zoom meeting
export const dismissTool: ToolDefinition = {
	name: 'dismiss',
	description:
		'Leave the current Zoom meeting. The opposite of summon/join_zoom. ' +
		'Use when user says "dismiss", "leave zoom", "end meeting", "leave the call", "hang up zoom".',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		try {
			// 1. Stop screen share (Cmd+Shift+S), 2. Cmd+W leave dialog, 3. Enter confirm
			execSync(`osascript -e '
tell application "zoom.us"
	activate
end tell
delay 0.5
tell application "System Events"
	-- Stop screen share first
	keystroke "s" using {command down, shift down}
	delay 1
	-- Open leave dialog
	keystroke "w" using command down
	delay 1.5
	-- Confirm (Enter hits default "End meeting for all")
	key code 36
end tell'`, { timeout: 15_000 });
			// Verify — if Zoom still has meeting windows, force kill
			try {
				const check = execSync(`osascript -e 'tell application "System Events" to tell process "zoom.us" to return count of windows'`, { timeout: 3_000 }).toString().trim();
				if (parseInt(check) > 2) {
					execSync('killall "zoom.us" 2>/dev/null; sleep 1', { timeout: 5_000 });
					console.log(`${ts()} [Dismiss] Force killed Zoom (${check} windows remaining)`);
				}
			} catch {}
			console.log(`${ts()} [Dismiss] Left Zoom meeting`);
			return { status: 'left_meeting' };
		} catch (err) {
			return { error: `Dismiss failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// Join Zoom via desktop app + computer audio (no screen share)
export const joinZoomTool: ToolDefinition = {
	name: 'join_zoom',
	description: 'Join a Zoom meeting via the desktop app with computer audio. No screen sharing. Use when user says "join the zoom", "join meeting", or provides a Zoom meeting ID.',
	parameters: z.object({
		meetingId: z.string().optional().describe('Zoom meeting ID. Omit for personal room.'),
		passcode: z.string().optional().describe('Meeting passcode. Omit for personal room.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { meetingId, passcode } = args as { meetingId?: string; passcode?: string };
		const pwd = passcode ?? ZOOM_PASSCODE;
		const cleanId = (meetingId ?? ZOOM_PMI).replace(/\D/g, '');
		if (!cleanId || cleanId.length < 6) return { error: `Invalid meeting ID: "${meetingId}"` };

		try {
			// Check if already in meeting
			let alreadyIn = false;
			try {
				const winNames = execSync(`osascript -e 'tell application "System Events" to return name of every window of process "zoom.us"'`, { timeout: 3_000 }).toString().trim();
				alreadyIn = winNames.includes('Zoom Meeting') || winNames.includes('zoom share');
			} catch {}

			if (!alreadyIn) {
				const zoomRunning = (() => { try { execSync('pgrep -f "zoom.us"', { timeout: 2_000 }); return true; } catch { return false; } })();
				if (zoomRunning) {
					execSync(`open "https://zoom.us/j/${cleanId}${pwd ? '?pwd=' + pwd : ''}"`, { timeout: 10_000 });
				} else {
					let zoomUrl = `zoommtg://zoom.us/join?confno=${cleanId}`;
					if (pwd) zoomUrl += `&pwd=${pwd}`;
					execSync(`open "${zoomUrl}"`, { timeout: 10_000 });
				}

				// Click Join button if preview window appears
				await new Promise(r => setTimeout(r, 3000));
				try {
					execSync(`/usr/bin/python3 -c "
import Quartz, subprocess, time
result = subprocess.run(['osascript', '-e', '''
tell application \\\"zoom.us\\\" to activate
tell application \\\"System Events\\\"
    tell process \\\"zoom.us\\\"
        repeat with w in windows
            try
                set wName to name of w
                if wName contains \\\"Meeting\\\" or wName contains \\\"Personal\\\" then
                    set wPos to position of w
                    set wSize to size of w
                    return (item 1 of wPos as text) & \\\",\\\" & (item 2 of wPos as text) & \\\",\\\" & (item 1 of wSize as text) & \\\",\\\" & (item 2 of wSize as text)
                end if
            end try
        end repeat
    end tell
end tell
'''], capture_output=True, text=True)
if result.stdout.strip():
    parts = result.stdout.strip().split(',')
    x, y, w, h = float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3])
    bx = x + w * 0.5
    by = y + h * 0.85
    evt = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, (bx, by), 0)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, evt)
    time.sleep(0.05)
    evt = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, (bx, by), 0)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, evt)
"`, { timeout: 15_000 });
				} catch {}

				// Handle "Continue without audio?" dialog if it appears
				await new Promise(r => setTimeout(r, 1500));
				try {
					execSync(`osascript -e '
						tell application "System Events"
							tell process "zoom.us"
								repeat with w in windows
									if name of w contains "without audio" then
										click button 1 of w
										return "dismissed"
									end if
								end repeat
							end tell
						end tell
					'`, { timeout: 3_000 });
				} catch {}
			}

			// Click "Join with Computer Audio"
			await new Promise(r => setTimeout(r, 1000));
			try {
				execSync(`osascript -e '
					tell application "zoom.us" to activate
					delay 0.5
					tell application "System Events"
						tell process "zoom.us"
							repeat with w in windows
								if name of w is "Join audio" then
									try
										click button "Join with Computer Audio" of w
										return "joined"
									end try
									repeat with b in buttons of w
										if name of b contains "Computer Audio" then
											click b
											return "joined"
										end if
									end repeat
								end if
							end repeat
						end tell
					end tell
				'`, { timeout: 5_000 });
				console.log(`${ts()} [join_zoom] Joined computer audio`);
			} catch {}

			// Handle "audio conference" variant
			await new Promise(r => setTimeout(r, 500));
			try {
				execSync(`osascript -e '
					tell application "System Events"
						tell process "zoom.us"
							repeat with w in windows
								if name of w contains "audio conference" then
									try
										click button "Join with Computer Audio" of w
										return "joined"
									end try
									repeat with b in buttons of w
										if name of b contains "Computer Audio" then
											click b
											return "joined"
										end if
									end repeat
								end if
							end repeat
						end tell
					end tell
				'`, { timeout: 5_000 });
			} catch {}

			return { status: 'joined', meetingId: cleanId, method: 'computer_audio', instruction: 'Joined Zoom with computer audio. No screen sharing.' };
		} catch (err) {
			return { error: `join_zoom failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// Join Google Meet via browser + computer audio
export const joinGmeetTool: ToolDefinition = {
	name: 'join_gmeet',
	description: 'Join a Google Meet meeting via browser with computer audio. Use when user says "join the meet" or provides a Google Meet link/code.',
	parameters: z.object({
		meetingCode: z.string().describe('Google Meet code (e.g., "abc-defg-hij") or full URL'),
	}),
	execution: 'inline',
	async execute(args) {
		const { meetingCode } = args as { meetingCode: string };
		// Extract code from URL or use as-is
		const code = meetingCode.replace(/^https?:\/\/meet\.google\.com\//, '').replace(/\?.*$/, '').trim();
		if (!code) return { error: 'Invalid meeting code' };

		const meetUrl = `https://meet.google.com/${code}`;

		try {
			// Open in Chrome
			execSync(`open -a "Google Chrome" "${meetUrl}"`, { timeout: 10_000 });
			console.log(`${ts()} [join_gmeet] Opened ${meetUrl} in Chrome`);

			// Wait for page to load
			await new Promise(r => setTimeout(r, 5000));

			// Focus the Meet tab and disable camera on preview screen
			try {
				execSync(`osascript -e '
					tell application "Google Chrome"
						set windowList to every window
						repeat with w in windowList
							set tabList to every tab of w
							set tabIdx to 1
							repeat with t in tabList
								if URL of t contains "meet.google.com" then
									set active tab index of w to tabIdx
									set index of w to 1
									activate
									return "focused"
								end if
								set tabIdx to tabIdx + 1
							end repeat
						end repeat
					end tell
				'`, { timeout: 5_000 });
			} catch {}

			// Disable camera by clicking the camera toggle button on the preview
			// The button is in the center-bottom of the preview area
			await new Promise(r => setTimeout(r, 1000));
			try {
				execSync(`/usr/bin/python3 -c "
import Quartz, subprocess, time

# Get Chrome window position and size
result = subprocess.run(['osascript', '-e', '''
tell application \\\"System Events\\\"
    tell process \\\"Google Chrome\\\"
        set winPos to position of front window
        set winSize to size of front window
        return (item 1 of winPos as text) & \\\",\\\" & (item 2 of winPos as text) & \\\",\\\" & (item 1 of winSize as text) & \\\",\\\" & (item 2 of winSize as text)
    end tell
end tell
'''], capture_output=True, text=True, timeout=5)

if result.stdout.strip():
    parts = result.stdout.strip().split(',')
    wx, wy, ww, wh = float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3])
    # Camera button is roughly at 36% across, 68% down in the window
    cx = wx + ww * 0.36
    cy = wy + wh * 0.68
    # Click the camera button
    evt = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, (cx, cy), 0)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, evt)
    time.sleep(0.05)
    evt = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, (cx, cy), 0)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, evt)
    print(f'Clicked camera at ({cx},{cy})')
"`, { timeout: 10_000 });
				console.log(`${ts()} [join_gmeet] Camera button clicked`);
			} catch { console.log(`${ts()} [join_gmeet] Could not click camera button`); }

			await new Promise(r => setTimeout(r, 500));

			// Click Join now button
			try {
				execSync(`osascript -e '
					tell application "Google Chrome"
						tell active tab of front window
							execute javascript "
								const btns = document.querySelectorAll(\\\"button\\\");
								for (const b of btns) {
									if (b.textContent.includes(\\\"Join now\\\") || b.textContent.includes(\\\"Ask to join\\\")) {
										b.click();
										\\\"clicked\\\";
									}
								}
							"
						end tell
					end tell
				'`, { timeout: 10_000 });
				console.log(`${ts()} [join_gmeet] Clicked Join button`);
			} catch {
				await new Promise(r => setTimeout(r, 3000));
				try {
					execSync(`osascript -e '
						tell application "Google Chrome"
							tell active tab of front window
								execute javascript "
									const btns = document.querySelectorAll(\\\"button\\\");
									for (const b of btns) {
										if (b.textContent.includes(\\\"Join now\\\") || b.textContent.includes(\\\"Ask to join\\\")) {
											b.click();
											\\\"clicked\\\";
										}
									}
								"
							end tell
						end tell
					'`, { timeout: 10_000 });
				} catch {}
			}

			return { status: 'joined', meetingCode: code, method: 'browser_audio', instruction: 'Joined Google Meet via browser with computer audio. Camera off.' };
		} catch (err) {
			return { error: `join_gmeet failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// --- Meeting ID lookup (inline, bypasses task bridge) ---

export const lookupMeetingIdTool: ToolDefinition = {
	name: 'lookup_meeting_id',
	description:
		'Look up the Zoom personal meeting ID from the environment. Instant — does NOT go through the task bridge. ' +
		'Use for: "what\'s the Zoom meeting ID", "find the meeting ID", "get the Zoom ID".',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		const meetingId = process.env.ZOOM_PERSONAL_MEETING_ID;
		if (!meetingId) {
			return { error: 'No ZOOM_PERSONAL_MEETING_ID found in environment.' };
		}
		const passcode = process.env.ZOOM_PERSONAL_PASSCODE || process.env.ZOOM_PASSCODE || null;
		console.log(`${ts()} [LookupMeetingId] found: ${meetingId}${passcode ? ' (with passcode)' : ''}`);
		return { meetingId, passcode, source: 'ZOOM_PERSONAL_MEETING_ID from .env', instruction: passcode ? `Meeting ID: ${meetingId}, Passcode: ${passcode}. Include BOTH when telling someone to join.` : `Meeting ID: ${meetingId}. No passcode needed.` };
	},
};

// --- Contact lookup + phone call (inline, bypasses task bridge) ---

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

// IMPORTANT: Every tool defined in browser-tools.ts MUST be added to BOTH arrays below.
// Tools not registered here are invisible to Gemini — it will hallucinate actions instead
// of calling them (e.g. "I've closed the video" without actually closing it).
// Exception: screenRecordTool is intentionally excluded — scroll_and_describe is the full
// recording workflow (narration + REC indicator + subtitles). Registering screenRecordTool
// caused Gemini to pick the bare recorder over scroll_and_describe, breaking narration.
export const inlineTools = [
	pressKeyTool, scrollTool, switchTabTool, openUrlTool,
	switchAppTool, captureScreenTool, typeTextTool,
	volumeTool, brightnessTool, clipboardTool,
	cancelTaskTool, toggleTasksTool, getCurrentTimeTool, summonTool, dismissTool,
	joinZoomTool, joinGmeetTool, lookupMeetingIdTool, callContactTool,
	describeScreenTool, clickTool, scrollAndDescribeTool, openVideoTool, playVideoTool, pauseVideoTool, resumeVideoTool, replayVideoTool, closeVideoTool, slideControlTool, fullscreenTool,
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
	describeScreenTool, clickTool, scrollAndDescribeTool, openVideoTool, playVideoTool, pauseVideoTool, resumeVideoTool, replayVideoTool, closeVideoTool,
];

/** Configurable tools — default to owner-only, can be opened to verified callers */
export const configurableTools = [
	lookupMeetingIdTool,
];
