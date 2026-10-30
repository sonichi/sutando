import assert from 'node:assert/strict';
import { spawn, type ChildProcess } from 'node:child_process';
import { createServer, request as httpRequest, type Server } from 'node:http';
import { once } from 'node:events';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, before, describe, it } from 'node:test';

const repo = process.cwd();
const token = 'dashboard-test-token';
let workspace = '';
let clientPort = 0;
let agentPort = 0;
let voicePort = 0;
let controlPort = 0;
let client: ChildProcess | undefined;
let agentApi: Server;
const forwarded: Array<{ path: string; authorization: string | undefined }> = [];

async function freePort(): Promise<number> {
	const server = createServer();
	server.listen(0, '127.0.0.1');
	await once(server, 'listening');
	const address = server.address();
	assert.ok(address && typeof address !== 'string');
	const port = address.port;
	await new Promise<void>(resolve => server.close(() => resolve()));
	return port;
}

async function request(
	port: number,
	path: string,
	extraHeaders: Record<string, string> = {},
): Promise<{ status: number; body: Buffer; contentType: string }> {
	return new Promise((resolve, reject) => {
		const req = httpRequest({
			host: '127.0.0.1',
			port,
			path,
			method: 'GET',
			headers: { Host: `localhost:${port}`, ...extraHeaders },
		}, res => {
			const chunks: Buffer[] = [];
			res.on('data', chunk => chunks.push(Buffer.from(chunk)));
			res.on('end', () => resolve({
				status: res.statusCode || 0,
				body: Buffer.concat(chunks),
				contentType: String(res.headers['content-type'] || ''),
			}));
		});
		req.on('error', reject);
		req.end();
	});
}

async function waitForClient(): Promise<void> {
	for (let attempt = 0; attempt < 100; attempt++) {
		if (client?.exitCode !== null && client?.exitCode !== undefined) {
			throw new Error(`web-client exited with ${client.exitCode}`);
		}
		try {
			const response = await request(clientPort, '/');
			if (response.status === 200) return;
		} catch {}
		await new Promise(resolve => setTimeout(resolve, 50));
	}
	throw new Error('web-client did not start');
}

before(async () => {
	workspace = mkdtempSync(join(tmpdir(), 'sutando-dashboard-proxy-'));
	clientPort = await freePort();
	agentPort = await freePort();
	voicePort = await freePort();
	controlPort = await freePort();
	agentApi = createServer((req, res) => {
		forwarded.push({ path: req.url || '', authorization: req.headers.authorization });
		if (req.headers.authorization !== `Bearer ${token}`) {
			res.writeHead(401, { 'Content-Type': 'application/json' });
			res.end(JSON.stringify({ error: 'unauthorized' }));
			return;
		}
		if (req.url?.startsWith('/media/')) {
			res.writeHead(200, { 'Content-Type': 'image/png' });
			res.end('media');
			return;
		}
		res.writeHead(200, { 'Content-Type': 'application/json' });
		res.end(JSON.stringify({ tasks: [], lines: ['voice log'] }));
	});
	agentApi.listen(agentPort, '127.0.0.1');
	await once(agentApi, 'listening');
	client = spawn(process.execPath, ['--import', 'tsx', 'src/web-client.ts'], {
		cwd: repo,
		env: {
			...process.env,
			SUTANDO_TEST_MODE: '1',
			SUTANDO_TELEMETRY: '0',
			SUTANDO_WORKSPACE: workspace,
			SUTANDO_API_TOKEN: token,
			SUTANDO_AGENT_API_PORT: String(agentPort),
			CLIENT_HOST: '127.0.0.1',
			CLIENT_PORT: String(clientPort),
			PORT: String(voicePort),
			VISION_CONTROL_PORT: String(controlPort),
		},
		stdio: 'ignore',
	});
	await waitForClient();
});

after(async () => {
	if (client && client.exitCode === null) {
		client.kill('SIGTERM');
		await once(client, 'exit');
	}
	if (agentApi) await new Promise<void>(resolve => agentApi.close(() => resolve()));
	if (workspace) rmSync(workspace, { recursive: true, force: true });
});

describe('web-client dashboard API bearer proxy', () => {
	it('forwards the configured bearer for owner task, media, and voice-log reads', async () => {
		const taskResponse = await request(clientPort, '/api/dashboard/tasks/active');
		const logResponse = await request(clientPort, '/api/dashboard/logs/voice');
		const mediaResponse = await request(clientPort, '/api/dashboard/media/sample.png');

		assert.equal(taskResponse.status, 200);
		assert.equal(logResponse.status, 200);
		assert.equal(mediaResponse.status, 200);
		assert.equal(mediaResponse.contentType, 'image/png');
		assert.equal(mediaResponse.body.toString(), 'media');
		assert.deepEqual(forwarded, [
			{ path: '/tasks/active', authorization: `Bearer ${token}` },
			{ path: '/logs/voice', authorization: `Bearer ${token}` },
			{ path: '/media/sample.png', authorization: `Bearer ${token}` },
		]);
	});

	it('allows a matching loopback Origin and rejects hostile origins and fetch metadata', async () => {
		const sameOrigin = await request(
			clientPort, '/api/dashboard/tasks/active', { Origin: `http://localhost:${clientPort}` });
		assert.equal(sameOrigin.status, 200);
		for (const headers of [
			{ Origin: 'https://evil.example' },
			{ Origin: `http://localhost:${clientPort}`, 'Sec-Fetch-Site': 'cross-site' },
			{ 'Sec-Fetch-Site': 'cross-site' },
			{ 'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'cors', Origin: 'null' },
			{ Origin: `http://localhost:${clientPort}:1` },
			// Another localhost port embeds owner media with same-site + no Origin.
			{ 'Sec-Fetch-Site': 'same-site' },
			{ Origin: 'http://localhost:1', 'Sec-Fetch-Site': 'same-site' },
		]) {
			const response = await request(clientPort, '/api/dashboard/tasks/active', headers);
			assert.equal(response.status, 403, JSON.stringify(headers));
		}
	});
});
