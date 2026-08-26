/**
 * `agent.state` v1 protocol emitter (design 1a′; impl plan WS1 Step 12,
 * amendments R8/A9/A10/S3/Z3).
 *
 * Unit:
 *  - frame shape/vectors from `createAgentStateProvider` (upstream mapping,
 *    credentialSource label mapping, opaque generation echo, legacy
 *    no-generation omission, launchdContract echo);
 *  - resolver generation REPORTING (S3 — managed `generation` field /
 *    SUTANDO_VOICE_CREDENTIAL_GENERATION; the agent never mints);
 *  - lifecycle-file atomicity (A9 — temp+rename, unique temp names, a
 *    cross-process reader never sees a torn write);
 *  - Z3 isolated idle-restore (arm/fire/fence semantics).
 *
 * Integration (spawned agents against a temp workspace, offline — the fake
 * key + file-triggered upstream-close injection make the classifier path
 * deterministic without live Gemini):
 *  - immediate v1 frame on every accepted real connection;
 *  - repeat frame on an upstream transition (injected terminal close →
 *    `failed`/`quota`/`quota-exceeded` through the REAL transport.onClose
 *    seam — classifier mapping pinned end-to-end);
 *  - `state/voice-lifecycle.json` published on attach/transition/detach;
 *  - legacy spawn (no injected generation, no launchd marker): frame omits
 *    `credentialGeneration` and `launchdContract`.
 *
 * Step 11 (PR group E — this change): the pinned bodhi intercepts `?probe=1`
 * before client attachment (`probeState` behind the voice-agent.ts
 * feature-detect), so the probe-frame + live-call acceptance test is ACTIVE
 * below: a probe upgrade against a spawned agent with a real client attached
 * gets one frame + clean close and the client keeps its slot.
 */

import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, type ChildProcess } from 'node:child_process';
import { createServer } from 'node:net';
import { setTimeout as delay } from 'node:timers/promises';
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
	createAgentStateProvider,
	createIsolatedIdleRestore,
	credentialSourceLabel,
	mapUpstream,
	publishCapabilitiesMarker,
	publishLifecycleSnapshot,
	voiceCapabilitiesPath,
	voiceLifecyclePath,
	type AgentStateV1,
} from '../src/voice-agent-state.ts';
import { resolveCredential } from '../src/credential-resolver.ts';
import type { ProtocolFailure } from '../src/voice-error-classifier.ts';

const REPO_ROOT = join(fileURLToPath(new URL('.', import.meta.url)), '..');
const FAKE_KEY = 'AIza-fake-key-for-agent-state-protocol-tests';
const UPSTREAM_ENUM = ['live', 'idle', 'connecting', 'backoff', 'failed'];

// ---------------------------------------------------------------------------
// Unit: frame shape / vectors
// ---------------------------------------------------------------------------

function makeProvider(overrides: {
	initialized?: boolean;
	sessionState?: string;
	clientAttached?: boolean;
	backoffUntil?: number;
	terminal?: ProtocolFailure | null;
	source?: 'managed' | 'env' | 'none';
	generation?: string;
	launchdContract?: boolean;
	now?: number;
} = {}) {
	return createAgentStateProvider({
		initialized: () => overrides.initialized ?? true,
		sessionState: () => overrides.sessionState ?? 'CLOSED',
		clientAttached: () => overrides.clientAttached ?? false,
		backoffUntil: () => overrides.backoffUntil ?? 0,
		lastTerminalFailure: () => overrides.terminal ?? null,
		credential: () => ({
			source: overrides.source ?? 'env',
			...(overrides.generation ? { credentialGeneration: overrides.generation } : {}),
		}),
		launchdContract: () => overrides.launchdContract ?? false,
		now: () => overrides.now ?? 1_000_000,
	});
}

