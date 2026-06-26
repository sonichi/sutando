/**
 * Live `.jsonl` transcript source — the first real `ExecutorSource` for the
 * interactive core. Tails Claude Code's session transcript and writes real
 * obs events + usage records into the spine's jsonl-file sink + usage ledger,
 * so a running core's actual tool calls + token usage show up downstream
 * (e.g. the visualizer) with NO relaunch and NO settings change.
 *
 * Parses the REAL transcript schema (verified 2026-06): lines are nested under
 * `.message`, tool calls live in `.message.content[]` as `{type:"tool_use"}`,
 * usage is `.message.usage` with `cache_read_input_tokens` /
 * `cache_creation_input_tokens`. (This is the Anthropic-internal format the
 * Phase-0 `transcript-map.ts` flagged as drift-prone — handled here against
 * ground truth.) Only structural fields are extracted — tool_name, ids, token
 * counts, file paths — never tool input/output content.
 *
 * Usage:
 *   SUTANDO_WORKSPACE=<dir> tsx src/observability/claude/jsonl-tail.ts \
 *       [--transcript <path>] [--project <slug>] [--once] [--backfill N]
 *
 * Default: auto-pick the most-recently-modified transcript under
 * ~/.claude/projects/*, backfill the whole file, then follow appended lines.
 */

