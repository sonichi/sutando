/**
 * Collector — the single, source-agnostic local ingestion point.
 *
 * ONE collector for ALL source types (there is no per-source collector).
 * Emitters push payloads tagged with a `source`; the collector routes each to
 * the `Normalizer` registered for that source, which maps it into the universal
 * ObsEvent/UsageRecord schema, then the collector writes every result through
 * the SAME sink-set (events) + usage ledger. That uniform write path is what
 * keeps heterogeneous sources consistent AND what forwards upstream: events fan
 * out via the `observability.sinks` set (adding a forward sink ships them to an
 * upstream collector), and usage records additionally fan out via the
 * `usageForwarder` — the metering EXPORT seam, resolved from `metering.endpoint`
 * (see meter-forward.ts), which POSTs the billable records upstream. Both are
 * best-effort second hops over the durable local floor (sinks' jsonl + the
 * meter ledger); neither is on by default.
 *
 * Writes go DIRECTLY through the sink-set (not `obs.emit`) so source-derived
 * ids/traces survive and nothing is sampled away — these events were already
 * decided at the source.
 *
 * Transport-free on purpose (unit-testable without HTTP). `server.ts` is the
 * thin HTTP shell; `src/boot/collector.ts` is the composition root that
 * registers the available normalizers.
 */

import { nodeId } from '../node.js';
import { sinkFromConfig, type Sink } from '../sink.js';
import { loadObservabilityConfig } from '../config.js';
import { writeLedger } from '../meter.js';
import { defaultMeterForwarder, type MeterForwarder } from '../meter-forward.js';
import type { ObsEvent } from '../events.js';
import type { UsageRecord } from '../usage.js';
import type { Normalizer, NormalizeResult } from './normalizer.js';

export interface IngestStat {
	ok: boolean;
	source: string;
	events: number;
	usage: number;
	reason?: string;
}

function defaultSinks(): Sink[] {
	const sinks: Sink[] = [];
	try {
		for (const sc of loadObservabilityConfig().observability.sinks) {
			const s = sinkFromConfig(sc);
			if (s) sinks.push(s);
		}
	} catch {
		/* leave empty; writes become a safe no-op */
	}
	return sinks;
}

function warn(msg: string): void {
	try {
		process.stderr.write(`[collector] ${msg}\n`);
	} catch {
		/* best-effort */
	}
}

export class Collector {
	private readonly normalizers = new Map<string, Normalizer>();
	private readonly sinks: Sink[];
	private readonly usageWriter: (u: UsageRecord) => void;
	private readonly usageForwarder: MeterForwarder | null;

	/** `sinks` defaults to the configured obs sink-set (jsonl-file floor + any
	 *  forward sink); `usageWriter` defaults to the durable meter ledger append;
	 *  `usageForwarder` defaults to the config-resolved metering export (`null`
	 *  when export is off). Pass `usageForwarder: null` to force it off (tests). */
	constructor(opts?: {
		sinks?: Sink[];
		usageWriter?: (u: UsageRecord) => void;
		usageForwarder?: MeterForwarder | null;
	}) {
		this.sinks = opts?.sinks ?? defaultSinks();
		this.usageWriter = opts?.usageWriter ?? writeLedger;
		this.usageForwarder =
			opts && 'usageForwarder' in opts ? (opts.usageForwarder ?? null) : defaultMeterForwarder();
	}

	register(n: Normalizer): this {
		if (this.normalizers.has(n.source)) warn(`source '${n.source}' re-registered, overwriting`);
		this.normalizers.set(n.source, n);
		return this;
	}

	sources(): string[] {
		return [...this.normalizers.keys()];
	}

	/** Route a raw source payload to its normalizer and write the results. */
	ingest(source: string, payload: unknown): IngestStat {
		const n = this.normalizers.get(source);
		if (!n) {
			warn(`no normalizer registered for source '${source}'`);
			return { ok: false, source, events: 0, usage: 0, reason: 'no normalizer for source' };
		}
		let res: NormalizeResult;
		try {
			res = n.normalize(payload, { node: nodeId(), receivedAt: Date.now() / 1000 });
		} catch (e) {
			warn(`normalizer '${source}' threw: ${(e as Error).message}`);
			return { ok: false, source, events: 0, usage: 0, reason: 'normalize threw' };
		}
		this.accept(res);
		return { ok: true, source, events: res.events.length, usage: res.usage.length };
	}

	/** Write ALREADY-normalized primitives — for an in-process emitter that maps
	 *  locally but ships formed records here for durable storage + forwarding. */
	accept(res: NormalizeResult): void {
		for (const e of res.events) this.writeEvent(e);
		for (const u of res.usage) {
			try {
				this.usageWriter(u); // durable local floor (ledger)
			} catch {
				/* never throw out of the collector */
			}
			try {
				this.usageForwarder?.forward(u); // optional upstream export
			} catch {
				/* forward is best-effort — the ledger is the source of truth */
			}
		}
	}

	/** Flush + stop the upstream usage forwarder so the final partial batch isn't
	 *  stranded on shutdown. No-op when metering export is off. */
	async stop(): Promise<void> {
		await this.usageForwarder?.stop();
	}

	private writeEvent(e: ObsEvent): void {
		for (const sink of this.sinks) {
			try {
				sink.write(e);
			} catch {
				/* one bad sink never blocks the others */
			}
		}
	}
}
