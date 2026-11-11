// Which voice-offline categories the owner has already been told about, kept in the workspace
// so the notice is owed once per outage, not once per voice-agent process (#6368).

import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';

/**
 * A set of announced failure categories that survives a restart of the voice agent. A restarted
 * process that meets the same refusal (a region Gemini Live does not serve, a revoked key) reads
 * that the owner was already told and stays quiet; only recovery clears it, so the next outage
 * is announced again. Reads and writes fail soft: an unreadable record is an empty one, and a
 * record that cannot be written leaves the set held in memory for this process, as before.
 */
export class OfflineNoticeStore implements Iterable<string> {
	private memory = new Set<string>();

	constructor(private readonly path: string) {}

	private load(): Set<string> {
		try {
			const raw = JSON.parse(readFileSync(this.path, 'utf8')) as { categories?: unknown };
			if (raw && typeof raw === 'object' && raw.categories && typeof raw.categories === 'object') {
				for (const c of Object.keys(raw.categories as Record<string, unknown>)) this.memory.add(c);
			}
		} catch {
			// Absent or unreadable: what this process holds is all there is.
		}
		return this.memory;
	}

	private save(): void {
		const categories: Record<string, number> = {};
		for (const c of this.memory) categories[c] = Math.floor(Date.now() / 1000);
		const tmp = `${this.path}.${process.pid}.tmp`;
		try {
			mkdirSync(dirname(this.path), { recursive: true });
			writeFileSync(tmp, JSON.stringify({ categories }));
			renameSync(tmp, this.path);
		} catch {
			// The set still holds for this process; only a restart would forget it.
		}
	}

	get size(): number {
		return this.load().size;
	}

	has(category: string): boolean {
		return this.load().has(category);
	}

	add(category: string): this {
		this.load().add(category);
		this.save();
		return this;
	}

	clear(): void {
		this.memory = new Set<string>();
		this.save();
	}

	[Symbol.iterator](): Iterator<string> {
		return this.load()[Symbol.iterator]();
	}
}
