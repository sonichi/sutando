import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { createServer, type Server, request as httpRequestFn } from 'node:http';
import { spawn, type ChildProcess } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

// Credential-proxy idle-timeout regression tests.
//
// The proxy (skills/quota-tracker/scripts/credential-proxy.ts) previously had
// no timeout at all — a network blip would hang the connection forever. Two
// code paths are exercised:
//
//  1. Upstream accepts connection but never sends headers → proxy returns 504
//     (the httpsRequest `timeout` option → upstream.on('timeout') → destroy).
//
//  2. Upstream sends headers + partial body then stalls → proxy closes the
//     client connection within ~IDLE_TIMEOUT_MS (idle timer resets per chunk).
//
//  3. Upstream responds promptly → 200 forwarded, timer never fires.
//
// CREDENTIAL_PROXY_IDLE_TIMEOUT_MS=150 keeps the test fast.

const REPO = join(dirname(fileURLToPath(import.meta.url)), '..');
const PROXY_SCRIPT = join(REPO, 'skills', 'quota-tracker', 'scripts', 'credential-proxy.ts');
const IDLE_TIMEOUT_MS = 150;

// Base proxy ports — well-separated to survive TIME_WAIT from a crashed prior run.
const PROXY_PORT_STALL_HEADERS = 47847;  // test 1
const PROXY_PORT_STALL_BODY    = 47857;  // test 2
const PROXY_PORT_QUICK         = 47867;  // test 3

function boundPort(server: Server): number {
	return (server.address() as { port: number }).port;
}

function waitForPort(port: number, timeoutMs = 8000): Promise<void> {
	return new Promise((resolve, reject) => {
		const deadline = Date.now() + timeoutMs;
		function attempt() {
			const req = httpRequestFn({ hostname: '127.0.0.1', port, path: '/', method: 'GET' }, () => resolve());
			req.on('error', () => {
				if (Date.now() > deadline) return reject(new Error(`port ${port} never opened within ${timeoutMs}ms`));
				setTimeout(attempt, 50);
			});
			req.end();
		}
		attempt();
	});
}

function spawnProxy(proxyPort: number, mockPort: number): ChildProcess {
	const p = spawn(
		'npx',
		['tsx', '--experimental-sqlite', PROXY_SCRIPT],
		{
			env: {
				...process.env,
				CREDENTIAL_PROXY_PORT: String(proxyPort),
				CREDENTIAL_PROXY_UPSTREAM: `http://127.0.0.1:${mockPort}`,
				CREDENTIAL_PROXY_IDLE_TIMEOUT_MS: String(IDLE_TIMEOUT_MS),
				CREDENTIAL_PROXY_SKIP_OAUTH: '1',
			},
			stdio: 'pipe',
		},
	);
	p.stderr?.on('data', (d: Buffer) => process.stderr.write(`[proxy:${proxyPort}] ${d}`));
	return p;
}

async function startMock(handler: Parameters<typeof createServer>[0]): Promise<Server> {
	const srv = createServer(handler);
	// listen(0) → OS picks a free port, avoiding TIME_WAIT collisions across runs.
	await new Promise<void>((resolve) => srv.listen(0, '127.0.0.1', resolve));
	return srv;
}

async function stopServer(srv: Server): Promise<void> {
	await new Promise<void>((resolve) => srv.close(() => resolve()));
}

