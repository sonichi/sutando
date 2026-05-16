import { describe, it, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, mkdirSync, writeFileSync, utimesSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { summarize, renderMarkdown } from '../src/audit-summary.ts';
import { record, __resetLedgerForTests } from '../src/cost-accounting.ts';
import { __resetForTests as resetTenant } from '../src/tenant.ts';

let savedEnv: Record<string, string | undefined>;

function snap(): Record<string, string | undefined> {
	return {
		SUTANDO_TENANT_ID: process.env.SUTANDO_TENANT_ID,
		SUTANDO_PRIVATE_DIR: process.env.SUTANDO_PRIVATE_DIR,
		SUTANDO_WORKSPACE: process.env.SUTANDO_WORKSPACE,
	};
}

function restore(s: Record<string, string | undefined>): void {
	for (const k of Object.keys(s)) {
		if (s[k] === undefined) delete process.env[k];
		else process.env[k] = s[k];
	}
}

function writeJsonl(path: string, items: unknown[]): void {
	writeFileSync(path, items.map((i) => JSON.stringify(i)).join('\n') + '\n');
}

describe('audit-summary', () => {
	let ws: string;
	let priv: string;

	beforeEach(() => {
		savedEnv = snap();
		resetTenant();
		__resetLedgerForTests();
		ws = mkdtempSync(join(tmpdir(), 'sutando-audit-ws-'));
		priv = mkdtempSync(join(tmpdir(), 'sutando-audit-priv-'));
		process.env.SUTANDO_WORKSPACE = ws;
		process.env.SUTANDO_PRIVATE_DIR = priv;
		delete process.env.SUTANDO_TENANT_ID;
		mkdirSync(join(ws, 'data'), { recursive: true });
		mkdirSync(join(ws, 'tasks', 'archive', '2026-05'), { recursive: true });
		mkdirSync(join(ws, 'results', 'archive', '2026-05'), { recursive: true });
	});

	afterEach(() => {
		restore(savedEnv);
		resetTenant();
		__resetLedgerForTests();
		try { rmSync(ws, { recursive: true, force: true }); } catch {}
		try { rmSync(priv, { recursive: true, force: true }); } catch {}
	});

	it('empty workspace → empty counts, no cost section', () => {
		const s = summarize();
		assert.equal(s.voice.sessions, 0);
		assert.equal(s.calls.count, 0);
		assert.equal(s.tasks.archived_count, 0);
		assert.equal(s.results.archived_count, 0);
		assert.equal(s.cost, null);
		// renderMarkdown still produces output without throwing.
		assert.ok(renderMarkdown(s).includes('Voice sessions'));
	});

	it('voice metrics: filtered by date range, aggregated', () => {
		const today = new Date().toISOString();
		const lastWeek = new Date(Date.now() - 8 * 86_400_000).toISOString();
		writeJsonl(join(ws, 'data', 'voice-metrics.jsonl'), [
			{
				timestamp: today, sessionId: 's1', source: 'voice', durationMs: 60_000,
				transcriptLines: 5, toolCount: 2,
				toolCalls: [
					{ name: 'triage_email', durationMs: 1000 },
					{ name: 'work', durationMs: 2000 },
				],
				events: [{ event: 'session_started', timestamp: today }],
			},
			{
				timestamp: today, sessionId: 's2', source: 'voice', durationMs: 30_000,
				transcriptLines: 2, toolCount: 1,
				toolCalls: [{ name: 'triage_email', durationMs: 800 }],
				events: [{ event: 'error:reconnect:timeout', timestamp: today }],
			},
			{
				timestamp: lastWeek, sessionId: 's-old', source: 'voice', durationMs: 99_999,
				transcriptLines: 1, toolCount: 0, toolCalls: [], events: [],
			},
		]);
		const s = summarize();
		// Today's window — only s1+s2 (not s-old).
		assert.equal(s.voice.sessions, 2);
		assert.equal(s.voice.total_duration_ms, 90_000);
		assert.equal(s.voice.total_transcript_lines, 7);
		assert.equal(s.voice.total_tool_calls, 3);
		assert.equal(s.voice.tools_by_count['triage_email'], 2);
		assert.equal(s.voice.tools_by_count['work'], 1);
		assert.equal(s.voice.errors.length, 1);
	});

	it('call metrics: owner + meeting flags counted', () => {
		const today = new Date().toISOString();
		writeJsonl(join(ws, 'data', 'call-metrics.jsonl'), [
			{ timestamp: today, callSid: 'c1', caller: '+1', isOwner: true, isMeeting: false, durationMs: 60_000, transcriptLines: 5, toolCount: 1, toolCalls: [{ name: 'hang_up', durationMs: 200 }], events: [] },
			{ timestamp: today, callSid: 'c2', caller: '+2', isOwner: false, isMeeting: true, durationMs: 120_000, transcriptLines: 20, toolCount: 0, toolCalls: [], events: [] },
		]);
		const s = summarize();
		assert.equal(s.calls.count, 2);
		assert.equal(s.calls.owner_calls, 1);
		assert.equal(s.calls.meeting_calls, 1);
		assert.equal(s.calls.total_duration_ms, 180_000);
		assert.equal(s.calls.tools_by_count['hang_up'], 1);
	});

	it('archive counts: tasks + results by mtime within range', () => {
		const today = new Date();
		const taskFile = join(ws, 'tasks', 'archive', '2026-05', 'task-health-1.txt');
		const resultFile = join(ws, 'results', 'archive', '2026-05', 'task-discord-2.txt');
		writeFileSync(taskFile, 'x');
		writeFileSync(resultFile, 'y');
		// Touch mtimes to today.
		utimesSync(taskFile, today, today);
		utimesSync(resultFile, today, today);
		const s = summarize();
		assert.equal(s.tasks.archived_count, 1);
		assert.equal(s.results.archived_count, 1);
		assert.ok(s.tasks.by_source['health'] === 1 || s.tasks.by_source['task'] === 1);
	});

	it('cost ledger surfaces when events are present', () => {
		record({ source: 'voice', kind: 'token', amount: 1000, unit: 'in_tokens', model: 'gemini-2.5-flash' });
		record({ source: 'voice', kind: 'token', amount: 200, unit: 'out_tokens', model: 'gemini-2.5-flash' });
		record({ source: 'voice', kind: 'tool', amount: 1, unit: 'tool_call', tool: 'triage_email' });
		const s = summarize();
		assert.ok(s.cost);
		assert.equal(s.cost!.totals.in_tokens, 1000);
		assert.equal(s.cost!.totals.out_tokens, 200);
		assert.equal(s.cost!.totals.tool_calls, 1);
		const md = renderMarkdown(s);
		assert.ok(md.includes('Cost ledger'));
		assert.ok(md.includes('1,000'));
	});

	it('renderMarkdown produces a structured report with key sections', () => {
		const today = new Date().toISOString();
		writeJsonl(join(ws, 'data', 'voice-metrics.jsonl'), [
			{ timestamp: today, sessionId: 's', source: 'voice', durationMs: 12_345, transcriptLines: 1, toolCount: 0, toolCalls: [], events: [] },
		]);
		const md = renderMarkdown(summarize());
		assert.ok(md.includes('# Sutando audit summary'));
		assert.ok(md.includes('## Voice sessions'));
		assert.ok(md.includes('## Phone calls'));
		assert.ok(md.includes('## Tasks / Results'));
		assert.ok(md.includes('## Cost ledger'));
	});
});
