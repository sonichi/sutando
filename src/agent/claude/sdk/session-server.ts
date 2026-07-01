/**
 * src/agent/claude/sdk/session-server.ts — a PERSISTENT, observable Sutando
 * core over the Agent SDK (`@anthropic-ai/claude-agent-sdk`), with a browser UI.
 *
 * This is the `claude_sdk` core: one long-lived `query()` session (async-iterable
 * prompt = multi-turn, context preserved) that runs the FULL Sutando runtime.
 *
 * RUNTIME SPLIT (hybrid):
 *  - AGENT-driven (via the `/startup` bootstrap turn): registers crons
 *    (CronCreate), runs proactive-loop passes, and everything else the
 *    interactive core does — because we load CLAUDE.md + skills (settingSources)
 *    and use Claude Code's system prompt (systemPrompt preset).
 *  - HOST-driven (this Node process): watches <workspace>/tasks/ and injects each
 *    task as a turn, and emits the per-host liveness heartbeat + core-status. We
 *    set SUTANDO_HOST_OWNS_WATCHER=1 so the agent's own Monitor task-watcher
 *    (src/watch-tasks-stream.sh) no-ops — otherwise every task double-processes.
 *    On pickup a task is atomically moved to an in-flight staging dir BEFORE
 *    injection, so the proactive loop's own tasks/ scan never races it.
 *
 * Endpoints (localhost only): GET / (UI) · GET /events (SSE, every SDK message) ·
 * POST /input {text} (a user turn) · GET /health.
 *
 * Env: SUTANDO_SESSION_PORT (4100) · SUTANDO_SESSION_BIND (127.0.0.1) ·
 * SUTANDO_SESSION_FAKE=1 (offline echo mode; host runtime still runs) ·
 * SUTANDO_SESSION_BOOTSTRAP (default "/startup") · SUTANDO_SESSION_NO_BOOTSTRAP=1.
 * Provider/auth come from the env (session-server.sh sources provider-env.sh).
 */

import { createServer, type ServerResponse, type IncomingMessage } from 'node:http';
import { readFileSync, writeFileSync, renameSync, mkdirSync, readdirSync, existsSync, unlinkSync, watch } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { execSync } from 'node:child_process';
import os from 'node:os';
import { query } from '@anthropic-ai/claude-agent-sdk';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = process.cwd(); // session-server.sh cd's to the repo before exec
const PORT = Number(process.env.SUTANDO_SESSION_PORT) || 4100;
const HOST = process.env.SUTANDO_SESSION_BIND || '127.0.0.1';
const FAKE = process.env.SUTANDO_SESSION_FAKE === '1';
const NO_BOOTSTRAP = process.env.SUTANDO_SESSION_NO_BOOTSTRAP === '1';
// The host owns task-watching, the heartbeat, and cron/proactive SCHEDULING.
// CronCreate/CronList are NOT available in an SDK session (verified live), so the
// bootstrap must NOT ask the agent to run /schedule-crons or manage the watcher —
// it would fail and improvise (e.g. shelling out to `claude`, spawning nested
// sessions). Instead we orient it: process what the host injects, nothing more.
// Override with SUTANDO_SESSION_BOOTSTRAP (e.g. '/startup') or disable entirely
// with SUTANDO_SESSION_NO_BOOTSTRAP=1.
const DEFAULT_BOOTSTRAP =
	'You are running as the Sutando SDK core (core_type claude_sdk); your CLAUDE.md instructions apply. ' +
	'This HOST process owns task-watching, the liveness heartbeat, and cron/proactive SCHEDULING — it injects ' +
	'each task and each scheduled prompt (e.g. /proactive-loop) directly into this session. Do NOT run ' +
	'/schedule-crons, do NOT try to register crons (the CronCreate tool is unavailable here), and do NOT start ' +
	'the task watcher — the host handles all of that. Just process each injected task/prompt per your ' +
	'instructions and write replies to the results/ dir. Acknowledge readiness in one short line.';
