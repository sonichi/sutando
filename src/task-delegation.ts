/**
 * TaskDelegationService — step 4 of the interaction-planes refactor
 * (issue #1947, built under the architecture names per design R3).
 *
 * The seam between the voice-agent's delegation logic and WHERE tasks/results
 * physically live. Two backends:
 *
 * - LocalTaskBackend — today's byte-identical file I/O against the local
 *   workspace (`tasks/`, `results/`). Selected whenever the workspace tasks/
 *   dir is writable, i.e. every co-located deployment. Cost of the seam on
 *   this path: one writability probe at boot, sync calls throughout, no
 *   behavior change.
 *
 * - RelayTaskBackend — the same operations over HTTP against agent-api.py on
 *   the core host, for a voice-agent running on a different machine. Only
 *   the task hand-off crosses the network; mic/TTS/Gemini stay local to the
 *   voice host. Requires CORE_API_URL (+ SUTANDO_API_TOKEN when the core
 *   enforces auth).
 *
 * Selection is automatic, not configured (design decision from the owner
 * thread 2026-07-06): probe the workspace at boot — writable → local;
 * else CORE_API_URL → relay; else fail LOUD. One boot log line states the
 * chosen mode.
 *
 * The result-watcher logic in task-bridge.ts (voice-file queueing, marker
 * handling, archival ordering) is deliberately NOT duplicated here: the
 * backend abstracts only the I/O primitives the watcher consumes (list /
 * read / archive), so the same watcher semantics run over either backend.
 */

import { writeFileSync, readdirSync, readFileSync, accessSync, constants, mkdirSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFile } from 'node:child_process';
import { findRepoRoot } from './sutando_config.js';
import { tryStampText } from './task_envelope.js';

function ts(): string { return new Date().toISOString().slice(11, 23); }

// Anonymous, opt-out product telemetry for locally-created tasks (voice, chat,
// context-drop). The Python bridges (discord/slack/telegram) emit task_processed
// at their own accept points; the co-located voice-agent path is the gap this
// closes. We shell out to src/telemetry.py (sibling of this module) rather than
// re-implement the PostHog client in TS — one source of truth, and it no-ops
// cleanly when telemetry is opted out / unconfigured. Fire-and-forget: never
// blocks or throws into task submission. Source is read from the task file's own
// `source:` header, so every surface is tagged with exactly what it wrote.
// Resolve telemetry.py from the repo root, not the running module's dir: the
// bundled build runs dist/voice-agent.js and ships no .py files in dist/ —
// src/*.py travel as a SIBLING of dist/ (same defect class as the
// screen-capture-server path, field report 2026-08-14). Module-sibling stays
// as the dev fallback; the fire-and-forget execFile tolerates a miss either way.
const _TELEMETRY_PY = (() => {
	const moduleDir = dirname(fileURLToPath(import.meta.url));
	const root = findRepoRoot(moduleDir);
	const canonical = root ? join(root, 'src', 'telemetry.py') : null;
	return canonical && existsSync(canonical) ? canonical : join(moduleDir, 'telemetry.py');
})();
/** Read the coarse surface bucket from a task file's own `source:` header —
 * `voice` / `chat` / `context-drop` / … — falling back to `unknown` when the
 * header is absent. Pure + exported so the tag is unit-tested without spawning. */
export function parseTaskSource(content: string): string {
	const m = content.match(/^source:\s*(\S+)/m);
	return m ? m[1] : 'unknown';
}
export function emitTaskProcessed(content: string): void {
	try {
		execFile('python3', [_TELEMETRY_PY, 'task_processed', parseTaskSource(content)], () => { /* fire-and-forget */ });
	} catch { /* telemetry must never break task submission */ }
}

export interface TaskDelegationService {
	readonly mode: 'local' | 'relay';
	/** Durably submit a fully-serialized task file body under the given id.
	 * The caller builds the content (header order is writer-owned — see
	 * local_task_protocol's shape taxonomy); the backend only stores it. */
	submitTask(taskId: string, content: string): void | Promise<void>;
	/** Basenames of files currently in results/ (sorted, .txt only). */
	listResultFiles(): string[] | Promise<string[]>;
	/** Raw body of one result file. */
	readResultFile(name: string): string | Promise<string>;
	/** Archive (or as a last resort delete) a delivered result file. */
	archiveResultFile(name: string, taskId: string): void | Promise<void>;
}

// ── Local backend — the co-located path, byte-identical to pre-seam code ────

