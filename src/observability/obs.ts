/**
 * `obs.emit(ev)` — the single, universal observability facade. Best-effort,
 * sampleable, and structurally unable to throw into its caller. Every service,
 * bridge, adapter, and watcher calls this instead of `console.log` for anything
 * that matters. Twin of obs.py.
 *
 * Contract:
 *  - The facade stamps `schema`, `ts`, `node`, and (if absent) `trace_id`.
 *  - Sampling drops only `ok` events; `error`/`denied` and `usage.recorded` are
 *    ALWAYS kept (you never want to sample away a failure or a usage mirror).
 *  - Each sink write is isolated — one failing sink never blocks another, and
 *    nothing propagates out.
 *  - On first emit, if no sink was registered, the configured default sinks
 *    (jsonl-file) are auto-registered from `loadObservabilityConfig()`.
 */

import { nodeId } from './node.js';
import { newTraceId } from './ids.js';
import { loadObservabilityConfig } from './config.js';
import { sinkFromConfig, type Sink } from './sink.js';
import type { ObsEvent, ObsEventInput } from './events.js';

let sinks: Sink[] = [];
let autoLoaded = false;
let traceSample = 1.0;
let traceSampleLoaded = false;
let customSampler: ((ev: ObsEvent) => boolean) | null = null;

/** Register an additional sink. Suppresses default-sink auto-loading. */
export function registerSink(sink: Sink): void {
	sinks.push(sink);
}

/** Override the sampler for `ok` events. Return true to keep. */
export function setSampler(fn: (ev: ObsEvent) => boolean): void {
	customSampler = fn;
}

/** Test hook: forget sinks + sampler + cached config so the next emit re-inits. */
export function resetSinks(): void {
	sinks = [];
	autoLoaded = false;
	traceSample = 1.0;
	traceSampleLoaded = false;
	customSampler = null;
}

function loadSampleOnce(): void {
	if (traceSampleLoaded) return;
	traceSampleLoaded = true;
	try {
		traceSample = loadObservabilityConfig().observability.sampling.trace;
	} catch {
		/* keep 1.0 */
	}
}

function ensureDefaultSinks(): void {
	if (autoLoaded || sinks.length > 0) return;
	autoLoaded = true;
	try {
		for (const sc of loadObservabilityConfig().observability.sinks) {
			const s = sinkFromConfig(sc);
			if (s) sinks.push(s);
		}
	} catch {
		/* leave empty; emit still no-ops safely */
	}
}

function shouldKeep(ev: ObsEvent): boolean {
	if (ev.outcome !== 'ok' || ev.kind === 'usage.recorded') return true;
	if (customSampler) return customSampler(ev);
	return Math.random() < traceSample;
}

export function emit(input: ObsEventInput): void {
	try {
		const ev: ObsEvent = {
			schema: 1,
			ts: Date.now() / 1000,
			trace_id: input.trace_id ?? newTraceId(),
			node: input.node ?? nodeId(),
			source: input.source,
			actor: input.actor,
			kind: input.kind,
			outcome: input.outcome,
		};
		if (input.span_id !== undefined) ev.span_id = input.span_id;
		if (input.parent_span_id !== undefined) ev.parent_span_id = input.parent_span_id;
		if (input.source_file !== undefined) ev.source_file = input.source_file;
		if (input.duration_ms !== undefined) ev.duration_ms = input.duration_ms;
		if (input.usage !== undefined) ev.usage = input.usage;
		if (input.data !== undefined) ev.data = input.data;

		loadSampleOnce();
		if (!shouldKeep(ev)) return;

		ensureDefaultSinks();
		for (const sink of sinks) {
			try {
				sink.write(ev);
			} catch {
				/* one bad sink never blocks the others */
			}
		}
	} catch {
		/* emit() is structurally incapable of throwing into its caller */
	}
}
