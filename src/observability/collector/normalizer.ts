/**
 * Normalizer — turns ONE source's raw payload into the universal spine
 * vocabulary (ObsEvent / UsageRecord).
 *
 * The collector is deliberately source-agnostic: every input TYPE (Claude Code
 * hooks, the voice agent, the filewatcher, a bridge) is mapped by a registered
 * `Normalizer` into the SAME schema and written through the SAME sink-set. That
 * is what keeps heterogeneous sources consistent — Claude Code is just one of
 * them. Adding a source = registering a Normalizer; the collector never changes.
 *
 * Source-specific knowledge (and any provider-isms — CC field names, etc.) lives
 * in the source module that owns it, NOT in the collector. The collector only knows
 * this interface.
 *
 * A normalizer MAY be stateful — e.g. accumulating streamed `delta` chunks into
 * one event: a call can return zero primitives for a partial chunk and the full
 * event on the terminal chunk. It MUST NOT throw; return EMPTY on garbage (the
 * collector also guards, but a normalizer that swallows keeps the logs clean).
 *
 * Normalizers run inside the (TS) collector daemon. Python emitters don't need a
 * Python normalizer — they POST their payload to `/ingest/<source>` and a TS
 * normalizer maps it, OR they emit already-formed events to the generic
 * `/ingest`. One collector, one place mapping happens.
 */

import type { ObsEvent } from '../events.js';
import type { UsageRecord } from '../usage.js';

export interface NormalizeContext {
	node: string; // which machine the collector runs on
	receivedAt: number; // float unix seconds — when the payload reached the collector
}

export interface NormalizeResult {
	events: ObsEvent[];
	usage: UsageRecord[];
}

export interface Normalizer {
	/** Route key — the `<source>` segment an emitter posts to
	 *  (`POST /ingest/<source>`). e.g. "claude-code-hooks" | "voice-agent" |
	 *  "filewatcher". Must be unique within a collector. */
	readonly source: string;
	normalize(payload: unknown, ctx: NormalizeContext): NormalizeResult;
}

export const EMPTY: NormalizeResult = { events: [], usage: [] };

/**
 * Composable normalizer: `normalize = map ∘ decode`.
 *
 * Splits ingestion into two single-responsibility halves so each source reads
 * as "DETECT + strictly type, THEN map":
 *
 *   - `decode(payload)` — validate the raw `unknown` body and return it as a
 *     strict, source-specific type `T` (or `null` to drop). This is where the
 *     "if it's this source, type it into an interface" rule lives.
 *   - `map(typed)` — transform the strictly-typed `T` into spine primitives,
 *     with full type narrowing (no casts).
 *
 * Stateful sources (e.g. accumulating streamed chunks) keep their state on the
 * subclass; `map` may return `EMPTY` for a partial input and the full result on
 * the terminal one.
 */
export abstract class AbstractNormalizer<T> implements Normalizer {
	abstract readonly source: string;
	abstract decode(payload: unknown, ctx: NormalizeContext): T | null;
	abstract map(typed: T, ctx: NormalizeContext): NormalizeResult;

	normalize(payload: unknown, ctx: NormalizeContext): NormalizeResult {
		const typed = this.decode(payload, ctx);
		return typed == null ? EMPTY : this.map(typed, ctx);
	}
}
