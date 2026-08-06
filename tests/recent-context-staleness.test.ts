import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { annotateContextFreshness, VOICE_CONTEXT_STALE_HOURS } from '../src/inline-tools.ts';

// Regression guard: `recent_context` used to return voice-session-context.json
// VERBATIM, with no notion of its own age — while the tool description promises
// "the CURRENT voice-session context".
//
// The writer is a PROSE INSTRUCTION (CLAUDE.md tells core to update the file when
// a durable decision lands), not code, so it lapses silently. Measured 2026-08-03:
// the canonical file was 97h old and still carried pending_action; the legacy copy
// was 878h old. Voice asked "what's pending?" would have answered with a four-day
// -old action as current.

const NOW = Date.parse('2026-08-03T07:00:00Z');
const iso = (hoursAgo: number) => new Date(NOW - hoursAgo * 3_600_000).toISOString();

describe('recent_context freshness annotation', () => {
	it('marks a context older than the threshold STALE and says how old', () => {
		// The real observed case: 97h, still advertising a pending action.
		const out = annotateContextFreshness(
			{ updated_at: iso(97), pending_action: { kind: 'paste', what: 'Draft A' } },
			NOW,
		);
		assert.equal(out.stale, true);
		assert.equal(out.age_hours, 97);
		assert.match(String(out.note), /EARLIER session/);
		assert.ok(out.pending_action, 'payload must still be returned — withholding it hides context that is often still correct');
	});

	it('leaves a fresh context unmarked', () => {
		const out = annotateContextFreshness({ updated_at: iso(0.5), active_drafts: [{ name: 'A' }] }, NOW);
		assert.equal(out.stale, undefined, 'a same-session context must not be flagged');
		assert.equal(out.age_hours, 0.5);
		assert.equal(out.note, undefined);
	});

	it('treats an unparseable or missing updated_at as UNKNOWN, never as fresh', () => {
		// Failing toward "unknown" matters: defaulting to fresh would restore the
		// exact defect for any file whose timestamp is malformed.
		for (const bad of [undefined, '', 'not-a-date', 12345]) {
			const out = annotateContextFreshness({ updated_at: bad as never, pending_action: {} }, NOW);
			assert.equal(out.freshness, 'unknown', `updated_at=${String(bad)} must be unknown`);
			assert.equal(out.stale, undefined);
			assert.match(String(out.note), /historical/);
		}
	});

	it('boundary: exactly at the threshold is stale, just under is not', () => {
		assert.equal(annotateContextFreshness({ updated_at: iso(VOICE_CONTEXT_STALE_HOURS) }, NOW).stale, true);
		assert.equal(annotateContextFreshness({ updated_at: iso(VOICE_CONTEXT_STALE_HOURS - 0.1) }, NOW).stale, undefined);
	});

	it('a materially FUTURE updated_at is unknown, never fresh', () => {
		// qingyun-wu + john-the-dev, review of #2560. age = now - updated goes NEGATIVE
		// for a future stamp, so it fails BOTH branches: not >= the stale threshold, and
		// Number.isFinite() is true so it never reaches 'unknown'. A skewed or corrupt
		// clock therefore bypassed the guard entirely and voice could assert an old
		// pending_action as current until wall time caught up — the exact defect this
		// PR exists to close, arriving through the one input I had not considered.
		const out = annotateContextFreshness(
			{ updated_at: iso(-24), pending_action: { kind: 'paste' } },
			NOW,
		);
		assert.equal(out.freshness, 'unknown', 'future stamp must not read as fresh');
		assert.equal(out.stale, undefined);
		assert.match(String(out.note), /future/i, 'say WHY it is untrusted');
		assert.ok(out.pending_action, 'payload still returned');
	});

	it('tolerates small clock skew rather than crying wolf on it', () => {
		// Control for the above: machine clocks routinely disagree by seconds. If any
		// future stamp were 'unknown', ordinary skew would flag every healthy context
		// and the marker would stop meaning anything.
		const out = annotateContextFreshness({ updated_at: iso(-0.02) }, NOW); // ~72s ahead
		assert.equal(out.freshness, undefined, 'small skew must stay healthy');
		assert.equal(out.stale, undefined);
		assert.equal(out.age_hours, 0, 'clamped to 0, never negative');
	});

	it('a non-finite clock is unknown, never fresh (closes the class, not the case)', () => {
		// SELF-REVIEW, not a reviewer finding. The reviewed defect was a FUTURE stamp
		// producing a negative age that satisfied neither branch. Enumerating the rest
		// of the input space turned up the same shape one step over: a non-finite
		// `nowMs` yields age=NaN, which is not >= the threshold and is not caught by
		// the isFinite check on updated_at — so it read as FRESH.
		//
		// Fixing the case (future stamps) and not the class (non-finite arithmetic)
		// is what produced three review rounds; this closes the arithmetic itself.
		for (const badNow of [Number.NaN, Number.POSITIVE_INFINITY]) {
			const out = annotateContextFreshness({ updated_at: iso(1), pending_action: {} }, badNow);
			assert.equal(out.freshness, 'unknown', `nowMs=${badNow} must not read as fresh`);
			assert.equal(out.stale, undefined);
			assert.ok(out.pending_action, 'payload still returned');
		}
	});

	it('survives a null/empty payload without throwing', () => {
		assert.equal(annotateContextFreshness(null, NOW).freshness, 'unknown');
		assert.equal(annotateContextFreshness({}, NOW).freshness, 'unknown');
	});
});
