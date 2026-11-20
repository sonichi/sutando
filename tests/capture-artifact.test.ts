import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync, chmodSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { describe, it } from 'node:test';

import { takeCaptureArtifact, verifyCaptureArtifact } from '../src/capture-artifact.ts';
import {
	getVisionState,
	setVisionSession,
	startStreaming,
	stopStreaming,
} from '../src/vision-tools.ts';

const untrustedPath = 'screen-capture-server returned an untrusted path';

describe('capture artifact verification', () => {
	it('rejects outside paths without deleting the file', () => {
		const root = mkdtempSync(join(tmpdir(), 'sutando-capture-outside-'));
		const dir = join(root, 'captures');
		const outside = join(root, 'screen-secret.jpg');
		mkdirSync(dir, { mode: 0o700 });
		writeFileSync(outside, 'secret');
		try {
			assert.throws(() => verifyCaptureArtifact(outside, dir), new RegExp(untrustedPath));
			assert.equal(readFileSync(outside, 'utf8'), 'secret');
		} finally {
			rmSync(root, { recursive: true, force: true });
		}
	});

	it('rejects symlinks, unexpected basenames, and group-writable directories', () => {
		const root = mkdtempSync(join(tmpdir(), 'sutando-capture-invalid-'));
		const dir = join(root, 'captures');
		const outside = join(root, 'screen-target.jpg');
		mkdirSync(dir, { mode: 0o700 });
		writeFileSync(outside, 'target');
		const link = join(dir, 'screen-link.jpg');
		symlinkSync(outside, link);
		const wrongName = join(dir, 'image.jpg');
		writeFileSync(wrongName, 'image');
		try {
			assert.throws(() => verifyCaptureArtifact(link, dir), new RegExp(untrustedPath));
			assert.equal(readFileSync(outside, 'utf8'), 'target');
			assert.throws(() => verifyCaptureArtifact(wrongName, dir), new RegExp(untrustedPath));
			if (typeof process.getuid === 'function') {
				chmodSync(dir, 0o770);
				const groupWritable = join(dir, 'screen-group.jpg');
				writeFileSync(groupWritable, 'image');
				assert.throws(() => verifyCaptureArtifact(groupWritable, dir), new RegExp(untrustedPath));
			}
		} finally {
			rmSync(root, { recursive: true, force: true });
		}
	});

	it('reads and deletes a valid capture', () => {
		const root = mkdtempSync(join(tmpdir(), 'sutando-capture-valid-'));
		const dir = join(root, 'captures');
		const path = join(dir, 'screen-1.jpg');
		mkdirSync(dir, { mode: 0o700 });
		writeFileSync(path, 'frame');
		const previousDir = process.env.SUTANDO_SCREENSHOT_DIR;
		process.env.SUTANDO_SCREENSHOT_DIR = dir;
		try {
			assert.equal(verifyCaptureArtifact(path, dir), path);
			assert.deepEqual(takeCaptureArtifact(path), Buffer.from('frame'));
			assert.equal(existsSync(path), false);
		} finally {
			if (previousDir === undefined) delete process.env.SUTANDO_SCREENSHOT_DIR;
			else process.env.SUTANDO_SCREENSHOT_DIR = previousDir;
			rmSync(root, { recursive: true, force: true });
		}
	});
});

describe('screen source artifact trust', () => {
	it('does not send or delete a capture path outside the screenshot directory', async (t) => {
		const root = mkdtempSync(join(tmpdir(), 'sutando-capture-stream-'));
		const dir = join(root, 'captures');
		const outside = join(root, 'screen-outside.jpg');
		mkdirSync(dir, { mode: 0o700 });
		writeFileSync(outside, 'outside frame');

		let received = false;
		const server = createServer((_req, res) => {
			received = true;
			res.writeHead(200, { 'Content-Type': 'application/json' });
			res.end(JSON.stringify({ status: 'ok', path: outside }));
		});
		try {
			await new Promise<void>((resolve, reject) => {
				server.once('error', reject);
				server.listen(7845, resolve);
			});
		} catch (error) {
			rmSync(root, { recursive: true, force: true });
			if ((error as NodeJS.ErrnoException).code === 'EADDRINUSE') {
				t.skip('screen-capture port 7845 is already in use');
				return;
			}
			throw error;
		}

		const previousDir = process.env.SUTANDO_SCREENSHOT_DIR;
		process.env.SUTANDO_SCREENSHOT_DIR = dir;
		let sent = 0;
		setVisionSession({ transport: { sendFile: () => { sent++; } } });
		try {
			startStreaming('screen', 1, 'pull');
			const deadline = Date.now() + 5_000;
			while (!received && Date.now() < deadline) await delay(20);
			await delay(50);
			assert.equal(received, true, 'screen source requested a frame');
			assert.equal(sent, 0, 'untrusted frame never reaches the vision transport');
			assert.equal(readFileSync(outside, 'utf8'), 'outside frame');
			assert.equal(getVisionState().streaming, true);
		} finally {
			stopStreaming();
			setVisionSession(null);
			if (previousDir === undefined) delete process.env.SUTANDO_SCREENSHOT_DIR;
			else process.env.SUTANDO_SCREENSHOT_DIR = previousDir;
			await new Promise<void>((resolve) => server.close(() => resolve()));
			rmSync(root, { recursive: true, force: true });
		}
	});
});
