// A region refusal (Gemini Live close 1007) must not keep voice down once the route is fixed: the real agent and
// bodhi recovery, dialing a local Gemini Live stand-in (GOOGLE_GEMINI_BASE_URL) that refuses until fixRoute().
// Run: npx tsx --test tests/voice-region-redial.test.ts
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync, spawn, type ChildProcess } from 'node:child_process';
import { createServer as createNetServer } from 'node:net';
import { createServer as createHttpServer } from 'node:http';
import { setTimeout as delay } from 'node:timers/promises';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { WebSocketServer } from 'ws';

import { voiceLifecyclePath, type AgentStateV1 } from '../src/voice-agent-state.ts';
import { describeAgentFailure } from '../src/web-voice-transport.ts';

const REPO_ROOT = join(fileURLToPath(new URL('.', import.meta.url)), '..');
const FAKE_KEY = 'AIza-fake-key-for-voice-region-redial-tests';
// Google's verbatim region refusal, as Gemini Live closes a setup with it.
const REGION_REFUSAL = 'User location is not supported for the API use.';

const children: ChildProcess[] = [];
const cleanups: Array<() => Promise<void> | void> = [];

function freePort(): Promise<number> {
	return new Promise((resolve, reject) => {
		const srv = createNetServer();
		srv.on('error', reject);
		srv.listen(0, '127.0.0.1', () => {
			const addr = srv.address();
			const p = typeof addr === 'object' && addr ? addr.port : 0;
			srv.close(() => (p ? resolve(p) : reject(new Error('no port'))));
		});
	});
}

/** Gemini Live stand-in: each setup is refused with the region close until `fixRoute()`. */
async function fakeGeminiLive(): Promise<{ baseUrl: string; fixRoute: () => void; dials: () => number }> {
	let refuse = true;
	let dials = 0;
	const http = createHttpServer((_req, res) => {
		res.writeHead(404, { 'content-type': 'application/json' });
		res.end('{"error":{"code":404,"message":"not served by the test endpoint"}}');
	});
	const wss = new WebSocketServer({ server: http });
	wss.on('connection', (sock) => {
		dials++;
		const refused = refuse;
		sock.on('error', () => {});
		sock.once('message', () => {
			if (refused) sock.close(1007, REGION_REFUSAL);
			else sock.send(JSON.stringify({ setupComplete: {} }));
		});
	});
	const port = await freePort();
	await new Promise<void>((resolve) => http.listen(port, '127.0.0.1', () => resolve()));
	cleanups.push(() => new Promise<void>((resolve) => {
		for (const c of wss.clients) c.terminate();
		wss.close();
		http.close(() => resolve());
	}));
	return { baseUrl: `http://127.0.0.1:${port}`, fixRoute: () => { refuse = false; }, dials: () => dials };
}

async function spawnAgent(geminiBaseUrl: string): Promise<{ port: number; ws: string; output: () => string }> {
	const ws = mkdtempSync(join(tmpdir(), 'voice-region-redial-'));
	cleanups.push(() => rmSync(ws, { recursive: true, force: true }));
	const port = await freePort();
	const tsxCli = join(REPO_ROOT, 'node_modules', 'tsx', 'dist', 'cli.mjs');
	const child = spawn(process.execPath, [tsxCli, 'src/voice-agent.ts'], {
		cwd: REPO_ROOT,
		detached: true,
		env: {
			...process.env,
			SUTANDO_WORKSPACE: ws,
			SUTANDO_TEST_MODE: '1',
			GEMINI_VOICE_API_KEY: FAKE_KEY,
			GOOGLE_GEMINI_BASE_URL: geminiBaseUrl,
			PORT: String(port),
			VISION_CONTROL_PORT: String(await freePort()),
		},
		stdio: ['ignore', 'pipe', 'pipe'],
	});
	children.push(child);
	let out = '';
	child.stdout?.on('data', (c) => { out += String(c); });
	child.stderr?.on('data', (c) => { out += String(c); });
	const settled = await waitUntil(() => /\[Startup\] (session\.start\(\) succeeded|session parked)/.test(out), 90_000);
	assert.ok(settled, `agent startup dial never settled:\n${out.slice(-3000)}`);
	return { port, ws, output: () => out };
}

