import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { publishVerifiedResult, readVerifiedResult } from '../src/result-read.js';

test('verified reads retain a complete result larger than the default subprocess buffer', () => {
	const dir = mkdtempSync(join(tmpdir(), 'paired-large-'));
	try {
		const results = join(dir, 'results');
		mkdirSync(results);
		const path = join(results, 'task-large.txt');
		const body = 'a'.repeat(1024 * 1024 + 8192);
		writeFileSync(path, body);
		assert.equal(readVerifiedResult(path), null);
		publishVerifiedResult(path, body);
		assert.equal(readVerifiedResult(path), body);
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
});
