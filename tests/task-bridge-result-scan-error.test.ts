import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

// Structural regression for issue #3026 — the result-scan catch must log
// throws that have no errno `code` (TypeError, ReferenceError, helper
// throws). Gating on `code &&` swallows those silently. ENOENT stays quiet.
// The try still wraps the whole for-loop (abort-the-pass); continuing to
// the next file is out of scope for #3026.

const SRC = readFileSync(join(import.meta.dirname ?? fileURLToPath(new URL('.', import.meta.url)), '..', 'src', 'task-bridge.ts'), 'utf-8').replace(/\r\n/g, '\n');

const LOG = '[TaskBridge] result-scan threw (non-fatal):';

function resultScanCatch(): string {
	const logIdx = SRC.indexOf(LOG);
	if (logIdx === -1) throw new Error('result-scan log not found');
	const catchIdx = SRC.lastIndexOf('} catch (err) {', logIdx);
	if (catchIdx === -1) throw new Error('catch not found before result-scan log');
	// Catch body is a handful of lines; take a short window past the log.
	return SRC.slice(catchIdx, logIdx + LOG.length + 40);
}

describe('task-bridge.ts — result-scan catch logs non-errno throws (#3026)', () => {
	it('result-scan catch exists', () => {
		assert.ok(SRC.includes(LOG), 'result-scan catch must log "result-scan threw (non-fatal)"');
	});

	it('logs when errno code is missing — does not gate on truthy code', () => {
		const block = resultScanCatch();
		assert.match(
			block,
			/if \(code !== 'ENOENT'\)/,
			'result-scan catch must log whenever code is not ENOENT, including missing code',
		);
		assert.doesNotMatch(
			block,
			/if \(code && code !== 'ENOENT'\)/,
			'gating on truthy code swallows TypeError/ReferenceError (#3026)',
		);
	});

	it('ENOENT stays excluded', () => {
		const block = resultScanCatch();
		assert.ok(block.includes("'ENOENT'"),
			'ENOENT (dir missing / file in transit) must stay the silent case');
	});

	it('console.error is inside the ENOENT guard', () => {
		const block = resultScanCatch();
		const ifIdx = block.indexOf("if (code !== 'ENOENT')");
		const errIdx = block.indexOf('console.error');
		assert.ok(ifIdx !== -1 && errIdx !== -1, 'if + console.error must both be in the catch');
		assert.ok(ifIdx < errIdx, "console.error must be inside if (code !== 'ENOENT')");
	});

	it('the try that owns this catch still wraps the for-loop (abort-the-pass)', () => {
		const readdirIdx = SRC.indexOf('const files = readdirSync(RESULT_DIR)');
		const forIdx = SRC.indexOf('for (const file of files)', readdirIdx);
		const logIdx = SRC.indexOf(LOG);
		assert.ok(readdirIdx !== -1, 'result-scan readdirSync not found');
		assert.ok(forIdx !== -1, 'for (const file of files) not found after readdirSync');
		assert.ok(logIdx !== -1, 'result-scan log not found');
		assert.ok(
			readdirIdx < forIdx && forIdx < logIdx,
			'result-scan try must wrap for (const file of files); catch comes after the loop',
		);

		// The catch that owns the log is the watcher-body catch (two tabs),
		// not a per-iteration handler. Per-file continue is out of scope for #3026.
		const catchIdx = SRC.lastIndexOf('} catch (err) {', logIdx);
		const catchLineStart = SRC.lastIndexOf('\n', catchIdx) + 1;
		const catchLine = SRC.slice(catchLineStart, SRC.indexOf('\n', catchIdx));
		assert.equal(
			catchLine,
			'\t\t} catch (err) {',
			'result-scan catch must remain the outer pass-level handler, not a per-file catch',
		);
	});
});