describe('buildAgentState — frame shape and vectors (design 1a′)', () => {
	it('base frame: exact v1 envelope, byok label, no optional fields', () => {
		const frame = makeProvider().build();
		assert.deepEqual(frame, {
			type: 'agent.state',
			v: 1,
			initialized: true,
			upstream: 'idle',
			clientAttached: false,
			credentialSource: 'byok',
		});
		// Legacy no-generation: the property must be ABSENT, not undefined.
		assert.equal('credentialGeneration' in frame, false);
		assert.equal('launchdContract' in frame, false);
		assert.equal('reason' in frame, false);
		assert.equal('category' in frame, false);
	});

	it('upstream vectors: ACTIVE/CONNECTING/RECONNECTING/TRANSFERRING/CLOSED', () => {
		const vectors: Array<[Parameters<typeof makeProvider>[0], string]> = [
			[{ sessionState: 'ACTIVE' }, 'live'],
			[{ sessionState: 'CONNECTING' }, 'connecting'],
			[{ sessionState: 'RECONNECTING' }, 'connecting'],
			[{ sessionState: 'TRANSFERRING' }, 'connecting'],
			[{ sessionState: 'CLOSED', clientAttached: true }, 'backoff'],
			[{ sessionState: 'CLOSED', backoffUntil: 2_000_000 }, 'backoff'],
			[{ sessionState: 'CLOSED' }, 'idle'],
			[{ sessionState: 'CREATED' }, 'idle'],
		];
		for (const [inputs, expected] of vectors) {
			const frame = makeProvider(inputs).build();
			assert.equal(frame.upstream, expected, JSON.stringify(inputs));
			assert.ok(UPSTREAM_ENUM.includes(frame.upstream));
		}
	});

	it('terminal classification → failed with stable reason + category', () => {
		const terminal: ProtocolFailure = { upstream: 'failed', reason: 'auth-invalid', category: 'auth' };
		const frame = makeProvider({ sessionState: 'CLOSED', terminal, clientAttached: true }).build();
		assert.equal(frame.upstream, 'failed');
		assert.equal(frame.reason, 'auth-invalid');
		assert.equal(frame.category, 'auth');
	});

	it('live ACTIVE wins over a stale terminal classification', () => {
		const terminal: ProtocolFailure = { upstream: 'failed', reason: 'quota-exceeded', category: 'quota' };
		const frame = makeProvider({ sessionState: 'ACTIVE', terminal }).build();
		assert.equal(frame.upstream, 'live');
		assert.equal('reason' in frame, false);
	});

	it('credentialSource label mapping (A10): managed/env/none', () => {
		assert.equal(credentialSourceLabel('managed'), 'managed');
		assert.equal(credentialSourceLabel('env'), 'byok');
		assert.equal(credentialSourceLabel('none'), undefined);
		assert.equal(makeProvider({ source: 'managed' }).build().credentialSource, 'managed');
		assert.equal(makeProvider({ source: 'env' }).build().credentialSource, 'byok');
		assert.equal('credentialSource' in makeProvider({ source: 'none' }).build(), false);
	});

	it('opaque generation is echoed verbatim, never derived (R7/S3)', () => {
		const frame = makeProvider({ generation: 'cg1-0f9a3b2c-echo' }).build();
		assert.equal(frame.credentialGeneration, 'cg1-0f9a3b2c-echo');
	});

	it('launchdContract:1 echoed only under the env marker (R17)', () => {
		assert.equal(makeProvider({ launchdContract: true }).build().launchdContract, 1);
		assert.equal('launchdContract' in makeProvider({ launchdContract: false }).build(), false);
	});

	it('mapUpstream: backoff only while the deadline is in the future', () => {
		assert.equal(mapUpstream({ sessionState: 'CLOSED', clientAttached: false, backoffUntil: 999, terminal: null, now: 1000 }).upstream, 'idle');
		assert.equal(mapUpstream({ sessionState: 'CLOSED', clientAttached: false, backoffUntil: 1001, terminal: null, now: 1000 }).upstream, 'backoff');
	});
});

// ---------------------------------------------------------------------------
// Unit: resolver generation reporting (S3 — agent side)
// ---------------------------------------------------------------------------

