import assert from 'node:assert/strict';
import { createServer, request as httpRequest, type IncomingHttpHeaders, type IncomingMessage } from 'node:http';
import { spawn, type ChildProcess } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';
import { describe, it } from 'node:test';
import WebSocket from 'ws';

const REPO_ROOT = join(fileURLToPath(new URL('.', import.meta.url)), '..');
const TOKEN = 'web-client-proxy-owner-token';

async function freePort(): Promise<number> {
	const server = createServer();
	await new Promise<void>((resolve, reject) => {
		server.once('error', reject);
		server.listen(0, '127.0.0.1', resolve);
	});
	const address = server.address();
	const port = typeof address === 'object' && address ? address.port : 0;
	await new Promise<void>((resolve) => server.close(() => resolve()));
	if (!port) throw new Error('could not allocate a free port');
	return port;
}

async function waitForClient(child: ChildProcess, port: number, output: () => string): Promise<void> {
	const base = `http://127.0.0.1:${port}`;
	const deadline = Date.now() + 15_000;
	while (Date.now() < deadline) {
		if (child.exitCode !== null) throw new Error(`web-client exited (${child.exitCode}): ${output()}`);
		try {
			await fetch(`${base}/`, { signal: AbortSignal.timeout(500) });
			return;
		} catch {
			await delay(50);
		}
	}
	throw new Error(`web-client did not listen: ${output()}`);
}

function sendRequest(
	port: number,
	path: string,
	method: string,
	headers: Record<string, string>,
	body?: string,
): Promise<{ status: number; headers: IncomingHttpHeaders; body: string }> {
	return new Promise((resolve, reject) => {
		const req = httpRequest({ hostname: '127.0.0.1', port, path, method, headers }, res => {
			const chunks: Buffer[] = [];
			res.on('data', chunk => chunks.push(Buffer.from(chunk)));
			res.on('end', () => resolve({
				status: res.statusCode ?? 0,
				headers: res.headers,
				body: Buffer.concat(chunks).toString('utf8'),
			}));
		});
		req.on('error', reject);
		req.end(body);
	});
}

async function stopChild(child: ChildProcess): Promise<void> {
	if (child.exitCode !== null || child.signalCode !== null) return;
	const exited = new Promise<void>((resolve) => child.once('exit', () => resolve()));
	child.kill('SIGTERM');
	await Promise.race([exited, delay(2_000)]);
	if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
}

