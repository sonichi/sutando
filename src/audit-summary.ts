/**
 * Sutando — Audit Summary CLI
 *
 * Implements §5 Next: "Audit UI built on existing audit.jsonl files. 'What
 * did the agent do, on which device, with which methodology, why?' A trust
 * feature for productization, not just a debugging artifact."
 *
 * First-cut shape: a CLI that aggregates the existing JSONL audit sources
 * into a markdown summary. Web UI is a future iteration; CLI lands first
 * because (a) it's the canonical interface for owner introspection,
 * (b) every other diagnostic in the repo (self-diagnose, regression-search)
 * is a CLI too, and (c) the output composes — `audit-summary | gh issue
 * create --body-file -` is one line of plumbing.
 *
 * Sources consumed (any subset; missing files are skipped):
 *   - data/voice-metrics.jsonl       (voice-agent session metrics)
 *   - data/call-metrics.jsonl        (phone-conversation metrics) — also
 *     looks under workspace/tasks/data/ and tasks/archive/data/ for moved
 *     fleet copies.
 *   - state/cost-accounting/usage-<tenant>.jsonl  (from PR #749)
 *   - tasks/archive/<YYYY-MM>/                    (task lifecycle counts)
 *   - results/archive/<YYYY-MM>/                  (result lifecycle counts)
 *
 * Output: markdown to stdout. Use `--json` for machine-readable shape.
 *
 * Usage:
 *   tsx src/audit-summary.ts                  # today
 *   tsx src/audit-summary.ts --since 7d       # last 7 days
 *   tsx src/audit-summary.ts --since 2026-05-01 --until 2026-05-15
 *   tsx src/audit-summary.ts --tenant acme    # cost rollup scoped
 *   tsx src/audit-summary.ts --json           # machine-readable
 */

