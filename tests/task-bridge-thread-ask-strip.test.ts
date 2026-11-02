import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
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

describe('result watcher: [thread] is stripped for speech; a skip right after it is a skip', () => {
	it('narrates without the marker, and archives [thread] + skip silently, as parse_markers and the broker do', async () => {
		writeFileSync(join(TASK_DIR, 'task-foreign-thread-probe.txt'),
			'id: task-foreign-thread-probe\nsource: ag2space\nchannel_id: !room:ag2.space\ntask: hi\n');
		writeFileSync(join(RESULT_DIR, 'task-foreign-thread-probe.txt'), '[thread]\nanswer body\n');
		writeFileSync(join(RESULT_DIR, 'proactive-thread-probe-1800000000000.txt'), '[thread]\nnudge body\n');
		// parse_markers: a skip right after the leading markers is a skip.
		const forms = { noSend: '[no-send]', replied: '[REPLIED]', deduped: '[deduped: task-other]' };
		for (const [k, marker] of Object.entries(forms)) {
			const id = `task-thread-then-${k}`;
			_pendingTasksForTest.set(id, { submittedAt: Date.now(), timeoutMs: 0, dmOnTimeout: false, taskText: k });
			writeFileSync(join(RESULT_DIR, `${id}.txt`), `[thread]\n${marker}\nvisible ${k}\n`);
		}
		_pendingTasksForTest.set('task-skip-then-thread', { submittedAt: Date.now(), timeoutMs: 0, dmOnTimeout: false, taskText: 'c' });
		writeFileSync(join(RESULT_DIR, 'task-skip-then-thread.txt'), '[no-send]\n[thread]\nhidden control\n');
		_pendingTasksForTest.set('task-dmonly-then-skip', { submittedAt: Date.now(), timeoutMs: 0, dmOnTimeout: false, taskText: 'd' });
		writeFileSync(join(RESULT_DIR, 'task-dmonly-then-skip.txt'), '[dm-only]\n[no-send]\nhidden dm\n');
		// The delivery verdict: an owner's skip directly after [channel:] is a skip, as in the gateway.
		_pendingTasksForTest.set('task-channel-then-skip', { submittedAt: Date.now(), timeoutMs: 0, dmOnTimeout: false, taskText: 'e' });
		writeFileSync(join(RESULT_DIR, 'task-channel-then-skip.txt'), '[channel: !r:s]\n[no-send]\nhidden redirect\n');
		const spoken: string[] = [];
		startResultWatcher((result: string) => { spoken.push(result); }, () => true);
		const skipped = ['noSend', 'replied', 'deduped'].map(k => `task-thread-then-${k}.txt`)
			.concat(['task-skip-then-thread.txt', 'task-dmonly-then-skip.txt', 'task-channel-then-skip.txt']);
		const ok = await until(() => spoken.some(s => s.includes('answer body')) && spoken.some(s => s.includes('nudge body'))
			&& skipped.every(f => !existsSync(join(RESULT_DIR, f))), 12000);
		assert.ok(ok, `spoken=${JSON.stringify(spoken)} left=${skipped.filter(f => existsSync(join(RESULT_DIR, f)))}`);
		assert.ok(!spoken.some(s => /visible|hidden/.test(s)), `a skip was spoken: ${JSON.stringify(spoken)}`);
		assert.ok(!spoken.some(s => /^\s*\[thread\]/.test(s)), `voice spoke the marker: ${JSON.stringify(spoken)}`);
		assert.ok(spoken.includes('answer body'), JSON.stringify(spoken));
	});
});

describe('bodyIsSkipMarked agrees with the delivery verdict of parse_markers on the leading block', () => {
	it('same skip verdict as src/result_markers.py (skip_after_channel=True) for every corpus body', async () => {
		const { execFileSync } = await import('node:child_process');
		const corpus = [];
		for (const lead of ['', '[thread]\n', '[thread: $r]\n', '[channel: !r:s]\n', '[dm-only]\n', '[reply: 12345678901234567]\n',
			'[channel: !r:s] ', '[dm-only] ', '[channel: !r:s] [thread]\n', '[dm-only] [thread]\n', '**[core: 2]**\n[thread]\n']) {
			for (const tail of ['[no-send]\nx', '[REPLIED]\nx', '[deduped: task-9]\nx', 'plain\n[no-send]', 'see [no-send] here']) {
				corpus.push(lead + tail);
			}
		}
		let py: boolean[];
		try {
			const out = execFileSync('python3', ['-c',
				'import json,sys; sys.path.insert(0,"src"); from result_markers import parse_markers as p; '
				+ 'print(json.dumps([any(a.kind=="skip" for a in p(t, skip_after_channel=True).actions) for t in json.load(sys.stdin)]))'],
			{ input: JSON.stringify(corpus), encoding: 'utf-8' });
			py = JSON.parse(out);
		} catch (e) {
			assert.fail(`python3 parse_markers unavailable: ${e}`);
		}
		const mismatches = corpus.filter((t, i) => markers.bodyIsSkipMarked(t) !== py[i]);
		assert.deepEqual(mismatches, [], 'TS and Python disagree');
	});
});

describe('the shared table tests/fixtures/thread-ask-cases.json (also run by result-markers-thread)', () => {
	it('stripVoiceControlLines and bodyIsSkipMarked match every row', async () => {
		const { readFileSync } = await import('node:fs');
		const table = JSON.parse(readFileSync(join(import.meta.dirname, 'fixtures', 'thread-ask-cases.json'), 'utf-8'));
		const bad: string[] = [];
		for (const c of table.cases) {
			if (markers.bodyIsSkipMarked(c.body) !== (c.delivery_skip ?? c.skip)) bad.push(`skip ${JSON.stringify(c.body)}`);
			if (c.voice !== null && strip!(c.body).trim() !== c.voice) bad.push(`voice ${JSON.stringify(c.body)} -> ${JSON.stringify(strip!(c.body).trim())}`);
		}
		assert.deepEqual(bad, []);
	});
});
