import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdirSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, it } from 'node:test';

import {
	getVisionState,
	startVisionControlServer,
	stopVisionControlServer,
} from '../src/vision-tools.ts';
import type { AddressInfo } from 'node:net';

describe('vision control bearer and Origin checks', () => {
	it('authorizes POST routes before actions while leaving state open', async (t) => {
		const workspace = mkdtempSync(join(tmpdir(), 'sutando-vision-auth-'));
		mkdirSync(join(workspace, 'state'));
		const previous = {
			apiToken: process.env.SUTANDO_API_TOKEN,
			testMode: process.env.SUTANDO_TEST_MODE,
			workspace: process.env.SUTANDO_WORKSPACE,
		};
		const token = 'vision-control-test-owner-token';
		process.env.SUTANDO_API_TOKEN = token;
		process.env.SUTANDO_TEST_MODE = '1';
		process.env.SUTANDO_WORKSPACE = workspace;

		const server = startVisionControlServer(0);
		const close = async () => {
			if (!server.listening) return;
			const closed = once(server, 'close');
			stopVisionControlServer();
			await closed;
		};
		t.after(async () => {
			await close();
			rmSync(workspace, { recursive: true, force: true });
			if (previous.apiToken === undefined) delete process.env.SUTANDO_API_TOKEN;
			else process.env.SUTANDO_API_TOKEN = previous.apiToken;
			if (previous.testMode === undefined) delete process.env.SUTANDO_TEST_MODE;
			else process.env.SUTANDO_TEST_MODE = previous.testMode;
			if (previous.workspace === undefined) delete process.env.SUTANDO_WORKSPACE;
			else process.env.SUTANDO_WORKSPACE = previous.workspace;
		});

		if (!server.listening) await once(server, 'listening');
		const port = (server.address() as AddressInfo).port;
		const base = `http://127.0.0.1:${port}`;
		const post = (path: string, headers: Record<string, string> = {}) =>
			fetch(`${base}${path}`, { method: 'POST', headers, body: JSON.stringify({ source: 'browser', mode: 'push' }) });

		assert.equal((await post('/vision/start')).status, 401);
		assert.equal((await post('/vision/start', { Authorization: 'Bearer wrong' })).status, 401);
		assert.equal((await post('/vision/start', {
			Authorization: `Bearer ${token}`,
			Origin: 'https://evil.example',
		})).status, 403);
		const authorizedStart = await post('/vision/start', { Authorization: `Bearer ${token}` });
		assert.notEqual(authorizedStart.status, 401);
		assert.notEqual(authorizedStart.status, 403);
		assert.equal(getVisionState().streaming, false);
		assert.equal((await post('/vision/frame')).status, 401);
		assert.equal((await fetch(`${base}/vision/state`)).status, 200);
		await close();
		assert.equal(server.listening, false);
	});
});
