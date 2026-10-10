import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// A bare `[thread]` line is a task-result control marker (src/result_markers.py
// _THREAD_ASK_RE): the gateway turns it into a wire field. Voice must never speak
// or log it, and must leave `[thread]` that is part of a prose line alone.
const TMP = mkdtempSync(join(tmpdir(), 'sutando-thread-ask-strip-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
const RESULT_DIR = join(TMP, 'results');
const TASK_DIR = join(TMP, 'tasks');
mkdirSync(RESULT_DIR, { recursive: true });
mkdirSync(TASK_DIR, { recursive: true });

const markers = await import('../src/skip_marker_ownership.js');
const strip = (markers as Record<string, unknown>).stripVoiceControlLines as ((s: string) => string) | undefined;
const { startResultWatcher, _pendingTasksForTest } = await import('../src/task-bridge.js');

const until = async (cond: () => boolean, ms: number) => {
	const end = Date.now() + ms;
	while (Date.now() < end) {
		if (cond()) return true;
		await new Promise(r => setTimeout(r, 100));
	}
	return cond();
};

describe('stripVoiceControlLines — the shared TS strip for voice/log callbacks', () => {
	it('is exported by the TS marker module', () => {
		assert.equal(typeof strip, 'function');
	});

	it('drops a standalone leading [thread], in any order with the other leading markers', () => {
		assert.equal(strip!('[thread]\nanswer body').trim(), 'answer body');
		assert.equal(strip!('[THREAD]  \r\nanswer').trim(), 'answer');
		assert.equal(strip!('[channel: !r:s]\n[thread]\nmoved').trim(), '[channel: !r:s]\nmoved');
		assert.equal(strip!('[dm-only]\n[thread]\nprivate').trim(), 'private');
		assert.equal(strip!('**[core: 2]**\n[thread]\nanswer'), '**[core: 2]**\nanswer');
	});

	it('keeps prose that merely starts with or mentions [thread]', () => {
		for (const prose of ['[thread]ing is a library primitive', '[thread] prose on one line',
			'use [thread] inline', 'answer\n[thread]\nlater line is prose']) {
			assert.equal(strip!(prose), prose);
		}
	});

	it('a [thread] glued after another marker on its line is prose, as in parse_markers', () => {
		assert.equal(strip!('[dm-only] [thread]\nbody').trim(), '[thread]\nbody');
		assert.equal(strip!('[channel: !r:s] [thread]\nbody'), '[channel: !r:s] [thread]\nbody');
	});

	it('keeps the rooted [thread: $root] a forwarded voice result still needs', () => {
		assert.equal(strip!('[thread: $root]\nupdate'), '[thread: $root]\nupdate');
	});
});

describe('result watcher: [thread] is stripped for speech, never for the skip decision', () => {
	it('narrates without the marker, and a skip line after [thread] is not a skip', async () => {
		writeFileSync(join(TASK_DIR, 'task-foreign-thread-probe.txt'),
			'id: task-foreign-thread-probe\nsource: ag2space\nchannel_id: !room:ag2.space\ntask: hi\n');
		writeFileSync(join(RESULT_DIR, 'task-foreign-thread-probe.txt'), '[thread]\nanswer body\n');
		writeFileSync(join(RESULT_DIR, 'proactive-thread-probe-1800000000000.txt'), '[thread]\nnudge body\n');
		// parse_markers: a skip is terminal only as the first line, so these are answers, not skips.
		const forms = { noSend: '[no-send]', replied: '[REPLIED]', deduped: '[deduped: task-other]' };
		for (const [k, marker] of Object.entries(forms)) {
			const id = `task-thread-then-${k}`;
			_pendingTasksForTest.set(id, { submittedAt: Date.now(), timeoutMs: 0, dmOnTimeout: false, taskText: k });
			writeFileSync(join(RESULT_DIR, `${id}.txt`), `[thread]\n${marker}\nvisible ${k}\n`);
		}
		// Control: a skip first stays a skip.
		_pendingTasksForTest.set('task-skip-then-thread', { submittedAt: Date.now(), timeoutMs: 0, dmOnTimeout: false, taskText: 'c' });
		writeFileSync(join(RESULT_DIR, 'task-skip-then-thread.txt'), '[no-send]\n[thread]\nhidden control\n');
		const spoken: string[] = [];
		startResultWatcher((result: string) => { spoken.push(result); }, () => true);
		const ok = await until(() => ['answer body', 'nudge body', 'visible noSend', 'visible replied', 'visible deduped']
			.every(t => spoken.some(s => s.includes(t))), 8000);
		assert.ok(ok, `not all spoken: ${JSON.stringify(spoken)}`);
		assert.ok(!spoken.some(s => /^\s*\[thread\]/.test(s)), `voice spoke the marker: ${JSON.stringify(spoken)}`);
		assert.ok(spoken.includes('answer body'), JSON.stringify(spoken));
		assert.ok(!spoken.some(s => s.includes('hidden control')), `a real skip was spoken: ${JSON.stringify(spoken)}`);
	});
});
