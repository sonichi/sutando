import { describe, it, after, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';

// Regression for the archive-path drift bug flagged by VasiliyRad 2026-05-06:
// `archiveFile()` writes to `tasks/archive/YYYY-MM/<taskId>.txt`, but
// `_isVoiceTask()` only checked the legacy flat path `tasks/archive/<taskId>.txt`.
// That meant the offline-forwarding gate in the result watcher misclassified
// every archived voice task as non-voice, suppressing DM-fallback delivery.
//
// These tests lock in: live + processed + legacy-flat-archive + month-partitioned-
// archive lookup all surface a voice task; non-voice content stays false; missing
// task files stay false.

// Fixtures use a tmp workspace, never the live queue (#3035): SUTANDO_TEST_MODE=1
// must be set before the source-ordered `await import` binds the bridge's paths.
const TMP = mkdtempSync(join(tmpdir(), 'sutando-isvoice-archive-test-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
const TASK_DIR = join(TMP, 'tasks');
const ARCHIVE_DIR = join(TASK_DIR, 'archive');
mkdirSync(TASK_DIR, { recursive: true });

const { _isVoiceTask, voiceTaskOrigin, setVoiceTaskOriginResolver } = await import('../src/task-bridge.js');

after(() => {
	try { rmSync(TMP, { recursive: true, force: true }); } catch {}
});

// Field order: `task:` LAST, per the writer convention enforced in PR #1023.
// `_isVoiceTask` stops scanning at the first `task:` line (closes the body-
// forging vector), so any header that lands AFTER `task:` is invisible to it.
// Pre-#1023 these fixtures had `task:` first — that worked by accident with
// the old `.some()` over all lines; post-#1023 it silently breaks the test.
const VOICE_BODY = `id: task-isvoice-test-aaa
timestamp: 2026-05-06T00:00:00Z
source: voice
channel_id: local-voice
task: hello world
`;
const NON_VOICE_BODY = `id: task-isvoice-test-bbb
timestamp: 2026-05-06T00:00:00Z
source: discord
channel_id: 1490906927675474030
task: hello world
`;
// interaction-model 4D step 1.5 (scope A): voice tasks now carry a
// `media_form: live_stream` header BEFORE `task:`. The stamp is additive —
// _isVoiceTask keys on source/channel_id, so the verdict must stay true.
const VOICE_BODY_LIVESTREAM = `id: task-isvoice-test-ls-aaa
timestamp: 2026-07-07T00:00:00Z
source: voice
interaction_type: realtime_audio
media_form: live_stream
channel_id: local-voice
task: hello world
`;

// Room-bound voice (2026-09): a session docked in a room addresses the task to
// that room, so `channel_id` carries the room id and no longer says
// `local-voice`. The verdict keys on `source: voice`; the literal stays only
// for files archived before rooms. `media_form: live_stream` is NOT a voice
// key: the phone skill stamps it on `source: phone` tasks.
const VOICE_BODY_ROOM = `id: task-isvoice-test-room-aaa
timestamp: 2026-09-18T00:00:00Z
source: voice
interaction_type: realtime_audio
media_form: live_stream
channel_id: !abc123:ag2.space
channel_kind: room
source_room_id: !abc123:ag2.space
user_id: voice-local
access_tier: owner
priority: urgent
task: hello room
`;
// Legacy archived shape: only the channel_id literal identifies voice.
const VOICE_BODY_LEGACY_LITERAL = `id: task-isvoice-test-legacy-aaa
timestamp: 2026-05-06T00:00:00Z
channel_id: local-voice
task: hello world
`;
// A non-voice room task carrying source_room_id must not read as a voice room.
const GATEWAY_ROOM_BODY = `id: task-isvoice-test-gw-aaa
timestamp: 2026-09-18T00:00:00Z
source: ag2space
channel_id: !abc123:ag2.space
source_room_id: !abc123:ag2.space
task: hello world
`;
// A room-bound body whose forged room id is not a Matrix room id.
const VOICE_BODY_BAD_ROOM = `id: task-isvoice-test-badroom-aaa
timestamp: 2026-09-18T00:00:00Z
source: voice
media_form: live_stream
channel_id: not-a-room
source_room_id: ../../etc
task: hello world
`;

const created: string[] = [];
function writeTask(path: string, body: string) {
	mkdirSync(dirname(path), { recursive: true });
	writeFileSync(path, body);
	created.push(path);
}

describe('_isVoiceTask — archive-path coverage', () => {
	afterEach(() => {
		for (const p of created.splice(0)) {
			try { rmSync(p, { force: true }); } catch {}
		}
	});

	it('resolves against the redirected tmp workspace, not the live queue (#3035)', () => {
		// If the resolver ever drops the test-mode hatch, fail HERE instead of
		// silently leaking owner-tier fixtures back into a live core's watcher.
		const id = 'task-isvoice-test-redirect-aaa';
		writeTask(join(TASK_DIR, `${id}.txt`), VOICE_BODY);
		assert.equal(_isVoiceTask(id), true,
			'a fixture in the tmp workspace must be visible to _isVoiceTask');
		assert.ok(TASK_DIR.startsWith(tmpdir()),
			'fixture dir must live under the OS tmpdir');
	});

	it('returns true for a voice task in the live tasks/ dir', () => {
		const id = 'task-isvoice-test-live-aaa';
		writeTask(join(TASK_DIR, `${id}.txt`), VOICE_BODY);
		assert.equal(_isVoiceTask(id), true);
	});

	it('returns true for a scope-A voice task carrying media_form: live_stream (stamp is additive)', () => {
		const id = 'task-isvoice-test-ls-aaa';
		writeTask(join(TASK_DIR, `${id}.txt`), VOICE_BODY_LIVESTREAM);
		assert.equal(_isVoiceTask(id), true);
	});

	it('returns true for a room-bound voice task (channel_id is the room, not local-voice)', () => {
		const id = 'task-isvoice-test-room-aaa';
		writeTask(join(TASK_DIR, `${id}.txt`), VOICE_BODY_ROOM);
		assert.equal(_isVoiceTask(id), true);
	});

	it('returns false for a phone task: media_form: live_stream alone is not voice', () => {
		const id = 'task-isvoice-test-phone-aaa';
		writeTask(join(TASK_DIR, `${id}.txt`), `id: ${id}\ntimestamp: 2026-09-18T00:00:00Z\nsource: phone\ninteraction_type: realtime_audio\nmedia_form: live_stream\ncallSid: CA1\naccess_tier: owner\ntask: hello\n`);
		assert.equal(_isVoiceTask(id), false);
	});

	it('keeps the legacy channel_id: local-voice literal for archived files', () => {
		const id = 'task-isvoice-test-legacy-aaa';
		writeTask(join(ARCHIVE_DIR, '2026-05', `${id}.txt`), VOICE_BODY_LEGACY_LITERAL);
		assert.equal(_isVoiceTask(id), true);
	});

	it('voiceTaskOrigin: the adapter\'s resolver rebuilds an origin from a voice task\'s header, and only a voice task\'s', () => {
		const seen: string[] = [];
		setVoiceTaskOriginResolver((header) => {
			const line = header.find(l => l.startsWith('source_room_id:')) ?? '';
			const target = line.slice('source_room_id:'.length).trim();
			seen.push(target);
			return target ? { channel: 'fakechan', target } : null;
		});
		try {
			writeTask(join(TASK_DIR, 'task-isvoice-test-room-aaa.txt'), VOICE_BODY_ROOM);
			assert.deepEqual(voiceTaskOrigin('task-isvoice-test-room-aaa'), { channel: 'fakechan', target: '!abc123:ag2.space' });
			writeTask(join(TASK_DIR, 'task-isvoice-test-aaa.txt'), VOICE_BODY);
			assert.equal(voiceTaskOrigin('task-isvoice-test-aaa'), null, 'a DM voice task has no origin');
			const before = seen.length;
			writeTask(join(TASK_DIR, 'task-isvoice-test-gw-aaa.txt'), GATEWAY_ROOM_BODY);
			assert.equal(voiceTaskOrigin('task-isvoice-test-gw-aaa'), null, 'a non-voice task never reaches the resolver');
			assert.equal(seen.length, before);
			writeTask(join(TASK_DIR, 'task-isvoice-test-badroom-aaa.txt'), VOICE_BODY_BAD_ROOM);
			assert.deepEqual(voiceTaskOrigin('task-isvoice-test-badroom-aaa'), { channel: 'fakechan', target: '../../etc' }, 'target grammar is the adapter\'s to refuse; the core only keeps it one bracket-free token');
			assert.equal(voiceTaskOrigin('task-isvoice-test-no-such-file'), null);
			// Archived copies resolve too — the result can land after the task moved.
			writeTask(join(ARCHIVE_DIR, '2026-09', 'task-isvoice-test-room-arch.txt'), VOICE_BODY_ROOM.replace('room-aaa', 'room-arch'));
			assert.equal(voiceTaskOrigin('task-isvoice-test-room-arch')?.target, '!abc123:ag2.space');
			setVoiceTaskOriginResolver(() => ({ channel: 'Bad Channel', target: 'x' }));
			assert.equal(voiceTaskOrigin('task-isvoice-test-room-aaa'), null, 'an unusable origin is no origin');
			setVoiceTaskOriginResolver(() => { throw new Error('boom'); });
			assert.equal(voiceTaskOrigin('task-isvoice-test-room-aaa'), null, 'a throwing resolver is no origin');
		} finally {
			setVoiceTaskOriginResolver(null);
		}
		assert.equal(voiceTaskOrigin('task-isvoice-test-room-aaa'), null, 'with no resolver a header alone binds nothing');
	});

	it('returns true for a voice task in tasks/processed/', () => {
		const id = 'task-isvoice-test-proc-aaa';
		writeTask(join(TASK_DIR, 'processed', `${id}.txt`), VOICE_BODY);
		assert.equal(_isVoiceTask(id), true);
	});

	it('returns true for a voice task in the legacy flat archive', () => {
		const id = 'task-isvoice-test-flat-aaa';
		writeTask(join(ARCHIVE_DIR, `${id}.txt`), VOICE_BODY);
		assert.equal(_isVoiceTask(id), true);
	});

	it('returns true for a voice task in month-partitioned archive (the drift bug)', () => {
		const id = 'task-isvoice-test-month-aaa';
		writeTask(join(ARCHIVE_DIR, '2026-01', `${id}.txt`), VOICE_BODY);
		assert.equal(_isVoiceTask(id), true);
	});

	it('returns false for a non-voice task in month-partitioned archive', () => {
		const id = 'task-isvoice-test-nonvoice-aaa';
		writeTask(join(ARCHIVE_DIR, '2026-05', `${id}.txt`), NON_VOICE_BODY);
		assert.equal(_isVoiceTask(id), false);
	});

	it('ignores non-month-shaped subdirs (e.g. "done", "tmp")', () => {
		// Negative-control: a voice task under a non-month-shaped subdir is
		// NOT found, confirming the YYYY-MM regex gate.
		const id = 'task-isvoice-test-stray-subdir-aaa';
		writeTask(join(ARCHIVE_DIR, 'done', `${id}.txt`), VOICE_BODY);
		assert.equal(_isVoiceTask(id), false);
	});

	it('returns false when the task file is missing entirely', () => {
		assert.equal(_isVoiceTask('task-isvoice-test-no-such-file'), false);
	});

	it('leaves no fixture behind in the tmp workspace after cleanup', () => {
		// afterEach already removed this describe-block's fixtures; the tmp
		// tree should hold only the (possibly empty) dirs the tests created.
		const leftover: string[] = [];
		const walk = (d: string) => {
			if (!existsSync(d)) return;
			for (const e of readdirSync(d, { withFileTypes: true })) {
				const p = join(d, e.name);
				if (e.isDirectory()) walk(p);
				else leftover.push(p);
			}
		};
		walk(TASK_DIR);
		assert.deepEqual(leftover, []);
	});
});