const BOOTSTRAP = process.env.SUTANDO_SESSION_BOOTSTRAP || DEFAULT_BOOTSTRAP;

// ── workspace + canonical dirs ───────────────────────────────────────────────
function resolveWorkspace(): string {
	try {
		return execSync('bash scripts/sutando-config.sh workspace', { cwd: REPO, encoding: 'utf8' }).trim();
	} catch {
		return join(REPO, 'workspace');
	}
}
const WORKSPACE = resolveWorkspace();
const TASKS_DIR = join(WORKSPACE, 'tasks');
const RESULTS_DIR = join(WORKSPACE, 'results');
const STATE_DIR = join(WORKSPACE, 'state');
const CORES_DIR = join(STATE_DIR, 'cores');
const INFLIGHT_DIR = join(TASKS_DIR, '.sdk-inflight'); // staged tasks the host has picked up
for (const d of [TASKS_DIR, RESULTS_DIR, STATE_DIR, CORES_DIR, INFLIGHT_DIR]) mkdirSync(d, { recursive: true });

// Make the agent's Monitor watcher a no-op — the host owns task watching.
process.env.SUTANDO_HOST_OWNS_WATCHER = '1';

const HOST_LABEL = process.env.SUTANDO_HOST_LABEL || os.hostname().split('.')[0];

function atomicWrite(path: string, data: string): void {
	const tmp = `${path}.tmp`;
	writeFileSync(tmp, data);
	renameSync(tmp, path);
}

// ── SSE fan-out (+ replay history) ───────────────────────────────────────────
// There is only ONE live session, so every viewer sees the same event log. We
// keep an in-memory history and replay it to each new SSE client, so a browser
// refresh shows the full session (input, output, tool calls) — not a blank page.
const clients = new Set<ServerResponse>();
const history: string[] = [];
const HISTORY_CAP = 20_000;
let lastSessionId = '';
function broadcast(evt: unknown): void {
	const payload = JSON.stringify(evt);
	history.push(payload);
	if (history.length > HISTORY_CAP) history.shift();
	const line = `data: ${payload}\n\n`;
	for (const res of clients) {
		try {
			res.write(line);
		} catch {
			/* evicted on close */
		}
	}
}

// ── core-status (host-written, reflects session busy/idle) ───────────────────
function writeStatus(status: 'running' | 'idle', step?: string): void {
	try {
		const payload: Record<string, unknown> = { status, ts: Math.floor(Date.now() / 1000) };
		if (step) payload.step = step;
		atomicWrite(join(STATE_DIR, 'core-status.json'), JSON.stringify(payload));
	} catch {
		/* non-fatal */
	}
}

// ── per-host liveness heartbeat (schema-v1, matches src/core_heartbeat.py) ───
const STARTED_AT = Date.now() / 1000;
const ALIVE_FILE = join(CORES_DIR, `${HOST_LABEL}.alive`);
let hbTimer: ReturnType<typeof setInterval> | undefined;
function writeBeat(status = 'running'): void {
	try {
		atomicWrite(
			ALIVE_FILE,
			JSON.stringify({
				host: HOST_LABEL,
				pid: process.pid,
				started_at: STARTED_AT,
				last_beat_at: Date.now() / 1000,
				status,
				schema_version: 1,
			}),
		);
	} catch {
		/* non-fatal */
	}
}
// When launched via src/startup.sh, core_heartbeat.py already emits this host's
// beat — set SUTANDO_SESSION_NO_HEARTBEAT=1 so we don't fight it for the .alive
// file. Standalone runs keep their own heartbeat.
const OWN_HEARTBEAT = process.env.SUTANDO_SESSION_NO_HEARTBEAT !== '1';
function startHeartbeat(): void {
	if (!OWN_HEARTBEAT) return;
	writeBeat();
	hbTimer = setInterval(() => writeBeat(), 30_000);
}
function shutdown(): void {
	if (hbTimer) clearInterval(hbTimer);
	if (OWN_HEARTBEAT) {
		try {
			unlinkSync(ALIVE_FILE); // signal graceful shutdown to peers immediately
		} catch {
			/* already gone */
		}
	}
	writeStatus('idle');
	process.exit(0);
}