import { readFileSync, existsSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { rollup as costRollup, type UsageRollup } from './cost-accounting.js';
import { tenantId } from './tenant.js';

interface VoiceMetric {
	timestamp: string;
	sessionId: string;
	source: string;
	durationMs: number;
	transcriptLines: number;
	toolCalls: Array<{ name: string; durationMs: number }>;
	toolCount: number;
	events: Array<{ event: string; timestamp: string }>;
}

interface CallMetric {
	timestamp: string;
	callSid: string;
	caller: string;
	isOwner: boolean;
	isMeeting: boolean;
	durationMs: number;
	transcriptLines: number;
	toolCalls: Array<{ name: string; durationMs: number }>;
	toolCount: number;
	events: Array<{ event: string; timestamp: string }>;
}

export interface AuditSummary {
	from: string;
	to: string;
	voice: {
		sessions: number;
		total_duration_ms: number;
		total_transcript_lines: number;
		total_tool_calls: number;
		tools_by_count: Record<string, number>;
		errors: string[];
	};
	calls: {
		count: number;
		total_duration_ms: number;
		owner_calls: number;
		meeting_calls: number;
		tools_by_count: Record<string, number>;
	};
	tasks: {
		archived_count: number;
		by_source: Record<string, number>;
	};
	results: {
		archived_count: number;
	};
	cost: UsageRollup | null;
}

function workspaceDir(): string {
	return process.env.SUTANDO_WORKSPACE || process.cwd();
}

function findFirst(...paths: string[]): string | null {
	for (const p of paths) if (existsSync(p)) return p;
	return null;
}

function readJsonl<T>(path: string | null): T[] {
	if (!path) return [];
	try {
		const raw = readFileSync(path, 'utf-8');
		return raw.split('\n').filter((l) => l.length > 0).flatMap((l) => {
			try { return [JSON.parse(l) as T]; }
			catch { return []; }
		});
	} catch { return []; }
}

function listArchive(dir: string, since: Date, until: Date): { count: number; bySource: Record<string, number> } {
	const result = { count: 0, bySource: {} as Record<string, number> };
	if (!existsSync(dir)) return result;
	for (const entry of readdirSync(dir)) {
		const full = join(dir, entry);
		try {
			const st = statSync(full);
			if (st.isDirectory()) {
				// recurse — archive layout is <YYYY-MM>/<file>.txt
				const sub = listArchive(full, since, until);
				result.count += sub.count;
				for (const [k, v] of Object.entries(sub.bySource)) {
					result.bySource[k] = (result.bySource[k] ?? 0) + v;
				}
				continue;
			}
			if (st.mtime < since || st.mtime > until) continue;
			result.count++;
			// Best-effort source classification from filename: task-<source>-<ts>.txt.
			// task-<source>-<ts>.txt — source is one segment of lowercase letters
			// between two hyphens. Stop at the first hyphen so we don't accidentally
			// pick up the trailing timestamp.
			const m = entry.match(/^(task|result|proactive|question|briefing|insight|friction)(?:-([a-z]+))?-?\d*/i);
			const source = m ? (m[2] || m[1] || 'unknown').toLowerCase() : 'unknown';
			result.bySource[source] = (result.bySource[source] ?? 0) + 1;
		} catch { /* unreadable entry */ }
	}
	return result;
}

function parseDuration(spec: string, now: Date): Date {
	// Accept "7d", "24h", "30m" or an ISO date.
	const m = spec.match(/^(\d+)([dhm])$/);
	if (m) {
		const n = Number(m[1]);
		const unit = m[2];
		const ms = unit === 'd' ? n * 86_400_000 : unit === 'h' ? n * 3_600_000 : n * 60_000;
		return new Date(now.getTime() - ms);
	}
	const d = new Date(spec);
	if (Number.isNaN(d.getTime())) throw new Error(`unparseable date: ${spec}`);
	return d;
}

export function summarize(opts: { since?: Date; until?: Date; tenant?: string; ws?: string } = {}): AuditSummary {
	const now = new Date();
	const until = opts.until ?? now;
	const since = opts.since ?? new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), 0, 0, 0));
	const ws = opts.ws ?? workspaceDir();
	const fromIso = since.toISOString();
	const toIso = until.toISOString();

	// --- voice metrics ---
	const voicePath = findFirst(
		join(ws, 'data', 'voice-metrics.jsonl'),
		join(ws, 'workspace', 'data', 'voice-metrics.jsonl'),
	);
	const voice = readJsonl<VoiceMetric>(voicePath).filter((m) => m.timestamp >= fromIso && m.timestamp <= toIso);
	const voiceTools: Record<string, number> = {};
	const voiceErrors: string[] = [];
	let voiceDuration = 0;
	let voiceLines = 0;
	let voiceToolCount = 0;
	for (const m of voice) {
		voiceDuration += m.durationMs;
		voiceLines += m.transcriptLines;
		voiceToolCount += m.toolCount;
		for (const tc of m.toolCalls || []) {
			voiceTools[tc.name] = (voiceTools[tc.name] ?? 0) + 1;
		}
		for (const ev of m.events || []) {
			if (ev.event.startsWith('error:')) voiceErrors.push(ev.event);
		}
	}

	// --- call metrics ---
	const callPath = findFirst(
		join(ws, 'data', 'call-metrics.jsonl'),
		join(ws, 'workspace', 'data', 'call-metrics.jsonl'),
		join(ws, 'tasks', 'archive', 'data', 'call-metrics.jsonl'),
	);
	const calls = readJsonl<CallMetric>(callPath).filter((m) => m.timestamp >= fromIso && m.timestamp <= toIso);
	const callTools: Record<string, number> = {};
	let callDuration = 0;
	let ownerCalls = 0;
	let meetingCalls = 0;
	for (const m of calls) {
		callDuration += m.durationMs;
		if (m.isOwner) ownerCalls++;
		if (m.isMeeting) meetingCalls++;
		for (const tc of m.toolCalls || []) {
			callTools[tc.name] = (callTools[tc.name] ?? 0) + 1;
		}
	}

	// --- task / result archive counts ---
	const tasksArch = listArchive(join(ws, 'tasks', 'archive'), since, until);
	const resultsArch = listArchive(join(ws, 'results', 'archive'), since, until);

	// --- cost rollup (per tenant, from #749) ---
	let cost: UsageRollup | null = null;
	try {
		cost = costRollup({ tenant_id: opts.tenant ?? tenantId(ws), from: fromIso, to: toIso });
		if (cost.event_count === 0) cost = null;
	} catch { cost = null; }

	return {
		from: fromIso,
		to: toIso,
		voice: {
			sessions: voice.length,
			total_duration_ms: voiceDuration,
			total_transcript_lines: voiceLines,
			total_tool_calls: voiceToolCount,
			tools_by_count: voiceTools,
			errors: voiceErrors.slice(0, 10),  // cap
		},
		calls: {
			count: calls.length,
			total_duration_ms: callDuration,
			owner_calls: ownerCalls,
			meeting_calls: meetingCalls,
			tools_by_count: callTools,
		},
		tasks: {
			archived_count: tasksArch.count,
			by_source: tasksArch.bySource,
		},
		results: {
			archived_count: resultsArch.count,
		},
		cost,
	};
}