describe('resolveCredential — generation reporting (S3)', () => {
	const savedEnv: Record<string, string | undefined> = {};
	const ENV_KEYS = ['GEMINI_VOICE_API_KEY', 'GEMINI_API_KEY', 'SUTANDO_VOICE_CREDENTIAL_GENERATION'];
	const stash = () => { for (const k of ENV_KEYS) { savedEnv[k] = process.env[k]; delete process.env[k]; } };
	const restore = () => {
		for (const k of ENV_KEYS) {
			if (savedEnv[k] === undefined) delete process.env[k];
			else process.env[k] = savedEnv[k];
		}
	};

	it('managed entry with a generation field reports it verbatim', () => {
		stash();
		try {
			const dir = mkdtempSync(join(tmpdir(), 'agent-state-managed-'));
			const managedPath = join(dir, 'managed-credentials.json');
			writeFileSync(managedPath, JSON.stringify({
				version: 1,
				capabilities: { 'gemini-voice': { key: 'managed-key-1', generation: 'cg1-rust-minted-42' } },
			}));
			const r = resolveCredential('gemini-voice', { managedPath });
			assert.equal(r.source, 'managed');
			assert.equal(r.credentialGeneration, 'cg1-rust-minted-42');
			rmSync(dir, { recursive: true, force: true });
		} finally { restore(); }
	});

	it('legacy managed entry without generation omits the field', () => {
		stash();
		try {
			const dir = mkdtempSync(join(tmpdir(), 'agent-state-managed-legacy-'));
			const managedPath = join(dir, 'managed-credentials.json');
			writeFileSync(managedPath, JSON.stringify({
				version: 1,
				capabilities: { 'gemini-voice': { key: 'managed-key-legacy' } },
			}));
			const r = resolveCredential('gemini-voice', { managedPath });
			assert.equal(r.source, 'managed');
			assert.equal('credentialGeneration' in r, false);
			rmSync(dir, { recursive: true, force: true });
		} finally { restore(); }
	});

	it('env key + injected SUTANDO_VOICE_CREDENTIAL_GENERATION reports it; without it, omits', () => {
		stash();
		try {
			process.env.GEMINI_VOICE_API_KEY = 'byok-key-1';
			process.env.SUTANDO_VOICE_CREDENTIAL_GENERATION = 'cg1-injected-7';
			const withGen = resolveCredential('gemini-voice', { managedPath: '/nonexistent/managed.json' });
			assert.equal(withGen.source, 'env');
			assert.equal(withGen.credentialGeneration, 'cg1-injected-7');

			delete process.env.SUTANDO_VOICE_CREDENTIAL_GENERATION;
			const legacy = resolveCredential('gemini-voice', { managedPath: '/nonexistent/managed.json' });
			assert.equal(legacy.source, 'env');
			assert.equal('credentialGeneration' in legacy, false);
		} finally { restore(); }
	});
});

// ---------------------------------------------------------------------------
// Unit: lifecycle snapshot atomicity (A9)
// ---------------------------------------------------------------------------

function frameFixture(overrides: Partial<AgentStateV1> = {}): AgentStateV1 {
	return {
		type: 'agent.state',
		v: 1,
		initialized: true,
		upstream: 'idle',
		clientAttached: false,
		credentialSource: 'byok',
		credentialGeneration: 'cg1-lifecycle-test',
		...overrides,
	};
}

describe('publishCapabilitiesMarker — group E activation switch', () => {
	it('writes the marker shape the desktop reader gates on, atomically', () => {
		const ws = mkdtempSync(join(tmpdir(), 'agent-state-caps-'));
		try {
			publishCapabilitiesMarker(ws, { now: () => 777, lockId: 'vl1-test-token' });
			const doc = JSON.parse(readFileSync(voiceCapabilitiesPath(ws), 'utf-8'));
			// The desktop supervisor requires STRICT `probeIsolation === true` AND
			// marker.{pid,lockId} === the live lock holder's (stale-marker rollback
			// defense; lockId defeats pid reuse) — the publisher's own pid/token
			// are the lock winner's by ordering.
			assert.deepEqual(doc, { probeIsolation: true, at: 777, pid: process.pid, lockId: 'vl1-test-token' });
			assert.deepEqual(readdirSync(join(ws, 'state')), ['voice-agent.capabilities.json']);
		} finally { rmSync(ws, { recursive: true, force: true }); }
	});

	// There is deliberately NO unbound-marker row: `lockId` is a required
	// parameter, so this repo cannot emit a marker that advertises probe
	// isolation without binding it to an acquisition. "No token → no
	// publication" is the caller's gate, proven against a spawned agent in
	// the integration suite below (fail closed at the writer — the desktop
	// reader's token requirement is a second fence, not the only one).

	it('is failure-silent: an unwritable target reports via onError, never throws', () => {
		const errs: unknown[] = [];
		// A path whose parent is a FILE cannot gain a state/ subdirectory.
		const ws = mkdtempSync(join(tmpdir(), 'agent-state-caps-ro-'));
		try {
			writeFileSync(join(ws, 'state'), 'occupied');
			publishCapabilitiesMarker(ws, { lockId: 'vl1-test-token', onError: (e) => errs.push(e) });
			assert.equal(errs.length, 1);
		} finally { rmSync(ws, { recursive: true, force: true }); }
	});
});