// ── streaming input: the async-iterable the SDK consumes as turns arrive ─────
type UserMsg = {
	type: 'user';
	message: { role: 'user'; content: string };
	parent_tool_use_id: null;
	session_id: string;
};
class InputStream {
	private buf: UserMsg[] = [];
	private waiters: Array<(r: IteratorResult<UserMsg>) => void> = [];
	private closed = false;
	push(text: string): void {
		const msg: UserMsg = { type: 'user', message: { role: 'user', content: text }, parent_tool_use_id: null, session_id: lastSessionId };
		const w = this.waiters.shift();
		if (w) w({ value: msg, done: false });
		else this.buf.push(msg);
	}
	close(): void {
		this.closed = true;
		const w = this.waiters.shift();
		if (w) w({ value: undefined as unknown as UserMsg, done: true });
	}
	[Symbol.asyncIterator](): AsyncIterator<UserMsg> {
		return {
			next: (): Promise<IteratorResult<UserMsg>> => {
				if (this.buf.length) return Promise.resolve({ value: this.buf.shift()!, done: false });
				if (this.closed) return Promise.resolve({ value: undefined as unknown as UserMsg, done: true });
				return new Promise((resolve) => this.waiters.push(resolve));
			},
		};
	}
}
const input = new InputStream();

// ── host task watcher (stage-on-pickup → inject → archive-on-result) ─────────
const inflight = new Map<string, string>(); // task id → staged path (awaiting result)

function taskIdFromName(name: string): string | null {
	const m = name.match(/^(task-[^./]+)\.txt$/);
	return m ? m[1] : null;
}

function injectTask(id: string, stagedPath: string): void {
	inflight.set(id, stagedPath);
	writeStatus('running', `task ${id}`);
	input.push(
		`A new Sutando task has arrived. Read the task file at ${stagedPath} and process it EXACTLY per your task-handling instructions in CLAUDE.md — notify first if warranted, honor its access_tier, and write the reply to ${join(RESULTS_DIR, `${id}.txt`)} using the result protocol (markers like [no-send]/[channel:]/[file:] as needed). Task id: ${id}.`,
	);
	broadcast({ type: 'server', subtype: 'task_injected', id, path: stagedPath });
}

function pickupTask(name: string): void {
	const id = taskIdFromName(name);
	if (!id || inflight.has(id)) return;
	const src = join(TASKS_DIR, name);
	const dst = join(INFLIGHT_DIR, name);
	try {
		renameSync(src, dst); // move OUT of tasks/ before injecting (no race with the proactive scan)
	} catch {
		return; // already picked up / gone
	}
	injectTask(id, dst);
}

function sweepTasks(): void {
	// Recover any staged tasks from a prior crash (mid-process) → re-inject.
	try {
		for (const f of readdirSync(INFLIGHT_DIR)) {
			const id = taskIdFromName(f);
			if (id && !inflight.has(id)) injectTask(id, join(INFLIGHT_DIR, f));
		}
	} catch {
		/* dir may be empty */
	}
	// Pick up any tasks already sitting in tasks/ at boot.
	try {
		for (const f of readdirSync(TASKS_DIR)) if (f.endsWith('.txt')) pickupTask(f);
	} catch {
		/* empty */
	}
}

function archiveInflight(id: string): void {
	const staged = inflight.get(id);
	inflight.delete(id);
	if (!staged) return;
	try {
		const d = new Date();
		const ym = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
		const archDir = join(TASKS_DIR, 'archive', ym);
		mkdirSync(archDir, { recursive: true });
		renameSync(staged, join(archDir, `${id}.txt`));
	} catch {
		/* best effort */
	}
}

