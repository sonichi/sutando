/**
 * Cross-language contract for src/task_envelope.ts: a stamp produced by the
 * TS writer half MUST read `verified` through the Python module that every
 * consumer uses — parity is the whole point of the mirror, so the test
 * verifies through the real src/task_envelope.py, not a TS re-derivation.
 */
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { stampText, tryStampText, keyPath } from '../src/task_envelope.js';

const REPO = dirname(dirname(fileURLToPath(import.meta.url)));
const WS = mkdtempSync(join(tmpdir(), 'envelope-ts-ws-'));
mkdirSync(join(WS, 'state', 'auth'), { recursive: true });

const BODY = 'id: task-ts1\ntimestamp: 2026-08-17T00:00:00Z\nsource: voice\ntask: hello\n';

after(() => rmSync(WS, { recursive: true, force: true }));

function pyVerdict(text: string): string {
	const py = [
		'import sys, json',
		`sys.path.insert(0, ${JSON.stringify(join(REPO, 'src'))})`,
		'from pathlib import Path',
		'from task_envelope import verify_text',
		'print(verify_text(sys.stdin.read(), Path(sys.argv[1]))["verdict"])',
	].join('\n');
	return execFileSync('python3', ['-c', py, WS], { input: text, encoding: 'utf-8' }).trim();
}

describe('task_envelope.ts', () => {
	it('TS stamp reads verified through the Python verifier', () => {
		const stamped = stampText(BODY, WS);
		assert.match(stamped.split('\n')[1], /^envelope_hmac: v1:[0-9a-f]{64}$/);
		assert.equal(pyVerdict(stamped), 'verified');
	});

	it('a tampered body reads invalid, and unstamped reads unsigned', () => {
		const stamped = stampText(BODY, WS);
		assert.equal(pyVerdict(stamped.replace('hello', 'goodbye')), 'invalid');
		assert.equal(pyVerdict(BODY), 'unsigned');
	});

	it('re-stamping replaces the canonical stamp, never doubles it', () => {
		const twice = stampText(stampText(BODY, WS), WS);
		const stamps = twice.split('\n').filter((l) => l.startsWith('envelope_hmac: v1:'));
		assert.equal(stamps.length, 1);
		assert.equal(pyVerdict(twice), 'verified');
	});

	it('a stamp-shaped line in user content survives byte-identically', () => {
		const tricky = BODY + 'envelope_hmac: v1:' + 'a'.repeat(64) + '\n';
		const stamped = stampText(tricky, WS);
		assert.ok(stamped.includes('envelope_hmac: v1:' + 'a'.repeat(64)));
		assert.equal(pyVerdict(stamped), 'verified');
	});

	it('key created by TS is 0600 hex and reused by Python (one shared key)', () => {
		const raw = readFileSync(keyPath(WS), 'utf-8').trim();
		assert.match(raw, /^[0-9a-f]{64}$/);
	});

	it('corrupt key file: never stamps under a short key, fails open instead', () => {
		// Buffer.from(x,'hex') never throws — before the length guard, an
		// empty or malformed key file stamped under a ZERO-LENGTH key and
		// emitted a well-formed envelope no verifier accepts.
		for (const bad of ['', 'zzzz', 'deadbeef', 'a'.repeat(63)]) {
			const ws = mkdtempSync(join(tmpdir(), 'envelope-ts-badkey-'));
			try {
				mkdirSync(dirname(keyPath(ws)), { recursive: true });
				writeFileSync(keyPath(ws), bad);
				assert.throws(() => stampText(BODY, ws),
					/invalid task-hmac key/,
					`stampText must reject key file ${JSON.stringify(bad)}`);
				assert.equal(tryStampText(BODY, ws), BODY,
					`tryStampText must fail open on key file ${JSON.stringify(bad)}`);
			} finally {
				rmSync(ws, { recursive: true, force: true });
			}
		}
	});

	it('tryStampText fails open when the key location is unwritable', () => {
		const lockedWs = mkdtempSync(join(tmpdir(), 'envelope-ts-locked-'));
		try {
			// state/ exists but is unwritable+untraversable: key read AND create fail.
			mkdirSync(join(lockedWs, 'state'), { recursive: true });
			chmodSync(join(lockedWs, 'state'), 0o000);
			const out = tryStampText(BODY, lockedWs);
			assert.equal(out, BODY);
		} finally {
			chmodSync(join(lockedWs, 'state'), 0o755);
			rmSync(lockedWs, { recursive: true, force: true });
		}
	});
});