describe('publishLifecycleSnapshot — atomic temp+rename (A9)', () => {
	it('writes the derived snapshot schema and leaves no temp files behind', () => {
		const ws = mkdtempSync(join(tmpdir(), 'agent-state-lifecycle-'));
		try {
			for (let i = 0; i < 50; i++) {
				publishLifecycleSnapshot(ws, frameFixture({ upstream: i % 2 ? 'idle' : 'backoff' }), { now: () => 555 + i });
			}
			const snap = JSON.parse(readFileSync(voiceLifecyclePath(ws), 'utf-8'));
			assert.deepEqual(snap, {
				at: 604,
				clientAttached: false,
				initialized: true,
				upstream: 'idle',
				credentialSource: 'byok',
				credentialGeneration: 'cg1-lifecycle-test',
			});
			// Unique temp names + rename: nothing but the target remains.
			assert.deepEqual(readdirSync(join(ws, 'state')), ['voice-lifecycle.json']);
		} finally { rmSync(ws, { recursive: true, force: true }); }
	});

	it('P7 D7.1: inputHealth rides the snapshot additively when supplied', () => {
		const ws = mkdtempSync(join(tmpdir(), 'agent-state-lifecycle-ih-'));
		try {
			publishLifecycleSnapshot(ws, frameFixture(), { now: () => 700, inputHealth: 'stalled' });
			const snap = JSON.parse(readFileSync(voiceLifecyclePath(ws), 'utf-8'));
			assert.equal(snap.inputHealth, 'stalled', 'P4\'s evidence ladder consumes this');
			// Omitted → absent (additive field, older readers unaffected).
			publishLifecycleSnapshot(ws, frameFixture(), { now: () => 701 });
			const snap2 = JSON.parse(readFileSync(voiceLifecyclePath(ws), 'utf-8'));
			assert.equal('inputHealth' in snap2, false);
		} finally { rmSync(ws, { recursive: true, force: true }); }
	});

	it('category included only for failed frames; write errors go to onError (never throw)', () => {
		const ws = mkdtempSync(join(tmpdir(), 'agent-state-lifecycle-cat-'));
		try {
			publishLifecycleSnapshot(ws, frameFixture({ upstream: 'failed', reason: 'quota-exceeded', category: 'quota' }));
			const snap = JSON.parse(readFileSync(voiceLifecyclePath(ws), 'utf-8'));
			assert.equal(snap.upstream, 'failed');
			assert.equal(snap.category, 'quota');
			assert.equal('reason' in snap, false, 'snapshot schema carries category, not reason');

			let seen: unknown = null;
			// Unwritable workspace path (a file where the state dir should be).
			const bogus = join(ws, 'not-a-dir');
			writeFileSync(bogus, 'x');
			publishLifecycleSnapshot(join(bogus, 'nested'), frameFixture(), { onError: (e) => { seen = e; } });
			assert.ok(seen !== null, 'write failure surfaced via onError');
		} finally { rmSync(ws, { recursive: true, force: true }); }
	});

	it('a concurrent cross-process reader never sees a torn write', async () => {
		const ws = mkdtempSync(join(tmpdir(), 'agent-state-lifecycle-race-'));
		mkdirSync(join(ws, 'state'), { recursive: true });
		try {
			// Child process hammers publishLifecycleSnapshot with alternating
			// payload sizes; the parent reads concurrently — every read must
			// parse. A plain writeFileSync writer fails this test.
			const script = `
				import { publishLifecycleSnapshot } from '${join(REPO_ROOT, 'src', 'voice-agent-state.ts').replace(/\\/g, '\\\\')}';
				const ws = process.argv[1];
				const big = 'cg1-' + 'x'.repeat(4000);
				for (let i = 0; i < 300; i++) {
					publishLifecycleSnapshot(ws, {
						type: 'agent.state', v: 1, initialized: true,
						upstream: i % 2 ? 'idle' : 'failed',
						...(i % 2 ? {} : { category: 'quota' }),
						clientAttached: false, credentialSource: 'byok',
						credentialGeneration: i % 2 ? 'cg1-small' : big,
					});
				}
			`;
			const child = spawn('npx', ['tsx', '-e', script, ws], { cwd: REPO_ROOT, stdio: 'ignore' });
			const exited = new Promise<number | null>((resolve) => child.once('exit', resolve));
			const target = voiceLifecyclePath(ws);
			let reads = 0;
			let sawFile = false;
			// Read as fast as we can while the child writes.
			for (;;) {
				if (existsSync(target)) {
					sawFile = true;
					const text = readFileSync(target, 'utf-8');
					assert.doesNotThrow(() => JSON.parse(text), `torn read after ${reads} reads: ${text.slice(0, 80)}`);
					reads++;
				}
				if (child.exitCode !== null) break;
				await delay(1);
			}
			assert.equal(await exited, 0, 'writer child exited cleanly');
			assert.ok(sawFile, 'reader observed the lifecycle file');
			assert.ok(reads > 0, 'reader raced at least one write');
		} finally { rmSync(ws, { recursive: true, force: true }); }
	});
});