// A result file is `task-{id}.txt` or `<channel-key>.task-{id}.txt`.
function onResultFile(name: string): void {
	for (const id of inflight.keys()) {
		if (name === `${id}.txt` || name.endsWith(`.${id}.txt`)) {
			archiveInflight(id);
			writeStatus('idle');
			broadcast({ type: 'server', subtype: 'task_done', id });
			return;
		}
	}
}

function startTaskWatcher(): void {
	sweepTasks();
	try {
		watch(TASKS_DIR, (_ev, name) => {
			if (!name || !String(name).endsWith('.txt')) return;
			// small debounce so the writer finishes; existence check ignores our own move-outs
			setTimeout(() => {
				if (existsSync(join(TASKS_DIR, String(name)))) pickupTask(String(name));
			}, 200);
		});
	} catch (e) {
		broadcast({ type: 'server', subtype: 'error', error: `tasks watch failed: ${String(e)}` });
	}
	try {
		watch(RESULTS_DIR, (_ev, name) => {
			if (name && String(name).endsWith('.txt')) onResultFile(String(name));
		});
	} catch {
		/* results archiving is best-effort */
	}
}

// ── host-driven cron scheduler (CronCreate is NOT available in SDK sessions) ─
// Reads the same hosts/<host>/crons.json the interactive core registers, matches
// each 5-field schedule against the clock, and injects the cron's prompt as a
// turn (prompt_skill → "/skill"; prompt → the literal text, which self-gates via
// scripts/cron-gate.sh for sub-daily entries). This is what makes /proactive-loop
// fire on `*/5`.
type Cron = { name: string; cron: string; prompt?: string; prompt_skill?: string };

function cronsPath(): string {
	const perHost = join(WORKSPACE, 'hosts', HOST_LABEL, 'crons.json');
	if (existsSync(perHost)) return perHost;
	const legacy = join(REPO, 'skills', 'schedule-crons', 'crons.json');
	if (existsSync(legacy)) return legacy;
	return join(REPO, 'skills', 'schedule-crons', 'crons.example.json');
}
function readCrons(): Cron[] {
	try {
		const raw = JSON.parse(readFileSync(cronsPath(), 'utf8'));
		return Array.isArray(raw) ? (raw.filter((c) => c && c.name && c.cron) as Cron[]) : [];
	} catch {
		return [];
	}
}
// Match one cron field (supports "*", "*/n", "n", "a-b", "a-b/n", and comma lists).
function fieldMatch(field: string, val: number): boolean {
	return field.split(',').some((part) => {
		let range = part;
		let step = 1;
		const slash = part.split('/');
		if (slash.length === 2) {
			range = slash[0];
			step = parseInt(slash[1], 10) || 1;
		}
		let lo: number;
		let hi: number;
		if (range === '*') {
			return step === 1 ? true : val % step === 0;
		}
		const dash = range.split('-');
		lo = parseInt(dash[0], 10);
		hi = dash.length === 2 ? parseInt(dash[1], 10) : lo;
		if (Number.isNaN(lo) || val < lo || val > hi) return false;
		return (val - lo) % step === 0;
	});
}
function cronMatches(expr: string, d: Date): boolean {
	const f = expr.trim().split(/\s+/);
	if (f.length !== 5) return false;
	const dow = d.getDay(); // 0=Sun..6=Sat; cron also allows 7=Sun
	return (
		fieldMatch(f[0], d.getMinutes()) &&
		fieldMatch(f[1], d.getHours()) &&
		fieldMatch(f[2], d.getDate()) &&
		fieldMatch(f[3], d.getMonth() + 1) &&
		(fieldMatch(f[4], dow) || (dow === 0 && fieldMatch(f[4], 7)))
	);
}
const cronLastFired = new Map<string, number>(); // name → minute-epoch (double-fire guard)
function cronTick(): void {
	const now = new Date();
	const minuteKey = Math.floor(now.getTime() / 60_000);
	for (const c of readCrons()) {
		if (!cronMatches(c.cron, now)) continue;
		if (cronLastFired.get(c.name) === minuteKey) continue;
		cronLastFired.set(c.name, minuteKey);
		const prompt = c.prompt_skill ? `/${c.prompt_skill}` : c.prompt || '';
		if (!prompt) continue;
		writeStatus('running', `cron ${c.name}`);
		input.push(prompt);
		broadcast({ type: 'server', subtype: 'cron_fired', name: c.name, prompt: c.prompt_skill ? `/${c.prompt_skill}` : prompt.slice(0, 60) });
	}
}
function startCrons(): void {
	// Seed the boot minute as already-fired for every cron, so a restart doesn't
	// immediately fire crons matching the current minute (mimics the interactive
	// core: crons fire at their scheduled time, not on registration). Firing
	// begins at the next matching minute.
	const crons = readCrons();
	const bootMinute = Math.floor(Date.now() / 60_000);
	for (const c of crons) cronLastFired.set(c.name, bootMinute);
	// Announce the loaded schedule up front (there is no CronCreate registration —
	// the host fires each cron's prompt at its time). This is the "crons are set
	// up" confirmation; each firing later shows as a `cron_fired` event.
	broadcast({
		type: 'server',
		subtype: 'cron_schedule',
		source: cronsPath(),
		crons: crons.map((c) => ({
			name: c.name,
			cron: c.cron,
			target: c.prompt_skill ? `/${c.prompt_skill}` : (c.prompt || '').slice(0, 70),
		})),
	});
	console.error(`session-server: ${crons.length} host-driven crons scheduled from ${cronsPath()}`);
	setInterval(cronTick, 30_000); // 30s tick; minute-dedup prevents double-fire
}

