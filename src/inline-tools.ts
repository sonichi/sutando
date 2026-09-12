/**
 * Inline tools — lightweight macOS actions that execute instantly without going through the core agent.
 * Shared between voice-agent.ts and phone conversation-server.ts.
 *
 * Add new tools here and they auto-appear in both voice and phone agents.
 */

import { execFileSync } from 'node:child_process';
import { writeFileSync, unlinkSync, readdirSync, readFileSync, existsSync, statSync, mkdirSync } from 'node:fs';
import { join, extname, dirname, delimiter } from 'node:path';
import { fileURLToPath } from 'node:url';
import { z } from 'zod';
import { requirePython } from './python-binary.js';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import { resolveWorkspace, statusPath, statusReadPath } from './workspace_default.js';
import { presenterModeActive } from './presenter-mode.js';

// Tasks/, results/, state/, dynamic-content.json are per-user runtime state
// — live under $SUTANDO_WORKSPACE. Pre-fix, sites below resolved against
// `process.cwd()` which only happened to match the workspace when the
// voice-agent was launched from the repo with SUTANDO_WORKSPACE unset.
// resolveWorkspace() is the canonical TS helper introduced in #821.
const WORKSPACE_DIR = resolveWorkspace();

// Gate slide-control + fullscreen on presenter-mode.sentinel.
// Issue #1171: registering these globally causes Gemini to fire them on greetings.
// Expiry-aware (#2501 policy twin): bare existsSync re-activated the gate
// forever after a talk window lapsed without `presenter-mode.sh stop`, because
// a naturally-expired sentinel stays on disk. Still evaluated once at module
// load — the per-session registration semantics are unchanged.
const _presenterActive = presenterModeActive(WORKSPACE_DIR);

// Code-adjacent paths (skills/, etc.) ship with the repo checkout, NOT the
// workspace. Compute REPO_ROOT from this file's URL so the resolution
// survives any cwd drift at startup. Used by the skill-loader below.
const REPO_ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

const ts = () => new Date().toLocaleTimeString('en-US', { hour12: false });

// Re-export recording/screen/browser tools from browser-tools
export { describeScreenTool, clickTool, scrollAndDescribeTool, playVideoTool, pauseVideoTool, resumeVideoTool, replayVideoTool, closeVideoTool, switchTabTool, closeTabTool, scrollTool, openUrlTool } from './browser-tools.js';
import { keystrokeOutcome } from './osascript-setup-hint.js';
import { describeScreenTool, clickTool, pointAtTool, scrollAndDescribeTool, screenRecordTool, playVideoTool, pauseVideoTool, resumeVideoTool, replayVideoTool, closeVideoTool, switchTabTool, closeTabTool, scrollTool, openUrlTool } from './browser-tools.js';

// Vision: one-shot frame + start/stop live screen-to-Gemini video.
export { sendVisionFrameTool, startVisionTool, stopVisionTool } from './vision-tools.js';
import { sendVisionFrameTool, startVisionTool, stopVisionTool } from './vision-tools.js';

// Active artifact cache — load a file once, query repeatedly without task-bridge round-trips.
export { setActiveArtifactTool, queryActiveArtifactTool, clearActiveArtifactTool, clearActiveArtifact } from './artifact-cache-tools.js';
export { switchVoiceConfigTool } from './voice-config-switch.js';
import { switchVoiceConfigTool } from './voice-config-switch.js';
import { setActiveArtifactTool, queryActiveArtifactTool, clearActiveArtifactTool } from './artifact-cache-tools.js';

// --- File-open tool (moved out of recording-tools — generic file open, optionally fullscreen) ---

