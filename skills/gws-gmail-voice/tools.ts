// gws-gmail-voice: inline Gmail triage for voice/phone agents.
//
// Why this exists: the voice agent's only path to "read Gmail" was openUrlTool
// (open Gmail in Chrome → caller can't read it via voice) or workTool (delegate
// to core agent → ~5-30s round-trip). Today's phone-call failure (2026-05-14):
// Gemini reached for openUrlTool and hallucinated the Gmail URL.
//
// This skill exposes `triage_email` as an inline tool that wraps the existing
// `gws gmail +triage` CLI (from `~/.claude/skills/gws-gmail/`) and returns
// parsed JSON the LLM can summarize in-band, sub-second.
//
// OSS-safety: if the `gws` CLI is not on PATH (user hasn't installed the
// gws-gmail skill / configured OAuth), this module exports an empty tools
// array. The voice agent simply doesn't see triage_email — no broken tool,
// no error noise. Detection is at module-load time via execFileSync('which').

import { execFileSync } from 'node:child_process';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';

const ts = () => new Date().toLocaleTimeString('en-US', { hour12: false });

function gwsAvailable(): boolean {
	try {
		execFileSync('which', ['gws'], { stdio: ['ignore', 'pipe', 'pipe'], timeout: 1_000 });
		return true;
	} catch {
		return false;
	}
}

const triageEmailTool: ToolDefinition = {
	name: 'triage_email',
	description:
		'Summarize the user\'s unread Gmail inbox. ' +
		'Use when the caller asks: "what unread emails do I have", "summarize my inbox", "what\'s the most important email I missed", "any urgent emails". ' +
		'Returns a list of recent unread messages with sender, subject, and date. ' +
		'Do NOT use openUrlTool to open Gmail in Chrome — that path can\'t read the content back to the caller. ' +
		'For sending email, deletion, or replies, delegate to work; this tool is read-only triage.',
	parameters: z.object({
		max: z.number().int().min(1).max(20).optional().describe('Max messages to return (default 5).'),
		query: z.string().optional().describe('Optional Gmail search query (default: is:unread).'),
	}),
	execution: 'inline',
	async execute(args) {
		const { max = 5, query } = args as { max?: number; query?: string };
		console.log(`${ts()} [TriageEmail] called (max=${max}${query ? `, query="${query}"` : ''})`);
		try {
			// execFileSync — no shell interpolation. `gws` is the canonical CLI
			// from ~/.claude/skills/gws-gmail/; --format json gives a structured
			// payload we can pass back to the LLM.
			const cmdArgs = ['gmail', '+triage', '--format', 'json', '--max', String(max)];
			if (query) cmdArgs.push('--query', query);
			const stdout = execFileSync('gws', cmdArgs, {
				timeout: 5_000,
				encoding: 'utf8',
				stdio: ['ignore', 'pipe', 'pipe'],
			});
			// gws prints diagnostic header lines + JSON; find the first '['.
			const jsonStart = stdout.indexOf('[');
			if (jsonStart === -1) return { error: 'triage_email: gws did not return JSON' };
			const messages = JSON.parse(stdout.slice(jsonStart));
			console.log(`${ts()} [TriageEmail] ${Array.isArray(messages) ? messages.length : '?'} messages`);
			return { status: 'ok', count: Array.isArray(messages) ? messages.length : 0, messages };
		} catch (err) {
			const msg = err instanceof Error ? err.message : String(err);
			console.log(`${ts()} [TriageEmail] failed: ${msg}`);
			return { error: `triage_email failed: ${msg}` };
		}
	},
};

// Self-detection: skip registration on OSS systems without gws-gmail installed.
export const tools: ToolDefinition[] = gwsAvailable() ? [triageEmailTool] : [];

if (tools.length === 0) {
	console.log(`${ts()} [gws-gmail-voice] gws CLI not on PATH — triage_email not registered (install gws-gmail skill to enable)`);
}
