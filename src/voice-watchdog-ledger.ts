/**
 * Durable append-only ledger for watchdog evidence rows (design §Observability:
 * the shared audio-health mailbox is a lossy one-slot queue, so watchdog rows
 * get their own small bounded channel). JSONL, one flush chain, bounded queue
 * with an explicit drop counter — losing a row is possible under sustained
 * pressure but always counted, never silent.
 */
import { appendFile } from 'node:fs/promises';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

export const LEDGER_QUEUE_CAP = 256;

export class WatchdogLedger {
	private readonly path: string;
	private meta: Record<string, unknown>;
	private queue: string[] = [];
	private chain: Promise<void> = Promise.resolve();
	private _dropped = 0;
	private _written = 0;
	private readonly onError: (err: Error) => void;

	constructor(o: {
		path: string;
		/** Stamped onto every row: detectorVersion, capabilitySet, mode, … */
		meta: Record<string, unknown>;
		onError?: (err: Error) => void;
	}) {
		this.path = o.path;
		this.meta = o.meta;
		this.onError = o.onError ?? (() => {});
		try {
			mkdirSync(dirname(this.path), { recursive: true });
		} catch {
			// surfaced on first append failure instead
		}
	}

	mergeMeta(extra: Record<string, unknown>): void {
		this.meta = { ...this.meta, ...extra };
	}

	get dropped(): number {
		return this._dropped;
	}
	get written(): number {
		return this._written;
	}

	append(row: Record<string, unknown> & { row: string }): void {
		if (this.queue.length >= LEDGER_QUEUE_CAP) {
			this._dropped += 1;
			return;
		}
		this.queue.push(
			JSON.stringify({
				...this.meta,
				...row,
				wallAtUnixMs: Date.now(),
				monoOffsetMs: Math.round(performance.now()),
			}) + '\n',
		);
		this.chain = this.chain.then(() => this.drain());
	}

	/** Resolves when every append offered so far is on disk (or counted dropped). */
	flush(): Promise<void> {
		return this.chain;
	}

	private async drain(): Promise<void> {
		if (this.queue.length === 0) return;
		const batch = this.queue.splice(0, this.queue.length);
		try {
			await appendFile(this.path, batch.join(''), 'utf8');
			this._written += batch.length;
		} catch (err) {
			this._dropped += batch.length;
			this.onError(err instanceof Error ? err : new Error(String(err)));
		}
	}
}
