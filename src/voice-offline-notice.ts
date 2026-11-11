// Which voice-offline categories the owner has already been told about, kept in the workspace
// so the notice is owed once per outage, not once per voice-agent process (#6368).

import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';

/** How long a notice stands: a longer outage is announced again once a day, never forgotten. */
export const OFFLINE_NOTICE_TTL_S = 24 * 3600;

/**
 * A set of announced failure categories that survives a restart of the voice agent. A restarted
 * process that meets the same refusal (a region Gemini Live does not serve, a revoked key) reads
 * that the owner was already told and stays quiet; recovery clears it, so the next outage is
 * announced again, and a notice older than OFFLINE_NOTICE_TTL_S lapses. Each category keeps the
 * time it was first announced.
 *
 * The file is the record whenever it was last written successfully: every read takes it whole, so
 * a clear another process wrote is seen here (the last writer wins). When a write fails, what this
 * process holds is authoritative until a write succeeds again, so a recovery that could not be
 * saved is never undone by the stale file. Reads fail soft: an absent, unreadable or wrong-shaped
 * record leaves the set as this process holds it.
 */
export class OfflineNoticeStore implements Iterable<string> {
	private notified = new Map<string, number>();
	private diskCurrent = true;

	constructor(
		private readonly path: string,
		private readonly now: () => number = () => Date.now() / 1000,
	) {}

	private load(): Map<string, number> {
		if (this.diskCurrent) {
			try {
				const raw = JSON.parse(readFileSync(this.path, 'utf8')) as { categories?: unknown } | null;
				const recorded = raw?.categories;
				if (recorded && typeof recorded === 'object' && !Array.isArray(recorded)) {
					const next = new Map<string, number>();
					for (const [category, at] of Object.entries(recorded as Record<string, unknown>)) {
						if (typeof at === 'number' && Number.isFinite(at)) next.set(category, at);
					}
					this.notified = next;
				}
			} catch {
				// Absent or unreadable: what this process holds is all there is.
			}
		}
		const cutoff = this.now() - OFFLINE_NOTICE_TTL_S;
		for (const [category, at] of this.notified) if (at < cutoff) this.notified.delete(category);
		return this.notified;
	}

	private save(): void {
		const tmp = `${this.path}.${process.pid}.tmp`;
		try {
			mkdirSync(dirname(this.path), { recursive: true });
			writeFileSync(tmp, JSON.stringify({ categories: Object.fromEntries(this.notified) }));
			renameSync(tmp, this.path);
			this.diskCurrent = true;
		} catch {
			// Unsaved: this process's set stands until a write succeeds, and the stale file is not read.
			this.diskCurrent = false;
		}
	}

	get size(): number {
		return this.load().size;
	}

	has(category: string): boolean {
		return this.load().has(category);
	}

	add(category: string): this {
		const notified = this.load();
		if (!notified.has(category)) notified.set(category, this.now());
		this.save();
		return this;
	}

	clear(): void {
		this.notified = new Map<string, number>();
		this.save();
	}

	[Symbol.iterator](): Iterator<string> {
		return this.load().keys();
	}
}
