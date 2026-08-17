import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { isLocalOnlyTask, isSkipMarked, mayRetireSkipMarked } from '../src/skip_marker_ownership.js';

// results/ is shared by every consumer. Matching `task-*` plus a skip marker
// retired results this bridge never dispatched: the gateway's bookkeeping
// [no-send] was archived here, its tid left the owning bridge's in-flight
// ledger, and a substantive reply written to that path minutes later was
// never read by anyone. Measured on a peer host: five replies (2-5 KB) sat
// resident while the archived copy was the tiny [no-send] note.

const OWN = 'task-0000000000000000aa';
const OTHERS = 'task-0000000000000000bb';
const owns = (id: string) => id === OWN;

describe('task-bridge — only retire skip-marked results this bridge owns', () => {
	it('retires its own dispatched task', () => {
		assert.equal(mayRetireSkipMarked(`${OWN}.txt`, '[no-send]\nbookkeeping', owns), true);
	});

	it('leaves another consumer\'s result alone', () => {
		assert.equal(
			mayRetireSkipMarked(`${OTHERS}.txt`, '[no-send]\nbookkeeping', owns), false,
			'archiving a result this bridge never dispatched strands the owner\'s reply'
		);
	});

	it('honours [REPLIED] under the same ownership rule', () => {
		assert.equal(mayRetireSkipMarked(`${OWN}.txt`, '[REPLIED]', owns), true);
		assert.equal(mayRetireSkipMarked(`${OTHERS}.txt`, '[REPLIED]', owns), false);
	});

	it('ignores unmarked results even when owned', () => {
		assert.equal(mayRetireSkipMarked(`${OWN}.txt`, 'the actual reply', owns), false);
	});

	it('ignores non-task files', () => {
		assert.equal(mayRetireSkipMarked('proactive-1.txt', '[no-send]', () => true), false);
	});

	it('matches the marker case-insensitively and after leading space', () => {
		assert.equal(mayRetireSkipMarked(`${OWN}.txt`, '  [NO-SEND] x', owns), true);
	});

	it('suppression is UNIVERSAL — a foreign marked result is still marked', () => {
		// The regression the ownership gate alone would introduce: a foreign
		// [no-send] that falls through gets NARRATED aloud, which is worse
		// than the silent mis-archive it replaced.
		assert.equal(isSkipMarked(`${OTHERS}.txt`, '[no-send]\nbookkeeping'), true);
		assert.equal(isSkipMarked(`${OWN}.txt`, '[REPLIED]'), true);
	});

	it('suppression and retirement disagree exactly on foreign items', () => {
		const foreign = `${OTHERS}.txt`, body = '[no-send]\nx';
		assert.equal(isSkipMarked(foreign, body), true, 'must be suppressed');
		assert.equal(mayRetireSkipMarked(foreign, body, owns), false, 'must not be retired');
	});

	it('unmarked results are neither suppressed nor retired', () => {
		assert.equal(isSkipMarked(`${OWN}.txt`, 'the actual reply'), false);
		assert.equal(mayRetireSkipMarked(`${OWN}.txt`, 'the actual reply', owns), false);
	});

	it('durable ownership: a task no longer in the in-memory map is still ours', () => {
		// _pendingTasks is in-memory, so a restart or the timeout sweep drops
		// the entry; ownership must not evaporate with it.
		const durable = (id: string) => id === OWN || id.startsWith('task-chat-');
		assert.equal(mayRetireSkipMarked('task-chat-123.txt', '[no-send]', durable), true);
	});

	it('local-only families keep an archiver — cron depends on it explicitly', () => {
		// codex-scheduler writes [no-send] "so this scheduled task is
		// archived"; cron never enters _pendingTasks and discord-bridge's
		// skip handling is scoped to its own pending map.
		const notDispatched = () => false;
		for (const id of ['task-cron-nightly-123', 'task-chat-9',
			'task-workstream-grouping-1', 'task-project-grouping-1',
			'task-health-1786958480', 'task-smoke-1', 'task-discord-e2e-1']) {
			assert.equal(isLocalOnlyTask(id), true, id);
			assert.equal(mayRetireSkipMarked(`${id}.txt`, '[no-send]', notDispatched),
				true, `${id} would have no archiver in any bridge`);
		}
	});

	it('a network consumer\'s task is NOT local-only', () => {
		// The whole point: gateway/discord/telegram ids stay foreign.
		assert.equal(isLocalOnlyTask('task-0000000000000000bb'), false);
		assert.equal(mayRetireSkipMarked(`${OTHERS}.txt`, '[no-send]', () => false), false);
	});

	it('covers every machine family web-client.ts already enumerates', () => {
		// isOwnerVisibleTask lists the durable-scheduler/health families; each
		// has no network consumer, so each needs an archiver here too.
		for (const id of ['task-cron-x', 'task-health-x', 'task-smoke-x',
			'task-discord-e2e-x']) {
			assert.equal(isLocalOnlyTask(id), true, `${id} has no archiver`);
		}
	});
});
