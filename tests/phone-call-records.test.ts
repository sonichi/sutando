import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { CallRecordStore, classifyCall, normalizePhone, type CallRecord } from '../skills/phone-conversation/scripts/call_records.js';

function record(overrides: Partial<Omit<CallRecord, 'createdAt'>> = {}): Omit<CallRecord, 'createdAt'> {
	return {
		callSid: 'CA123',
		callerNumber: '',
		recipientNumber: '',
		stirVerstat: '',
		purpose: '',
		isMeeting: false,
		meetingId: '',
		passcode: '',
		parentCallSid: '',
		...overrides,
	};
}

describe('phone call records', () => {
	it('does not claim a call record for an unknown peer-chosen call SID', () => {
		const store = new CallRecordStore();
		assert.equal(store.claim('CA-peer-chosen'), null);
		assert.equal(normalizePhone('+1 (415) 555-0100'), '14155550100');
	});

	it('claims each call record once', () => {
		const store = new CallRecordStore();
		store.put(record({ callSid: 'CA-once' }));
		assert.equal(store.claim('CA-once')?.callSid, 'CA-once');
		assert.equal(store.claim('CA-once'), null);
	});

	it('rejects a record after its TTL and prunes expired entries on put', () => {
		let now = 1_000;
		const store = new CallRecordStore(100, () => now);
		store.put(record({ callSid: 'CA-old' }));
		now = 1_101;
		assert.equal(store.claim('CA-old'), null);
		store.put(record({ callSid: 'CA-new' }));
		assert.equal(store.claim('CA-old'), null);
		assert.equal(store.claim('CA-new')?.callSid, 'CA-new');
	});

	it('recognizes outbound calls to the owner without STIR/SHAKEN gating', () => {
		const result = classifyCall(record({ recipientNumber: '+1 (415) 555-0100' }), {
			ownerNumber: '+14155550100',
			verifiedCallers: new Set(),
			verifiedMeetings: new Set(),
		});
		assert.equal(result.personOnLine, '+1 (415) 555-0100');
		assert.equal(result.isOwner, true);
		assert.equal(result.ownerDowngraded, false);
	});

	it('downgrades an inbound owner-number match without A-level STIR verification', () => {
		const result = classifyCall(record({
			callerNumber: '+14155550100',
			stirVerstat: 'TN-Validation-Passed-C',
		}), {
			ownerNumber: '+14155550100',
			verifiedCallers: new Set(),
			verifiedMeetings: new Set(),
		});
		assert.equal(result.isOwner, false);
		assert.equal(result.ownerDowngraded, true);
	});

	it('recognizes an inbound owner-number match with A-level STIR verification', () => {
		const result = classifyCall(record({
			callerNumber: '+14155550100',
			stirVerstat: 'TN-Validation-Passed-A',
		}), {
			ownerNumber: '+14155550100',
			verifiedCallers: new Set(),
			verifiedMeetings: new Set(),
		});
		assert.equal(result.isOwner, true);
		assert.equal(result.ownerDowngraded, false);
	});

	it('verifies a meeting through its numeric ID fallback', () => {
		const result = classifyCall(record({ isMeeting: true, meetingId: '123-456-789' }), {
			ownerNumber: '',
			verifiedCallers: new Set(),
			verifiedMeetings: new Set(['123456789']),
		});
		assert.equal(result.callerVerified, true);
	});

	it('keeps the no-owner-number and empty-verified-list owner fallback', () => {
		const result = classifyCall(record({ callerNumber: '+14155550100' }), {
			ownerNumber: '',
			verifiedCallers: new Set(),
			verifiedMeetings: new Set(),
		});
		assert.equal(result.callerVerified, true);
		assert.equal(result.isOwner, true);
	});
});
