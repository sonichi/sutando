/**
 * The `ExecutorSource` port — provider-agnostic. A live telemetry source for an
 * executor (the interactive core) feeds already-mapped primitives into the
 * spine through a `SourceSink`. The port has NO Claude-isms: the OTLP receiver,
 * the .jsonl tailer, and the hook handler each implement this later (deferred);
 * the Claude-specific mapping lives in `adapters/executor/claude-code/sources/`.
 *
 * Phase 0 ships ONLY this interface + the pure mappers. No live source exists
 * yet. A future source: obtain CC telemetry → map it to ObsEvent/UsageRecord →
 * push via the sink callbacks below.
 */

import type { ObsEvent } from '../obs/types.js';
import type { UsageRecord } from '../meter/types.js';

/** What the spine hands a source so it can push results in. `emit` is
 *  best-effort (obs); `record` is durable (meter). Both take FULLY-FORMED
 *  records — the interactive core's events already carry CC-derived ids, so they
 *  are not re-stamped by `obs.emit`/`meter.record`. */
export interface SourceSink {
	emit(event: ObsEvent): void;
	record(usage: UsageRecord): void;
}

export interface SourceStartOpts {
	sink: SourceSink;
	signal?: AbortSignal; // cooperative shutdown
}

export interface ExecutorSource {
	readonly name: string; // "claude-code.otel" | "claude-code.jsonl" | "claude-code.hooks"
	start(opts: SourceStartOpts): Promise<void> | void;
	stop(): Promise<void> | void;
}