export const openFileTool: ToolDefinition = {
	name: 'open_file',
	description:
		'Open a file with macOS. ALWAYS pass an absolute `path` (or one starting with $VAR / ~). ' +
		'Use for: "open the file", "open that", "can you open it". ' +
		'If the user says "open the log" or similar, ASK which log they mean (voice-agent, discord-bridge, etc.) — do NOT guess. ' +
		'Known files: "diagnostic tracker" or "diagnostics" = /tmp/phone-diagnostics-tracker.html, ' +
		'"voice diagnostics" = /tmp/voice-diagnostics-tracker.html, ' +
		'"voice context" / "the voice context file" / "the active context" = $SUTANDO_MEMORY_DIR/voice-contexts/<active>.txt where <active> is the trimmed contents of $SUTANDO_MEMORY_DIR/voice-contexts/active (legacy users may have $SUTANDO_PRIVATE_DIR set instead — either expands). Pass it with the env-var expanded by you, or as $SUTANDO_MEMORY_DIR/voice-contexts/<active>.txt — both work. ' +
		'Pass `app` when the user names a specific app ("open with Sublime Text", "open the SQLite db in TablePlus") OR when recent conversation makes the intended app clear (e.g. user just said "I\'ll review this in VS Code"). Without `app`, macOS uses its default handler for that file type — leave unset when the default is fine. ' +
		'Pass `fullscreen=true` if the user wants the file opened in fullscreen — works generically for any file type via Cmd+Ctrl+F to whichever app the OS routed the file to (QuickTime → Present mode, Preview → fullscreen PDF, Chrome → fullscreen page, etc.).',
	parameters: z.object({
		path: z.string().describe('Absolute file path to open.'),
		app: z.string().optional().describe('Optional app name (e.g. "Sublime Text", "VS Code", "TablePlus") to open the file with. If omitted, macOS uses its default handler for the file type. Set this when the user names an app explicitly OR recent conversation makes the intended app clear; otherwise leave unset.'),
		fullscreen: z.boolean().optional().describe('If true, send Cmd+Ctrl+F to the default app right after opening — generic native-fullscreen toggle, works for any file type (video, PDF, image, web page).'),
	}),
	execution: 'inline',
	async execute(args) {
		const { path, app, fullscreen } = args as { path: string; app?: string; fullscreen?: boolean };
		console.log(`${ts()} [OpenFile] called (path=${path || 'none'}, app=${app || 'default'}, fullscreen=${fullscreen || false})`);
		try {
			if (!path) return { error: 'No path provided. Pass an absolute file path. (For the most recent recording, call play_video — it auto-finds the file.)' };
			// Expand $VAR / ${VAR} env-var references and ~ in the path so Sutando
			// can pass paths like "$SUTANDO_MEMORY_DIR/voice-contexts/X.txt"
			// without us hardcoding a fallback root. Track any unset variables so
			// we can surface them as a clear diagnostic rather than letting the
			// silently-empty substitution flow through to a generic "file not
			// found" error below.
			const unresolvedVars: string[] = [];
			const filePath = path
				.replace(/^~/, process.env.HOME || '')
				.replace(/\$\{([A-Z_][A-Z0-9_]*)\}|\$([A-Z_][A-Z0-9_]*)/g, (_, a, b) => {
					const name = a || b;
					const val = process.env[name];
					if (val === undefined) unresolvedVars.push(name);
					return val || '';
				});
			if (unresolvedVars.length > 0) {
				console.log(`${ts()} [OpenFile] path "${path}" has unset env var(s): ${unresolvedVars.join(', ')}`);
				return { error: `Unresolved env var(s) in path: ${unresolvedVars.join(', ')}. Set them before calling open_file, or pass a fully-expanded absolute path.` };
			}
			if (!existsSync(filePath)) {
				console.log(`${ts()} [OpenFile] path "${filePath}" does not exist`);
				return { error: `File not found: ${filePath}. Do not invent paths — use the exact path returned by the tool that produced the file (e.g. record_screen_with_narration returns subtitled_path/narrated_path/recording_path). For the most recent recording without a known path, call play_video instead.` };
			}
			// execFileSync — no shell interpolation of caller-controlled filePath
			// or caller-controlled app name (same CodeQL js/command-line-injection
			// class as #27). Both are passed as separate argv entries, never spliced
			// into a shell string.
			//
			// Resolution per issue #560:
			//   1. Explicit `app` arg → `open -a <app> <path>`
			//   2. No `app` → `open <path>` (macOS LaunchServices picks default)
			// Contextual inference (rule 2 from issue) is the model's job — Gemini
			// reads the conversation and decides whether to pass `app`. The tool
			// only honors what it's told.
			const openArgs = app ? ['-a', app, filePath] : [filePath];
			execFileSync('open', openArgs, { timeout: 5_000 });
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
			// If we just opened a video file, write the path to the playback-path
			// marker so the existing video-control tools (pause_video / replay_video
			// / etc.) work against the open_file-opened video. Without this, those
			// tools fall back to findRecording() which only finds phone-call
			// recordings — so any "pause" / "replay" cue after open_file returns
			// "No video to play". This makes the existing tool surface QuickTime-
			// aware, regardless of whether the video came from a phone-call
			// recording or open_file.
			const ext = extname(filePath).toLowerCase();
			if (['.mp4', '.mov', '.webm', '.m4v'].includes(ext)) {
				try {
					const fs = await import('node:fs');
					fs.writeFileSync('/tmp/sutando-playback-path', filePath);
					console.log(`${ts()} [OpenFile] wrote playback-path for video-control tools`);
				} catch {}
			}
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

// Zoom tools (summon, dismiss, join_zoom) are NOT imported here — they live in
// the manifest-loaded skill `skills/zoom/` (manifest.json + tools.ts) and reach
// `inlineTools` / `ownerOnlyTools` via the loadSkillManifestTools() path below
// (#976 conformance). Core no longer has a compile-time dependency on the skill,
// so it is genuinely optional.
// Re-export remaining meeting tools
export { joinGmeetTool, lookupMeetingIdTool, callContactTool } from './meeting-tools.js';
import { joinGmeetTool, lookupMeetingIdTool, callContactTool } from './meeting-tools.js';

// --- Keyboard tool ---

export const pressKeyTool: ToolDefinition = {
	name: 'press_key',
	description:
		'Press a keyboard key or shortcut in the frontmost app. Use for: "press enter", "press escape", ' +
		'"press tab", "send the message" (Enter), "close the dialog" (Escape), "select all" (Cmd+A), ' +
		'"clear the input" (Cmd+A then Delete). Instant — do NOT use work for simple keystrokes. ' +
		'Call this ONLY on an explicit key/shortcut request. NEVER when the user is addressing another ' +
		'assistant or device ("Hey Google", "Alexa", "Siri"), and never on filler or garbled speech — ' +
		'a spurious keystroke acts on whatever app is frontmost. When unsure, fire nothing.',
	parameters: z.object({
		key: z.string().describe('Key to press: enter, escape, tab, delete, space, up, down, left, right, or a letter'),
		modifiers: z.array(z.enum(['command', 'shift', 'control', 'option'])).optional().describe('Modifier keys'),
		app: z.string().optional().describe('Target app name (e.g. "QuickTime Player"). If set, activates it first.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { key, modifiers = [], app } = args as { key: string; modifiers?: string[]; app?: string };
		// Activate target app if specified. Escape `app` before embedding
		// in the AppleScript string literal — without this, a value like
		// `"; do shell script "rm -rf ~"; tell application "Finder` would
		// break out of `tell application "..."` and run arbitrary
		// AppleScript (and AppleScript can `do shell script`, so this is
		// arbitrary code execution from a tool-call argument). Same
		// escape pattern as `safeKey` below and `safeApp` in switchAppTool.
		if (app) {
			const safeApp = app.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
			try { execFileSync('osascript', ['-e', `tell application "${safeApp}" to activate`], { timeout: 3_000 }); await new Promise(r => setTimeout(r, 300)); } catch {}
		}
		const keyMap: Record<string, number> = {
			'enter': 36, 'return': 36, 'escape': 53, 'esc': 53, 'tab': 48,
			'delete': 51, 'backspace': 51, 'space': 49,
			'up': 126, 'down': 125, 'left': 123, 'right': 124,
			// Common voice-spoken aliases — without these, the fallthrough to
			// `keystroke "<key>"` types the literal string into the focused
			// field instead of pressing the arrow. Observed 2026-05-13 voice
			// call: Gemini called press_key(key="downarrow"); nothing scrolled.
			'uparrow': 126, 'downarrow': 125, 'leftarrow': 123, 'rightarrow': 124,
			'arrowup': 126, 'arrowdown': 125, 'arrowleft': 123, 'arrowright': 124,
			'a': 0, 'c': 8, 'v': 9, 'x': 7, 'z': 6, 'f': 3, 's': 1, 'w': 13, 'q': 12,
		};
		const keyCode = keyMap[key.toLowerCase()];
		if (keyCode === undefined) {
			// Use keystroke for unknown keys
			const modStr = modifiers.length ? ` using {${modifiers.map(m => m + ' down').join(', ')}}` : '';
			// Escape for AppleScript string literal — no shell layer needed with execFileSync.
			const safeKey = key.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
			try {
				execFileSync('osascript', ['-e', `tell application "System Events" to keystroke "${safeKey}"${modStr}`], { timeout: 3_000 });
			} catch (err) {
				return keystrokeOutcome('press_key', err instanceof Error ? err.message : String(err));
			}
		} else {
			const modStr = modifiers.length ? ` using {${modifiers.map(m => m + ' down').join(', ')}}` : '';
			try {
				execFileSync('osascript', ['-e', `tell application "System Events" to key code ${keyCode}${modStr}`], { timeout: 3_000 });
			} catch (err) {
				return keystrokeOutcome('press_key', err instanceof Error ? err.message : String(err));
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
		// Escape for AppleScript string literals — no shell layer needed with execFileSync.
		const safeApp = app.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
		const processName = (PROCESS_NAMES[app] ?? app).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
		try {
			execFileSync('osascript', [
				'-e', `tell application "${safeApp}" to activate`,
				'-e', `tell application "System Events" to set frontmost of process "${processName}" to true`,
			], { timeout: 10_000 });
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
			const _capTok = readCaptureToken();
			const res = await fetch(`http://localhost:7845/capture${query}`, _capTok ? { headers: { 'X-Sutando-Capture-Token': _capTok } } : {});
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
		'Type text into the currently focused field. Use for: "type hello", "enter my email". Instant. ' +
		'Pass `mode` to control how the text lands relative to existing content. `mode: "replace_all"` selects ' +
		'everything in the field first, then writes the new text — pick this for in-place edits (rewrite the ' +
		'draft, shorten, add words to the existing paragraph; compute the FULL edited version and call with ' +
		'replace_all). `mode: "append"` collapses any selection to its end before writing — pick this when the ' +
		'user says "add", "append", "type at the end". `mode: "at_caret"` (default) inserts at the current ' +
		'caret position — pick this for fill-in-a-blank ("type hello", "enter my email"). The legacy ' +
		'`append: true` is still honored and treated as `mode: "append"` for backward compat.',
	parameters: z.object({
		text: z.string().describe('The text to type. For mode="replace_all" this is the FULL new content of the field (compute the edited version locally before calling).'),
		mode: z.enum(['replace_all', 'append', 'at_caret']).optional().describe('How the text lands: "replace_all" selects all + writes new content (in-place edits); "append" collapses selection to end + writes (add-to-end); "at_caret" (default) inserts at caret.'),
		append: z.boolean().optional().describe('Deprecated — pass `mode: "append"` instead. Still honored: if true (and mode is unset), behaves like mode="append".'),
	}),
	execution: 'inline',
	async execute(args) {
		const a = args as { text: string; mode?: 'replace_all' | 'append' | 'at_caret'; append?: boolean };
		const text = a.text;
		// Resolve effective mode. Explicit `mode` wins; legacy `append: true` → 'append';
		// otherwise default to 'at_caret' (the long-standing default behavior pre-2026-06-01).
		const mode: 'replace_all' | 'append' | 'at_caret' = a.mode ?? (a.append ? 'append' : 'at_caret');
		// Multi-line, long, or non-ASCII text: use clipboard paste.
		// AppleScript's `keystroke "..."` routes through virtual-key codes that
		// can't represent characters outside the basic ASCII typing range —
		// em-dashes (U+2014), curly quotes (U+2018-U+201D), and emoji all get
		// corrupted (UTF-8 bytes reinterpreted as Mac Roman → e.g. 🤖 mojibake,
		// — → "‚Äî"). The paste branch round-trips through the system pasteboard
		// which preserves bytes. Per Chi 2026-05-13 frustration with emoji corruption.
		// Gemini sends literal \n (two chars backslash+n), not actual newlines.
		const hasNonAscii = /[^\x00-\x7f]/.test(text);
		const needsPaste = text.includes('\n') || text.includes('\r') || /\\n/.test(text) || text.length > 80 || hasNonAscii;
		if (needsPaste) {
			try {
				// Force UTF-8 locale for the child shell — voice-agent runs under
				// launchd which doesn't inherit terminal LANG/LC_CTYPE, so the
				// default POSIX/C locale would make `pbcopy < file` treat
				// multi-byte UTF-8 sequences as garbled single-byte and put
				// "??" on the pasteboard instead of 🤖. Pipe via stdin (input:)
				// to bypass shell redirection entirely. Per Chi 2026-05-13 (PR #660
				// follow-up: the first fix routed emoji to paste-branch but
				// pbcopy still mangled bytes in launchd context).
				const utf8Env = { ...process.env, LANG: 'en_US.UTF-8', LC_ALL: 'en_US.UTF-8' };
				let savedClipboard = '';
				try { savedClipboard = execFileSync('pbpaste', [], { encoding: 'utf-8', timeout: 2_000, env: utf8Env }); } catch {}
				// Convert literal \n to actual newlines
				const pasteText = text.replace(/\\n/g, '\n').replace(/\\t/g, '\t');
				execFileSync('pbcopy', [], { input: pasteText, encoding: 'utf-8', timeout: 2_000, env: utf8Env });
				// replace_all: emit Cmd+A first so the subsequent Cmd+V replaces the
				// entire field content (closes the selection-state ambiguity that
				// 'replace' default had — relied on caller to have selected).
				// append: collapse selection to its end via Right-arrow before Cmd+V.
				// at_caret (default): paste at current caret / replace current selection per macOS Cmd+V semantics.
				if (mode === 'replace_all') {
					execFileSync('osascript', ['-e', 'tell application "System Events" to keystroke "a" using command down'], { timeout: 3_000, env: utf8Env });
				} else if (mode === 'append') {
					execFileSync('osascript', ['-e', 'tell application "System Events" to key code 124'], { timeout: 3_000, env: utf8Env });
				}
				execFileSync('osascript', ['-e', 'tell application "System Events" to keystroke "v" using command down'], { timeout: 5_000, env: utf8Env });
				execFileSync('sleep', ['0.3']);
				if (savedClipboard) {
					execFileSync('pbcopy', [], { input: savedClipboard, encoding: 'utf-8', timeout: 2_000, env: utf8Env });
				}
				console.log(`${ts()} [TypeText] pasted (multi-line, mode=${mode}): ${text.slice(0, 40)}...`);
				return { status: 'typed', text };
			} catch (err) {
				return keystrokeOutcome('Paste', err instanceof Error ? err.message : String(err));
			}
		}
		// Single-line short text: use keystroke
		// Escape for AppleScript string literal only — no shell layer needed with execFileSync.
		const safeText = text.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
		try {
			// replace_all: Cmd+A first so the keystroke replaces the entire field.
			// append: collapse selection to its end via Right-arrow before typing.
			// at_caret (default): keystroke at caret / replaces current selection per System Events behavior.
			if (mode === 'replace_all') {
				execFileSync('osascript', ['-e', 'tell application "System Events" to keystroke "a" using command down'], { timeout: 3_000 });
			} else if (mode === 'append') {
				execFileSync('osascript', ['-e', 'tell application "System Events" to key code 124'], { timeout: 3_000 });
			}
			execFileSync('osascript', ['-e', `tell application "System Events" to keystroke "${safeText}"`], { timeout: 5_000 });
			console.log(`${ts()} [TypeText] typed (mode=${mode}): ${text.slice(0, 40)}`);
			return { status: 'typed', text };
		} catch (err) {
			return keystrokeOutcome('Type', err instanceof Error ? err.message : String(err));
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
				execFileSync('osascript', ['-e', 'set volume with output muted'], { timeout: 5_000 });
				console.log(`${ts()} [Volume] muted`);
				return { status: 'muted' };
			}
			if (mute === false) {
				execFileSync('osascript', ['-e', 'set volume without output muted'], { timeout: 5_000 });
				console.log(`${ts()} [Volume] unmuted`);
				return { status: 'unmuted' };
			}
			if (level !== undefined) {
				// Gemini sometimes passes 0-1 instead of 0-100 — normalize
				const normalizedLevel = level <= 1 && level > 0 ? Math.round(level * 100) : Math.round(level);
				execFileSync('osascript', ['-e', `set volume output volume ${normalizedLevel}`], { timeout: 5_000 });
				console.log(`${ts()} [Volume] set to ${normalizedLevel}%`);
				return { status: 'set', level: normalizedLevel };
			}
			return { error: 'Specify level (0-100) or mute (true/false)' };
		} catch (err) {
			return { error: `Volume failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// The smooth ramp lands within a rounding step of the request; wider than that
// means the display did not take the change.
const BRIGHTNESS_TOLERANCE_PCT = 2;

// Identifies the target display before choosing a mechanism: DisplayServices
// drives the built-in panel only, and external panels need DDC over I2C. Prints
// "<kind> <level>" so the caller can report which path ran; exits 2 for an
// external display, which it cannot drive itself.
const BRIGHTNESS_PY = `
import ctypes, ctypes.util, sys, time
cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
cg.CGMainDisplayID.restype = ctypes.c_uint32
cg.CGDisplayIsBuiltin.argtypes = [ctypes.c_uint32]
did = cg.CGMainDisplayID()
if not cg.CGDisplayIsBuiltin(did):
    # Report how many displays are attached: the caller cannot safely pick one
    # for DDC when several are, so it refuses rather than driving the wrong panel.
    n = ctypes.c_uint32()
    arr = (ctypes.c_uint32 * 16)()
    cg.CGGetActiveDisplayList(16, arr, ctypes.byref(n))
    print("external", n.value)
    sys.exit(2)
ds = ctypes.CDLL("/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices")
ds.DisplayServicesGetBrightness.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_float)]
ds.DisplayServicesSetBrightnessSmooth.argtypes = [ctypes.c_uint32, ctypes.c_float]
before = ctypes.c_float()
if ds.DisplayServicesGetBrightness(did, ctypes.byref(before)) != 0:
    sys.exit(1)
if ds.DisplayServicesSetBrightnessSmooth(did, ctypes.c_float(float(sys.argv[1]) - before.value)) != 0:
    sys.exit(1)
time.sleep(0.6)
after = ctypes.c_float()
if ds.DisplayServicesGetBrightness(did, ctypes.byref(after)) != 0:
    sys.exit(1)
print("builtin", after.value, before.value)
`;

/**
 * DDC brightness for a single external panel, 0-100. Both tools address a
 * display by their own index, which no CoreGraphics id maps onto, so this
 * refuses when more than one display is attached rather than guessing which
 * panel `display 1` denotes.
 *
 * Reads the level back where the tool supports it. DDC is widely half-implemented
 * in monitor firmware, so a command can be accepted and ignored — reporting the
 * REQUESTED level here would reproduce the silent-success bug this file exists to
 * fix, on the one path that cannot be hardware-tested. When no readback is
 * available the result says `requested`, never `set`.
 */
type ExternalResult =
	| { status: 'set' | 'partial' | 'requested'; method: string; level: number; requested: number; verified: boolean }
	| { error: string };

function setExternalBrightness(level: number, displayCount: number): ExternalResult {
	if (displayCount > 1) {
		return { error: `${displayCount} displays attached — refusing to guess which external panel to dim. Set it on the monitor, or attach one display.` };
	}
	const failures: string[] = [];
	for (const [bin, setArgs, getArgs] of [
		['m1ddc', ['display', '1', 'set', 'luminance', String(level)], ['display', '1', 'get', 'luminance']],
		['ddcctl', ['-d', '1', '-b', String(level)], null],
	] as const) {
		try {
			execFileSync(bin, [...setArgs], { timeout: 5_000, stdio: ['ignore', 'ignore', 'pipe'] });
		} catch (err) {
			// ENOENT (absent) and a non-zero exit (present but refused) are different
			// diagnoses; collapsing them tells a user who HAS the tool to install it.
			const e = err as { code?: string; stderr?: Buffer; status?: number };
			if (e?.code === 'ENOENT') continue;
			failures.push(`${bin} exited ${e?.status ?? '?'}: ${String(e?.stderr ?? '').trim() || 'no stderr'}`);
			continue;
		}
		if (getArgs) {
			try {
				const out = execFileSync(bin, [...getArgs], { timeout: 5_000, encoding: 'utf8' });
				const actual = parseInt(out.trim(), 10);
				if (Number.isFinite(actual)) {
					return Math.abs(actual - level) <= BRIGHTNESS_TOLERANCE_PCT
						? { status: 'set', method: bin, level: actual, requested: level, verified: true }
						: { status: 'partial', method: bin, level: actual, requested: level, verified: true };
				}
			} catch { /* readback unsupported by this panel — fall through unverified */ }
		}
		return { status: 'requested', method: bin, level, requested: level, verified: false };
	}
	return {
		error: failures.length
			? `External display: DDC command failed — ${failures.join('; ')}`
			: 'External display needs a DDC tool. Install one with: brew install m1ddc',
	};
}

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
		level = Math.max(0, Math.min(100, level));
		const bLevel = (level / 100).toFixed(2);
		try {
			// DisplayServicesSetBrightnessSmooth takes a RELATIVE delta and persists;
			// the absolute setter is reverted by the display daemon within ~30s.
			const out = execFileSync(requirePython(), ['-c', BRIGHTNESS_PY, bLevel], { timeout: 5_000, encoding: 'utf8' });
			const [, afterRaw, beforeRaw] = out.trim().split(/\s+/);
			const actual = Math.round(parseFloat(afterRaw) * 100);
			const before = Math.round(parseFloat(beforeRaw) * 100);
			if (!Number.isFinite(actual) || !Number.isFinite(before)) throw new Error(`unreadable brightness: ${out.trim()}`);
			// A readback alone cannot tell "set" from "did nothing" — that was the
			// original silent-success bug. Require the display to have reached the
			// target, or at least moved toward it.
			if (Math.abs(actual - level) > BRIGHTNESS_TOLERANCE_PCT) {
				const moved = actual !== before;
				console.log(`${ts()} [Brightness] builtin: requested ${level}%, reads ${actual}% (was ${before}%) — ${moved ? 'partial' : 'no movement'}`);
				return moved
					? { status: 'partial', level: actual, requested: level, was: before, display: 'builtin' }
					: { error: `Brightness did not move: still ${actual}% after requesting ${level}%.` };
			}
			console.log(`${ts()} [Brightness] builtin: requested ${level}%, display reads ${actual}%`);
			return { status: 'set', level: actual, requested: level, display: 'builtin' };
		} catch (err) {
			// Exit 2 means the probe identified an EXTERNAL main display, which
			// DisplayServices cannot drive at all — route to DDC rather than retrying.
			if ((err as { status?: number })?.status === 2) {
				const count = parseInt(String((err as { stdout?: string }).stdout ?? '').trim().split(/\s+/)[1] ?? '1', 10);
				const outcome = setExternalBrightness(level, Number.isFinite(count) ? count : 1);
				if ('error' in outcome) return outcome;
				console.log(`${ts()} [Brightness] external: requested ${level}% via ${outcome.method} — ${outcome.verified ? `display reads ${outcome.level}%` : 'NOT verified (no readback)'}`);
				return { ...outcome, display: 'external' };
			}
			// Last resort when the probe itself could not run. nriley/brightness has no
			// readback we use here, so this reports `requested`, never `set` — the same
			// rule the external path follows, and the reason the review found this
			// class in the first place.
			try {
				execFileSync('brightness', [bLevel], { timeout: 5_000 });
				console.log(`${ts()} [Brightness] requested ${level}% via brightness CLI — NOT verified`);
				return { status: 'requested', level, requested: level, method: 'cli', verified: false };
			} catch (e) {
				return { error: `Brightness failed: ${e instanceof Error ? e.message : e}` };
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
				const content = execFileSync('pbpaste', [], { encoding: 'utf-8', timeout: 5_000 });
				console.log(`${ts()} [Clipboard] read: ${content.slice(0, 40)}`);
				return { status: 'read', content };
			} else {
				if (!text) return { error: 'No text provided to write' };
				execFileSync('pbcopy', [], { input: text, timeout: 5_000 });
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
	description:
		'Cancel a pending or in-flight task by writing a CANCEL_INSTRUCTION task that core will see next. ' +
		'Default (no args) cancels the most recent. ' +
		'Pass `taskId` to cancel a specific task by id (e.g. "task-1777686932069"). ' +
		'Pass `query` to cancel the first task whose content contains the substring (case-insensitive). ' +
		'Pass `list: true` to list pending tasks (id + first 60 chars of content) without cancelling. ' +
		'Use when user says "cancel", "nevermind", "stop that", "what\'s queued", "cancel the one about X". ' +
		'Note: in-flight processing only halts when core reaches the CANCEL_INSTRUCTION task in its queue — ' +
		'this prevents future pickup + tells core to abort if mid-task, but doesn\'t interrupt a single LLM turn.',
	parameters: z.object({
		taskId: z.string().optional().describe('Specific task id to cancel (matches the filename without .txt).'),
		query: z.string().optional().describe('Case-insensitive substring to match against task content. Cancels first match.'),
		list: z.boolean().optional().describe('If true, list pending tasks (id + 60-char preview) without cancelling.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { taskId, query, list } = (args ?? {}) as { taskId?: string; query?: string; list?: boolean };
		try {
			const tasksDir = join(WORKSPACE_DIR, 'tasks');
			const resultsDir = join(WORKSPACE_DIR, 'results');
			const files = readdirSync(tasksDir).filter(f => f.endsWith('.txt')).sort();

			// list mode: return id + preview, no cancel
			if (list) {
				if (files.length === 0) return { status: 'nothing_pending', count: 0, tasks: [] };
				const items = files.map(f => {
					const id = f.replace('.txt', '');
					let preview = '';
					try {
						const body = readFileSync(join(tasksDir, f), 'utf-8');
						const taskLine = body.split('\n').find(l => l.startsWith('task:')) ?? body;
						preview = taskLine.replace(/^task:\s*/, '').slice(0, 60);
					} catch { /* ignore */ }
					return { id, preview };
				});
				console.log(`${ts()} [CancelTask] list: ${items.length} pending`);
				return { status: 'pending_tasks', count: items.length, tasks: items };
			}

			// Targeting: by exact id, by query, or default-to-most-recent.
			// IMPORTANT: target can be a file in `tasks/` OR a recently-archived task whose
			// processing is in-flight (file already moved). For id-based cancels we accept
			// either case; for query-based we need the file present to grep its content.
			let targetId: string | undefined;
			let targetFile: string | undefined;
			if (taskId) {
				const wantFile = taskId.endsWith('.txt') ? taskId : `${taskId}.txt`;
				targetId = wantFile.replace('.txt', '');
				if (files.includes(wantFile)) targetFile = wantFile;
				// else: accept the cancel even if file is gone (in-flight); core sees CANCEL and decides
			} else if (query) {
				if (files.length === 0) return { status: 'nothing_pending' };
				const needle = query.toLowerCase();
				for (const f of files) {
					try {
						const body = readFileSync(join(tasksDir, f), 'utf-8').toLowerCase();
						if (body.includes(needle)) { targetFile = f; targetId = f.replace('.txt', ''); break; }
					} catch { /* ignore */ }
				}
				if (!targetId) return { status: 'not_found', query };
			} else {
				// default: most recent pending file
				if (files.length === 0) return { status: 'nothing_pending' };
				targetFile = files[files.length - 1];
				targetId = targetFile.replace('.txt', '');
			}

			// Write a CANCEL_INSTRUCTION task — core picks it up next and aborts/skips
			// the named target. Design (Chi 2026-05-13): reuse the task pipeline as the
			// cancel signal channel instead of building a parallel one.
			const cancelTs = Date.now();
			const cancelFilename = `task-${cancelTs}.txt`;
			// Strip newlines from targetId (Gemini-supplied; task IDs are alphanumeric
			// in practice but defence-in-depth). task: field is placed LAST so a
			// forged line in the body cannot shadow the real source/access_tier above it.
			const safeTargetId = (targetId ?? '').replace(/[\r\n]/g, '');
			const cancelBody = [
				`id: task-${cancelTs}`,
				`timestamp: ${new Date().toISOString()}`,
				`source: voice`,
				`channel_id: local-voice`,
				`user_id: voice-local`,
				`access_tier: owner`,
				`task: CANCEL_INSTRUCTION: stop processing ${safeTargetId} if still in flight. If already completed, no-op. Reply briefly confirming.`,
				``,
			].join('\n');
			writeFileSync(join(tasksDir, cancelFilename), cancelBody);

			// Also unlink the original task file if it's still present — prevents
			// double-pickup if core hadn't started yet. Best-effort.
			if (targetFile) {
				try { unlinkSync(join(tasksDir, targetFile)); } catch { /* already gone is fine */ }
			}

			// Touch a cancelled result for the web UI's cancel icon (best-effort).
			try { writeFileSync(join(resultsDir, `${targetId}.txt`), 'Cancelled.'); } catch { /* ignore */ }

			console.log(`${ts()} [CancelTask] cancel-instruction written for ${targetId}${taskId ? ' (by id)' : query ? ` (by query: ${query})` : ''} → ${cancelFilename}`);
			return { status: 'cancel_instruction_queued', taskId: targetId, instruction: `task-${cancelTs}` };
		} catch (err) {
			return { error: `Cancel failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

export const toggleTasksTool: ToolDefinition = {
	name: 'toggle_tasks',
	description:
		'Collapse or expand tasks in the web UI. Use for: "collapse tasks", "expand tasks", "hide tasks", "show tasks", "expand only the first task". Pass taskIndex=1 for "the first task", 2 for "the second", etc. (1-based, by display order); omit for all-tasks. Instant.',
	parameters: z.object({
		action: z.enum(['collapse', 'expand']).describe('"collapse" to hide task results, "expand" to show them'),
		taskIndex: z.number().int().min(1).optional().describe('1-based index of a single task to act on (by display order). Omit to act on all tasks.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { action, taskIndex } = args as { action: 'collapse' | 'expand'; taskIndex?: number };
		// Set data attribute on body — MutationObserver in the page picks it up and updates state.
		// When taskIndex is set, encode as "expand:N" / "collapse:N"; handler in web-client.ts parses the suffix.
		const actionStr = taskIndex ? `${action}:${taskIndex}` : action;
		const js = `document.body.dataset.taskAction = \\\"${actionStr}\\\"; \\\"done\\\"`;
		try {
			execFileSync('osascript', ['-e', `tell application "Google Chrome"
				repeat with w in windows
					repeat with t in tabs of w
						if URL of t contains "localhost:8080" then
							execute t javascript "${js}"
							return "ok"
						end if
					end repeat
				end repeat
				return "not found"
			end tell`], { timeout: 5_000 });
			console.log(`${ts()} [ToggleTasks] ${actionStr}`);
			return { status: action === 'collapse' ? 'collapsed' : 'expanded' };
		} catch (err) {
			return { error: `Toggle tasks failed: ${err instanceof Error ? err.message : err}` };
		}
	},
};

export const getCurrentTimeTool: ToolDefinition = {
	name: 'get_current_time',
	description:
		'Get the current date and time. Instant. Call this ONLY when the user explicitly asks for ' +
		'the time, date, or day. NEVER call it for any other question, on filler ("hm", "okay"), or ' +
		'as a fallback when you are unsure what the user wants — answering an unrelated question ' +
		'(e.g. about a paper) by announcing the time is always wrong; when unsure, fire nothing.',
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
		'or similar questions about background work. Instant file read. Call it ONLY for those ' +
		'explicit status questions — NEVER on greetings ("hello"), filler, garbled speech, or as ' +
		'a fallback when unsure what the user wants; fire nothing instead.',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		try {
			// core-status.json is per-user runtime state under <workspace>/state/
			// (workspace resolves via the M0 helper; default <repo>/workspace/ post-v0.8).
			// statusReadPath falls back to the legacy workspace-root location for one release.
			const corePath = statusReadPath('core-status.json', WORKSPACE_DIR);
			if (!existsSync(corePath)) {
				return { status: 'idle', ready: true, description: 'Core agent is idle (no task in flight) — available and ready to accept a delegated task via work.' };
			}
			const raw = readFileSync(corePath, 'utf-8');
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
			return { status: 'idle', ready: true, description: 'Core agent is idle (no task in flight) — available and ready to accept a delegated task via work. "idle" means READY, not unavailable.' };
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
		'Mutates the active slide via DOM (Chrome execute javascript) — works regardless of which element has focus, ' +
		'so it is safe to call even when a textarea or contenteditable on the deck has focus (e.g. live-edit demos). ' +
		'PREFER this over press_key("leftarrow"/"rightarrow"/"space") for slide navigation: arrow / space keystrokes ' +
		'get captured by focused editables (cursor moves within the field) and may be suppressed by deck-side ' +
		'focus-guard handlers — slide_control sidesteps both.',
	parameters: z.object({
		action: z.enum(['next', 'previous', 'goto']).describe('Navigation action'),
		slideNumber: z.number().optional().describe('Slide number for goto action'),
	}),
	execution: 'inline',
	async execute(args) {
		const { action, slideNumber } = args as { action: 'next' | 'previous' | 'goto'; slideNumber?: number };
		try {
			// All slide navigation uses DOM manipulation for reliability, and is
			// LAYOUT-AGNOSTIC: it addresses slides by VISUAL POSITION (1-indexed) via the
			// deck's live querySelectorAll('.slide') order — never by id="s"+N. The deck
			// owns its own id-map; slide IDs are non-contiguous and deck-specific, so
			// id-based addressing would silently misroute "go to slide N" cues. Reading the
			// live .slide DOM keeps this tool correct across any deck with no per-deck edits.
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
			execFileSync('osascript', ['-e', script], { timeout: 15_000 });
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

// --- Chat task creation (future hook) ------------------------------------------
// Definition kept here for when /chat gets a tool-calling surface (SSE wiring +
// UI handler). Currently NOT registered in inlineTools / ownerOnlyTools because
// no caller in the architecture can reach it — /chat connects to agent-api, not
// voice-agent. The active chat-path tracking is the shell snippet in CLAUDE.md.
// See round-5 discussion on PR #695 for the architectural analysis.
export const createChatTaskTool: ToolDefinition = {
	name: 'create_chat_task',
	description:
		'Create a tracked task entry for the /chat web UI route. ' +
		'Future hook: no current caller in the chat path (/chat connects to agent-api, not voice-agent). ' +
		'The core agent (Claude Code) uses the CLAUDE.md shell-snippet path instead. ' +
		'Voice tasks have their own tracking (source: voice).',
	parameters: z.object({
		task: z.string().describe('Description of the task being tracked'),
	}),
	execution: 'inline',
	async execute(args) {
		const { task } = args as { task: string };
		const { writeChatTask } = await import('./task-bridge.js');
		const taskId = writeChatTask(task);
		return { status: 'created', taskId, message: `Chat task created: ${taskId}` };
	},
};

/** All inline tools — import and spread into your tools list */
// ─── Notes tools ─────────────────────────────────────────
// Resolve at module-init: $SUTANDO_MEMORY_DIR/notes (canonical) when set
// (legacy $SUTANDO_PRIVATE_DIR honored via sharedPersonalPath()), else
// <workspace>/notes fallback. Notes are SHARED across the fleet so they live
// at the top-level memory dir, not under machine-<host>/.
import { sharedPersonalPath, memoryDirEnv, readCaptureToken } from './util_paths.js';
const NOTES_DIR = sharedPersonalPath('notes', WORKSPACE_DIR);

export const showViewTool: ToolDefinition = {
	name: 'show_view',
	description: 'Switch the web UI to a specific view. Use when user says "show notes", "show tasks", "show activity", etc.',
	parameters: z.object({
		view: z.enum(['starter', 'tasks', 'notes', 'questions', 'activity']).describe('Which view to show'),
	}),
	execution: 'inline',
	async execute(args) {
		const { view } = args as { view: string };
		const dcPath = statusPath('dynamic-content.json', WORKSPACE_DIR);
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
			// NOTES_DIR resolves against the workspace, which may not have a
			// notes/ subdir yet on a fresh install — create it before writing
			// so the first save_note never fails with ENOENT.
			mkdirSync(NOTES_DIR, { recursive: true });
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

// --- Voice session context (Chi 2026-05-13: voice agent loses context across turns) ---
//
// Background: voice-agent's Gemini context window is independent from core's. After
// ~10 minutes of turns earlier transcript rolls off and voice "forgets" specifics
// like "the post" or "Mini Draft A". The fix is a small JSON file at
// `state/voice-session-context.json` that core writes whenever a durable decision
// lands (active draft, pending paste, today's selected option). Voice can ask for
// the file's contents at any time via `recent_context`.
//
// Schema (informal):
//   {
//     "updated_at": "<ISO ts>",
//     "active_drafts": [
//       { "name": "Mini Draft A", "summary": "...", "path": "/tmp/sutando-draft.txt" }
//     ],
//     "pending_action": { "kind": "paste", "what": "Mini Draft A", "where": "Cursor / X compose" } | null,
//     "last_results": [
//       { "task_id": "task-...", "subject": "DeepMind post drafted", "ts": "<ISO>" }
//     ]
//   }
//
// Core writes the file by direct fs operations — no inline tool needed for the writer
// path (core is this Claude Code session and already has fs access). The tool here
// is the READ path that voice-agent's Gemini can call when it senses confusion
// ("what was the post we picked?" / "what's pending?").

const VOICE_SESSION_CONTEXT_PATH = join(WORKSPACE_DIR, 'state', 'voice-session-context.json');

// Anything older than this is almost certainly a PREVIOUS session's context.
// The file exists to bridge voice's ~10-minute Gemini window inside one live
// session, so a multi-hour gap means the session that wrote it is long gone.
export const VOICE_CONTEXT_STALE_HOURS = 6;

// Clocks between the writing process and the reading one disagree by seconds in
// practice. Inside this window a future timestamp is ordinary skew and the age is
// clamped to 0; beyond it the stamp is untrustworthy and degrades to 'unknown'.
export const VOICE_CONTEXT_SKEW_TOLERANCE_MS = 5 * 60 * 1000;

/**
 * Stamp the context payload with its own age.
 *
 * WHY: the writer is a PROSE INSTRUCTION, not code — CLAUDE.md tells core to
 * update this file "whenever a durable decision lands". That is a discipline,
 * and disciplines lapse silently. Measured 2026-08-03: the canonical file was
 * **97 hours old and still carried `pending_action`**, and the legacy copy was
 * 878 hours old. `recent_context` returned both verbatim, so voice would answer
 * "what's pending?" with a four-day-old action stated as current — while the
 * tool's own description promises "the CURRENT voice-session context".
 *
 * The payload is deliberately NOT withheld when stale: dropping it would hide
 * context that is often still correct, and the failure this guards against is
 * voice asserting currency it cannot verify. So it returns everything and adds
 * the one fact the caller could not otherwise know.
 */
export function annotateContextFreshness(
	parsed: Record<string, unknown> | null | undefined,
	nowMs: number = Date.now(),
): Record<string, unknown> {
	const base: Record<string, unknown> = { ...(parsed ?? {}) };
	const rawTs = base.updated_at;
	const updatedMs = typeof rawTs === 'string' ? Date.parse(rawTs) : Number.NaN;
	if (!Number.isFinite(updatedMs)) {
		base.freshness = 'unknown';
		base.note =
			'context has no parseable updated_at — age unknown, so treat pending_action and active_drafts as historical unless the user confirms them.';
		return base;
	}
	// A FUTURE timestamp fails both branches below unless it is caught here: the age
	// goes negative, so it is never >= the stale threshold, and Number.isFinite() is
	// true so it never reaches 'unknown'. A skewed or corrupt clock would therefore
	// bypass the guard completely and let voice assert an old pending_action as
	// current until wall time caught up — the very defect this function exists to
	// close, through the one input I had not considered (qingyun-wu + john-the-dev,
	// review of #2560).
	//
	// The tolerance matters as much as the check: machine clocks routinely disagree
	// by seconds, so treating ANY future stamp as untrusted would flag healthy
	// contexts and train the reader to ignore the marker. Inside the window the age
	// is clamped to 0 (healthy, never negative); beyond it the stamp cannot be
	// trusted at all, so it degrades to unknown rather than to fresh.
	const ageMs = nowMs - updatedMs;
	// Close the CLASS, not the case. The reviewed defect was a future timestamp
	// producing a negative age that satisfied neither branch; a non-finite `nowMs`
	// (NaN/Infinity, e.g. a caller passing a parsed value) fails both the same way
	// and reads as fresh. Found by enumerating this function's inputs rather than
	// waiting for a fourth review round. Any age arithmetic that is not a finite
	// number means the age is unknowable, so it degrades to unknown — never fresh.
	if (!Number.isFinite(ageMs)) {
		base.freshness = 'unknown';
		base.note =
			'context age could not be computed (the current time was not a finite value), so it ' +
			'cannot be trusted. Treat pending_action and active_drafts as historical unless the ' +
			'user confirms them.';
		return base;
	}
	if (ageMs < -VOICE_CONTEXT_SKEW_TOLERANCE_MS) {
		const aheadHours = Math.round((-ageMs / 3_600_000) * 10) / 10;
		base.age_hours = Math.round((ageMs / 3_600_000) * 10) / 10;
		base.freshness = 'unknown';
		base.note =
			`this context is timestamped ${aheadHours}h in the FUTURE — a skewed or corrupt clock, ` +
			'so its age cannot be trusted. Treat pending_action and active_drafts as historical ' +
			'unless the user confirms them.';
		return base;
	}
	const ageHours = Math.max(0, ageMs) / 3_600_000;
	base.age_hours = Math.round(ageHours * 10) / 10;
	if (ageHours >= VOICE_CONTEXT_STALE_HOURS) {
		base.stale = true;
		base.note =
			`this context is ${base.age_hours}h old — almost certainly written by an EARLIER session, ` +
			'not the one you are in. Do not present pending_action or active_drafts as current; ' +
			'say how old it is, or confirm with the user before acting on it.';
	}
	return base;
}

export const recentContextTool: ToolDefinition = {
	name: 'recent_context',
	description:
		'Return the current voice-session context — active drafts, pending actions, recent task results — so you can pick up a thread even if it predates your Gemini context window. ' +
		'Call this when the user references something with a deictic pronoun ("the post", "the draft", "the one I just typed") that you can\'t place from your own recent transcript. ' +
		'Also fine to call proactively at the start of an active session to ground yourself. ' +
		'Returns JSON with keys: active_drafts (array), pending_action (object|null), last_results (array of {task_id, subject, ts}). ' +
		'If the file is missing or empty, returns {note: "no context recorded yet"}. ' +
		'The response also carries age_hours, and stale:true with a note when the context predates this session. ' +
		'Age is load-bearing: when stale is set, do NOT state pending_action or active_drafts as current — ' +
		'say how old it is, or ask the user to confirm, before acting on it.',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		try {
			if (!existsSync(VOICE_SESSION_CONTEXT_PATH)) {
				return { note: 'no context recorded yet — core hasn\'t written voice-session-context.json' };
			}
			const raw = readFileSync(VOICE_SESSION_CONTEXT_PATH, 'utf-8');
			const parsed = annotateContextFreshness(JSON.parse(raw));
			console.log(`${ts()} [RecentContext] returned (updated_at=${parsed.updated_at || 'unknown'}, age=${parsed.age_hours ?? '?'}h${parsed.stale ? ' STALE' : ''}, ${((parsed.active_drafts as unknown[]) || []).length} drafts, ${((parsed.last_results as unknown[]) || []).length} results)`);
			return parsed;
		} catch (err) {
			return { error: `recent_context read failed: ${err instanceof Error ? err.message : err}` };
		}
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
// Split by manifest `access_tier` so phone-conversation can include
// owner-tier tools only when the caller is the verified owner. Manifest
// access_tier values: "owner" (default if omitted) | "any_caller".
// OPTIONAL hook a skill's tools.ts may export; core calls it once per voice
// session so the skill registers session handlers without importing core.
export type { SkillSetupCtx, SkillSetup } from './skill-setup-runner.js';
import type { SkillSetup } from './skill-setup-runner.js';

async function loadSkillManifestTools(): Promise<{ owner: ToolDefinition[]; anyCaller: ToolDefinition[]; setups: SkillSetup[] }> {
	// Scan the public-repo `skills/` dir, the per-user workspace
	// `$SUTANDO_WORKSPACE/skills/`, AND the optional private skills dir
	// pointed to by `$SUTANDO_MEMORY_DIR/skills/` (legacy `$SUTANDO_PRIVATE_DIR`
	// honored via memoryDirEnv(); e.g. `~/.sutando/memory-sync/skills/`). The
	// private dir lets users keep personal tooling with real per-file git
	// history outside the public repo. Order: public first, then workspace,
	// then private — last-write-wins for same-name skills.
	const dirsToScan: string[] = [join(REPO_ROOT, 'skills'), join(WORKSPACE_DIR, 'skills')];
	const privateRoot = memoryDirEnv();
	if (privateRoot) {
		const expanded = privateRoot.replace(/^~/, process.env.HOME || '');
		dirsToScan.push(join(expanded, 'skills'));
	}
	// External plugin checkouts: an optional voice-surface plugin can live
	// ENTIRELY in its own sibling repo, so this host keeps no in-repo copy and
	// names no plugin. Mirrors src/discord-bridge.py's hook loader — scan
	// $SUTANDO_EXTERNAL_PLUGIN_DIRS (os.pathsep-separated) + every sibling
	// checkout's skills/. The dedupe-by-name below makes a stray duplicate safe.
	for (const d of (process.env.SUTANDO_EXTERNAL_PLUGIN_DIRS || '').split(delimiter)) {
		if (d.trim()) dirsToScan.push(join(d.trim(), 'skills'));
	}
	try {
		const siblingsRoot = dirname(REPO_ROOT); // dir holding sibling checkouts
		const ownSkills = join(REPO_ROOT, 'skills');
		for (const sib of readdirSync(siblingsRoot)) {
			const sibSkills = join(siblingsRoot, sib, 'skills');
			if (sibSkills !== ownSkills && existsSync(sibSkills)) dirsToScan.push(sibSkills);
		}
	} catch { /* siblings root unreadable — skip */ }
	const owner: ToolDefinition[] = [];
	const anyCaller: ToolDefinition[] = [];
	// Keyed by skill identity (manifest.name || dirName), not tool name: the same
	// skill scanned from two roots must attach its handler ONCE, last-write-wins.
	const setups = new Map<string, SkillSetup>();
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
			let manifest: { enabled?: boolean; tools?: string; config?: Record<string, string>; name?: string; access_tier?: string };
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
			const tier = manifest.access_tier === 'any_caller' ? 'any_caller' : 'owner';
			try {
				// @ts-ignore — dynamic relative import resolved at runtime by tsx
				const mod = await import(toolsPath);
				if (Array.isArray(mod.tools)) {
					(tier === 'any_caller' ? anyCaller : owner).push(...mod.tools);
					console.log(`[skill-loader] loaded ${mod.tools.length} tool(s) from ${manifest.name || dirName} [tier=${tier}] (${skillsDir})`);
				}
				if (typeof mod.setup === 'function') {
					// DISCOVERY, not registration: a skill in N roots hits this line N times
					// but registers once. The authoritative count is logged after the scan.
					console.log(`[skill-loader] found setup() hook in ${manifest.name || dirName} (${skillsDir})`);
					setups.set(manifest.name || dirName, mod.setup as SkillSetup);
				}
			} catch (err) {
				console.warn(`[skill-loader] failed to import ${dirName}/${manifest.tools} from ${skillsDir}:`, err instanceof Error ? err.message : err);
			}
		}
	}
	// Dedupe by tool name (last-write-wins, matching the public→workspace→private
	// scan order). The SAME skill present in two scanned dirs, or two skills
	// declaring the same tool name (e.g. summon/dismiss/copres_*), otherwise yields
	// duplicate names → assertUniqueToolNames(inlineTools) throws at module load →
	// Gemini 3.1 Live closes with 1011 at setup → voice / plugin surfaces can't start.
	// See reference_gemini_1011_tool_name_conflict.
	const dedupeByName = (arr: ToolDefinition[]): ToolDefinition[] => {
		const byName = new Map<string, ToolDefinition>();
		for (const t of arr) byName.set(t.name, t);
		return [...byName.values()];
	};
	// One authoritative line for what actually got registered, after dedupe.
	if (setups.size) console.log(`[skill-loader] registered ${setups.size} setup() hook(s): ${[...setups.keys()].join(', ')}`);
	return { owner: dedupeByName(owner), anyCaller: dedupeByName(anyCaller), setups: [...setups.values()] };
}
const personalTools = await loadSkillManifestTools();
// Also dedupe across the owner+anyCaller union (a tool declared in both tiers).
const personalAllTools = (() => {
	const seen = new Set<string>();
	return [...personalTools.owner, ...personalTools.anyCaller].filter(t => (seen.has(t.name) ? false : (seen.add(t.name), true)));
})();

// Names of env-dependent tools (manifest-loaded per install, plus the
// presenter-sentinel conditionals) — exported so behavior-anchor tests can
// pin the STATIC tool surface portably (CI has no personal skill manifests;
// see tests/voice-behavior-anchors.test.ts).
export const envDependentToolNames: ReadonlySet<string> = new Set([
	...personalAllTools.map(t => t.name), 'slide_control', 'fullscreen',
]);
// voice-agent invokes each once per session with {session, injectText}.
// Empty when no skill exports setup().
export const personalSkillSetups: SkillSetup[] = personalTools.setups;

// Manifest-driven discovery of skills that core (not voice-inline) runs.
// When a manifest has `documented_for_core: true` and a `core_description`,
// the description is exposed to voice-agent's system-prompt assembly so
// Gemini knows the capability exists and to delegate via `work` instead
// of saying "I can't do that". The skill itself is NOT loaded inline — it
// stays as docs+scripts and the core agent runs it when the work-task
// arrives. Same scan-paths as loadSkillManifestTools.
//
// SYNC vs ASYNC NOTE (Mini's #592 review): this helper is sync because
// we never need to import any module — we just read manifest.json files.
// loadSkillManifestTools is async because it dynamically `await import()`s
// a tools.ts. Don't try to align them — they're correctly sync/async for
// what each one does.
function loadCoreDocumentedSkills(): { name: string; description: string }[] {
	const dirsToScan: string[] = [join(REPO_ROOT, 'skills'), join(WORKSPACE_DIR, 'skills')];
	const privateRoot = memoryDirEnv();
	if (privateRoot) {
		const expanded = privateRoot.replace(/^~/, process.env.HOME || '');
		dirsToScan.push(join(expanded, 'skills'));
	}
	// Last-write-wins map so private (later in dirsToScan) overrides public —
	// same precedence convention as loadSkillManifestTools above.
	const byName = new Map<string, { name: string; description: string }>();
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
			let manifest: { documented_for_core?: boolean; core_description?: string; name?: string; tools?: string };
			try {
				manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
			} catch { continue; }
			if (!manifest.documented_for_core || !manifest.core_description) continue;
			// Dedup against inline-loaded skills: if the manifest also exposes
			// a `tools:` entry, the skill is already inline-listed and
			// double-listing here would teach Gemini both "call this inline"
			// AND "delegate via work" simultaneously. Pick the inline path.
			if (manifest.tools) continue;
			const name = manifest.name || dirName;
			byName.set(name, { name, description: manifest.core_description });
		}
	}
	return Array.from(byName.values());
}
export const coreDocumentedSkills = loadCoreDocumentedSkills();

export const inlineTools = assertUniqueToolNames([
	pressKeyTool, scrollTool, switchTabTool, closeTabTool, openUrlTool,
	switchAppTool, captureScreenTool, typeTextTool,
	volumeTool, brightnessTool, clipboardTool,
	cancelTaskTool, toggleTasksTool, getCurrentTimeTool, getCoreStatusTool,
	joinGmeetTool, lookupMeetingIdTool, callContactTool,
	describeScreenTool, clickTool, pointAtTool, scrollAndDescribeTool, screenRecordTool, openFileTool, playVideoTool, pauseVideoTool, resumeVideoTool, replayVideoTool, closeVideoTool, ...(_presenterActive ? [slideControlTool, fullscreenTool] : []),
	showViewTool, readNoteTool, saveNoteTool, deleteNoteTool,
	recentContextTool,
	sendVisionFrameTool, startVisionTool, stopVisionTool,
	setActiveArtifactTool, queryActiveArtifactTool, clearActiveArtifactTool,
	switchVoiceConfigTool,
	...personalAllTools ]);

/** Tools available to any caller (including unverified) */
export const anyCallerTools = [
	getCurrentTimeTool,
	getCoreStatusTool,
	...personalTools.anyCaller,
];

/** Owner-only tools (require isOwner) */
export const ownerOnlyTools = [
	volumeTool, brightnessTool,
	pressKeyTool, scrollTool, switchTabTool, closeTabTool, openUrlTool,
	switchAppTool, captureScreenTool, typeTextTool,
	clipboardTool, cancelTaskTool, toggleTasksTool,
	joinGmeetTool, callContactTool, ...(_presenterActive ? [slideControlTool, fullscreenTool] : []),
	showViewTool, readNoteTool, saveNoteTool, deleteNoteTool,
	recentContextTool,
	describeScreenTool, clickTool, pointAtTool, scrollAndDescribeTool, screenRecordTool, openFileTool, playVideoTool, pauseVideoTool, resumeVideoTool, replayVideoTool, closeVideoTool,
	sendVisionFrameTool, startVisionTool, stopVisionTool,
	setActiveArtifactTool, queryActiveArtifactTool, clearActiveArtifactTool,
	switchVoiceConfigTool,
	...personalTools.owner,
];

/** Configurable tools — default to owner-only, can be opened to verified callers */
export const configurableTools = [
	lookupMeetingIdTool,
];