import { readdirSync, statSync, openSync, readSync, closeSync, appendFileSync, mkdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { claudeHomePath } from '../../util_paths.js';
import { resolveWorkspace } from '../../workspace_default.js';
import { JsonlFileSink } from '../sink.js';
import { ledgerPath } from '../meter.js';
import { nodeId } from '../node.js';
import type { ObsEvent, AccessTier } from '../events.js';
import type { UsageRecord } from '../usage.js';

const ACTOR = { user_id: 'core', channel: 'claude-code', access_tier: 'owner' as AccessTier, tenant_id: null };
const sink = new JsonlFileSink();

function arg(flag: string): string | undefined {
	const i = process.argv.indexOf(flag);
	return i >= 0 ? process.argv[i + 1] : undefined;
}
const num = (v: unknown): number | undefined => (typeof v === 'number' ? v : undefined);
const clean = <T extends Record<string, unknown>>(o: T): T => Object.fromEntries(Object.entries(o).filter(([, v]) => v !== undefined)) as T;

// Full tool input/output capture with a per-field safety cap (raise via SUTANDO_IO_CAP).
const IO_CAP = Number(process.env.SUTANDO_IO_CAP) || 16384;
function trunc(v: unknown): unknown {
	if (v == null) return undefined;
	if (typeof v === 'string') return v.length > IO_CAP ? v.slice(0, IO_CAP) + `…[+${v.length - IO_CAP} chars]` : v;
	let s: string;
	try { s = JSON.stringify(v); } catch { return String(v).slice(0, IO_CAP); }
	return s.length > IO_CAP ? s.slice(0, IO_CAP) + `…[+${s.length - IO_CAP} chars]` : v;
}
function contentToStr(c: unknown): string | undefined {
	if (c == null) return undefined;
	if (typeof c === 'string') return c;
	if (Array.isArray(c)) return c.map((x: any) => (typeof x === 'string' ? x : (x?.text ?? JSON.stringify(x)))).join('\n');
	try { return JSON.stringify(c); } catch { return String(c); }
}

function projectsDir(): string {
	return process.env.CLAUDE_CONFIG_DIR
		? join(process.env.CLAUDE_CONFIG_DIR, 'projects')
		: claudeHomePath('projects');
}

/** Find the newest transcript across all projects (or a given --project slug). */
function newestTranscript(): string | undefined {
	const base = projectsDir();
	const slug = arg('--project');
	const dirs = slug ? [join(base, slug)] : safeList(base).map((d) => join(base, d));
	let best: { f: string; m: number } | undefined;
	for (const d of dirs) {
		for (const f of safeList(d)) {
			if (!f.endsWith('.jsonl')) continue;
			const p = join(d, f);
			let m: number;
			try { m = statSync(p).mtimeMs; } catch { continue; }
			if (!best || m > best.m) best = { f: p, m };
		}
	}
	return best?.f;
}
function safeList(d: string): string[] { try { return readdirSync(d); } catch { return []; } }

function fileOp(tool: string): { kind: 'file.read' | 'file.change'; op?: string } | null {
	if (tool === 'Read') return { kind: 'file.read' };
	if (tool === 'Write') return { kind: 'file.change', op: 'written' };
	if (tool === 'Edit' || tool === 'MultiEdit' || tool === 'NotebookEdit') return { kind: 'file.change', op: 'modified' };
	return null;
}

function writeUsage(u: UsageRecord): void {
	const path = ledgerPath(u.ts * 1000);
	mkdirSync(dirname(path), { recursive: true });
	appendFileSync(path, JSON.stringify(u) + '\n', { flag: 'a' });
}

let usageCount = 0;
let eventCount = 0;

/** Map one real transcript line to spine records and write them. */
function handleLine(line: { type?: string; message?: any; requestId?: string; sessionId?: string; timestamp?: string; isSidechain?: boolean }): void {
	const session = line.sessionId || 'unknown';
	const trace = 'cc-sess:' + session;
	const ts = line.timestamp ? Date.parse(line.timestamp) / 1000 : Date.now() / 1000;
	const node = nodeId();
	const base = (kind: string, outcome: ObsEvent['outcome'] = 'ok'): ObsEvent => ({
		schema: 1, ts, trace_id: trace, node, source: 'core-cli', actor: ACTOR, kind, outcome,
	});

	if (line.type === 'assistant' && line.message) {
		const m = line.message;
		// usage record for the turn
		const u = m.usage;
		if (u && (u.input_tokens != null || u.output_tokens != null)) {
			const inTok = num(u.input_tokens) ?? 0;
			const outTok = num(u.output_tokens) ?? 0;
			writeUsage({
				schema: 1, usage_id: 'cc:tok:' + (line.requestId || session + ':' + Math.floor(ts)),
				ts, tenant_id: null, trace_id: trace, actor: ACTOR, source: 'core-cli',
				meter: 'claude.tokens', quantity: inTok + outTok, unit: 'tokens',
				provider: 'anthropic', provider_ref: line.requestId || null,
				attrs: clean({ model: m.model, input_tokens: inTok, output_tokens: outTok, cache_read: num(u.cache_read_input_tokens), cache_creation: num(u.cache_creation_input_tokens), _cc_source: 'jsonl' }),
			});
			usageCount++;
		}
		// assistant content blocks: thinking · text · tool calls
		for (const c of Array.isArray(m.content) ? m.content : []) {
			if (c?.type === 'thinking') {
				// Claude Code persists thinking as signature-only — the plaintext
				// `thinking` field is empty on disk. Surface a redacted marker (so the
				// reasoning cadence is visible) and carry text only if a build ever ships it.
				const txt = typeof c.thinking === 'string' ? c.thinking : typeof c.text === 'string' ? c.text : '';
				const e = base('cc.thinking');
				e.data = txt.length > 0 ? clean({ text: trunc(txt) }) : { redacted: true };
				sink.write(e); eventCount++;
			} else if (c?.type === 'text') {
				const e = base('cc.text');
				e.data = clean({ text: trunc(c.text) });
				sink.write(e); eventCount++;
			} else if (c?.type === 'tool_use') {
				const ev = base('tool.call');
				ev.data = clean({ tool_name: c.name, tool_use_id: c.id, subagent: line.isSidechain || undefined, tool_input: trunc(c.input) });
				sink.write(ev); eventCount++;
				const fo = fileOp(c.name);
				const path = c.input && (c.input.file_path || c.input.notebook_path || c.input.path);
				if (fo && typeof path === 'string') {
					const fe = base(fo.kind);
					fe.source_file = path;
					fe.data = clean({ op: fo.op, tool_name: c.name });
					sink.write(fe); eventCount++;
				}
			}
		}
	} else if (line.type === 'user' && line.message) {
		for (const c of Array.isArray(line.message.content) ? line.message.content : []) {
			if (c?.type !== 'tool_result') continue;
			const ev = base('tool.result', c.is_error ? 'error' : 'ok');
			ev.data = clean({ tool_use_id: c.tool_use_id, tool_output: trunc(contentToStr(c.content)) });
			sink.write(ev); eventCount++;
		}
	}
}

// ---- tail loop ------------------------------------------------------------
function main(): void {
	const file = arg('--transcript') || newestTranscript();
	if (!file) {
		console.error('no transcript found under', projectsDir());
		process.exit(1);
	}
	const once = process.argv.includes('--once');
	const ws = resolveWorkspace();
	console.log(`tailing ${file}\n   → ${ws}/logs + /data/usage`);

	let offset = 0;
	// optional backfill: start N bytes back from EOF instead of from 0
	const backfillN = Number(arg('--backfill'));
	const pump = () => {
		let size: number;
		try { size = statSync(file).size; } catch { return; }
		if (size <= offset) return;
		let buf: Buffer;
		try {
			const fd = openSync(file, 'r');
			buf = Buffer.alloc(size - offset);
			readSync(fd, buf, 0, size - offset, offset);
			closeSync(fd);
		} catch { return; }
		offset = size;
		for (const ln of buf.toString('utf8').split('\n')) {
			const s = ln.trim();
			if (!s) continue;
			try { handleLine(JSON.parse(s)); } catch { /* skip malformed / partial line */ }
		}
	};

	// backfill the whole file (offset stays 0) unless --backfill <N> trims it
	if (Number.isFinite(backfillN) && backfillN > 0) {
		try { offset = Math.max(0, statSync(file).size - backfillN); } catch { /* ignore */ }
	}
	pump();
	console.log(`backfilled ${eventCount} events · ${usageCount} usage records`);
	if (once) return;
	console.log('following live (ctrl-c to stop)…');
	setInterval(pump, 1000);
}

main();
