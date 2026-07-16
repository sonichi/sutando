// Tmux-pane status scraper. Fallback signal for `effectiveAgentState()` when
// `core-status.json` is stale or missing (e.g. a proactive-loop pass crashed
// before writing idle). Scrapes the CLI's tmux pane via `tmux capture-pane`
// and categorizes the rendered state.
//
// Disabled by setting `SUTANDO_TMUX_SCRAPE=0`. Tmux session name override via
// `SUTANDO_TMUX_SESSION` (default `sutando-core`). Tmux socket override via
// `SUTANDO_TMUX_SOCKET` (default `/tmp/sutando-tmux.sock` — matches the socket
// the core session is actually launched on, per src/Sutando/main.swift and
// src/watch-tasks-stream.sh; the plain `tmux` command targets the per-uid
// default socket instead, which is never the one Sutando uses). Legacy name
// `SUTANDO_TMUX_SOCK` is honored as a one-release fallback — #2087 unified
// start-cli.sh / main.swift / watch-tasks-stream.sh on `SUTANDO_TMUX_SOCKET`.
//
// All failure modes (tmux missing, timeout, parse error) return `idle` — this
// is a best-effort hint, never ground-truth, never throws.

import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);

export type TmuxParseResult = {
	state: 'idle' | 'working';
	label: string;
};

const DEFAULT_SESSION = 'sutando-core';
const DEFAULT_SOCK = '/tmp/sutando-tmux.sock';
const CACHE_TTL_MS = 3_000;
const CAPTURE_TIMEOUT_MS = 500;
const LINES_BACK = 30;

let _cache: { ts: number; result: TmuxParseResult } | null = null;
let _refreshInFlight = false;
let _lastLogAt = 0; // throttled logging

function logOnce(msg: string): void {
	const now = Date.now();
	if (now - _lastLogAt < 60_000) return;
	_lastLogAt = now;
	console.error(`[tmux-status] ${msg}`);
}

/**
 * Build the `tmux` argv for capturing the core pane. Exported for testing.
 *
 * The `-S <socket>` server flag MUST precede the `capture-pane` command — tmux
 * parses server options before the command word. Sutando.app / start-cli.sh run
 * the tmux server on this custom socket, so omitting `-S` targets the default
 * server (where `sutando-core` does not exist), the capture throws, and the
 * scraper silently reports `idle` forever. See the header note.
 */
export function buildCaptureArgs(sock: string, session: string): string[] {
	return ['-S', sock, 'capture-pane', '-t', session, '-pS', `-${LINES_BACK}`];
}

/**
 * Resolve the tmux socket path: `SUTANDO_TMUX_SOCKET` (canonical, #2087) takes
 * priority, then the legacy `SUTANDO_TMUX_SOCK` (one-release fallback), then
 * the default. Exported for testing.
 */
export function resolveSock(): string {
	return process.env.SUTANDO_TMUX_SOCKET || process.env.SUTANDO_TMUX_SOCK || DEFAULT_SOCK;
}

async function _refreshCache(): Promise<void> {
	if (_refreshInFlight) return;
	_refreshInFlight = true;
	const session = process.env.SUTANDO_TMUX_SESSION || DEFAULT_SESSION;
	const sock = resolveSock();
	try {
		const { stdout } = await execFileAsync(
			'tmux',
			buildCaptureArgs(sock, session),
			{ encoding: 'utf-8', timeout: CAPTURE_TIMEOUT_MS },
		);
		_cache = { ts: Date.now(), result: parseTmuxPane(stdout) };
	} catch (err) {
		logOnce(`capture failed: ${err instanceof Error ? err.message : String(err)}`);
		_cache = { ts: Date.now(), result: { state: 'idle', label: '' } };
	} finally {
		_refreshInFlight = false;
	}
}