// ── the real SDK session (full Sutando runtime) ──────────────────────────────
async function runRealSession(): Promise<void> {
	broadcast({
		type: 'server',
		subtype: 'starting',
		provider: process.env.ANTHROPIC_BASE_URL || '(subscription / stock endpoint)',
		model: process.env.ANTHROPIC_MODEL || '(default)',
		workspace: WORKSPACE,
	});
	const q = query({
		prompt: input as unknown as AsyncIterable<never>,
		options: {
			// Exit SDK isolation mode: load CLAUDE.md + skills + settings so the
			// runtime (slash commands, /startup, proactive-loop, crons) exists.
			settingSources: ['user', 'project', 'local'],
			systemPrompt: { type: 'preset', preset: 'claude_code' },
			permissionMode: 'bypassPermissions',
			allowDangerouslySkipPermissions: true,
			additionalDirectories: [os.homedir()],
			// Surface the spawned claude's stderr into the event stream so auth /
			// connection failures are visible in the UI instead of a silent hang.
			stderr: (d: string) => {
				const t = String(d).trim();
				if (t) broadcast({ type: 'server', subtype: 'stderr', text: t.slice(0, 2000) });
			},
		} as Record<string, unknown>,
	});
	try {
		for await (const msg of q as AsyncIterable<Record<string, unknown>>) {
			if (msg?.type === 'system' && typeof msg?.session_id === 'string') lastSessionId = msg.session_id as string;
			if (msg?.type === 'result') writeStatus('idle');
			broadcast(msg);
		}
		broadcast({ type: 'server', subtype: 'session_ended' });
	} catch (err) {
		broadcast({ type: 'server', subtype: 'error', error: String((err as Error)?.message ?? err) });
	}
}

