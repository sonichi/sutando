export type VisionCall<T> = { callSid: string | null; session: T };

export class OwnerVisionBinding<T> {
	private readonly calls = new Map<string, T>();

	constructor(private readonly priorSession: T | null = null) {}

	attach(callSid: string, session: T): VisionCall<T> {
		this.calls.delete(callSid);
		this.calls.set(callSid, session);
		return { callSid, session };
	}

	remove(callSid: string): VisionCall<T> | null {
		this.calls.delete(callSid);
		const remaining = [...this.calls.entries()].at(-1);
		if (remaining) return { callSid: remaining[0], session: remaining[1] };
		return this.priorSession === null ? null : { callSid: null, session: this.priorSession };
	}
}
