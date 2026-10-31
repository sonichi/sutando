export interface CallRecord {
	callSid: string;
	callerNumber: string;
	recipientNumber: string;
	stirVerstat: string;
	purpose: string;
	isMeeting: boolean;
	meetingId: string;
	passcode: string;
	parentCallSid: string;
	createdAt: number;
}

export function normalizePhone(num: string): string {
	const digits = num.replace(/\D/g, '');
	return digits.length === 10 ? '1' + digits : digits;
}

export class CallRecordStore {
	private readonly records = new Map<string, CallRecord>();

	constructor(
		private readonly ttlMs = 120_000,
		private readonly now: () => number = () => Date.now(),
	) {}

	put(rec: Omit<CallRecord, 'createdAt'>): void {
		const now = this.now();
		for (const [sid, record] of this.records) {
			if (now - record.createdAt > this.ttlMs) this.records.delete(sid);
		}
		this.records.set(rec.callSid, {
			...rec,
			callerNumber: rec.callerNumber ?? '',
			recipientNumber: rec.recipientNumber ?? '',
			stirVerstat: rec.stirVerstat ?? '',
			purpose: rec.purpose ?? '',
			meetingId: rec.meetingId ?? '',
			passcode: rec.passcode ?? '',
			parentCallSid: rec.parentCallSid ?? '',
			createdAt: now,
		});
	}

	claim(callSid: string): CallRecord | null {
		const record = this.records.get(callSid);
		if (!record) return null;
		this.records.delete(callSid);
		return this.now() - record.createdAt > this.ttlMs ? null : record;
	}
}

export function classifyCall(
	rec: Omit<CallRecord, 'createdAt'>,
	policy: { ownerNumber: string; verifiedCallers: Set<string>; verifiedMeetings: Set<string> },
): { personOnLine: string; callerVerified: boolean; isOwner: boolean; ownerDowngraded: boolean } {
	const personOnLine = rec.recipientNumber || rec.callerNumber;
	const isOutbound = !!rec.recipientNumber;
	let callerVerified: boolean;
	if (rec.isMeeting) {
		const numericId = rec.meetingId.replace(/\D/g, '');
		callerVerified = policy.verifiedMeetings.has(rec.meetingId)
			|| (numericId !== rec.meetingId && policy.verifiedMeetings.has(numericId));
	} else {
		const normalizedPerson = normalizePhone(personOnLine);
		callerVerified = policy.verifiedCallers.size === 0 || policy.verifiedCallers.has(normalizedPerson);
	}

	const ownerNumbers = policy.ownerNumber
		? policy.ownerNumber.split(',').map(number => normalizePhone(number.trim()))
		: [];
	const numberMatchesOwner = ownerNumbers.length > 0 && ownerNumbers.includes(normalizePhone(personOnLine));
	let isOwner: boolean;
	let ownerDowngraded = false;
	if (isOutbound && numberMatchesOwner) {
		isOwner = true;
	} else if (numberMatchesOwner && rec.stirVerstat && rec.stirVerstat !== 'TN-Validation-Passed-A') {
		isOwner = false;
		ownerDowngraded = true;
	} else {
		isOwner = numberMatchesOwner || (!policy.ownerNumber && callerVerified);
	}
	return { personOnLine, callerVerified, isOwner, ownerDowngraded };
}