describe('credential-proxy idle timeout', { timeout: 20_000 }, () => {
	// ── Test 1: upstream accepts connection but never sends headers ────────────
	//    Expected: proxy returns 504 within ~IDLE_TIMEOUT_MS.

	let stallHeadersMock: Server;
	let stallHeadersProxy: ChildProcess;

	before(async () => {
		stallHeadersMock = await startMock((_req, _res) => { /* intentionally silent */ });
		stallHeadersProxy = spawnProxy(PROXY_PORT_STALL_HEADERS, boundPort(stallHeadersMock));
		await waitForPort(PROXY_PORT_STALL_HEADERS);
	});

	after(async () => {
		stallHeadersProxy.kill();
		await stopServer(stallHeadersMock);
	});

	it('returns 504 when upstream never sends headers (connect/headers timeout)', async () => {
		const start = Date.now();
		const statusCode = await new Promise<number>((resolve, reject) => {
			const req = httpRequestFn(
				{
					hostname: '127.0.0.1',
					port: PROXY_PORT_STALL_HEADERS,
					path: '/v1/messages',
					method: 'POST',
					headers: { 'content-type': 'application/json', authorization: 'Bearer test-token' },
				},
				(res) => resolve(res.statusCode ?? 0),
			);
			req.on('error', reject);
			req.end();
		});
		const elapsed = Date.now() - start;

		assert.equal(statusCode, 504, `expected 504 Gateway Timeout, got ${statusCode}`);
		// Resolve within 5× the configured timeout (generous for CI).
		assert.ok(elapsed < IDLE_TIMEOUT_MS * 5, `took ${elapsed}ms — expected < ${IDLE_TIMEOUT_MS * 5}ms`);
	});

	// ── Test 2: upstream sends headers + partial body, then stalls ────────────
	//    Expected: client connection closed within ~IDLE_TIMEOUT_MS.

	it('closes client connection when upstream stalls mid-body (idle timer)', async () => {
		const mock = await startMock((_req, res) => {
			res.writeHead(200, { 'content-type': 'text/plain' });
			res.write('partial body chunk');
			// Deliberately never calls res.end() — mid-body stall.
		});
		const proxy = spawnProxy(PROXY_PORT_STALL_BODY, boundPort(mock));
		try {
			await waitForPort(PROXY_PORT_STALL_BODY);

			const start = Date.now();
			await new Promise<void>((resolve, reject) => {
				const req = httpRequestFn(
					{
						hostname: '127.0.0.1',
						port: PROXY_PORT_STALL_BODY,
						path: '/v1/messages',
						method: 'POST',
						headers: { 'content-type': 'application/json', authorization: 'Bearer test-token' },
					},
					(res) => {
						// Drain body; 'close' fires when the connection terminates.
						res.resume();
						res.on('close', resolve);
					},
				);
				req.on('error', reject);
				req.end();
			});
			const elapsed = Date.now() - start;

			// Connection must close within 5× idle timeout, not hang forever.
			assert.ok(elapsed < IDLE_TIMEOUT_MS * 5, `connection took ${elapsed}ms — idle timer may not have fired`);
		} finally {
			proxy.kill();
			await stopServer(mock);
		}
	});

	// ── Test 3: upstream responds promptly → no premature timeout ─────────────
	//    Expected: 200 forwarded correctly.

	it('proxies a prompt upstream response without premature timeout', async () => {
		const mock = await startMock((_req, res) => {
			res.writeHead(200, { 'content-type': 'application/json' });
			res.end('{"id":"msg_test","type":"message"}');
		});
		const proxy = spawnProxy(PROXY_PORT_QUICK, boundPort(mock));
		try {
			await waitForPort(PROXY_PORT_QUICK);

			const statusCode = await new Promise<number>((resolve, reject) => {
				const req = httpRequestFn(
					{
						hostname: '127.0.0.1',
						port: PROXY_PORT_QUICK,
						path: '/v1/messages',
						method: 'POST',
						headers: { 'content-type': 'application/json', authorization: 'Bearer test-token' },
					},
					(res) => resolve(res.statusCode ?? 0),
				);
				req.on('error', reject);
				req.end();
			});

			// Prompt responses must be forwarded as-is, not cut off by the idle timer.
			assert.equal(statusCode, 200, `expected 200, got ${statusCode} — idle timer fired prematurely`);
		} finally {
			proxy.kill();
			await stopServer(mock);
		}
	});
});
