import { fileURLToPath } from 'node:url';
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

const SRC = readFileSync(join(import.meta.dirname ?? fileURLToPath(new URL('.', import.meta.url)), '..', 'src', 'task-bridge.ts'), 'utf-8');

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

	it('has NO private [deduped:] matcher left in code — it is a skip marker now', () => {
		// Comments are stripped first: `deduped` may survive in prose, never in CODE.
		// The grammar lives once, in skip_marker_ownership.ts.
		const code = SRC
			.replace(/\/\*[\s\S]*?\*\//g, '')
			.split('\n').filter(l => !l.trim().startsWith('//')).join('\n');
		assert.ok(
			!code.includes('deduped'),
			'task-bridge.ts still carries a private [deduped:] matcher; it must delegate to SKIP_MARKER_RE'
		);
	});

	it('skip-marker guard delegates to the ownership predicate', () => {
		// The `task-` prefix check moved into src/skip_marker_ownership.ts along
		// with the ownership gate it was missing (#3018): a prefix match is not
		// ownership, and results/ is shared by every consumer. The behavior is
		// tested directly in task-bridge-skip-marker-ownership.test.ts; here we
		// only pin that the branch still routes through that predicate.
		const after = afterBlock('has skip marker; archiving silently');
		const beforeSkip = SRC.slice(0, SRC.length - after.length);
		const lastIfBeforeSkip = beforeSkip.lastIndexOf('if (');
		const ifCondition = SRC.slice(lastIfBeforeSkip, lastIfBeforeSkip + 120);
		assert.ok(
			ifCondition.includes('mayRetireSkipMarked('),
			`skip-marker if-condition must delegate to mayRetireSkipMarked(); got: ${ifCondition.slice(0, 80)}`
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