// ── deterministic FAKE session (UI/runtime-host dev + tests; no claude spawn) ─
async function runFakeSession(): Promise<void> {
	lastSessionId = 'fake-session';
	broadcast({ type: 'system', subtype: 'init', session_id: lastSessionId, model: 'fake-model', tools: [] });
	for await (const m of input) {
		writeStatus('running', 'fake turn');
		broadcast({
			type: 'assistant',
			message: { role: 'assistant', content: [{ type: 'text', text: `echo: ${m.message.content.slice(0, 120)}` }] },
			session_id: lastSessionId,
		});
		broadcast({ type: 'result', subtype: 'success', result: 'echo', usage: { input_tokens: 1, output_tokens: 1 }, total_cost_usd: 0, session_id: lastSessionId });
		writeStatus('idle');
	}
}

// ── HTTP ─────────────────────────────────────────────────────────────────────
function readBody(req: IncomingMessage, done: (s: string) => void): void {
	let body = '';
	req.on('data', (c) => {
		body += c;
		if (body.length > 1_000_000) req.destroy();
	});
	req.on('end', () => done(body));
}

const server = createServer((req, res) => {
	const url = req.url ?? '/';
	if (req.method === 'GET' && (url === '/' || url === '/index.html')) {
		try {
			res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' }).end(readFileSync(join(HERE, 'ui.html')));
		} catch {
			res.writeHead(500).end('ui.html not found');
		}
		return;
	}
	if (req.method === 'GET' && url === '/events') {
		res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache', connection: 'keep-alive' });
		// Replay the session log so a refresh shows the full history (single session).
		for (const p of history) {
			try {
				res.write(`data: ${p}\n\n`);
			} catch {
				/* client gone mid-replay */
			}
		}
		res.write(`data: ${JSON.stringify({ type: 'server', subtype: 'connected', session_id: lastSessionId, workspace: WORKSPACE, replayed: history.length })}\n\n`);
		clients.add(res);
		const hb = setInterval(() => {
			try {
				res.write(': hb\n\n');
			} catch {
				/* evicted */
			}
		}, 15000);
		req.on('close', () => {
			clearInterval(hb);
			clients.delete(res);
		});
		return;
	}
	if (req.method === 'POST' && url === '/input') {
		readBody(req, (body) => {
			try {
				const { text } = JSON.parse(body || '{}') as { text?: unknown };
				if (typeof text === 'string' && text.trim()) {
					writeStatus('running', 'user turn');
					input.push(text);
					broadcast({ type: 'server', subtype: 'user_turn', text });
				}
				res.writeHead(204).end();
			} catch {
				res.writeHead(400).end('bad json');
			}
		});
		return;
	}
	if (req.method === 'GET' && url === '/health') {
		res.writeHead(200, { 'content-type': 'application/json' }).end(
			JSON.stringify({ ok: true, clients: clients.size, session_id: lastSessionId, fake: FAKE, workspace: WORKSPACE, inflight: inflight.size }),
		);
		return;
	}
	res.writeHead(404).end();
});

// ── boot ─────────────────────────────────────────────────────────────────────
process.once('SIGTERM', shutdown);
process.once('SIGINT', shutdown);

server.listen(PORT, HOST, () => {
	console.error(`session-server: http://${HOST}:${PORT}  (${FAKE ? 'FAKE echo mode' : 'SDK mode'})  workspace=${WORKSPACE}`);
	if (HOST !== '127.0.0.1' && HOST !== 'localhost') {
		console.error('session-server: WARNING — non-loopback bind with NO auth; it relays full prompts + tool I/O.');
	}
});

startHeartbeat();
writeStatus('running', 'booting');
if (!NO_BOOTSTRAP) {
	// Orient the agent FIRST (host owns tasks/crons/watcher; don't self-manage them).
	broadcast({ type: 'server', subtype: 'bootstrap', command: BOOTSTRAP.slice(0, 80) });
	input.push(BOOTSTRAP);
}
startTaskWatcher(); // host task ingestion (fs.watch → inject)
startCrons(); // host cron/proactive scheduling (reads crons.json → inject prompts)

void (FAKE ? runFakeSession() : runRealSession()).catch((e) => {
	broadcast({ type: 'server', subtype: 'error', error: String(e?.message ?? e) });
});