export class LocalTaskBackend implements TaskDelegationService {
	readonly mode = 'local' as const;

	constructor(
		private taskDir: string,
		private resultDir: string,
		private archiveFile: (srcPath: string, kind: 'tasks' | 'results', taskId: string) => void,
	) {}

	submitTask(taskId: string, content: string): void {
		// HMAC envelope (#3014 writer census): stamp at this writer's edge,
		// fail-open so a stamping error never costs the delegation.
		const stamped = tryStampText(content, dirname(this.taskDir));
		writeFileSync(join(this.taskDir, `${taskId}.txt`), stamped);
		emitTaskProcessed(stamped);
	}

	listResultFiles(): string[] {
		return readdirSync(this.resultDir).filter(f => f.endsWith('.txt')).sort();
	}

	readResultFile(name: string): string {
		return readFileSync(join(this.resultDir, name), 'utf-8');
	}

	archiveResultFile(name: string, taskId: string): void {
		this.archiveFile(join(this.resultDir, name), 'results', taskId);
	}
}

// ── Relay backend — same operations over agent-api on the core host ─────────

export class RelayTaskBackend implements TaskDelegationService {
	readonly mode = 'relay' as const;
	private headers: Record<string, string>;

	constructor(private baseUrl: string, apiToken?: string) {
		this.baseUrl = baseUrl.replace(/\/+$/, '');
		this.headers = { 'Content-Type': 'application/json' };
		if (apiToken) this.headers['Authorization'] = `Bearer ${apiToken}`;
	}

	private async req(method: string, path: string, body?: unknown): Promise<Response> {
		const res = await fetch(`${this.baseUrl}${path}`, {
			method,
			headers: this.headers,
			body: body === undefined ? undefined : JSON.stringify(body),
			signal: AbortSignal.timeout(30_000),
		});
		if (!res.ok) throw new Error(`${method} ${path} → ${res.status}`);
		return res;
	}

	async submitTask(taskId: string, content: string): Promise<void> {
		await this.req('POST', '/delegation/tasks', { id: taskId, content });
	}

	async listResultFiles(): Promise<string[]> {
		const res = await this.req('GET', '/delegation/results');
		const data = await res.json() as { files?: string[] };
		return (data.files ?? []).filter(f => f.endsWith('.txt')).sort();
	}

	async readResultFile(name: string): Promise<string> {
		const res = await this.req('GET', `/delegation/results/${encodeURIComponent(name)}`);
		const data = await res.json() as { body?: string };
		return data.body ?? '';
	}

	async archiveResultFile(name: string, taskId: string): Promise<void> {
		await this.req('POST', '/delegation/archive', { name, task_id: taskId });
	}
}

// ── Automatic selection (boot-time, loud failure) ────────────────────────────

export function selectBackend(
	taskDir: string,
	resultDir: string,
	archiveFile: (srcPath: string, kind: 'tasks' | 'results', taskId: string) => void,
): TaskDelegationService {
	// Relay is a POSITIVE configuration: CORE_API_URL set → relay, period.
	// The earlier draft probed local writability FIRST, which made relay
	// unreachable on any normal voice-host checkout (the default workspace is
	// <repo>/workspace — always writable), silently stranding delegated tasks
	// on the wrong machine (Codex P1 on PR #1956). Explicit config beats
	// probing: an operator who set CORE_API_URL said where the core lives.
	const coreUrl = process.env.CORE_API_URL || '';
	if (coreUrl) {
		console.log(`${ts()} [TaskDelegation] mode=relay (CORE_API_URL=${coreUrl})`);
		return new RelayTaskBackend(coreUrl, process.env.SUTANDO_API_TOKEN);
	}

	try {
		mkdirSync(taskDir, { recursive: true });
		mkdirSync(resultDir, { recursive: true });
		accessSync(taskDir, constants.W_OK);
	} catch {
		// Fail loud: silently picking a broken backend strands owner tasks —
		// the 2026-05-18 context-drop silent-write lesson, applied at the seam.
		throw new Error(
			'[TaskDelegation] no viable backend: CORE_API_URL is not set and the ' +
			`workspace tasks/ dir is not writable (${taskDir}). Co-located: fix ` +
			'workspace permissions. Split-host: set CORE_API_URL ' +
			'(+ SUTANDO_API_TOKEN) to the core host agent-api.');
	}
	console.log(`${ts()} [TaskDelegation] mode=local (workspace tasks/ writable: ${taskDir})`);
	return new LocalTaskBackend(taskDir, resultDir, archiveFile);
}
