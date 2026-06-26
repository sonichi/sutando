/**
 * `meter.record(u)` — the durable usage primitive. Twin of meter.py.
 *
 * Guarantees:
 *  - DURABLE: synchronous append (O_APPEND, single write per line) to
 *    `<workspace>/data/usage/usage-YYYY-MM-DD.jsonl`. This append is the
 *    durability point — it survives crash/restart/offline. `fsync` is added
 *    when `SUTANDO_METERING_FSYNC` is truthy (default off, matching event_log).
 *  - IDEMPOTENT: `usage_id` is the dedup key. record() does NOT dedup on append
 *    (append-only, at-least-once) — a crash-retry may legitimately re-append.
 *    A caller may supply `usage_id` so the re-append carries the same key;
 *    downstream (the deferred shipper / metering API) dedups on it →
 *    exactly-once billing. Auto-minted when absent.
 *  - NEVER THROWS: a thrown billing call is worse than a logged miss. On append
 *    failure record() logs loudly and still RETURNS the stamped record.
 *  - The durable append happens BEFORE the advisory `usage.recorded` obs event.
 *
 * The metering SHIPPER (cursor + batch POST + retry) is intentionally NOT here —
 * that is post-parity. record() only writes the local source-of-truth ledger.
 */

import { appendFileSync, mkdirSync, openSync, writeSync, fsyncSync, closeSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { resolveWorkspace } from '../workspace_default.js';
import { newUsageId, newTraceId } from './ids.js';
import { loadObservabilityConfig } from './config.js';
import { emit } from './obs.js';
import type { UsageRecord, UsageRecordInput } from './usage.js';

function pad(n: number): string {
	return String(n).padStart(2, '0');
}

export function ledgerPath(atMs: number = Date.now(), workspace?: string): string {
	const d = new Date(atMs);
	const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
	return join(workspace ?? resolveWorkspace(), 'data', 'usage', `usage-${date}.jsonl`);
}

function fsyncEnabled(): boolean {
	const v = process.env.SUTANDO_METERING_FSYNC?.trim().toLowerCase();
	return v === '1' || v === 'true' || v === 'yes' || v === 'on';
}

/** Durable append of a FULLY-FORMED usage record to the daily ledger — the
 *  durability point (O_APPEND, single write per line; fsync behind
 *  SUTANDO_METERING_FSYNC). Never throws. Used by `record()` and by the
 *  collector, which receives already-stamped records from source normalizers
 *  (CC-derived `usage_id`/`trace_id`) and must NOT re-stamp them. */
export function writeLedger(rec: UsageRecord): void {
	try {
		const path = ledgerPath(rec.ts * 1000);
		mkdirSync(dirname(path), { recursive: true });
		const line = JSON.stringify(rec) + '\n';
		if (fsyncEnabled()) {
			const fd = openSync(path, 'a');
			try {
				writeSync(fd, line);
				fsyncSync(fd);
			} finally {
				closeSync(fd);
			}
		} else {
			appendFileSync(path, line, { flag: 'a' });
		}
	} catch (e) {
		try {
			process.stderr.write(`[meter] FAILED to record usage ${rec.usage_id} (${rec.meter}): ${(e as Error).message}\n`);
		} catch {
			/* best-effort */
		}
	}
}

export function record(input: UsageRecordInput): UsageRecord {
	// tenant_id defaults from config when the caller doesn't specify (BYOK → null).
	let tenantId = input.tenant_id;
	if (tenantId === undefined) {
		try {
			tenantId = loadObservabilityConfig().tenant.id;
		} catch {
			tenantId = null;
		}
	}

	const rec: UsageRecord = {
		schema: 1,
		usage_id: input.usage_id ?? newUsageId(),
		ts: input.ts ?? Date.now() / 1000,
		tenant_id: tenantId ?? null,
		trace_id: input.trace_id ?? newTraceId(),
		actor: input.actor,
		source: input.source,
		meter: input.meter,
		quantity: input.quantity,
		unit: input.unit,
		provider: input.provider,
		provider_ref: input.provider_ref ?? null,
		attrs: input.attrs ?? {},
	};
	if (input.source_file !== undefined) rec.source_file = input.source_file;

	// --- durability point: synchronous append BEFORE the advisory emit ---
	writeLedger(rec);

	// --- advisory obs event so usage shows inline in traces (the ledger bills) ---
	try {
		emit({
			source: rec.source,
			source_file: rec.source_file,
			trace_id: rec.trace_id,
			actor: rec.actor,
			kind: 'usage.recorded',
			outcome: 'ok',
			usage: {
				provider: rec.provider,
				model: rec.attrs.model,
				input_tokens: rec.attrs.input_tokens,
				output_tokens: rec.attrs.output_tokens,
				cache_read: rec.attrs.cache_read,
				cache_creation: rec.attrs.cache_creation,
				cost_usd: rec.attrs.cost_usd,
			},
			data: {
				meter: rec.meter,
				quantity: rec.quantity,
				unit: rec.unit,
				usage_id: rec.usage_id,
				provider_ref: rec.provider_ref,
			},
		});
	} catch {
		/* advisory is fire-and-forget; the ledger is the source of truth */
	}

	return rec;
}