// ---------------------------------------------------------------------------
// Unit: Z3 isolated idle restore
// ---------------------------------------------------------------------------

describe('createIsolatedIdleRestore — Z3 fence', () => {
	it('arms after probe close and fires teardown when no real client arrives', async () => {
		let toreDown = 0;
		const restore = createIsolatedIdleRestore({
			delayMs: 30,
			clientAttached: () => false,
			teardown: () => { toreDown++; },
		});
		restore.arm();
		assert.equal(restore.pending(), true);
		await delay(80);
		assert.equal(toreDown, 1, 'teardown restored the prior idle state');
		assert.equal(restore.pending(), false);
	});

	it('no-op while a real client is attached at arm time', async () => {
		let toreDown = 0;
		const restore = createIsolatedIdleRestore({ delayMs: 10, clientAttached: () => true, teardown: () => { toreDown++; } });
		restore.arm();
		assert.equal(restore.pending(), false);
		await delay(40);
		assert.equal(toreDown, 0);
	});

	it('a later real connection fences a pending restore', async () => {
		let toreDown = 0;
		const restore = createIsolatedIdleRestore({ delayMs: 30, clientAttached: () => false, teardown: () => { toreDown++; } });
		restore.arm();
		restore.fence(); // real client connected before the timer fired
		assert.equal(restore.pending(), false);
		await delay(80);
		assert.equal(toreDown, 0, 'fenced restore must never tear down under a real client');
	});

	it('re-checks real attachment at fire time', async () => {
		let attached = false;
		let toreDown = 0;
		const restore = createIsolatedIdleRestore({ delayMs: 30, clientAttached: () => attached, teardown: () => { toreDown++; } });
		restore.arm();
		attached = true; // client raced in without fence() (belt and braces)
		await delay(80);
		assert.equal(toreDown, 0);
	});
});

// ---------------------------------------------------------------------------
// Integration: spawned voice-agent — frames + lifecycle file
// ---------------------------------------------------------------------------

const children: ChildProcess[] = [];
const tempDirs: string[] = [];

function makeWorkspace(tag: string): string {
	const ws = mkdtempSync(join(tmpdir(), `agent-state-proto-${tag}-`));
	tempDirs.push(ws);
	return ws;
}

interface AgentHandle {
	child: ChildProcess;
	stderr: () => string;
	stdout: () => string;
}

// Ephemeral free port (bind :0, read the assigned port, release) so parallel
// CI runners or a stray listener never collide with a hardcoded number.
function freePort(): Promise<number> {
	return new Promise((resolve, reject) => {
		const srv = createServer();
		srv.on('error', reject);
		srv.listen(0, '127.0.0.1', () => {
			const addr = srv.address();
			const p = typeof addr === 'object' && addr ? addr.port : 0;
			srv.close(() => (p ? resolve(p) : reject(new Error('no port'))));
		});
	});
}

