/**
 * The voice session's conversation log (conversation.log + the sqlite mirror) as a bodhi
 * ConversationHistoryStore, written like bodhi's own Markdown store: bodhi's history writer calls
 * it asynchronously after each turn, it appends the spoken lines without blocking (fs/promises,
 * sqlite in a worker) on its own serial queue, and a failed write is logged, never thrown. It also
 * orders the session-end boundary marker against the items by time, so a goodbye said before the
 * marker never lands after it.
 */
import type { ConversationHistoryStore, ConversationItem, SessionRecord, SessionReport, SessionSummary } from 'bodhi-realtime-agent';

export interface ConversationLogStoreDeps {
	/** Appends one spoken line (conversation.log + sqlite) without blocking. */
	log(role: string, text: string, sessionId: string, at: Date): Promise<void>;
	/** Appends the session-end boundary marker without blocking. */
	boundary(reason: string): Promise<void>;
	/** True while a goodbye is closing the session: its remaining lines are not logged. */
	suppress(): boolean;
}

/** How long a boundary waits for the items said before it, when no later item arrives. */
export const BOUNDARY_WAIT_MS = 1000;

export class ConversationLogStore implements ConversationHistoryStore {
	private tail: Promise<void> = Promise.resolve();
	private pending: { reason: string; at: number; timer: ReturnType<typeof setTimeout> } | null = null;

	constructor(private readonly deps: ConversationLogStoreDeps, private readonly boundaryWaitMs = BOUNDARY_WAIT_MS) {}

	async createSession(_session: SessionRecord): Promise<void> {}
	async updateSession(_sessionId: string, _update: Partial<SessionRecord>): Promise<void> {}
	async saveSessionReport(_report: SessionReport): Promise<void> {}

	addItems(sessionId: string, items: ConversationItem[]): Promise<void> {
		return this.chain(async () => {
			for (const item of items) {
				if (this.pending && item.timestamp > this.pending.at) await this.writeBoundary();
				if (item.role !== 'user' && item.role !== 'assistant') continue;
				if (!item.content || this.deps.suppress()) continue;
				await this.deps.log(item.role, item.content, sessionId, new Date(item.timestamp));
			}
		});
	}

	/** The session ended at this moment: the marker goes after every item said before it. */
	markBoundary(reason: string): void {
		if (this.pending) void this.chain(() => this.writeBoundary());
		const timer = setTimeout(() => { void this.chain(() => this.writeBoundary()); }, this.boundaryWaitMs);
		timer.unref?.();
		this.pending = { reason, at: Date.now(), timer };
	}

	/** Resolves once every write queued so far is done. */
	drain(): Promise<void> {
		return this.tail;
	}

	// Reads are not served from this log: bodhi uses them only to resume a session record.
	async getSession(_sessionId: string): Promise<SessionRecord | null> { return null; }
	async getSessionItems(_sessionId: string): Promise<ConversationItem[]> { return []; }
	async listUserSessions(_userId: string): Promise<SessionSummary[]> { return []; }

	private async writeBoundary(): Promise<void> {
		if (!this.pending) return;
		clearTimeout(this.pending.timer);
		const { reason } = this.pending;
		this.pending = null;
		await this.deps.boundary(reason);
	}

	private chain(fn: () => Promise<void> | void): Promise<void> {
		this.tail = this.tail.then(fn).catch((e) => { console.error('[conversation-log-store] write failed:', e); });
		return this.tail;
	}
}