describe('web-client local vision and voice token routes', () => {
	it('rejects hostile Origins and Host rebinding, forwards the local bearer, and restricts token delivery', async () => {
		const workspace = mkdtempSync(join(tmpdir(), 'sutando-web-client-auth-'));
		const controllerRequests: IncomingMessage[] = [];
		const controller = createServer((req, res) => {
			controllerRequests.push(req);
			res.writeHead(200, { 'Content-Type': 'application/json' });
			res.end(JSON.stringify({ status: 'ok' }));
		});
		const voiceRequests: Array<{ url: string | undefined; headers: IncomingMessage['headers'] }> = [];
		const voiceSockets = new Set<import('node:net').Socket>();
		const voiceServer = createServer();
		voiceServer.on('upgrade', (req, socket) => {
			voiceRequests.push({ url: req.url, headers: req.headers });
			voiceSockets.add(socket);
			socket.once('close', () => voiceSockets.delete(socket));
			const key = req.headers['sec-websocket-key'];
			const accept = createHash('sha1').update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`).digest('base64');
			socket.write([
				'HTTP/1.1 101 Switching Protocols',
				'Upgrade: websocket',
				'Connection: Upgrade',
				`Sec-WebSocket-Accept: ${accept}`,
				'',
				'',
			].join('\r\n'));
			socket.on('data', () => socket.write(Buffer.from([0x88, 0x00])));
		});
		await new Promise<void>((resolve, reject) => {
			controller.once('error', reject);
			controller.listen(0, '127.0.0.1', resolve);
		});
		await new Promise<void>((resolve, reject) => {
			voiceServer.once('error', reject);
			voiceServer.listen(0, '127.0.0.1', resolve);
		});
		const visionPort = (controller.address() as { port: number }).port;
		const voicePort = (voiceServer.address() as { port: number }).port;
		const clientPort = await freePort();
		const cli = join(REPO_ROOT, 'node_modules', 'tsx', 'dist', 'cli.mjs');
		const child = spawn(process.execPath, [cli, 'src/web-client.ts'], {
			cwd: REPO_ROOT,
			env: {
				...process.env,
				CLIENT_HOST: '127.0.0.1',
				CLIENT_PORT: String(clientPort),
				SUTANDO_API_TOKEN: TOKEN,
				SUTANDO_LAN_SHARE: '0',
				SUTANDO_TEST_MODE: '1',
				SUTANDO_WORKSPACE: workspace,
				VISION_CONTROL_PORT: String(visionPort),
				PORT: String(voicePort),
			},
			stdio: ['ignore', 'pipe', 'pipe'],
		});
		let output = '';
		child.stdout?.on('data', chunk => { output += String(chunk); });
		child.stderr?.on('data', chunk => { output += String(chunk); });

		try {
			await waitForClient(child, clientPort, () => output);
			const path = '/vision/start';
			const hostileOrigin = await sendRequest(clientPort, path, 'POST', {
				host: `localhost:${clientPort}`,
				origin: 'https://evil.example',
				'content-type': 'application/json',
			}, JSON.stringify({ mode: 'push' }));
			assert.equal(hostileOrigin.status, 403);
			assert.equal(controllerRequests.length, 0);

			const reboundHost = await sendRequest(clientPort, path, 'POST', {
				host: `evil.example:${clientPort}`,
				origin: `http://evil.example:${clientPort}`,
				'content-type': 'application/json',
			}, JSON.stringify({ mode: 'push' }));
			assert.equal(reboundHost.status, 403);
			assert.equal(controllerRequests.length, 0);

			const wrongContentType = await sendRequest(clientPort, path, 'POST', {
				host: `localhost:${clientPort}`,
				origin: `http://localhost:${clientPort}`,
				'content-type': 'text/plain',
			}, 'not-json');
			assert.equal(wrongContentType.status, 415);
			assert.equal(controllerRequests.length, 0);

			const forwarded = await sendRequest(clientPort, path, 'POST', {
				host: `localhost:${clientPort}`,
				origin: `http://localhost:${clientPort}`,
				'content-type': 'application/json',
			}, JSON.stringify({ mode: 'push' }));
			assert.equal(forwarded.status, 200);
			assert.equal(controllerRequests.length, 1);
			assert.equal(controllerRequests[0].headers.authorization, `Bearer ${TOKEN}`);

			const forwardedTokenResponse = await sendRequest(clientPort, '/voice-ws-token', 'POST', {
				host: `localhost:${clientPort}`,
				origin: `http://localhost:${clientPort}`,
				'cf-connecting-ip': '198.51.100.4',
			});
			assert.equal(forwardedTokenResponse.status, 403);

			const tokenResponse = await sendRequest(clientPort, '/voice-ws-token', 'POST', {
				host: `localhost:${clientPort}`,
				origin: `http://localhost:${clientPort}`,
			});
			assert.equal(tokenResponse.status, 200);
			assert.equal(tokenResponse.headers['cache-control'], 'no-store');
			assert.deepEqual(JSON.parse(tokenResponse.body), { token: TOKEN });

			const rejectedSocket = new WebSocket(`ws://127.0.0.1:${clientPort}/ws?takeover=1`, {
				origin: 'https://evil.example',
			});
			const rejected = await new Promise<string>((resolve) => {
				rejectedSocket.once('open', () => resolve('open'));
				rejectedSocket.once('error', () => resolve('error'));
			});
			assert.equal(rejected, 'error');
			assert.equal(voiceRequests.length, 0);

			const socket = new WebSocket(`ws://127.0.0.1:${clientPort}/ws?takeover=1`, {
				origin: `http://127.0.0.1:${clientPort}`,
				headers: { Authorization: 'Bearer client-supplied-token' },
			});
			await new Promise<void>((resolve, reject) => {
				socket.once('open', resolve);
				socket.once('error', reject);
			});
			assert.equal(voiceRequests[0].url, '/?takeover=1');
			assert.equal(voiceRequests[0].headers.authorization, `Bearer ${TOKEN}`);
			assert.equal(voiceRequests[0].headers.origin, undefined);
			const socketClosed = new Promise<void>((resolve) => socket.once('close', () => resolve()));
			socket.close();
			await Promise.race([socketClosed, delay(2_000)]);

			const hostileHost = await sendRequest(clientPort, '/voice-ws-token', 'POST', {
				host: `evil.example:${clientPort}`,
				origin: `http://evil.example:${clientPort}`,
			});
			assert.equal(hostileHost.status, 403);

			const missingOrigin = await sendRequest(clientPort, '/voice-ws-token', 'POST', {
				host: `localhost:${clientPort}`,
			});
			assert.equal(missingOrigin.status, 403);
		} finally {
			await stopChild(child);
			for (const socket of voiceSockets) socket.destroy();
			await new Promise<void>((resolve) => voiceServer.close(() => resolve()));
			await new Promise<void>((resolve) => controller.close(() => resolve()));
			rmSync(workspace, { recursive: true, force: true });
		}
	});
});
