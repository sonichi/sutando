import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Cross-consumer window: with a voice client connected, the result watcher must
// leave a `.to-<bridge>` proactive file for that bridge's drain — never speak it,
// never archive it — while an untagged proactive file is still spoken and archived.
const TMP = mkdtempSync(join(tmpdir(), 'sutando-destined-proactive-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
const RESULT_DIR = join(TMP, 'results');
mkdirSync(RESULT_DIR, { recursive: true });
mkdirSync(join(TMP, 'tasks'), { recursive: true });

const { startResultWatcher } = await import('../src/task-bridge.js');

const until = async (cond: () => boolean, ms: number) => {
	const end = Date.now() + ms;
	while (Date.now() < end) {
		if (cond()) return true;
		await new Promise(r => setTimeout(r, 100));
	}
	return cond();
};

describe('result watcher vs a bridge-destined proactive file', () => {
	it('speaks and archives the untagged control, leaves the .to-ag2space ask in place, unspoken', async () => {
		const destined = 'proactive-ask-1800000000000-7-abcdef.to-ag2space.txt';
		const control = 'proactive-ask-1800000000001-7-abcdef.txt';
		writeFileSync(join(RESULT_DIR, destined), '[channel: !room:ag2.space]\nQuestion for you: ship it?\n');
		writeFileSync(join(RESULT_DIR, control), '[dm-only]\nControl: untagged proactive.\n');
		const spoken: string[] = [];
		startResultWatcher((result: string) => { spoken.push(result); }, () => true);
		assert.ok(await until(() => spoken.some(s => s.includes('Control: untagged')), 8000), `control never spoken: ${JSON.stringify(spoken)}`);
		// The voice path archives what it consumed after 10 s; wait past that.
		assert.ok(await until(() => !existsSync(join(RESULT_DIR, control)), 15000), `control not archived: ${readdirSync(RESULT_DIR).join(', ')}`);
		assert.ok(existsSync(join(RESULT_DIR, destined)), 'the destined ask is still there for its bridge to claim');
		assert.ok(!spoken.some(s => s.includes('ship it?')), `voice spoke the destined ask: ${JSON.stringify(spoken)}`);
	});
});
