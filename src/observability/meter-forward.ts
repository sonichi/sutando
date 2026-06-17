/**
 * Upstream usage forwarder — the metering EXPORT seam.
 *
 * The collector writes every normalized `UsageRecord` to the durable local
 * ledger (the billing floor). This forwarder is the OPTIONAL second hop: when
 * export is configured (`metering.enabled` + `metering.endpoint`), it batches
 * those same records and POSTs them upstream as `{ usage: UsageRecord[] }` —
 * the exact envelope the collector's own `POST /ingest` accepts, so the target
 * may be another collector OR a custom receiver (e.g. a cloud shipper) with no
 * shape translation.
 *
 * Best-effort, never-throws, and provider-agnostic: it carries NO auth or
 * cloud-contract specifics — authentication, the upstream API shape, and the
 * durable cursor/exactly-once shipper are the receiver's concern (still
 * post-parity; see meter.ts). The local ledger stays the source of truth, so a
 * dropped batch never loses billing data — it's reconcilable from the ledger.
 * On a transient failure the batch is re-queued at the head and retried; the
 * receiver dedups on `usage_id`, so an at-least-once re-POST is safe.
 *
 * The factory returns `null` unless BOTH `enabled` and `endpoint` are set, so
 * existing deployments (`metering.enabled` defaults false) are unchanged.
 */

import type { UsageRecord } from './usage.js';
import { loadObservabilityConfig, type MeteringSection } from './config.js';

/** Time-based flush for partial batches. Realtime usage trickles in (one tick
 *  per USAGE_TICK_MS), so size-based flushing alone would strand records. */
export const METER_FORWARD_FLUSH_MS = 5_000;

/** Bound the in-memory queue during an upstream outage — drop OLDEST on
 *  overflow. Losing the tail of a long outage beats unbounded memory growth,
 *  and the ledger still holds every record. */
const QUEUE_CAP = 5_000;

export interface MeterForwarder {
	/** Enqueue a record for upstream export. Best-effort; never throws. */
	forward(rec: UsageRecord): void;
	/** Flush the queued batch now; resolves once the in-flight POST settles. */
	flush(): Promise<void>;
	/** Cancel the timer and flush once. Idempotent. Call on shutdown. */
	stop(): Promise<void>;
}

/** Minimal fetch shape — the global `fetch` satisfies it; tests inject a stub. */
export type FetchLike = (url: string, init: RequestInit) => Promise<{ ok: boolean }>;

class HttpMeterForwarder implements MeterForwarder {
	private queue: UsageRecord[] = [];
	private timer: ReturnType<typeof setTimeout> | null = null;
	private flushing = false;
	private stopped = false;

	constructor(
		private readonly endpoint: string,
		private readonly batchMax: number,
		private readonly flushMs: number,
		private readonly fetchImpl: FetchLike,
	) {}

	forward(rec: UsageRecord): void {
		if (this.stopped) return;
		if (this.queue.length >= QUEUE_CAP) this.queue.splice(0, this.queue.length - QUEUE_CAP + 1);
		this.queue.push(rec);
		this.schedule();
		if (this.queue.length >= this.batchMax) void this.flush();
	}

	async flush(): Promise<void> {
		if (this.flushing || this.queue.length === 0) return;
		this.flushing = true;
		const batch = this.queue.splice(0, this.queue.length);
		try {
			const res = await this.fetchImpl(this.endpoint, {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ usage: batch }),
			});
			if (!res.ok) this.requeue(batch); // non-2xx → retry next flush
		} catch {
			this.requeue(batch); // network down → keep for the next attempt
		} finally {
			this.flushing = false;
			if (this.queue.length > 0 && !this.stopped) this.schedule();
		}
	}

	async stop(): Promise<void> {
		this.stopped = true;
		if (this.timer) {
			clearTimeout(this.timer);
			this.timer = null;
		}
		await this.flush();
	}

	/** Put a failed batch back at the head (oldest-first), capped. */
	private requeue(batch: UsageRecord[]): void {
		this.queue.unshift(...batch);
		if (this.queue.length > QUEUE_CAP) this.queue.splice(0, this.queue.length - QUEUE_CAP);
	}

	private schedule(): void {
		if (this.timer || this.stopped) return;
		this.timer = setTimeout(() => {
			this.timer = null;
			void this.flush();
		}, this.flushMs);
		// Never keep the process alive just for the forward timer.
		if (typeof this.timer.unref === 'function') this.timer.unref();
	}
}

/** Build a forwarder from a metering config block, or `null` when export is off
 *  (disabled or no endpoint). `opts` is for tests (inject fetch / flush cadence). */
export function meterForwarderFromConfig(
	cfg: MeteringSection,
	opts?: { fetchImpl?: FetchLike; flushMs?: number },
): MeterForwarder | null {
	if (!cfg.enabled || !cfg.endpoint) return null;
	const batchMax = cfg.batchMax > 0 ? cfg.batchMax : 100;
	return new HttpMeterForwarder(
		cfg.endpoint,
		batchMax,
		opts?.flushMs ?? METER_FORWARD_FLUSH_MS,
		opts?.fetchImpl ?? (fetch as FetchLike),
	);
}

/** Forwarder resolved from the ambient observability config; `null` when export
 *  is off or the config fails to load. The collector's default. */
export function defaultMeterForwarder(): MeterForwarder | null {
	try {
		return meterForwarderFromConfig(loadObservabilityConfig().metering);
	} catch {
		return null;
	}
}
