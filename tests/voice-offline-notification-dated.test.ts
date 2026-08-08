// An AppleScript notification cannot be withdrawn, so the banner must be dated
// and recovery announced — an undated one reads as a live outage.

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import {
	formatNotificationTimestamp,
	formatVoiceOfflineNotification,
	formatVoiceRecoveryNotification,
} from '../src/voice-error-classifier.js';

const AT = new Date(2026, 7, 8, 22, 34, 0); // Aug 8 2026, 10:34 PM local

describe('dated offline notification', () => {
	it('carries a timestamp — the whole point, since it cannot be retracted', () => {
		const msg = formatVoiceOfflineNotification(
			'Voice is offline — Gemini API key is invalid or revoked. Update GEMINI_API_KEY in .env.',
			AT,
		);
		const stamp = formatNotificationTimestamp(AT);
		assert.ok(msg.includes(stamp), `no timestamp in: ${msg}`);
		assert.match(msg, /detected/, 'the stamp must be labelled, not a bare number');
	});

	it('preserves the classifier message so the remedy survives', () => {
		// Regression guard: the fix must not truncate the actionable half. A dated
		// banner that no longer says WHAT to do would trade one defect for another.
		const msg = formatVoiceOfflineNotification('Update GEMINI_API_KEY in .env.', AT);
		assert.ok(msg.includes('Update GEMINI_API_KEY in .env.'), msg);
	});

	it('strips characters that would break the AppleScript literal', () => {
		// execFileSync means no shell is involved, so this is about the
		// `display notification "..."` literal only.
		const msg = formatVoiceOfflineNotification('bad "quoted" and back\\slash', AT);
		assert.ok(!msg.includes('"'), `double quote survived: ${msg}`);
		assert.ok(!msg.includes('\\'), `backslash survived: ${msg}`);
	});

	it('timestamp is local and human, not an ISO string', () => {
		// An ISO/UTC stamp would reintroduce the confusion in a new form — the
		// reader thinks in local time.
		const stamp = formatNotificationTimestamp(AT);
		assert.ok(!stamp.includes('T'), `looks like ISO: ${stamp}`);
		assert.ok(!/Z$/.test(stamp), `looks like UTC: ${stamp}`);
		assert.match(stamp, /8/, `should mention the day: ${stamp}`);
	});
});

describe('recovery notification', () => {
	it('is dated and explicitly retires the earlier alert', () => {
		const msg = formatVoiceRecoveryNotification(AT);
		assert.ok(msg.includes(formatNotificationTimestamp(AT)), msg);
		assert.match(msg, /no longer applies/i,
			'must say the earlier banner is dead — that is the counter-signal');
		assert.match(msg, /back online|recovered/i, msg);
	});
});

// Wiring guards, read from source: correct formatters are worthless if the call
// site stops using them, and the module cannot be imported in a test.
const AGENT_SRC = readFileSync(
	join(import.meta.dirname ?? '.', '..', 'src/voice-agent.ts'),
	'utf-8',
);

describe('voice-agent wiring', () => {
	it('the offline banner is built by the dated formatter, not inline', () => {
		assert.match(AGENT_SRC, /formatVoiceOfflineNotification\(c\.userMessage, new Date\(\)\)/,
			'offline notification no longer goes through the dated formatter');
	});

	it('recovery is hooked to the ACTIVE transition, not the 30s health poll', () => {
		// Event-driven, and exactly one recovery site: the same seam that already
		// clears the classification, so a polled second copy cannot drift.
		assert.match(AGENT_SRC, /toState === 'ACTIVE'\)\s*\{[\s\S]{0,600}?notifyVoiceRecovered\(\)/,
			'notifyVoiceRecovered is not called on the ACTIVE stateChange');
		const inHealthPoll = /state === 'ACTIVE' && voiceNotifiedCategories\.size/.test(AGENT_SRC);
		assert.equal(inHealthPoll, false,
			'recovery duplicated into the health monitor — keep one recovery site');
	});

	it('recovery clears the notified set, so a second outage still alerts', () => {
		// The dangerous half: without the clear, the throttle is once-per-process
		// and outage #2 on a long-lived agent is silent.
		assert.match(AGENT_SRC, /voiceNotifiedCategories\.clear\(\)/,
			'notified set is never cleared — a later failure would never notify');
	});

	it('the notified set is declared outside the classifier IIFE', () => {
		// If it slips back inside the IIFE, recovery silently stops while the offline
		// path keeps working — a partial failure that looks fine.
		const decl = AGENT_SRC.indexOf('const voiceNotifiedCategories = new Set<string>()');
		const iife = AGENT_SRC.indexOf('const transport = (session as any).transport;');
		assert.ok(decl > 0 && iife > 0, 'markers not found — update this test');
		assert.ok(decl < iife,
			'voiceNotifiedCategories must be declared before/outside the classifier IIFE');
	});
});
