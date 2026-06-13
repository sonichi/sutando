import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

// Structural regression for issue #1381 — task-bridge.ts must honor
// [no-send] / [REPLIED] skip markers by archiving silently without calling
// onResult() (which would speak the raw marker text via voice).
//
// Python bridges (discord-bridge.py, telegram-bridge.py, slack-bridge.py)
// already honor these via parse_markers(). This test locks in parity for the
// TypeScript voice surface.

const SRC = readFileSync(join(import.meta.dirname ?? new URL('.', import.meta.url).pathname, '..', 'src', 'task-bridge.ts'), 'utf-8');

// Helpers — find a block by its unique anchor text, return the source after it.
function afterBlock(anchor: string): string {
	const idx = SRC.indexOf(anchor);
	if (idx === -1) throw new Error(`anchor not found: ${JSON.stringify(anchor)}`);
	return SRC.slice(idx);
}

describe('task-bridge.ts — [no-send]/[REPLIED] skip-marker handling (#1381)', () => {
	it('contains the skip-marker regex for [no-send]', () => {
		assert.ok(
			SRC.includes('no-send'),
			'task-bridge.ts must contain a [no-send] regex guard'
		);
	});

	it('contains the skip-marker regex for [REPLIED]', () => {
		assert.ok(
			SRC.includes('REPLIED'),
			'task-bridge.ts must contain a [REPLIED] regex guard'
		);
	});

	it('skip-marker block logs "has skip marker" and archives silently', () => {
		assert.ok(
			SRC.includes('has skip marker'),
			'skip-marker block must log "has skip marker" so failures are diagnosable'
		);
	});

	it('skip-marker guard appears BEFORE the fallthrough onResult() call', () => {
		// There are two onResult() calls: one in the voice-only short-circuit
		// (~line 681) and one in the main fallthrough (~line 807). The skip-marker
		// guard only needs to precede the fallthrough one — that's the path that
		// would otherwise speak raw marker text via voice.
		const skipIdx = SRC.indexOf('has skip marker');
		// Find the second onResult() call (the fallthrough path).
		const first = SRC.indexOf('onResult(result)');
		const fallthroughOnResultIdx = SRC.indexOf('onResult(result)', first + 1);
		assert.ok(skipIdx !== -1, '"has skip marker" log not found');
		assert.ok(fallthroughOnResultIdx !== -1, 'fallthrough onResult(result) not found');
		assert.ok(
			skipIdx < fallthroughOnResultIdx,
			`skip-marker guard (pos ${skipIdx}) must appear before fallthrough onResult() (pos ${fallthroughOnResultIdx})`
		);
	});

	it('skip-marker block calls continue to prevent fallthrough to onResult()', () => {
		// Locate the skip-marker section and verify it ends with continue before
		// the next major branch ("Voice client offline").
		const anchor = 'has skip marker';
		const afterSkip = afterBlock(anchor);
		const continueIdx = afterSkip.indexOf('continue;');
		const onResultIdx = afterSkip.indexOf('onResult(result)');
		assert.ok(continueIdx !== -1, 'skip-marker block must call continue;');
		assert.ok(
			continueIdx < onResultIdx,
			'continue; must appear before onResult() within the skip-marker block'
		);
	});

	it('skip-marker guard appears after the [deduped:] check (correct ordering)', () => {
		const dedupIdx = SRC.indexOf('deduped marker; archiving silently');
		const skipIdx = SRC.indexOf('has skip marker');
		assert.ok(dedupIdx !== -1, '"deduped marker; archiving silently" log not found');
		assert.ok(skipIdx !== -1, '"has skip marker" log not found');
		assert.ok(
			dedupIdx < skipIdx,
			`[deduped:] guard (pos ${dedupIdx}) must appear before skip-marker guard (pos ${skipIdx})`
		);
	});

	it('skip-marker guard has file.startsWith("task-") guard (mirrors [deduped:] block)', () => {
		// The guard must only apply to task files, not proactive-result-*.txt or
		// other result files that could have marker-like content. Without this guard
		// a proactive-result file beginning with [no-send] would be swallowed here
		// instead of reaching discord-bridge's poll_proactive delivery path.
		const afterSkip = afterBlock('has skip marker');
		// Look backward from the log line to find the enclosing if-condition.
		const beforeSkip = SRC.slice(0, SRC.indexOf('has skip marker'));
		const lastIfBeforeSkip = beforeSkip.lastIndexOf('if (');
		const ifCondition = SRC.slice(lastIfBeforeSkip, lastIfBeforeSkip + 120);
		assert.ok(
			ifCondition.includes('file.startsWith('),
			`skip-marker if-condition must include file.startsWith() guard; got: ${ifCondition.slice(0, 80)}`
		);
	});

	it('skip-marker block POSTs task-done to local API (mirrors [deduped:] block)', () => {
		// Without the task-done POST, the dashboard would show skip-marker tasks
		// as stuck. The [deduped:] block sends this POST; skip-marker must too.
		const afterSkip = afterBlock('has skip marker');
		const taskDoneIdx = afterSkip.indexOf('task-done');
		const continueIdx = afterSkip.indexOf('continue;');
		assert.ok(taskDoneIdx !== -1, 'skip-marker block must POST to task-done endpoint');
		assert.ok(
			taskDoneIdx < continueIdx,
			'task-done POST must appear before continue; in skip-marker block'
		);
	});
});