// Tool invocation: "⏺ ToolName(" at the start of a line. Claude Code renders
// this marker on every tool call; the tool name is the stable label for the
// most recent call in the pane.
const RE_TOOL = /^\s*(?:⏺|●)\s*(\w+)\(/m;
// Inline "Running…" inside a tool block = tool currently executing.
const RE_RUNNING_INLINE = /⎿\s+Running…/;
// Background task indicator: tool launched with run_in_background.
const RE_RUNNING_BG = /Running in the background/;
// Thinking spinner: "thought for Ns" (embedded in a richer parenthesized
// block — e.g. "(16s · ↓ 111 tokens · thought for 5s)") or "Cogitated for
// Ns" (separate format). Verb rotates ("Flummoxing", "Pondering",
// "Nebulizing", …) but the suffix is stable.
const RE_THOUGHT = /thought for \d+s|Cogitated for [\dm\s]+s/;
// Empty prompt: `❯ ` alone on a line (typical idle end-of-pane).
const RE_IDLE_PROMPT = /^❯\s*$/m;

/**
 * Parse a captured tmux pane into an agent state hint.
 *
 * Contract:
 * - Never throws. On malformed/unexpected input, returns `{state: 'idle', label: ''}`.
 * - Tool detection takes priority over thinking detection.
 * - Background-task markers still count as `working` — the CLI has an
 *   outstanding tool invocation and the user wants to see that signal.
 * - An unrelated pane (empty, non-Claude-Code output) → `idle`.
 */
export function parseTmuxPane(text: string): TmuxParseResult {
	if (!text || typeof text !== 'string') return { state: 'idle', label: '' };

	// Find the MOST RECENT tool call — Claude Code writes these in order, so
	// the last match in the pane is the freshest.
	let lastTool: string | null = null;
	const lines = text.split('\n');
	for (const line of lines) {
		const m = line.match(RE_TOOL);
		if (m) lastTool = m[1];
	}

	const toolInline = RE_RUNNING_INLINE.test(text);
	const toolBg = RE_RUNNING_BG.test(text);
	const thinking = RE_THOUGHT.test(text);

	if (toolInline && lastTool) return { state: 'working', label: lastTool };
	if (toolBg && lastTool) return { state: 'working', label: lastTool };
	if (thinking) return { state: 'working', label: 'thinking' };
	// Recent tool but no active-run marker → already finished, idle.
	if (RE_IDLE_PROMPT.test(text)) return { state: 'idle', label: '' };
	// Ambiguous / unknown format → silent fallback.
	return { state: 'idle', label: '' };
}

/**
 * Return the last-known tmux-pane state hint. Non-blocking: reads a
 * background-refreshed cache and fires an async refresh if the cache is
 * stale. First call (no cache yet) returns the idle fallback and triggers
 * the first refresh; subsequent polls within CACHE_TTL_MS of the last
 * refresh return the cached result instantly.
 *
 * Respects `SUTANDO_TMUX_SCRAPE=0` kill-switch. Never throws. Never blocks
 * the event loop — the previous implementation used `execSync` with a
 * 500ms timeout, which stalled `/sse-status` polls during CLI hiccups.
 */
export function readTmuxStatus(): TmuxParseResult {
	if (process.env.SUTANDO_TMUX_SCRAPE === '0') return { state: 'idle', label: '' };

	const now = Date.now();
	if (!_cache || now - _cache.ts >= CACHE_TTL_MS) {
		// Fire and forget — the await-less call lets the sync caller return
		// immediately with the previous cache value (or idle fallback on
		// cold start). Errors are caught inside _refreshCache.
		void _refreshCache();
	}
	return _cache ? _cache.result : { state: 'idle', label: '' };
}

/**
 * Async variant: await the fresh capture directly. Useful when the caller
 * can afford to suspend and wants the freshest read (e.g. a one-shot
 * diagnostic endpoint). Most hot-path callers should stick with the sync
 * `readTmuxStatus`.
 */
export async function readTmuxStatusAsync(): Promise<TmuxParseResult> {
	if (process.env.SUTANDO_TMUX_SCRAPE === '0') return { state: 'idle', label: '' };
	const now = Date.now();
	if (_cache && now - _cache.ts < CACHE_TTL_MS) return _cache.result;
	await _refreshCache();
	return _cache ? _cache.result : { state: 'idle', label: '' };
}

/** Test-only: reset the capture cache between tests. */
export function _resetTmuxCacheForTests(): void {
	_cache = null;
	_refreshInFlight = false;
	_lastLogAt = 0;
}
