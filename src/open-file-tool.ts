/**
 * open_file: opens a file with the OS default handler. Outside inline-tools.ts, whose load scans and imports
 * every skill (one may connect to a live service and keep a test's process from exiting).
 */
import { execFileSync } from 'node:child_process';
import { existsSync, statSync } from 'node:fs';
import { dirname, extname, isAbsolute, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import { resolveWorkspace } from './workspace_default.js';
import { isMacOS, openWithDefault } from './platform.js';
import { PLAYBACK_PATH } from './tmp-paths.js';
import { expandHome } from './util_paths.js';

const WORKSPACE_DIR = resolveWorkspace();
const REPO_ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const ts = () => new Date().toLocaleTimeString('en-US', { hour12: false });

/**
 * Where open_file finds a path the core gave relative to the workspace or the repo. Results say
 * "workspace/data/apps/x.html" in prose, and the model put "~/" in front to make it look absolute
 * (2026-10-21 17:43:48: "~/workspace/data/apps/injury-recovery.html" missed under $HOME, and the model
 * sent the core a work task that took 55 s). Only when the literal path misses: a relative path is tried as
 * <workspace>/<rel without a leading "workspace/">, <workspace>/<rel>, <repoRoot>/<rel>; a "~/" path only as
 * "~/workspace/…" in the workspace (any other "~/README.md" asked for the home folder, not the repo's file of that
 * name). A candidate must stay inside its root ("../" does not climb out). Undefined when nothing is there.
 */
export function resolveOpenFilePath(raw: string, expanded: string, roots: { workspace: string; repoRoot?: string }): string | undefined {
	if (existsSync(expanded)) return expanded;
	const home = raw.startsWith('~/');
	let rel: string | undefined;
	if (home) rel = raw.slice(2);
	else if (!isAbsolute(raw) && !raw.startsWith('~') && !raw.startsWith('$')) rel = raw;
	if (!rel) return undefined;
	const candidates: Array<[string, string]> = [];
	// The literal segment, whatever the workspace folder is called: results name it "workspace/".
	if (rel.startsWith('workspace/')) candidates.push([roots.workspace, rel.slice('workspace/'.length)]);
	if (!home) {
		candidates.push([roots.workspace, rel]);
		if (roots.repoRoot) candidates.push([roots.repoRoot, rel]);
	}
	for (const [root, r] of candidates) {
		const base = resolve(root);
		const abs = resolve(base, r);
		if (abs.startsWith(base + sep) && existsSync(abs)) return abs;
	}
	return undefined;
}

export const openFileTool: ToolDefinition = {
	name: 'open_file',
	description:
		'Open a file with the OS default handler (macOS: `open`, Windows: ShellExecute). Pass an absolute `path`, one starting with $VAR / ~, or a workspace-relative path exactly as a result gave it (e.g. workspace/data/apps/x.html); never put ~ in front of a relative path. ' +
		'Use for: "open the file", "open that", "can you open it". ' +
		'If the user says "open the log" or similar, ASK which log they mean (voice-agent, discord-bridge, etc.) — do NOT guess. ' +
		'Known files: "diagnostic tracker" or "diagnostics" = /tmp/phone-diagnostics-tracker.html, ' +
		'"voice diagnostics" = /tmp/voice-diagnostics-tracker.html, ' +
		'"voice context" / "the voice context file" / "the active context" = $SUTANDO_MEMORY_DIR/voice-contexts/<active>.txt where <active> is the trimmed contents of $SUTANDO_MEMORY_DIR/voice-contexts/active (legacy users may have $SUTANDO_PRIVATE_DIR set instead — either expands). Pass it with the env-var expanded by you, or as $SUTANDO_MEMORY_DIR/voice-contexts/<active>.txt — both work. ' +
		'Pass `app` when the user names a specific app ("open with Sublime Text", "open the SQLite db in TablePlus") OR when recent conversation makes the intended app clear (e.g. user just said "I\'ll review this in VS Code"). Without `app`, the OS picks the default handler for that file type — leave unset when the default is fine. NOTE: the `app` argument is macOS-only; on Windows the file always opens with its default handler. ' +
		'Pass `fullscreen=true` if the user wants the file opened in fullscreen — macOS sends Cmd+Ctrl+F to whichever app the OS routed the file to. Windows-only ignores this flag.',
	parameters: z.object({
		path: z.string().describe('File path to open: absolute, $VAR / ~, or workspace-relative exactly as a result gave it (never prepend ~ to a relative path).'),
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
			const expanded = expandHome(path)
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
			const resolved = resolveOpenFilePath(path, expanded, { workspace: WORKSPACE_DIR, repoRoot: REPO_ROOT });
			if (!resolved) {
				console.log(`${ts()} [OpenFile] path "${expanded}" does not exist`);
				return { error: `File not found: ${expanded}. Do not invent paths — use the exact path returned by the tool that produced the file (e.g. record_screen_with_narration returns subtitled_path/narrated_path/recording_path). For the most recent recording without a known path, call play_video instead.` };
			}
			if (resolved !== expanded) console.log(`${ts()} [OpenFile] resolved "${path}" -> ${resolved}`);
			// The result names the file actually opened, so the model can say which one it was.
			const filePath = resolved;
			// execFileSync — no shell interpolation of caller-controlled filePath
			// or caller-controlled app name (same CodeQL js/command-line-injection
			// class as #27). Both are passed as separate argv entries, never spliced
			// into a shell string.
			//
			// Resolution per issue #560:
			//   1. Explicit `app` arg → `open -a <app> <path>` (macOS)
			//   2. No `app` → `open <path>` (macOS LaunchServices picks default) / ShellExecute (Windows)
			// Contextual inference (rule 2 from issue) is the model's job — Gemini
			// reads the conversation and decides whether to pass `app`. The tool
			// only honors what it's told.
			if (isMacOS()) {
				const openArgs = app ? ['-a', app, filePath] : [filePath];
				execFileSync('open', openArgs, { timeout: 5_000 });
			} else {
				// Windows + Linux: app-specific open isn't portable; defer to default
				// handler via ShellExecute / xdg-open. Surfaced in the tool description.
				openWithDefault(filePath);
			}
			if (fullscreen && isMacOS()) {
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
					fs.writeFileSync(PLAYBACK_PATH, filePath);
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
