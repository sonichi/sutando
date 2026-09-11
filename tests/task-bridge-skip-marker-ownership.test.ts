import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { bodyIsSkipMarked, hasNetworkConsumer, isSkipMarked, mayRetireSkipMarked }
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

describe('a durable claim outranks the source label', () => {
	// The label set is not closed: remote_gateway_bridge writes
	// `source: {task.source or PROVIDER}` with PROVIDER from
	// $REMOTE_TASK_PROVIDER, so an operator can emit a label no list
	// anticipates. Extending the list on each new label observed is the
	// failure mode, not the fix.
	it('claimed wins even when the label is unknown', () => {
		assert.equal(hasNetworkConsumer({ source: 'acme-corp-relay', claimedElsewhere: true }), true);
		assert.equal(
			mayRetireSkipMarked(`${FOREIGN}.txt`, '[no-send]', noneOwned,
				() => ({ source: 'acme-corp-relay', claimedElsewhere: true })),
			false, 'a claimed result was retired because its label was not on a list');
	});

	it('the default gateway label with no explicit source is covered', () => {
		// PROVIDER falls back to the literal 'remote'.
		assert.equal(hasNetworkConsumer({ source: 'remote' }), true);
	});

	it('an unknown label with NO claim stays local', () => {
		// The ledger is authoritative-positive only. Absence of a claim is not
		// proof of absence of a consumer, so the label net still applies —
		// but an unrecognised label with no claim is treated as local, which
		// is what keeps every local family retirable without being named.
		assert.equal(hasNetworkConsumer({ source: 'events-promotion', claimedElsewhere: false }), false);
		assert.equal(
			mayRetireSkipMarked('task-taskify-1.txt', '[no-send]', noneOwned,
				() => ({ source: 'events-promotion', claimedElsewhere: false })),
			true);
	});

	it('a claim on a LOCAL-looking source is still foreign', () => {
		assert.equal(hasNetworkConsumer({ source: 'cron', claimedElsewhere: true }), true);
	});

	it('a claim outranks a MISSING source line — this pins the order', () => {
		// The only input that distinguishes the two orderings. Checking the
		// label first returns local for (source: null, claimed), retiring a
		// result its claimant is about to deliver. Without this case the
		// ordering is unpinned and a reader cannot tell it is deliberate.
		assert.equal(hasNetworkConsumer({ source: null, claimedElsewhere: true }), true);
		assert.equal(
			mayRetireSkipMarked(`${FOREIGN}.txt`, '[no-send]', noneOwned,
				() => ({ source: null, claimedElsewhere: true })),
			false, 'a claimed result with no source line was retired');
	});
});

// Third skip marker; had its own branch that returned before the ownership gate.
// Expectations MEASURED against src/result_markers.py, not authored by hand.
describe('task-bridge — [deduped:] is a skip marker under the same ownership rule', () => {
	it('recognises a well-formed deduped marker', () => {
		assert.equal(isSkipMarked(`${OWN}.txt`, '[deduped: task-123]'), true);
	});

	it('will NOT retire a foreign bridge\'s deduped result', () => {
		// The case that motivated this: previously archived unconditionally.
		assert.equal(
			mayRetireSkipMarked(`${FOREIGN}.txt`, '[deduped: task-123]', noneOwned, origin), false,
			'a deduped result this bridge never dispatched was retired, stranding its reply');
	});

	it('still retires its own deduped result', () => {
		assert.equal(mayRetireSkipMarked(`${OWN}.txt`, '[deduped: task-123]', owns, origin), true);
	});

	// A correct predicate still leaks if the branches compose wrongly, so this
	// composes the result-loop's gate order rather than re-testing the grammar.
	it('neither empty spelling reaches the fallthrough — result-loop gate order', () => {
		const owns = () => true;              // this bridge dispatched it
		const origin = () => undefined;       // no foreign origin recorded
		for (const body of ['[deduped:]', '[deduped: ]']) {
			const file = `${OWN}.txt`;
			assert.equal(isSkipMarked(file, body), true,
				`loop gate 1 missed ${JSON.stringify(body)} — falls through to onResult()`);
			assert.equal(mayRetireSkipMarked(file, body, owns, origin), true,
				`loop gate 2 refused ${JSON.stringify(body)} — result would be left unarchived`);
		}
		// Negative control: an ordinary body must NOT take the silent branch,
		// or the guard would swallow every real reply.
		assert.equal(isSkipMarked(`${OWN}.txt`, 'an ordinary reply'), false);
	});

	it('matches result_markers.py exactly on the grammar edges', () => {
		const py: [string, boolean][] = [
			['[deduped: task-123]',         true ],
			['[deduped: task-123',          false],  // closing bracket REQUIRED
			['[deduped: phone-abc.task-9]', true ],  // any target, not just task-*
			['[DEDUPED: task-123]',         true ],  // case-insensitive
			['  [deduped:task-123]',        true ],
			['[deduped: ]',                 true ],  // whitespace target
			// Both empty spellings must parse alike: the shared Python parser maps
			// '[deduped: ]' and '[deduped:]' alike to skip(deduped, extra='').
			['[deduped:]',                  true ],
		];
		for (const [body, want] of py) {
			assert.equal(isSkipMarked(`${OWN}.txt`, body), want,
				`grammar disagrees with parse_markers() on ${JSON.stringify(body)}`);
		}
	});
});

// A pool core prepends `**[core: N]**` before the marker; parse_markers peels it
// before scanning, so a consumer that does not is narrating a suppressed result.
describe('task-bridge — D7 `**[core: N]**` header is peeled before the marker scan', () => {
	const D7: [string, boolean][] = [
		['**[core: 1]**\n[deduped: task-1]',      true ],
		['**[core: 1]**\n[no-send]',              true ],
		['**[core: 1]**\n[REPLIED]',              true ],
		['**[core: 7]**\n_(routed)_\n[no-send]',  true ],
		['**[core: 1]**\nthe actual reply',       false],
	];

	it('matches result_markers.py on every D7-prefixed body', () => {
		for (const [body, want] of D7) {
			assert.equal(bodyIsSkipMarked(body), want,
				`disagrees with parse_markers() on ${JSON.stringify(body)}`);
		}
	});

	it('retires an OWNED D7-prefixed deduped result', () => {
		assert.equal(
			mayRetireSkipMarked(`${OWN}.txt`, '**[core: 1]**\n[deduped: task-1]', owns, origin), true,
			'a D7-prefixed skip marker was narrated instead of suppressed');
	});

	it('still refuses a FOREIGN D7-prefixed result', () => {
		assert.equal(
			mayRetireSkipMarked(`${FOREIGN}.txt`, '**[core: 1]**\n[no-send]', noneOwned, origin), false);
	});
});
