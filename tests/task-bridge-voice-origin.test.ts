import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// The voice session origin, driven by a made-up adapter: the bridge carries an opaque
// channel + target from the session to the task header and on to the result file.

const TMP = mkdtempSync(join(tmpdir(), 'sutando-voice-origin-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
const RESULT_DIR = join(TMP, 'results');
const TASK_DIR = join(TMP, 'tasks');
mkdirSync(RESULT_DIR, { recursive: true });

const {
	setVoiceSessionOrigin, getVoiceSessionOrigin, voiceTaskOrigin, resolveVoiceResultOrigin, forwardVoiceResultToOrigin, forwardVoiceResultToOwnerDm,
	keepVoiceResultToDm, forwardOfflineVoiceResult, startResultWatcher, workTool, DM_ONLY_DELIVERY_NOTE, LEADING_REDIRECT_RE, DM_ONLY_RE,
	_isDeliveredResult, _shouldFallthrough, _shouldRegisterTaskRow, _resultFileOps, _deliverOriginBoundResult,
} = await import('../src/task-bridge.js');

after(() => {
	setVoiceSessionOrigin(null);
	try { rmSync(TMP, { recursive: true, force: true }); } catch {}
});

const origin = (target: string, extra: Record<string, unknown> = {}) => ({ channel: 'fakechan', target, ...extra });
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const delegate = async (task: string) => (await (workTool.execute as any)({ task }, null)) as { taskId: string };
const until = async (cond: () => boolean, ms: number) => { const t0 = Date.now(); while (!cond() && Date.now() - t0 < ms) await new Promise(r => setTimeout(r, 100)); return cond(); };
const proactiveFor = (id: string) => readdirSync(RESULT_DIR).filter(f => f.startsWith(`proactive-result-${id}-`));

describe('setVoiceSessionOrigin — one origin per live client, opaque to the bridge', () => {
	it('binds, reads back and releases', () => {
		assert.equal(getVoiceSessionOrigin(), null);
		const o = origin('place-1', { label: 'The Place' });
		setVoiceSessionOrigin(o);
		assert.equal(getVoiceSessionOrigin(), o);
		setVoiceSessionOrigin(null);
		assert.equal(getVoiceSessionOrigin(), null);
	});

	it('an origin that could not be written as a filename tag or a [channel:] marker binds nothing', () => {
		for (const bad of [{ channel: 'Fake Chan', target: 'x' }, { channel: '', target: 'x' }, { channel: 'fakechan', target: '' }, { channel: 'fakechan', target: 'a b' },
			{ channel: 'fakechan', target: 'x]\n[channel: y' }, { channel: 'fakechan', target: 7 }, { target: 'x' }]) {
			setVoiceSessionOrigin(origin('place-1'));
			setVoiceSessionOrigin(bad as never);
			assert.equal(getVoiceSessionOrigin(), null, JSON.stringify(bad));
		}
	});

	it('a task carries the origin current at write time, and a task written with none carries none', async () => {
		setVoiceSessionOrigin(origin('place-1'));
		const t1 = await delegate('origin probe one');
		setVoiceSessionOrigin(origin('place-2'));
		const t2 = await delegate('origin probe two');
		setVoiceSessionOrigin(null);
		const t3 = await delegate('origin probe three');
		assert.equal(voiceTaskOrigin(t1.taskId)?.target, 'place-1');
		assert.equal(voiceTaskOrigin(t2.taskId)?.target, 'place-2');
		assert.equal(voiceTaskOrigin(t3.taskId), null);
		assert.match(readFileSync(join(TASK_DIR, `${t1.taskId}.txt`), 'utf-8'), /^channel_id: place-1$/m);
		assert.match(readFileSync(join(TASK_DIR, `${t3.taskId}.txt`), 'utf-8'), /^channel_id: local-voice$/m);
		for (const t of [t1, t2, t3]) rmSync(join(TASK_DIR, `${t.taskId}.txt`), { force: true });
	});
});

describe('forwardVoiceResultToOrigin — the result reaches its origin, and voice never speaks it twice', () => {
	it('writes proactive-result-<id>-<ts>.to-<channel>.txt with [channel: <target>] first, claimed at once', () => {
		const file = forwardVoiceResultToOrigin('task-1700000000000', 'Three listings.\nSecond line.', origin('place-1'), 1_800_000_000);
		assert.equal(file, 'proactive-result-task-1700000000000-1800000000.to-fakechan.txt');
		assert.equal(readFileSync(join(RESULT_DIR, file), 'utf-8'), '[channel: place-1]\nThree listings.\nSecond line.');
		assert.equal(_shouldFallthrough(file), true, 'the drain would otherwise speak this file on its next tick');
		assert.equal(_shouldRegisterTaskRow(file), false);
		assert.equal(_isDeliveredResult(file), true);
		const py = spawnSync('python3', ['-c', `import sys; sys.path.insert(0, "src")\nfrom proactive_routing import proactive_destination\nprint(proactive_destination(${JSON.stringify(file)}))`], { cwd: process.cwd(), encoding: 'utf-8' });
		assert.equal(py.status, 0, py.stderr);
		assert.equal(py.stdout.trim(), 'fakechan', 'the name tag is the claim grammar every bridge reads');
	});

	it('a result that opens with its own [channel:] redirect keeps it; one further down, or an empty one, does not', () => {
		const own = '\n[channel: elsewhere]\nFor the other place.';
		const file = forwardVoiceResultToOrigin('task-1700000000005', own, origin('place-1'), 1_800_000_005);
		assert.equal(readFileSync(join(RESULT_DIR, file), 'utf-8'), own);
		assert.equal(_isDeliveredResult(file), true);
		for (const [i, body] of ['Intro.\n[channel: elsewhere]\nbody', '[channel: ]\nbody', '[channel:]\nbody'].entries()) {
			const f = forwardVoiceResultToOrigin(`task-170000000001${i}`, body, origin('place-1'), 1_800_000_010 + i);
			assert.equal(readFileSync(join(RESULT_DIR, f), 'utf-8'), `[channel: place-1]\n${body}`);
		}
		assert.match('  [channel: 123]', LEADING_REDIRECT_RE);
	});

	it('the owner-DM shape: same bridge tag, no [channel:] line, [dm-only] on top, claimed at once', () => {
		const file = forwardVoiceResultToOwnerDm('task-1700000000700', 'Private findings.', 'fakechan', 1_800_000_700);
		assert.equal(file, 'proactive-result-task-1700000000700-1800000700.to-fakechan.txt');
		assert.equal(readFileSync(join(RESULT_DIR, file), 'utf-8'), '[dm-only]\nPrivate findings.');
		assert.equal(_isDeliveredResult(file), true);
		assert.match('findings\n[DM-only]', DM_ONLY_RE);
		assert.doesNotMatch('dm only, please', DM_ONLY_RE);
	});
});

describe('the result leg re-checks the origin and fails closed', () => {
	it('verify true → the origin; false or a throw → null; no verify → the origin; no origin → null', async () => {
		const cases: Array<[string, Record<string, unknown>, boolean]> = [
			['ok', { verify: async () => true }, true], ['refused', { verify: async () => false }, false],
			['throws', { verify: async () => { throw new Error('down'); } }, false], ['truthy', { verify: async () => 'yes' as never }, false], ['none', {}, true],
		];
		for (const [name, extra, expected] of cases) {
			setVoiceSessionOrigin(origin(`place-${name}`, extra));
			const t = await delegate(`verify probe ${name}`);
			assert.equal((await resolveVoiceResultOrigin(t.taskId))?.target ?? null, expected ? `place-${name}` : null, name);
			rmSync(join(TASK_DIR, `${t.taskId}.txt`), { force: true });
		}
		setVoiceSessionOrigin(null);
		assert.equal(await resolveVoiceResultOrigin('task-does-not-exist'), null);
	});

	it('offline: verified → the origin file; refused → the untagged owner-DM shape, left unclaimed; [dm-only] → the DM shape with no verify', async () => {
		let asked = 0;
		setVoiceSessionOrigin(origin('place-ok', { verify: async () => { asked++; return true; } }));
		const ok = await delegate('offline probe ok');
		setVoiceSessionOrigin(origin('place-no', { verify: async () => false }));
		const no = await delegate('offline probe refused');
		setVoiceSessionOrigin(null);
		const toOrigin = await forwardOfflineVoiceResult(ok.taskId, 'the answer', 1_800_000_101);
		assert.equal(toOrigin, `proactive-result-${ok.taskId}-1800000101.to-fakechan.txt`);
		assert.equal(readFileSync(join(RESULT_DIR, toOrigin), 'utf-8'), '[channel: place-ok]\nthe answer');
		const dm = await forwardOfflineVoiceResult(no.taskId, 'the answer', 1_800_000_100);
		assert.equal(dm, `proactive-result-${no.taskId}-1800000100.txt`);
		assert.equal(readFileSync(join(RESULT_DIR, dm), 'utf-8'), 'the answer');
		assert.equal(_isDeliveredResult(dm), false, 'left unclaimed: with no bridge to take it, the drain speaks it on reconnect');
		asked = 0;
		const kept = await forwardOfflineVoiceResult(ok.taskId, 'private', 1_800_000_102, true);
		assert.equal(readFileSync(join(RESULT_DIR, kept), 'utf-8'), '[dm-only]\nprivate');
		assert.equal(asked, 0, 'the origin is not a destination for a dm-only result');
		assert.equal(keepVoiceResultToDm(ok.taskId, 'for the place', false, 1_800_000_103), null);
		assert.equal(keepVoiceResultToDm('task-with-no-origin', 'private', true, 1_800_000_104), null);
		assert.ok(!existsSync(join(RESULT_DIR, 'proactive-result-task-with-no-origin-1800000104.to-fakechan.txt')));
		for (const t of [ok, no]) rmSync(join(TASK_DIR, `${t.taskId}.txt`), { force: true });
	});
});

/** One watcher per file (the module keeps one delivered-set): the drain tests below share it. */
const spoken: Array<{ result: string; note?: string }> = [];

describe('the drain: a plain origin-bound result is written to its origin, a [dm-only] one to the DM and voice is told', () => {
	it('three results through the drain', async () => {
		setVoiceSessionOrigin(origin('place-1', { verify: async () => true }));
		const plain = await delegate('drain probe plain');
		const priv = await delegate('drain probe private');
		setVoiceSessionOrigin(origin('place-2', { verify: async () => true, dmOnlyNote: 'It went to the DM, not the place.' }));
		const noted = await delegate('drain probe noted');
		setVoiceSessionOrigin(null);
		writeFileSync(join(RESULT_DIR, `${plain.taskId}.txt`), 'Three listings.');
		writeFileSync(join(RESULT_DIR, `${priv.taskId}.txt`), '[dm-only]\nPrivate one.');
		writeFileSync(join(RESULT_DIR, `${noted.taskId}.txt`), '[dm-only]\nPrivate two.');
		startResultWatcher((result, note) => spoken.push({ result, note }), () => true);
		assert.ok(await until(() => [plain, priv, noted].every(t => proactiveFor(t.taskId).length > 0), 8000), readdirSync(RESULT_DIR).join(', '));
		assert.equal(readFileSync(join(RESULT_DIR, proactiveFor(plain.taskId)[0]), 'utf-8'), '[channel: place-1]\nThree listings.');
		assert.equal(readFileSync(join(RESULT_DIR, proactiveFor(priv.taskId)[0]), 'utf-8'), '[dm-only]\nPrivate one.');
		for (const t of [plain, priv, noted]) assert.match(proactiveFor(t.taskId)[0], /\.to-fakechan\.txt$/);
		assert.equal(spoken.find(s => s.result === 'Three listings.')?.note, undefined);
		assert.equal(spoken.find(s => s.result === 'Private one.')?.note, DM_ONLY_DELIVERY_NOTE, 'the default note when the adapter supplies none');
		assert.equal(spoken.find(s => s.result === 'Private two.')?.note, 'It went to the DM, not the place.');
		assert.equal(spoken.filter(s => s.result === 'Private one.').length, 1, 'spoken once: the DM file is claimed before the next tick');
	});
});

describe('the drain, connected: an origin that refuses at delivery keeps the result to the DM and voice is told', () => {
	it('refused and throwing verifies: the DM shape is written, no [channel:] file exists, the note accompanies the result', async () => {
		setVoiceSessionOrigin(origin('place-refused', { verify: async () => false }));
		const refused = await delegate('drain probe refused at delivery');
		setVoiceSessionOrigin(origin('place-thrown', { verify: async () => { throw new Error('verifier down'); }, dmOnlyNote: 'Kept to the DM.' }));
		const thrown = await delegate('drain probe thrown at delivery');
		setVoiceSessionOrigin(null);
		writeFileSync(join(RESULT_DIR, `${refused.taskId}.txt`), 'For the place, refused.');
		writeFileSync(join(RESULT_DIR, `${thrown.taskId}.txt`), 'For the place, thrown.');
		assert.ok(await until(() => [refused, thrown].every(t => proactiveFor(t.taskId).length > 0), 8000), readdirSync(RESULT_DIR).join(', '));
		for (const [t, body] of [[refused, 'For the place, refused.'], [thrown, 'For the place, thrown.']] as const) {
			const files = proactiveFor(t.taskId);
			assert.equal(files.length, 1, files.join(', '));
			assert.match(files[0], /\.to-fakechan\.txt$/, 'the owner DM on the same bridge');
			assert.equal(readFileSync(join(RESULT_DIR, files[0]), 'utf-8'), `[dm-only]\n${body}`, 'no [channel:] line: nothing addresses the refused place');
			assert.equal(_isDeliveredResult(files[0]), true, 'claimed: voice has spoken it');
		}
		assert.equal(spoken.find(s => s.result === 'For the place, refused.')?.note, DM_ONLY_DELIVERY_NOTE, 'voice is told the copy went to the DM');
		assert.equal(spoken.find(s => s.result === 'For the place, thrown.')?.note, 'Kept to the DM.', 'with the adapter\'s wording when it supplies one');
		for (const t of [refused, thrown]) rmSync(join(TASK_DIR, `${t.taskId}.txt`), { force: true });
	});

	it('_deliverOriginBoundResult: verified → the origin file and no note; a failed write still speaks', async () => {
		setVoiceSessionOrigin(origin('place-ok', { verify: async () => true }));
		const ok = await delegate('deliver probe ok');
		setVoiceSessionOrigin(null);
		const spoken: Array<{ result: string; note?: string }> = [];
		const file = await _deliverOriginBoundResult(ok.taskId, 'to the place', voiceTaskOrigin(ok.taskId)!, (result, note) => spoken.push({ result, note }));
		assert.match(file ?? '', /\.to-fakechan\.txt$/);
		assert.equal(readFileSync(join(RESULT_DIR, file!), 'utf-8'), '[channel: place-ok]\nto the place');
		assert.deepEqual(spoken, [{ result: 'to the place', note: undefined }]);
		const realWrite = _resultFileOps.write;
		_resultFileOps.write = (() => { throw new Error('disk full'); }) as typeof realWrite;
		try {
			assert.equal(await _deliverOriginBoundResult(ok.taskId, 'unwritten', voiceTaskOrigin(ok.taskId)!, (result, note) => spoken.push({ result, note })), null);
		} finally {
			_resultFileOps.write = realWrite;
		}
		assert.deepEqual(spoken[1], { result: 'unwritten', note: undefined }, 'spoken, with no claim about a written copy');
		rmSync(join(TASK_DIR, `${ok.taskId}.txt`), { force: true });
	});
});

describe('the untagged offline fallback keeps a [dm-only] the body carried', () => {
	it('no origin, [channel:] + [dm-only]: the marker stays on top, so every bridge still cancels the redirect', async () => {
		const file = await forwardOfflineVoiceResult('task-1700000000600', '[channel: !room:x]\nsecret for the owner', 1_800_000_600, true);
		assert.equal(file, 'proactive-result-task-1700000000600-1800000600.txt', 'the untagged owner-DM shape');
		const body = readFileSync(join(RESULT_DIR, file), 'utf-8');
		assert.equal(body, '[dm-only]\n[channel: !room:x]\nsecret for the owner');
		const py = spawnSync('python3', ['-c', [
			'import sys; sys.path.insert(0, "src")',
			'from result_markers import parse_markers',
			`p = parse_markers(open(${JSON.stringify(join(RESULT_DIR, file))}, encoding="utf-8").read())`,
			'print([a.kind for a in p.actions], repr(p.body))',
		].join('\n')], { cwd: process.cwd(), encoding: 'utf-8' });
		assert.equal(py.status, 0, py.stderr);
		assert.equal(py.stdout.trim(), "['dm-only'] 'secret for the owner'", 'parse_markers sees no redirect: the owner DM, never !room:x');
		const plain = await forwardOfflineVoiceResult('task-1700000000601', 'not private', 1_800_000_601, false);
		assert.equal(readFileSync(join(RESULT_DIR, plain), 'utf-8'), 'not private', 'a body without the marker is written as before');
	});
});

describe('result files are published whole: staged as a dotfile, renamed into place', () => {
	const dotfiles = () => readdirSync(RESULT_DIR).filter(f => f.startsWith('.'));

	it('each writer stages under a name no drain matches and the final name appears only through the rename', async () => {
		setVoiceSessionOrigin(origin('place-atomic', { verify: async () => true }));
		const ok = await delegate('atomic probe');
		setVoiceSessionOrigin(null);
		const realWrite = _resultFileOps.write, realRename = _resultFileOps.rename;
		const writes: Array<{ path: string; finalExisted: boolean }> = [];
		const renames: Array<{ from: string; to: string; staged: string }> = [];
		_resultFileOps.write = ((path: string, body: string) => {
			const final = path.split('/').pop()!.replace(/^\./, '').replace(/\.\d+\.\d+$/, '');
			writes.push({ path, finalExisted: existsSync(join(RESULT_DIR, final)) });
			realWrite(path, body);
		}) as typeof realWrite;
		_resultFileOps.rename = ((from: string, to: string) => {
			renames.push({ from, to, staged: readFileSync(from, 'utf-8') });
			realRename(from, to);
		}) as typeof realRename;
		try {
			const cases: Array<[string, string]> = [
				[forwardVoiceResultToOrigin(ok.taskId, 'to the place', voiceTaskOrigin(ok.taskId)!, 1_800_000_700), '[channel: place-atomic]\nto the place'],
				[forwardVoiceResultToOwnerDm(ok.taskId, 'to the dm', 'fakechan', 1_800_000_701), '[dm-only]\nto the dm'],
				[await forwardOfflineVoiceResult('task-1700000000702', 'to the owner', 1_800_000_702), 'to the owner'],
			];
			assert.equal(writes.length, 3);
			assert.equal(renames.length, 3);
			cases.forEach(([file, expected], i) => {
				const final = join(RESULT_DIR, file);
				assert.equal(writes[i].path.startsWith(join(RESULT_DIR, `.${file}.`)), true, `staged beside it as a dotfile: ${writes[i].path}`);
				assert.doesNotMatch(writes[i].path, /\.txt$/, 'no drain glob or suffix filter matches the staged name');
				assert.equal(writes[i].finalExisted, false, 'the final name did not exist while the body was being written');
				assert.deepEqual(renames[i], { from: writes[i].path, to: final, staged: expected }, 'the rename publishes the complete body');
				assert.equal(readFileSync(final, 'utf-8'), expected, 'the published content is unchanged');
			});
			assert.deepEqual(dotfiles(), [], 'no staged file is left behind');
		} finally {
			_resultFileOps.write = realWrite;
			_resultFileOps.rename = realRename;
		}
		rmSync(join(TASK_DIR, `${ok.taskId}.txt`), { force: true });
	});
});

describe('the core names no product', () => {
	it('no adapter grammar, tag or wording in the bridge, the context builder or the prompt factory', () => {
		for (const f of ['src/task-bridge.ts', 'src/voice-context.ts', 'src/voice-agent-config.ts', 'src/inline-tools.ts']) {
			const src = readFileSync(join(process.cwd(), f), 'utf-8');
			assert.doesNotMatch(src, /\.to-ag2space|MATRIX_ROOM|docked in room|session\.context|voice-room-checks|channel_kind: room/, f);
		}
	});
});
