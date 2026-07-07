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

import { writeFileSync, readdirSync, readFileSync, accessSync, constants, mkdirSync } from 'node:fs';
import { join } from 'node:path';

function ts(): string { return new Date().toISOString().slice(11, 23); }

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
		writeFileSync(join(this.taskDir, `${taskId}.txt`), content);
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