function spawnAgent(ws: string, port: number, visionPort: number, extraEnv: Record<string, string> = {}): AgentHandle {
	const child = spawn('npx', ['tsx', 'src/voice-agent.ts'], {
		cwd: REPO_ROOT,
		detached: true,
		env: {
			...process.env,
			SUTANDO_WORKSPACE: ws,
			SUTANDO_TEST_MODE: '1',
			GEMINI_VOICE_API_KEY: FAKE_KEY,
			PORT: String(port),
			VISION_CONTROL_PORT: String(visionPort),
			...extraEnv,
		},
		stdio: ['ignore', 'pipe', 'pipe'],
	});
	children.push(child);
	let out = '';
	let err = '';
	child.stdout?.on('data', (c) => { out += String(c); });
	child.stderr?.on('data', (c) => { err += String(c); });
	return { child, stderr: () => err, stdout: () => out };
}

async function killAndWait(child: ChildProcess): Promise<void> {
	try { if (child.pid) process.kill(-child.pid, 'SIGKILL'); } catch { /* gone */ }
	if (child.exitCode !== null || child.signalCode !== null) return;
	await new Promise<void>((resolve) => {
		const hard = setTimeout(() => { try { child.kill('SIGKILL'); } catch {} resolve(); }, 2000);
		child.once('exit', () => { clearTimeout(hard); resolve(); });
		try { child.kill('SIGKILL'); } catch { clearTimeout(hard); resolve(); }
	});
}

/** Connect a real WS client, retrying until the agent's server accepts. */
async function connectClient(port: number, timeoutMs: number): Promise<WebSocket> {
	const deadline = Date.now() + timeoutMs;
	for (;;) {
		try {
			const ws = new WebSocket(`ws://127.0.0.1:${port}/`);
			await new Promise<void>((resolve, reject) => {
				ws.addEventListener('open', () => resolve(), { once: true });
				ws.addEventListener('error', () => reject(new Error('connect failed')), { once: true });
			});
			return ws;
		} catch {
			if (Date.now() > deadline) throw new Error(`could not connect to ws://127.0.0.1:${port} within ${timeoutMs}ms`);
			await delay(300);
		}
	}
}

/** Collect agent.state frames off a socket into an array (text frames only). */
function collectFrames(ws: WebSocket): AgentStateV1[] {
	const frames: AgentStateV1[] = [];
	ws.addEventListener('message', (ev) => {
		if (typeof ev.data !== 'string') return;
		try {
			const msg = JSON.parse(ev.data);
			if (msg?.type === 'agent.state') frames.push(msg);
		} catch { /* non-JSON frame */ }
	});
	return frames;
}

async function waitFor(cond: () => boolean, ms: number, what: string): Promise<void> {
	const deadline = Date.now() + ms;
	while (Date.now() < deadline) {
		if (cond()) return;
		await delay(150);
	}
	throw new Error(`timed out waiting for ${what}`);
}

function assertValidV1(frame: AgentStateV1, label: string): void {
	assert.equal(frame.type, 'agent.state', label);
	assert.equal(frame.v, 1, label);
	assert.equal(typeof frame.initialized, 'boolean', label);
	assert.equal(typeof frame.clientAttached, 'boolean', label);
	assert.ok(UPSTREAM_ENUM.includes(frame.upstream), `${label}: upstream ${frame.upstream} in enum`);
}

