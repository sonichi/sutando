// Tmux-pane status scraper. Fallback signal for `effectiveAgentState()` when
// `core-status.json` is stale or missing (e.g. a proactive-loop pass crashed
// before writing idle). Scrapes the CLI's tmux pane via `tmux capture-pane`
// and categorizes the rendered state.
//
// Disabled by setting `SUTANDO_TMUX_SCRAPE=0`. Tmux session name override via
// `SUTANDO_TMUX_SESSION` (default `sutando-core`).
//
// All failure modes (tmux missing, timeout, parse error) return `idle` — this
// is a best-effort hint, never ground-truth, never throws.

import { execSync } from 'node:child_process';

export type TmuxParseResult = {
	state: 'idle' | 'working';
	label: string;
};

const DEFAULT_SESSION = 'sutando-core';
const CACHE_TTL_MS = 3_000;
const CAPTURE_TIMEOUT_MS = 500;
const LINES_BACK = 30;

let _cache: { ts: number; result: TmuxParseResult } | null = null;
let _lastLogAt = 0; // throttled logging

function logOnce(msg: string): void {
	const now = Date.now();
	if (now - _lastLogAt < 60_000) return;
	_lastLogAt = now;
	console.error(`[tmux-status] ${msg}`);
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
 * Capture the pane and parse it. Cached for CACHE_TTL_MS to avoid shelling
 * out on every `/sse-status` poll. Respects `SUTANDO_TMUX_SCRAPE=0` kill-switch.
 * Returns `{state:'idle', label:''}` on any error (tmux missing, timeout, etc.).
 */
export function readTmuxStatus(): TmuxParseResult {
	if (process.env.SUTANDO_TMUX_SCRAPE === '0') return { state: 'idle', label: '' };

	const now = Date.now();
	if (_cache && now - _cache.ts < CACHE_TTL_MS) return _cache.result;

	const session = process.env.SUTANDO_TMUX_SESSION || DEFAULT_SESSION;
	try {
		const out = execSync(
			`tmux capture-pane -t ${session} -pS -${LINES_BACK}`,
			{ encoding: 'utf-8', timeout: CAPTURE_TIMEOUT_MS, stdio: ['ignore', 'pipe', 'ignore'] },
		);
		const result = parseTmuxPane(out);
		_cache = { ts: now, result };
		return result;
	} catch (err) {
		logOnce(`capture failed: ${err instanceof Error ? err.message : String(err)}`);
		const result: TmuxParseResult = { state: 'idle', label: '' };
		_cache = { ts: now, result };
		return result;
	}
}

/** Test-only: reset the capture cache between tests. */
export function _resetTmuxCacheForTests(): void {
	_cache = null;
	_lastLogAt = 0;
}