export function renderMarkdown(s: AuditSummary): string {
	const mins = (ms: number) => (ms / 60_000).toFixed(1);
	const topN = (o: Record<string, number>, n = 5): string =>
		Object.entries(o).sort((a, b) => b[1] - a[1]).slice(0, n)
			.map(([k, v]) => `${k} (${v})`).join(', ') || '(none)';

	const lines: string[] = [];
	lines.push(`# Sutando audit summary — ${s.from} → ${s.to}`);
	lines.push('');
	lines.push('## Voice sessions');
	lines.push(`- Sessions: **${s.voice.sessions}**`);
	lines.push(`- Total duration: ${mins(s.voice.total_duration_ms)} min`);
	lines.push(`- Transcript lines: ${s.voice.total_transcript_lines}`);
	lines.push(`- Tool calls: ${s.voice.total_tool_calls} — top: ${topN(s.voice.tools_by_count)}`);
	if (s.voice.errors.length > 0) {
		lines.push(`- Errors: ${s.voice.errors.length}`);
		for (const e of s.voice.errors) lines.push(`  - ${e}`);
	}
	lines.push('');
	lines.push('## Phone calls');
	lines.push(`- Calls: **${s.calls.count}** (${s.calls.owner_calls} owner, ${s.calls.meeting_calls} meetings)`);
	lines.push(`- Total duration: ${mins(s.calls.total_duration_ms)} min`);
	lines.push(`- Tool calls top: ${topN(s.calls.tools_by_count)}`);
	lines.push('');
	lines.push('## Tasks / Results');
	lines.push(`- Archived tasks: **${s.tasks.archived_count}** — top sources: ${topN(s.tasks.by_source)}`);
	lines.push(`- Archived results: **${s.results.archived_count}**`);
	lines.push('');
	if (s.cost) {
		lines.push(`## Cost ledger (tenant=${s.cost.tenant_id})`);
		lines.push(`- Events: ${s.cost.event_count}`);
		lines.push(`- Tokens in/out: ${s.cost.totals.in_tokens.toLocaleString()} / ${s.cost.totals.out_tokens.toLocaleString()}`);
		lines.push(`- Tool calls: ${s.cost.totals.tool_calls}, requests: ${s.cost.totals.requests}`);
		const models = Object.keys(s.cost.by_model);
		if (models.length > 0) {
			lines.push(`- By model: ${models.map((m) => `${m} (in=${s.cost!.by_model[m].in_tokens}, out=${s.cost!.by_model[m].out_tokens})`).join('; ')}`);
		}
		const tools = Object.entries(s.cost.by_tool);
		if (tools.length > 0) {
			lines.push(`- By tool: ${tools.sort((a, b) => b[1] - a[1]).slice(0, 5).map(([k, v]) => `${k} (${v})`).join(', ')}`);
		}
		const devices = Object.entries(s.cost.by_device);
		if (devices.length > 0) {
			lines.push(`- By device: ${devices.sort((a, b) => b[1] - a[1]).slice(0, 5).map(([k, v]) => `${k} (${v})`).join(', ')}`);
		}
	} else {
		lines.push('## Cost ledger');
		lines.push('- No events recorded yet (cost-accounting emitters not wired).');
	}
	return lines.join('\n') + '\n';
}

// --- CLI entry point ---

function parseArgs(argv: string[]): { since?: string; until?: string; tenant?: string; json: boolean } {
	const out: { since?: string; until?: string; tenant?: string; json: boolean } = { json: false };
	for (let i = 0; i < argv.length; i++) {
		const a = argv[i];
		if (a === '--since') out.since = argv[++i];
		else if (a === '--until') out.until = argv[++i];
		else if (a === '--tenant') out.tenant = argv[++i];
		else if (a === '--json') out.json = true;
		else if (a === '--help' || a === '-h') {
			console.log('Usage: tsx src/audit-summary.ts [--since 7d|YYYY-MM-DD] [--until YYYY-MM-DD] [--tenant ID] [--json]');
			process.exit(0);
		}
	}
	return out;
}

// Only run main() when invoked directly, not when imported by tests.
// Use `realpath` on both sides so macOS's `/tmp` → `/private/tmp` symlink
// doesn't cause a false "imported" classification when invoked from a
// tmp path. import.meta.url includes the resolved /private/tmp form;
// process.argv[1] is the user-supplied path which may not.
import { realpathSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
const isMain = (() => {
	try {
		return realpathSync(fileURLToPath(import.meta.url)) === realpathSync(process.argv[1]);
	} catch { return false; }
})();
if (isMain) {
	const args = parseArgs(process.argv.slice(2));
	const now = new Date();
	const since = args.since ? parseDuration(args.since, now) : undefined;
	const until = args.until ? parseDuration(args.until, now) : undefined;
	const summary = summarize({ since, until, tenant: args.tenant });
	if (args.json) console.log(JSON.stringify(summary, null, 2));
	else process.stdout.write(renderMarkdown(summary));
}