async function killAndWait(child: ChildProcess): Promise<void> {
	try {
		if (child.pid && process.platform === 'win32') execFileSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
		else if (child.pid) process.kill(-child.pid, 'SIGKILL');
	} catch { /* gone */ }
	if (child.exitCode !== null || child.signalCode !== null) return;
	await new Promise<void>((resolve) => {
		const hard = setTimeout(resolve, 2000);
		child.once('exit', () => { clearTimeout(hard); resolve(); });
	});
}

async function waitUntil(cond: () => boolean, ms: number): Promise<boolean> {
	const deadline = Date.now() + ms;
	while (Date.now() < deadline) {
		if (cond()) return true;
		await delay(100);
	}
	return cond();
}

/** A client attach; `closeOnFailed` does what the web client's failUpstream does on a failed frame. */
async function attach(port: number, closeOnFailed: boolean): Promise<{ sock: WebSocket; frames: AgentStateV1[] }> {
	const sock = new WebSocket(`ws://127.0.0.1:${port}/`);
	const frames: AgentStateV1[] = [];
	sock.addEventListener('message', (ev) => {
		if (typeof ev.data !== 'string') return;
		let msg: AgentStateV1;
		try { msg = JSON.parse(ev.data); } catch { return; }
		if (msg?.type !== 'agent.state') return;
		frames.push(msg);
		if (closeOnFailed && msg.upstream === 'failed') sock.close();
	});
	await new Promise<void>((resolve, reject) => {
		sock.addEventListener('open', () => resolve(), { once: true });
		sock.addEventListener('error', () => reject(new Error('client connect failed')), { once: true });
	});
	return { sock, frames };
}

function assertRegionFailure(frame: AgentStateV1 | undefined): void {
	assert.ok(frame, 'the region refusal reached the client as a failed frame');
	assert.equal(frame.reason, 'location-unsupported');
	assert.equal(frame.category, 'network');
	const { detail } = describeAgentFailure(frame.reason, frame.category);
	assert.match(detail, /isn't available from your network's region/);
}

describe('voice after a region refusal (spawned agent, fake Gemini Live endpoint)', () => {
	after(async () => {
		for (const c of children) await killAndWait(c);
		for (const f of cleanups) await f();
	});

	it('a Retry after the route is fixed gets voice back on that attach', async () => {
		const gemini = await fakeGeminiLive();
		const agent = await spawnAgent(gemini.baseUrl);

		// Refused: the client is told why, then closes as the web client does.
		const first = await attach(agent.port, true);
		await waitUntil(() => first.frames.some((f) => f.upstream === 'failed'), 15_000);
		assertRegionFailure(first.frames.find((f) => f.upstream === 'failed'));
		// The agent serves one client: the Retry attaches only once it has seen the first one leave.
		const detached = await waitUntil(() => {
			try { return JSON.parse(readFileSync(voiceLifecyclePath(agent.ws), 'utf-8')).clientAttached === false; } catch { return false; }
		}, 15_000);
		assert.ok(detached, 'the first client detached');

		gemini.fixRoute();
		const retry = await attach(agent.port, true);
		await waitUntil(() => retry.frames.some((f) => f.upstream === 'live' || f.upstream === 'failed'), 15_000);
		const decisive = retry.frames.find((f) => f.upstream === 'live' || f.upstream === 'failed');
		assert.equal(
			decisive?.upstream,
			'live',
			`Retry after the route was fixed was answered by the stale region refusal (frames: ${retry.frames.map((f) => f.upstream).join(' > ')})`,
		);
		retry.sock.close();
	});

	it('an attached client is redialed on the ladder, not held by the fatal backoff', async () => {
		const gemini = await fakeGeminiLive();
		const agent = await spawnAgent(gemini.baseUrl);

		const client = await attach(agent.port, false);
		await waitUntil(() => client.frames.some((f) => f.upstream === 'failed'), 15_000);
		assertRegionFailure(client.frames.find((f) => f.upstream === 'failed'));

		const dialsWhenFixed = gemini.dials();
		gemini.fixRoute();
		const live = await waitUntil(() => client.frames.some((f) => f.upstream === 'live'), 20_000);
		assert.ok(
			live,
			`voice not back 20 s after the route was fixed: held by the fatal backoff (dials since the fix: ${gemini.dials() - dialsWhenFixed})`,
		);
		assert.doesNotMatch(agent.output(), /fatal close \(location_unsupported\)/, 'a region refusal starts no fatal backoff');
		client.sock.close();
	});
});
