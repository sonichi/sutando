// gws-gmail-voice: inline Gmail triage for voice/phone agents.
//
// Why this exists: the voice agent's only path to "read Gmail" was openUrlTool
// (open Gmail in Chrome → caller can't read it via voice) or workTool (delegate
// to core agent → ~5-30s round-trip). Today's phone-call failure (2026-05-14):
// Gemini reached for openUrlTool and hallucinated the Gmail URL.
//
// 3-tier path (cache → live gws → work fallback):
//   1) CACHE — read `state/external-cache/inbox-important.json` (importance-
//      scored top-3, refreshed periodically by the ACT loop's `score-inbox-llm`
//      cron — LLM judges importance using inbox + calendar + recent owner-
//      intent context, with incremental scoring of new mail only). Sub-50ms.
//      Returns top-3 ranked by importance, not recency.
//   2) LIVE — cache stale (>15min) or missing → call `gws gmail +triage` directly.
//      Returns up to `max` messages by recency.
//   3) WORK — gws call fails (timeout / OAuth / network) → description tells
//      Gemini to call the `work` tool with "check gmail unread inbox".
//
// Why cache-first solves the recency-vs-importance bug: importance scoring is
// expensive and stateful (sender frequency, blacklist domains, keyword matches);
// pre-computing in ACT amortizes it and makes the voice path instant. Gemini
// sees pre-ranked top-3 instead of having to rank from raw N-by-date.
//
// OSS-safety: if the `gws` CLI is not on PATH (user hasn't installed the
// gws-gmail skill / configured OAuth), this module exports an empty tools
// array. The voice agent simply doesn't see triage_email — no broken tool,
// no error noise. Detection is at module-load time via execFileSync('which').

import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';

const ts = () => new Date().toLocaleTimeString('en-US', { hour12: false });

const CACHE_PATH = join(process.cwd(), 'state', 'external-cache', 'inbox-important.json');
// 30 min TTL: balances cache-hit rate (~95% at 30min vs ~70% at 15min) against
// staleness. Live gws fallback covers the freshness edge case anyway. Per Mini PR #704 review.
const CACHE_MAX_AGE_MS = 30 * 60 * 1000;

function gwsAvailable(): boolean {
	try {
		execFileSync('which', ['gws'], { stdio: ['ignore', 'pipe', 'pipe'], timeout: 1_000 });
		return true;
	} catch {
		return false;
	}
}

function readCacheIfFresh(): { ts: string; top_3_important: unknown[]; all_unread_count: number; query?: string } | null {
	if (!existsSync(CACHE_PATH)) return null;
	try {
		const age = Date.now() - statSync(CACHE_PATH).mtimeMs;
		if (age > CACHE_MAX_AGE_MS) return null;
		return JSON.parse(readFileSync(CACHE_PATH, 'utf8'));
	} catch {
		return null;
	}
}

