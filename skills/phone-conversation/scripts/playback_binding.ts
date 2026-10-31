export class PlaybackOwner {
	private binding: { callSid: string; path: string } | null = null;

	bind(callSid: string, path: string): void {
		this.binding = { callSid, path };
	}

	current(): { callSid: string; path: string } | null {
		return this.binding;
	}

	controlledBy(callSid: string): boolean {
		return this.binding?.callSid === callSid;
	}

	releaseCall(callSid: string): boolean {
		if (!this.controlledBy(callSid)) return false;
		this.binding = null;
		return true;
	}
}

export function clearActivePlaybackProcess<T>(active: T | null, closing: T): T | null {
	return active === closing ? null : active;
}