describe('agent.state emission (integration, spawned agent)', () => {
	after(async () => {
		for (const c of children) await killAndWait(c);
		for (const d of tempDirs) rmSync(d, { recursive: true, force: true });
	});

	it('immediate frame on connect; transition frame on injected terminal close; lifecycle file tracks it', async () => {
		const ws = makeWorkspace('main');
		const port = await freePort();
		const trigger = join(ws, 'close-trigger');
		const GEN = 'cg1-11111111-2222-3333-4444-555555555555';
		spawnAgent(ws, port, await freePort(), {
			SUTANDO_VOICE_CREDENTIAL_GENERATION: GEN,
			SUTANDO_VOICE_LAUNCHD_CONTRACT: '1',
			// Injected close fires only when the trigger file appears — the
			// quota reason pins the classifier's quota mapping end-to-end
			// (the auth mapping is pinned in voice-error-classifier.test.ts
			// and, opportunistically, by the fake key's own startup failure).
			SUTANDO_TEST_UPSTREAM_CLOSE: `${trigger}|1011|You exceeded your current quota, please check your plan and billing details.`,
		});

		const client = await connectClient(port, 90_000);
		try {
			const frames = collectFrames(client);

			// Step 12: immediate frame on every accepted real connection.
			await waitFor(() => frames.length >= 1, 20_000, 'immediate agent.state frame');
			const first = frames[0];
			assertValidV1(first, 'immediate frame');
			assert.equal(first.initialized, true, 'L2 initialized by the time a client can connect');
			assert.equal(first.clientAttached, true, 'real client counted');
			assert.equal(first.credentialSource, 'byok', 'env key maps to byok on the wire (A10)');
			assert.equal(first.credentialGeneration, GEN, 'opaque generation echoed verbatim (S3)');
			assert.equal(first.launchdContract, 1, 'launchd contract marker echoed (R17)');

			// Lifecycle snapshot published (attach transition, A9).
			await waitFor(() => existsSync(voiceLifecyclePath(ws)), 10_000, 'voice-lifecycle.json');

			// Force an upstream transition through the REAL wrapped
			// transport.onClose seam → classifier → failed/quota frame.
			const before = frames.length;
			writeFileSync(trigger, '1');
			await waitFor(
				() => frames.length > before && frames.some((f) => f.upstream === 'failed' && f.category === 'quota'),
				20_000,
				'transition frame with failed/quota',
			);
			const failedFrame = frames.find((f) => f.upstream === 'failed' && f.category === 'quota')!;
			assertValidV1(failedFrame, 'transition frame');
			assert.equal(failedFrame.reason, 'quota-exceeded', 'stable protocol reason code (R8)');
			assert.equal(failedFrame.credentialGeneration, GEN);

			// Lifecycle mirrors the terminal upstream (A9 + S3 fields). NOT
			// pinned to quota: the fake key's own startup auth close arrives
			// through the SAME wrapped onClose seam and can land after the
			// injected quota close, legitimately overwriting the category —
			// R8 makes reason/category changes within 'failed' meaningful
			// transitions, and the frame assertions above already pinned the
			// quota mapping end-to-end. The file's invariant is that it
			// mirrors the LATEST failed classification, so assert exactly
			// that (captured live: the file read failed/auth at timeout while
			// the quota frame sat in the frames array).
			await waitFor(() => {
				try {
					const snap = JSON.parse(readFileSync(voiceLifecyclePath(ws), 'utf-8'));
					const lastFailed = [...frames].reverse().find((f) => f.upstream === 'failed');
					return !!lastFailed && snap.upstream === 'failed' && snap.category === lastFailed.category;
				} catch { return false; }
			}, 10_000, 'lifecycle snapshot mirrors the latest failed classification');
			const snap = JSON.parse(readFileSync(voiceLifecyclePath(ws), 'utf-8'));
			assert.equal(typeof snap.at, 'number');
			assert.equal(snap.clientAttached, true);
			assert.equal(snap.initialized, true);
			assert.equal(snap.credentialSource, 'byok');
			assert.equal(snap.credentialGeneration, GEN);
		} finally {
			client.close();
		}

		// Detach transition: lifecycle flips clientAttached → false (A9).
		await waitFor(() => {
			try { return JSON.parse(readFileSync(voiceLifecyclePath(ws), 'utf-8')).clientAttached === false; } catch { return false; }
		}, 15_000, 'lifecycle clientAttached=false after disconnect');
	});

	it('legacy spawn (no injected generation, no launchd marker) omits both fields', async () => {
		const ws = makeWorkspace('legacy');
		const port = await freePort();
		spawnAgent(ws, port, await freePort());

		const client = await connectClient(port, 90_000);
		try {
			const frames = collectFrames(client);
			await waitFor(() => frames.length >= 1, 20_000, 'immediate agent.state frame');
			const first = frames[0];
			assertValidV1(first, 'legacy frame');
			assert.equal(first.credentialSource, 'byok');
			assert.equal('credentialGeneration' in first, false, 'legacy env key stays generationless (S3)');
			assert.equal('launchdContract' in first, false, 'no contract marker → no echo (R17)');
		} finally {
			client.close();
		}
	});

	// Group E acceptance (Step 11): the failure mode this activation exists to
	// prevent is a `?probe=1` upgrade landing in the normal single-client
	// attach path and STEALING the live call. Against the pinned bodhi, a
	// probe socket must be intercepted before client attachment: it gets one
	// agent.state frame + a clean close(1000), while the attached real client
	// keeps its slot (socket stays open, lifecycle still counts it). Also
	// proves the capability marker end-to-end: published because the pinned
	// bodhi supports probeState, and BOUND to the structured lock (pid +
	// lockId equality) — the desktop reader's stale-marker defense contract.
	it('?probe=1 gets one frame + close(1000) and never steals the attached client', async () => {
		const ws = makeWorkspace('probe');
		const port = await freePort();
		spawnAgent(ws, port, await freePort());

		const client = await connectClient(port, 90_000);
		try {
			const frames = collectFrames(client);
			await waitFor(() => frames.length >= 1, 20_000, 'immediate agent.state frame');
			assert.equal(frames[0].clientAttached, true, 'real client holds the slot');
			await waitFor(() => existsSync(voiceLifecyclePath(ws)), 10_000, 'voice-lifecycle.json');

			const probe = new WebSocket(`ws://127.0.0.1:${port}/?probe=1`);
			const probeFrames: AgentStateV1[] = [];
			let probeCloseCode: number | null = null;
			probe.addEventListener('message', (ev) => {
				if (typeof ev.data !== 'string') return;
				try { probeFrames.push(JSON.parse(ev.data)); } catch { /* non-JSON */ }
			});
			probe.addEventListener('close', (ev) => { probeCloseCode = ev.code; });
			await waitFor(() => probeCloseCode !== null, 15_000, 'probe close');

			assert.equal(probeCloseCode, 1000, 'probe closed cleanly — not an eviction, not an error');
			assert.ok(probeFrames.length >= 1, 'probe received its status frame');
			assert.equal(probeFrames[0].type, 'agent.state', 'probe frame is the agent.state snapshot');
			assert.equal(probeFrames[0].clientAttached, true, 'probe observes the attached client without touching it');

			// The steal would fire onClientDisconnected on probe close: the real
			// client's socket would drop and the lifecycle would flip detached.
			await delay(500);
			assert.equal(client.readyState, WebSocket.OPEN, 'live client socket survived the probe');
			const snap = JSON.parse(readFileSync(voiceLifecyclePath(ws), 'utf-8'));
			assert.equal(snap.clientAttached, true, 'agent still counts the real client attached');

			// Marker end-to-end: published (capable pin) and bound to THIS
			// acquisition — pid and lockId equal the structured lock's.
			const marker = JSON.parse(readFileSync(voiceCapabilitiesPath(ws), 'utf-8'));
			assert.equal(marker.probeIsolation, true);
			const lock = JSON.parse(readFileSync(join(ws, 'state', 'locks', 'voice-agent.pid'), 'utf-8'));
			assert.equal(marker.pid, lock.pid, 'marker.pid = lock holder pid');
			assert.equal(marker.lockId, lock.lockId, 'marker.lockId = lock acquisition token');
		} finally {
			client.close();
		}
	});

	// The FALSE branch of the capability-marker gate — the safety property
	// itself ("a bodhi without probe isolation never gets an advertising
	// marker"). Forced via the SUTANDO_TEST_MODE-only detect seam; the agent
	// otherwise boots normally, so reaching a first frame proves the wiring
	// init ran PAST the marker site and chose not to publish. Without this
	// row, deleting the gate would regress silently.
	it('publishes NO capability marker when bodhi lacks probeState', async () => {
		const ws = makeWorkspace('no-probe-state');
		const port = await freePort();
		const agent = spawnAgent(ws, port, await freePort(), {
			SUTANDO_TEST_FORCE_NO_PROBE_STATE: '1',
		});

		const client = await connectClient(port, 90_000);
		try {
			const frames = collectFrames(client);
			await waitFor(() => frames.length >= 1, 20_000, 'immediate agent.state frame');
			await waitFor(
				() => agent.stderr().includes('capability marker NOT published'),
				10_000,
				'dormant-branch log line',
			);
			assert.equal(
				existsSync(voiceCapabilitiesPath(ws)),
				false,
				'no advertising marker for a bodhi without probe isolation',
			);
		} finally {
			client.close();
		}
	});
});