const triageEmailTool: ToolDefinition = {
	name: 'triage_email',
	description:
		'Get the user\'s unread Gmail. ' +
		'Use when the caller asks: "what unread emails do I have", "what\'s the most important email I missed", "any urgent emails", "summarize my inbox". ' +
		'**mode="important" (default):** returns top-3 importance-RANKED unread messages — pre-scored by the ACT loop (filters newsletters/digests; promotes deadline/RSVP/CI/academic keywords; weights by sender). `max` is ignored in this mode (always returns top-3). ' +
		'**mode="recent":** returns top-N by date (no importance ranking). Pass `max` to control N (default 5). Use only when caller explicitly asks for "list my latest emails" or "show me everything unread". ' +
		'On timeout/error: tell the caller you\'re using a slower path, then call the `work` tool with "check gmail unread inbox via gws gmail +triage". ' +
		'For sending email, deletion, or replies, delegate to work; this tool is read-only triage.',
	parameters: z.object({
		mode: z.enum(['important', 'recent']).optional().describe('"important" (default): top-3 importance-ranked. "recent": top-N by date.'),
		max: z.number().int().min(1).max(20).optional().describe('Top-N for mode="recent" (default 5). Ignored when mode="important".'),
		query: z.string().optional().describe('Optional Gmail search query (default: is:unread). Only used in live calls.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { mode = 'important', max = 5, query } = args as { mode?: 'important' | 'recent'; max?: number; query?: string };
		// In important mode, `max` is schema-meaningless — the cache returns
		// pre-ranked top-3 regardless. Log if Gemini passes it so we can audit.
		if (mode === 'important' && args && (args as { max?: number }).max !== undefined) {
			console.log(`${ts()} [TriageEmail] mode=important: ignoring max=${(args as { max?: number }).max} (cache returns top-3)`);
		}
		console.log(`${ts()} [TriageEmail] called (mode=${mode}, max=${max}${query ? `, query="${query}"` : ''})`);

		// Tier 1: cache hit (importance mode only — recent mode always wants live)
		if (mode === 'important' && !query) {
			const cached = readCacheIfFresh();
			if (cached) {
				console.log(`${ts()} [TriageEmail] cache hit (top_3 of ${cached.all_unread_count} unread, cached at ${cached.ts})`);
				return {
					status: 'ok',
					source: 'cache',
					mode: 'important',
					count: cached.top_3_important.length,
					messages: cached.top_3_important,
					all_unread_count: cached.all_unread_count,
					cached_at: cached.ts,
				};
			}
			console.log(`${ts()} [TriageEmail] cache miss/stale — falling back to live gws`);
		}

		// Tier 2: live gws call
		try {
			const cmdArgs = ['gmail', '+triage', '--format', 'json', '--max', String(max)];
			if (query) cmdArgs.push('--query', query);
			const stdout = execFileSync('gws', cmdArgs, {
				timeout: 10_000,
				encoding: 'utf8',
				stdio: ['ignore', 'pipe', 'pipe'],
			});
			// gws emits diagnostic header lines + JSON object: { messages, query, resultSizeEstimate }.
			// Match `{` at start-of-line (multiline) — robust against future header lines that contain
			// a literal `{` (e.g. `Loading {token}.json`). Fall back to first `{` if not found.
			const match = stdout.match(/^\{/m);
			const jsonStart = match?.index ?? stdout.indexOf('{');
			if (jsonStart === -1) return { error: 'triage_email: gws did not return JSON' };
			const parsed = JSON.parse(stdout.slice(jsonStart));
			const messages = Array.isArray(parsed) ? parsed : parsed.messages ?? [];
			console.log(`${ts()} [TriageEmail] live ${messages.length} messages`);
			return { status: 'ok', source: 'live', mode, count: messages.length, messages, query: parsed.query };
		} catch (err) {
			const msg = err instanceof Error ? err.message : String(err);
			console.log(`${ts()} [TriageEmail] failed: ${msg}`);
			// Tier 3: signaled in description — Gemini calls `work` next.
			return { error: `triage_email failed: ${msg}. Fall back to work tool: "check gmail unread inbox".` };
		}
	},
};

// Closes the §5 Now "Gmail gap" — adds inline read + search tools so voice
// doesn't have to fall through to `work` (5-30s round-trip) for those calls.
// Send / reply / delete intentionally remain `work`-delegated because they
// need explicit owner confirmation; this surface stays read-only.

const readEmailTool: ToolDefinition = {
	name: 'read_email',
	description:
		'Read a specific Gmail message by id. Use after `triage_email` returns a list and the caller asks to hear one specific message ("read me the first one", "open the email from Jane"). ' +
		'Returns the message body (plain text by default) + headers (From, Subject, Date). Pass the `id` from a prior `triage_email` result. ' +
		'On error: tell the caller you\'re using a slower path, then call the `work` tool with "read gmail message <id> via gws gmail +read".',
	parameters: z.object({
		id: z.string().describe('Gmail message id (from a prior triage_email result).'),
		html: z.boolean().optional().describe('Return HTML body instead of plain text. Default false.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { id, html = false } = args as { id: string; html?: boolean };
		console.log(`${ts()} [ReadEmail] id=${id} html=${html}`);
		try {
			const cmdArgs = ['gmail', '+read', '--id', id, '--format', 'json', '--headers'];
			if (html) cmdArgs.push('--html');
			const stdout = execFileSync('gws', cmdArgs, {
				timeout: 10_000,
				encoding: 'utf8',
				stdio: ['ignore', 'pipe', 'pipe'],
			});
			const match = stdout.match(/^\{/m);
			const jsonStart = match?.index ?? stdout.indexOf('{');
			if (jsonStart === -1) return { error: 'read_email: gws did not return JSON' };
			const parsed = JSON.parse(stdout.slice(jsonStart));
			return { status: 'ok', message: parsed };
		} catch (err) {
			const msg = err instanceof Error ? err.message : String(err);
			console.log(`${ts()} [ReadEmail] failed: ${msg}`);
			return { error: `read_email failed: ${msg}. Fall back to work tool: "read gmail message ${id} via gws gmail +read".` };
		}
	},
};

const searchEmailTool: ToolDefinition = {
	name: 'search_email',
	description:
		'Search Gmail by query string. Use when the caller asks "find emails from X", "did I get an email about Y", "search inbox for Z". ' +
		'`query` follows Gmail search syntax — `from:foo@example.com`, `subject:invoice`, `after:2026/05/01`, `has:attachment`, etc. ' +
		'Returns matching messages (id, sender, subject, date). Pass an id to `read_email` to hear the body. ' +
		'On error: fall back to `work` with "search gmail for <query>".',
	parameters: z.object({
		query: z.string().describe('Gmail search query (from:, subject:, after:, has:, etc.). Required.'),
		max: z.number().int().min(1).max(20).optional().describe('Max results (default 5).'),
	}),
	execution: 'inline',
	async execute(args) {
		const { query, max = 5 } = args as { query: string; max?: number };
		console.log(`${ts()} [SearchEmail] query="${query}" max=${max}`);
		try {
			// +triage with --query is the existing search surface; reuse it so the
			// JSON shape matches triage_email's. Avoid `users messages list`
			// which returns ids only (would force a second fetch per message).
			const cmdArgs = ['gmail', '+triage', '--format', 'json', '--max', String(max), '--query', query];
			const stdout = execFileSync('gws', cmdArgs, {
				timeout: 10_000,
				encoding: 'utf8',
				stdio: ['ignore', 'pipe', 'pipe'],
			});
			const match = stdout.match(/^\{/m);
			const jsonStart = match?.index ?? stdout.indexOf('{');
			if (jsonStart === -1) return { error: 'search_email: gws did not return JSON' };
			const parsed = JSON.parse(stdout.slice(jsonStart));
			const messages = Array.isArray(parsed) ? parsed : parsed.messages ?? [];
			return { status: 'ok', count: messages.length, messages, query: parsed.query ?? query };
		} catch (err) {
			const msg = err instanceof Error ? err.message : String(err);
			console.log(`${ts()} [SearchEmail] failed: ${msg}`);
			return { error: `search_email failed: ${msg}. Fall back to work tool: "search gmail for ${query}".` };
		}
	},
};

// Self-detection: skip registration on OSS systems without gws-gmail installed.
export const tools: ToolDefinition[] = gwsAvailable()
	? [triageEmailTool, readEmailTool, searchEmailTool]
	: [];

if (tools.length === 0) {
	console.log(`${ts()} [gws-gmail-voice] gws CLI not on PATH — Gmail tools not registered (install gws-gmail skill to enable)`);
}
