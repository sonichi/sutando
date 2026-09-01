// The bundled-build Watch regression (field report 2026-08-14): the lazy
// screen-capture-server spawn resolved the .py next to the RUNNING module,
// which is dist/voice-agent.js in the bundle — and dist/ ships no .py files,
// so every attempt died as a silent 8s port timeout. The fix resolves from
// the sutando root (marker: sutando.config.json — the same walk
// python-binary.ts uses) with the module-sibling kept as the dev fallback.
// These tests pin the candidate order against both on-disk shapes.

import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { _captureServerScriptCandidates } from '../src/vision-tools.js';

const scratch = mkdtempSync(join(tmpdir(), 'capture-path-'));
after(() => {
	try {
		rmSync(scratch, { recursive: true, force: true });
	} catch {
		/* best effort */
	}
});

describe('P7 bundled-build capture-server path resolution', () => {
	it('bundle shape: module runs from dist/, script ships in the SIBLING src/ — root candidate wins', () => {
		const root = join(scratch, 'bundle', 'sutando');
		mkdirSync(join(root, 'dist'), { recursive: true });
		mkdirSync(join(root, 'src'), { recursive: true });
		writeFileSync(join(root, 'sutando.config.json'), '{}');
		writeFileSync(join(root, 'src', 'screen-capture-server.py'), '# server');
		const candidates = _captureServerScriptCandidates(join(root, 'dist'));
		assert.equal(candidates[0], join(root, 'src', 'screen-capture-server.py'));
		const resolved = candidates.find((c) => existsSync(c));
		assert.equal(resolved, join(root, 'src', 'screen-capture-server.py'), 'the bundle resolves to the shipped copy');
	});

	it('dev shape: module runs from src/ — root candidate and sibling coincide, deduplicated', () => {
		const root = join(scratch, 'dev', 'sutando');
		mkdirSync(join(root, 'src'), { recursive: true });
		writeFileSync(join(root, 'sutando.config.json'), '{}');
		writeFileSync(join(root, 'src', 'screen-capture-server.py'), '# server');
		const candidates = _captureServerScriptCandidates(join(root, 'src'));
		assert.deepEqual(candidates, [join(root, 'src', 'screen-capture-server.py')], 'one deduplicated candidate');
	});

	it('no root marker: falls back to the module sibling (pre-fix behavior preserved for exotic layouts)', () => {
		const bare = join(scratch, 'bare');
		mkdirSync(bare, { recursive: true });
		const candidates = _captureServerScriptCandidates(bare);
		assert.ok(candidates.includes(join(bare, 'screen-capture-server.py')));
	});

	it('the real repo resolves to an existing script from BOTH src/ and a simulated dist/', () => {
		// Run against the actual checkout: from src/ (dev truth) and from a
		// hypothetical sibling dist/ (bundle truth) the walk must land on the
		// same real file.
		const realSrc = join(process.cwd(), 'src');
		const fromSrc = _captureServerScriptCandidates(realSrc).find((c) => existsSync(c));
		assert.ok(fromSrc && fromSrc.endsWith(join('src', 'screen-capture-server.py')));
		const fromDist = _captureServerScriptCandidates(join(process.cwd(), 'dist')).find((c) => existsSync(c));
		assert.equal(fromDist, fromSrc, 'dist-resident module finds the same shipped script');
	});
});
