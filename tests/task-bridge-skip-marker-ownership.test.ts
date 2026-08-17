import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { hasNetworkConsumer, isSkipMarked, mayRetireSkipMarked }
	from '../src/skip_marker_ownership.js';

// results/ is shared by every consumer. Matching `task-*` plus a skip marker
// retired results this bridge never dispatched: the gateway's bookkeeping
// [no-send] was archived here, its tid left the owning bridge's in-flight
// ledger, and a substantive reply written to that path minutes later was
// never read by anyone. Measured on a peer host: five replies (2-5 KB) sat
// resident while the archived copy was the tiny [no-send] note.

const OWN = 'task-0000000000000000aa';
const FOREIGN = 'task-0000000000000000bb';
const owns = (id: string) => id === OWN;
const noneOwned = () => false;

// Origin oracle: FOREIGN came over a bridge, everything else is local.
const origin = (id: string) =>
	id === FOREIGN ? { source: 'ag2space' } : { source: 'cron' };

describe('task-bridge — only retire skip-marked results this bridge owns', () => {
	it('retires its own dispatched task', () => {
		assert.equal(mayRetireSkipMarked(`${OWN}.txt`, '[no-send]\nbookkeeping', owns, origin), true);
	});

	it('leaves another consumer\'s result alone', () => {
		assert.equal(
			mayRetireSkipMarked(`${FOREIGN}.txt`, '[no-send]\nbookkeeping', noneOwned, origin), false,
			'archiving a result this bridge never dispatched strands the owner\'s reply'
		);
	});

	it('honours [REPLIED] under the same ownership rule', () => {
		assert.equal(mayRetireSkipMarked(`${OWN}.txt`, '[REPLIED]', owns, origin), true);
		assert.equal(mayRetireSkipMarked(`${FOREIGN}.txt`, '[REPLIED]', noneOwned, origin), false);
	});

	it('ignores unmarked results even when owned', () => {
		assert.equal(mayRetireSkipMarked(`${OWN}.txt`, 'the actual reply', owns, origin), false);
	});

	it('ignores non-task files', () => {
		assert.equal(mayRetireSkipMarked('proactive-1.txt', '[no-send]', () => true, origin), false);
	});

	it('matches the marker case-insensitively and after leading space', () => {
		assert.equal(mayRetireSkipMarked(`${OWN}.txt`, '  [NO-SEND] x', owns, origin), true);
	});

	it('suppression is UNIVERSAL — a foreign marked result is still marked', () => {
		// The regression the ownership gate alone would introduce: a foreign
		// [no-send] that falls through gets NARRATED aloud, which is worse
		// than the silent mis-archive it replaced.
		assert.equal(isSkipMarked(`${FOREIGN}.txt`, '[no-send]\nbookkeeping'), true);
		assert.equal(isSkipMarked(`${OWN}.txt`, '[REPLIED]'), true);
	});

	it('suppression and retirement disagree exactly on foreign items', () => {
		const body = '[no-send]\nx';
		assert.equal(isSkipMarked(`${FOREIGN}.txt`, body), true, 'must be suppressed');
		assert.equal(mayRetireSkipMarked(`${FOREIGN}.txt`, body, noneOwned, origin), false,
			'must not be retired');
	});

	it('unmarked results are neither suppressed nor retired', () => {
		assert.equal(isSkipMarked(`${OWN}.txt`, 'the actual reply'), false);
		assert.equal(mayRetireSkipMarked(`${OWN}.txt`, 'the actual reply', owns, origin), false);
	});

	it('durable ownership: a task no longer in the in-memory map is still ours', () => {
		// _pendingTasks is in-memory, so a restart or the timeout sweep drops
		// the entry; ownership must not evaporate with it.
		const durable = (id: string) => id === OWN || id.startsWith('task-voice-');
		assert.equal(mayRetireSkipMarked('task-voice-1.txt', '[no-send]', durable,
			() => ({ source: 'voice' })), true);
	});
});

describe('retirement is scoped by the CLOSED foreign set, not a local allowlist', () => {
	// Two hosts measured the same day disagreed completely on which local
	// families exist: one had task-wire-/task-newsradar-/task-activity-upload-,
	// the other task-taskify- (94 results). No overlap. An allowlist of local
	// families is therefore unmaintainable by construction; the set WITH a
	// consumer is closed and small.
	const LOCAL_FAMILIES_SEEN_IN_THE_FIELD = [
		['task-cron-nightly-1', 'cron'],
		['task-chat-9', 'chat'],
		['task-workstream-grouping-1', 'cron'],
		['task-taskify-abc', 'events-promotion'],   // 94 on this host, never listed
		['task-wire-1', 'wire'],                    // 49 on the peer host
		['task-newsradar-1', 'news-radar'],
		['task-activity-upload-1', 'activity-upload'],
		['task-health-1786958480', 'health-check'],
		['task-summary-1', 'summary'],
		['task-approve-1', 'approve'],
		['task-gh-1', 'github'],
	];

	it('retires every local family without naming any of them', () => {
		for (const [id, source] of LOCAL_FAMILIES_SEEN_IN_THE_FIELD) {
			assert.equal(
				mayRetireSkipMarked(`${id}.txt`, '[no-send]', noneOwned, () => ({ source })),
				true, `${id} (source=${source}) would have no archiver in any bridge`);
		}
	});

	it('never retires a task whose own bridge archives it', () => {
		for (const source of ['discord', 'ag2space', 'telegram', 'slack', 'whatsapp']) {
			assert.equal(
				mayRetireSkipMarked(`${FOREIGN}.txt`, '[no-send]', noneOwned, () => ({ source })),
				false, `${source} bridge owns its own results`);
		}
	});

	it('an unreadable origin fails toward KEEPING, not retiring', () => {
		// Wrongly retiring strands an owner reply; wrongly keeping accumulates
		// a file. Suppression is universal either way, so the unknown case
		// must not archive.
		assert.equal(hasNetworkConsumer(null), true, 'gone task file reads as foreign');
		assert.equal(mayRetireSkipMarked(`${FOREIGN}.txt`, '[no-send]', noneOwned, () => null),
			false);
		// ...unless this bridge dispatched it, which is positive evidence.
		assert.equal(mayRetireSkipMarked(`${OWN}.txt`, '[no-send]', owns, () => null), true);
	});

	it('a missing source: line is local, not unknown', () => {
		// conversation-server omits source: DELIBERATELY so discord-bridge
		// will not treat the result as a DM candidate — by design nothing
		// else can archive it.
		assert.equal(hasNetworkConsumer({ source: null }), false);
		assert.equal(mayRetireSkipMarked('task-summary-1.txt', '[no-send]', noneOwned,
			() => ({ source: null })), true);
	});

	it('source matching is case- and whitespace-insensitive', () => {
		assert.equal(hasNetworkConsumer({ source: '  Discord ' }), true);
	});
});
